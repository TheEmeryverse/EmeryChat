import asyncio
import copy
import inspect
import json
import logging
import re
import time
from urllib.parse import urlparse

from telegram.error import BadRequest

from emery.config import (
    MAIN_MODEL_URL,
    MODEL_ID,
    MAIN_MODEL_CONTEXT_TOKENS,
    MAIN_MODEL_REASONING_EFFORT,
    CONTEXT_COMPACTION_THRESHOLD,
    MODEL_CHARS_PER_TOKEN,
    TOOL_LOOP,
    MODEL_NAME,
    THINK,
    ENABLE_LIVE_PROGRESS,
    LIVE_PROGRESS_MAX_CHARS,
)
from emery import tool_registry
import emery.globals as globals
from emery.helpers import (
    get_stable_system_prompt,
    message_content_to_text,
    normalize_gemma_thinking,
    clean_thinking_tags,
    query_fast_model,
    telegram_escape,
)
from emery.logging_utils import (
    format_cache_diagnostics,
    format_logging_payload,
    format_llama_perf_line,
    safe_preview,
)
from emery.prompt_cache import (
    cache_diagnostics,
    dispatch_tool_call,
    endpoint_cache_options,
    clear_prompt_cache,
    current_prompt_epoch,
    get_stable_prompt_state,
    invalidate_prompt_cache,
    invalidate_prompt_epoch,
    request_shape_hash as prompt_request_shape_hash,
    resolve_tooling,
    stable_hash,
)
from emery.telegram_utils import normalize_message_thread_id

AVAILABLE_TOOLS = tool_registry.AVAILABLE_TOOLS
tools_schema = tool_registry.tools_schema

def _strip_id_prefix(text: str) -> str:
    return re.sub(r'^\s*\[ID:\s*\d+[^\]]*\]\s*', '', text or '', flags=re.IGNORECASE)


def _extract_thinking_blocks(content: str) -> tuple[list[str], str]:
    if not content:
        return [], ""

    normalized = normalize_gemma_thinking(content)
    pattern = re.compile(r'<[tT]hink>(.*?)</[tT]hink>', re.DOTALL)
    thoughts = [match.strip() for match in pattern.findall(normalized) if match.strip()]
    cleaned = pattern.sub('', normalized).strip()
    return thoughts, cleaned


def _format_thinking_turn(loop_count: int, phase: str, thought: str) -> str:
    thought = (thought or "").strip()
    if not thought:
        return ""
    return f"Turn {loop_count + 1}\n{phase}\n\n{thought}"


_REASONING_SUMMARY_TIMEOUT_SECONDS = 12.0
_REASONING_SUMMARY_MAX_TOKENS = 160


async def _summarize_reasoning_block(reasoning: str, *, loop_count: int) -> str:
    """Convert a completed internal reasoning block into safe, high-level text.

    The main model's reasoning is never used as a fallback here. If the fast
    model is unavailable, the user still gets the tool timeline without a raw
    chain-of-thought leak.
    """
    reasoning = _strip_id_prefix(reasoning).strip()
    if not reasoning:
        return ""

    prompt = (
        "Rewrite the internal reasoning below as a concise, user-visible high-level rationale. "
        "Do not reproduce hidden chain-of-thought, private deliberation, or step-by-step reasoning. "
        "Mention only the relevant goal, decision, or purpose of the next action. "
        "Return one or two plain-English sentences, with no heading or preamble.\n\n"
        f"Internal reasoning from model turn {loop_count + 1}:\n{reasoning}"
    )
    system_prompt = (
        "You summarize internal model reasoning for display to an end user. "
        "Output only a brief high-level rationale; never reveal chain-of-thought."
    )
    try:
        summary = await asyncio.wait_for(
            query_fast_model(
                prompt,
                system_prompt=system_prompt,
                max_tokens=_REASONING_SUMMARY_MAX_TOKENS,
                temperature=0.2,
                enable_thinking=False,
            ),
            timeout=_REASONING_SUMMARY_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logging.warning("⚠️ COPROCESSOR: Reasoning summary unavailable: %s", exc)
        return ""

    summary = _sanitize_model_preamble(summary)
    if not summary:
        logging.warning("⚠️ COPROCESSOR: Reasoning summary was empty for model turn %s.", loop_count + 1)
    return summary


def _format_tool_timeline_entry(fn: str) -> str:
    return f"{MODEL_NAME} used {fn}"


# These functions are model-facing plumbing for dynamic tool discovery. They
# should not appear as if Emery is performing a user-requested action.
_INTERNAL_TOOL_NAMES = frozenset({"tool_search", "tool_describe", "tool_call"})


def _append_tool_timeline_entry(timeline: list[str], fn: str) -> None:
    if fn not in _INTERNAL_TOOL_NAMES:
        timeline.append(_format_tool_timeline_entry(fn))


class _StreamingModelError(RuntimeError):
    def __init__(self, message: str, *, events_seen: bool = False):
        super().__init__(message)
        self.events_seen = events_seen


async def _emit_engine_event(on_event, event: dict) -> None:
    if not on_event:
        return
    try:
        result = on_event(event)
        if inspect.isawaitable(result):
            await result
    except Exception as exc:
        logging.warning("⚠️ ENGINE: Progress callback failed for %s: %s", event.get("type"), exc)


def _sanitize_model_preamble(text: str) -> str:
    """Return a short, non-reasoning progress note suitable for Telegram."""
    cleaned = clean_thinking_tags(normalize_gemma_thinking(str(text or ""))).strip()
    cleaned = re.sub(r"<\/?progress>", "", cleaned, flags=re.IGNORECASE)
    cleaned = _strip_id_prefix(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return ""

    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    cleaned = " ".join(sentences[:2]).strip()
    if len(cleaned) > LIVE_PROGRESS_MAX_CHARS:
        cleaned = cleaned[:LIVE_PROGRESS_MAX_CHARS].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return cleaned


def _merge_stream_tool_call(tool_calls: dict, fragment: dict, fallback_index: int) -> None:
    index = fragment.get("index", fallback_index)
    key = str(index)
    current = tool_calls.setdefault(
        key,
        {
            "index": index,
            "id": "",
            "type": "function",
            "function": {"name": "", "arguments": ""},
        },
    )
    if fragment.get("id"):
        current["id"] = fragment["id"]
    if fragment.get("type"):
        current["type"] = fragment["type"]
    function = fragment.get("function") or {}
    if function.get("name"):
        current["function"]["name"] += str(function["name"])
    if function.get("arguments") is not None:
        current["function"]["arguments"] += str(function["arguments"])


def _stream_choice_delta(payload: dict) -> tuple[dict, dict]:
    choices = payload.get("choices") or []
    if not choices:
        return {}, {}
    choice = choices[0] or {}
    return choice, choice.get("delta") or choice.get("message") or {}


async def _stream_main_model_response(url: str, payload: dict, on_event=None) -> tuple[dict, float]:
    """Assemble an OpenAI-compatible SSE response without executing partial tool calls."""
    content_parts = []
    reasoning_parts = []
    tool_calls = {}
    role = "assistant"
    finish_reason = None
    usage = None
    events_seen = False
    pending_preamble = None
    progress_emitted = False
    raw_content = ""
    request_started = time.perf_counter()

    try:
        async with globals.http_client.stream("POST", url, json=payload, timeout=900) as response:
            if response.status_code != 200:
                raise _StreamingModelError(
                    f"Main model returned {response.status_code} — {response.text[:200]}",
                    events_seen=False,
                )

            async for line in response.aiter_lines():
                line = str(line or "").strip()
                if not line or line.startswith(":"):
                    continue
                if line.lower().startswith("data:"):
                    line = line[5:].strip()
                if line == "[DONE]":
                    break

                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    logging.debug("ENGINE: Ignoring non-JSON streaming line: %s", safe_preview(line, max_len=160))
                    continue

                if not isinstance(chunk, dict):
                    continue
                events_seen = True
                if isinstance(chunk.get("usage"), dict):
                    usage = chunk["usage"]

                choice, delta = _stream_choice_delta(chunk)
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                if delta.get("role"):
                    role = delta["role"]

                content = message_content_to_text(delta.get("content"))
                if content:
                    content_parts.append(content)
                    raw_content += content
                    if not progress_emitted:
                        tagged = re.search(
                            r"<progress>(.*?)</progress>",
                            clean_thinking_tags(normalize_gemma_thinking(raw_content)),
                            flags=re.DOTALL | re.IGNORECASE,
                        )
                        if tagged:
                            preamble = _sanitize_model_preamble(tagged.group(1))
                            if preamble:
                                pending_preamble = preamble

                reasoning = message_content_to_text(
                    delta.get("reasoning_content") or delta.get("thinking") or delta.get("reasoning")
                )
                if reasoning:
                    reasoning_parts.append(reasoning)

                fragments = delta.get("tool_calls") or []
                if isinstance(fragments, dict):
                    fragments = [fragments]
                for fragment_index, fragment in enumerate(fragments):
                    if isinstance(fragment, dict):
                        if pending_preamble and not progress_emitted:
                            progress_emitted = True
                            await _emit_engine_event(
                                on_event,
                                {"type": "preamble", "text": pending_preamble, "source": "model"},
                            )
                        _merge_stream_tool_call(tool_calls, fragment, fragment_index)

                function_call = delta.get("function_call")
                if isinstance(function_call, dict):
                    if pending_preamble and not progress_emitted:
                        progress_emitted = True
                        await _emit_engine_event(
                            on_event,
                            {"type": "preamble", "text": pending_preamble, "source": "model"},
                        )
                    _merge_stream_tool_call(
                        tool_calls,
                        {"index": 0, "type": "function", "function": function_call},
                        0,
                    )
    except _StreamingModelError:
        raise
    except Exception as exc:
        raise _StreamingModelError(str(exc), events_seen=events_seen) from exc

    if not events_seen:
        raise _StreamingModelError("Main model returned an empty stream.", events_seen=False)

    message = {
        "role": role,
        "content": "".join(content_parts),
    }
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = [
            {key: value for key, value in call.items() if key != "index"}
            for call in sorted(tool_calls.values(), key=lambda item: item["index"])
        ]

    response_json = {
        "choices": [{"message": message, "finish_reason": finish_reason}],
    }
    if usage:
        response_json["usage"] = usage
    return response_json, time.perf_counter() - request_started


_SEARCH_SUMMARY_TIMEOUT_SECONDS = 8.0
_MAX_SEARCH_STATUS_WORDS = 6
_MAX_STATUS_FRAGMENT_CHARS = 72
_SEARCH_FALLBACK_STOP_WORDS = {
    "a", "an", "and", "at", "by", "for", "from", "in", "into", "is", "latest", "near",
    "news", "of", "on", "or", "search", "source", "the", "to", "today", "tomorrow",
    "update", "updates", "vs", "web", "with", "yesterday",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
}
_FETCH_BRAND_DOMAINS = {
    "abcnews.go.com": "ABC News",
    "apnews.com": "AP News",
    "bbc.co.uk": "BBC.co.uk",
    "bbc.com": "BBC.com",
    "bloomberg.com": "Bloomberg",
    "cnbc.com": "CNBC.com",
    "cnn.com": "CNN.com",
    "ft.com": "Financial Times",
    "github.com": "GitHub",
    "google.com": "Google",
    "npr.org": "NPR.org",
    "nytimes.com": "NYTimes.com",
    "reuters.com": "Reuters",
    "theguardian.com": "The Guardian",
    "wsj.com": "WSJ.com",
    "x.com": "X.com",
    "youtube.com": "YouTube",
}
_MULTI_PART_PUBLIC_SUFFIXES = {
    "co.uk", "com.au", "com.br", "com.mx", "com.sg", "com.tr", "co.jp", "co.nz",
    "co.kr", "co.in", "com.cn", "com.hk", "com.tw", "com.sa", "com.ar",
}


def _clean_status_fragment(text: str, *, max_words: int = _MAX_SEARCH_STATUS_WORDS) -> str:
    text = str(text or "").strip()
    text = clean_thinking_tags(normalize_gemma_thinking(text))
    text = text.splitlines()[0] if text else ""
    text = re.sub(r"^[`\"'“”‘’\s]+|[`\"'“”‘’\s]+$", "", text)
    text = re.sub(
        r"^(?:model\s+is\s+)?(?:search(?:ing)?(?:\s+the\s+web)?\s+(?:for\s+)?|about\s+|query:\s*)",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s+", " ", text).strip(" .,:;!?-/")
    if not text:
        return ""

    words = text.split()
    text = " ".join(words[:max_words])
    if len(text) > _MAX_STATUS_FRAGMENT_CHARS:
        text = text[:_MAX_STATUS_FRAGMENT_CHARS].rsplit(" ", 1)[0].strip()
    return text.strip(" .,:;!?-/")


def _status_arg(args: dict, key: str, default: str = "") -> str:
    if not isinstance(args, dict):
        return default
    value = args.get(key, default)
    if value is None:
        return default
    return str(value).strip()


def _status_label(text: str, *, max_words: int = _MAX_SEARCH_STATUS_WORDS) -> str:
    return _clean_status_fragment(text, max_words=max_words)


def _status_quote(text: str) -> str:
    return telegram_escape(_status_label(text, max_words=8))


def _format_stock_symbol(symbol: str) -> str:
    symbol = str(symbol or "").strip().upper()
    return telegram_escape(symbol) if symbol else ""


def _format_country_list(countries: str) -> str:
    country_list = [
        part.strip().upper()
        for part in str(countries or "").split(",")
        if part.strip()
    ]
    return telegram_escape(",".join(country_list[:6]))


def _format_fahrenheit(celsius) -> str:
    try:
        fahrenheit = (float(celsius) * 9 / 5) + 32
    except (TypeError, ValueError):
        return ""
    rounded = round(fahrenheit, 1)
    if rounded.is_integer():
        return f"{int(rounded)}F"
    return f"{rounded:g}F"


def _with_indefinite_article(text: str) -> str:
    text = str(text or "").strip()
    if not text or re.match(r"^(?:a|an|the)\s+", text, flags=re.IGNORECASE):
        return text
    article = "an" if re.match(r"^[aeiou]", text, flags=re.IGNORECASE) else "a"
    return f"{article} {text}"


def _format_thermostat_mode(mode: str) -> str:
    normalized = str(mode or "").strip().upper()
    labels = {
        "HEAT": "heat",
        "COOL": "cool",
        "HEATCOOL": "heat/cool",
        "OFF": "off",
    }
    return labels.get(normalized, normalized.lower())


def _fallback_search_summary(query: str) -> str:
    text = str(query or "")
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\b\d{1,4}(?:[/-]\d{1,2}){1,2}\b", " ", text)
    text = re.sub(r"\b(?:19|20)\d{2}\b", " ", text)
    text = re.sub(r"[/|,;:?()\[\]{}]+", " ", text)

    raw_tokens = re.findall(r"\$?[A-Za-z][A-Za-z0-9'&.-]*", text)
    kept = [
        token.strip(".-")
        for token in raw_tokens
        if token.strip(".-") and token.strip(".-").lower() not in _SEARCH_FALLBACK_STOP_WORDS
    ]
    if not kept:
        kept = [token.strip(".-") for token in raw_tokens if token.strip(".-")]

    return _clean_status_fragment(" ".join(kept), max_words=_MAX_SEARCH_STATUS_WORDS)


async def _summarize_search_query_for_status(query: str) -> str:
    fallback = _fallback_search_summary(query)
    query = str(query or "").strip()
    if not query:
        return fallback

    prompt = (
        "Return only a 2 to 6 word plain-English noun phrase summarizing this web search query. "
        "Do not include quotes, punctuation, or words like search/query/web. Preserve the main entity.\n\n"
        f"Search query: {query}"
    )
    system_prompt = "You write very short tool-status labels."
    try:
        summary = await asyncio.wait_for(
            query_fast_model(prompt, system_prompt=system_prompt),
            timeout=_SEARCH_SUMMARY_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logging.debug("⚡ COPROCESSOR: Search status summary unavailable: %s", exc)
        return fallback

    summary = _clean_status_fragment(summary, max_words=_MAX_SEARCH_STATUS_WORDS)
    return summary or fallback


async def _summarize_text_for_status(text: str, instruction: str, fallback: str = "") -> str:
    fallback = _status_label(fallback or text)
    text = str(text or "").strip()
    if not text:
        return fallback

    prompt = (
        "Return only a 2 to 6 word plain-English status label. "
        "No quotes, punctuation, preambles, or complete sentences.\n\n"
        f"Instruction: {instruction}\n"
        f"Text: {text[:2500]}"
    )
    system_prompt = "You write very short tool-status labels."
    try:
        summary = await asyncio.wait_for(
            query_fast_model(prompt, system_prompt=system_prompt),
            timeout=_SEARCH_SUMMARY_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logging.debug("⚡ COPROCESSOR: Tool status summary unavailable: %s", exc)
        return fallback

    summary = _clean_status_fragment(summary, max_words=_MAX_SEARCH_STATUS_WORDS)
    return summary or fallback


def _registered_domain(hostname: str) -> str:
    host = str(hostname or "").strip().lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return ""

    labels = [label for label in host.split(".") if label]
    if len(labels) <= 2:
        return host

    suffix = ".".join(labels[-2:])
    if suffix in _MULTI_PART_PUBLIC_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _format_generic_domain(domain: str) -> str:
    if not domain:
        return ""
    if domain in _FETCH_BRAND_DOMAINS:
        return _FETCH_BRAND_DOMAINS[domain]

    labels = domain.split(".")
    base = labels[0]
    suffix = ".".join(labels[1:])
    if len(base) <= 4:
        display_base = base.upper()
    else:
        display_base = "-".join(part.capitalize() for part in base.split("-") if part)
    return f"{display_base}.{suffix}" if suffix else display_base


def _website_name_from_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw if re.match(r"^[a-z][a-z0-9+.-]*://", raw, flags=re.IGNORECASE) else f"//{raw}")
    domain = _registered_domain(parsed.hostname or "")
    return _format_generic_domain(domain)


async def _format_tool_status_message(fn: str, args: dict) -> str:
    args = args if isinstance(args, dict) else {}

    if fn == "web_search":
        summary = await _summarize_search_query_for_status(args.get("query", ""))
        if summary:
            return f"{MODEL_NAME} is searching for {telegram_escape(summary)}..."
        return TOOL_STATUS_MESSAGES[fn]

    if fn == "fetch_web_content":
        website = _website_name_from_url(args.get("url", ""))
        if website:
            return f"{MODEL_NAME} is fetching {telegram_escape(website)}..."
        return TOOL_STATUS_MESSAGES[fn]

    if fn == "generate_image":
        prompt = _status_arg(args, "prompt")
        summary = await _summarize_text_for_status(
            prompt,
            "Summarize what image is being generated as a noun phrase.",
        )
        if summary:
            return f"{MODEL_NAME} is painting {telegram_escape(_with_indefinite_article(summary))}..."
        return TOOL_STATUS_MESSAGES[fn]

    if fn == "speak_message":
        text = _status_arg(args, "text")
        summary = await _summarize_text_for_status(
            text,
            "Summarize what this voice memo is about as a noun phrase.",
        )
        if summary:
            return f"{MODEL_NAME} is recording a voice memo about {telegram_escape(summary)}..."
        return TOOL_STATUS_MESSAGES[fn]

    if fn == "delegate_to_coprocessor":
        task_prompt = _status_arg(args, "task_prompt")
        summary = await _summarize_text_for_status(
            task_prompt,
            "Summarize this coprocessor task as a short verb phrase.",
        )
        if summary:
            summary = re.sub(r"^to\s+", "", summary, flags=re.IGNORECASE)
            return f"{MODEL_NAME} is asking the coprocessor to {telegram_escape(summary)}..."
        return TOOL_STATUS_MESSAGES[fn]

    if fn == "get_noaa_weather":
        location = _status_arg(args, "location")
        timeframe = _status_arg(args, "timeframe", "forecast").lower()
        weather_type = "hourly weather" if timeframe == "hourly" else "weather"
        if location:
            return f"{MODEL_NAME} is checking {weather_type} for {_status_quote(location)}..."
        return f"{MODEL_NAME} is checking {weather_type}..."

    if fn == "set_weather_location_alias":
        alias = _status_quote(_status_arg(args, "alias"))
        location = _status_quote(_status_arg(args, "location"))
        if alias and location:
            return f"{MODEL_NAME} is saving weather location {alias} as {location}..."
        if alias:
            return f"{MODEL_NAME} is saving weather location {alias}..."
        return TOOL_STATUS_MESSAGES[fn]

    if fn == "remove_weather_location_alias":
        alias = _status_quote(_status_arg(args, "alias"))
        if alias:
            return f"{MODEL_NAME} is removing weather location {alias}..."
        return TOOL_STATUS_MESSAGES[fn]

    if fn == "set_nest_thermostat_mode":
        mode = _status_arg(args, "mode")
        mode_label = _format_thermostat_mode(mode)
        if mode_label == "off":
            return f"{MODEL_NAME} is turning the thermostat off..."
        if mode_label:
            return f"{MODEL_NAME} is setting the thermostat to {telegram_escape(mode_label)}..."
        return TOOL_STATUS_MESSAGES[fn]

    if fn == "set_nest_thermostat_temperature":
        temp = _format_fahrenheit(args.get("temp_celsius"))
        heat = _format_fahrenheit(args.get("heat_temp_celsius"))
        cool = _format_fahrenheit(args.get("cool_temp_celsius"))
        if heat and cool:
            return f"{MODEL_NAME} is setting the thermostat range to {heat}-{cool}..."
        if temp:
            return f"{MODEL_NAME} is setting the thermostat to {temp}..."
        if heat:
            return f"{MODEL_NAME} is setting the thermostat heat to {heat}..."
        if cool:
            return f"{MODEL_NAME} is setting the thermostat cool to {cool}..."
        return TOOL_STATUS_MESSAGES[fn]

    if fn == "overseer_search_movie":
        query = _status_quote(_status_arg(args, "query"))
        if query:
            return f"{MODEL_NAME} is searching movies for {query}..."
        return TOOL_STATUS_MESSAGES[fn]

    if fn == "overseer_search_tv":
        query = _status_quote(_status_arg(args, "query"))
        if query:
            return f"{MODEL_NAME} is searching TV shows for {query}..."
        return TOOL_STATUS_MESSAGES[fn]

    if fn == "overseer_request_tv_season":
        season = _status_arg(args, "season_number")
        if season:
            return f"{MODEL_NAME} is requesting TV season {telegram_escape(season)}..."
        return TOOL_STATUS_MESSAGES[fn]

    if fn == "search_fred_series":
        query = _status_quote(_status_arg(args, "query"))
        if query:
            return f"{MODEL_NAME} is searching FRED for {query}..."
        return TOOL_STATUS_MESSAGES[fn]

    if fn == "get_fred_series_observations":
        series_id = _format_stock_symbol(_status_arg(args, "series_id"))
        if series_id:
            return f"{MODEL_NAME} is pulling FRED series {series_id}..."
        return TOOL_STATUS_MESSAGES[fn]

    if fn == "search_imf_indicators":
        query = _status_quote(_status_arg(args, "query"))
        if query:
            return f"{MODEL_NAME} is searching IMF for {query}..."
        return TOOL_STATUS_MESSAGES[fn]

    if fn == "get_imf_datamapper_series":
        indicator = _format_stock_symbol(_status_arg(args, "indicator"))
        countries = _format_country_list(_status_arg(args, "countries"))
        if indicator and countries:
            return f"{MODEL_NAME} is pulling IMF data for {indicator} in {countries}..."
        if indicator:
            return f"{MODEL_NAME} is pulling IMF data for {indicator}..."
        return TOOL_STATUS_MESSAGES[fn]

    if fn == "get_stock_snapshot":
        symbol = _format_stock_symbol(_status_arg(args, "symbol"))
        if symbol:
            return f"{MODEL_NAME} is checking {symbol}..."
        return TOOL_STATUS_MESSAGES[fn]

    if fn == "get_stock_price_history":
        symbol = _format_stock_symbol(_status_arg(args, "symbol"))
        if symbol:
            return f"{MODEL_NAME} is pulling {symbol} price history..."
        return TOOL_STATUS_MESSAGES[fn]

    if fn == "get_global_macro_dashboard":
        countries = _format_country_list(_status_arg(args, "countries"))
        if countries:
            return f"{MODEL_NAME} is assembling a global macro dashboard for {countries}..."
        return TOOL_STATUS_MESSAGES[fn]

    if fn == "get_reolink_snapshot":
        camera = _status_quote(_status_arg(args, "camera_name"))
        if camera:
            return f"{MODEL_NAME} is checking the {camera} camera..."
        return TOOL_STATUS_MESSAGES[fn]

    if fn == "get_camera_security_log":
        camera = _status_quote(_status_arg(args, "camera_name"))
        if camera:
            return f"{MODEL_NAME} is reviewing the {camera} security log..."
        return TOOL_STATUS_MESSAGES[fn]

    if fn == "list_portainer_containers":
        environment = _status_quote(_status_arg(args, "environment_name"))
        if environment:
            return f"{MODEL_NAME} is listing containers in {environment}..."
        return TOOL_STATUS_MESSAGES[fn]

    if fn == "update_portainer_container":
        environment = _status_quote(_status_arg(args, "environment_name"))
        container = _status_quote(_status_arg(args, "container_name"))
        if container and environment:
            return f"{MODEL_NAME} is updating {container} in {environment}..."
        if container:
            return f"{MODEL_NAME} is updating {container}..."
        return TOOL_STATUS_MESSAGES[fn]

    if fn == "add_scheduled_job":
        description = _status_quote(_status_arg(args, "description"))
        if description:
            return f"{MODEL_NAME} is scheduling {description}..."
        return TOOL_STATUS_MESSAGES[fn]

    if fn == "remove_scheduled_job":
        job_id = _status_quote(_status_arg(args, "job_id"))
        if job_id:
            return f"{MODEL_NAME} is removing scheduled job {job_id}..."
        return TOOL_STATUS_MESSAGES[fn]

    return TOOL_STATUS_MESSAGES.get(fn, f"{MODEL_NAME} is using {fn}...")


def _extract_response_message(response_json: dict) -> dict:
    choices = response_json.get("choices")
    if isinstance(choices, list) and choices:
        return choices[0].get("message") or {}
    return response_json.get("message", {})


def _normalize_tool_arguments(arguments):
    if isinstance(arguments, str):
        try:
            return json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError:
            logging.warning("⚠️ ENGINE: Tool arguments were not valid JSON: %r", arguments[:200])
            return {}
    return arguments or {}


def _parse_tool_arguments(arguments):
    """Parse a complete tool argument object, returning None for malformed input."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError:
            return None
    if arguments is None:
        return {}
    return arguments if isinstance(arguments, dict) else None


def _log_main_model_perf(response_json: dict, wall_seconds: float) -> None:
    logging.info(format_llama_perf_line("MAIN", response_json, wall_seconds))


def _message_token_estimate(msg: dict) -> int:
    content = msg.get("content")
    if isinstance(content, list):
        content = " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return max(1, int(len(str(content or "")) / max(MODEL_CHARS_PER_TOKEN, 1.0)) + 16)


def _compact_history_for_model(history_buffer) -> list[dict]:
    budget_tokens = max(256, int(MAIN_MODEL_CONTEXT_TOKENS * CONTEXT_COMPACTION_THRESHOLD))
    selected = []
    used_tokens = 0
    for msg in reversed(list(history_buffer or [])):
        message_tokens = _message_token_estimate(msg)
        if selected and used_tokens + message_tokens > budget_tokens:
            break
        if not selected and message_tokens > budget_tokens:
            msg = dict(msg)
            content = str(msg.get("content") or "")
            msg["content"] = content[: int(budget_tokens * MODEL_CHARS_PER_TOKEN)]
            message_tokens = _message_token_estimate(msg)
        selected.append(msg)
        used_tokens += message_tokens

    selected.reverse()
    while selected and selected[0].get("role") == "tool":
        selected.pop(0)
    if selected and selected[0].get("role") == "assistant" and selected[0].get("tool_calls"):
        selected.pop(0)

    omitted = len(selected) < len(history_buffer or [])
    if omitted:
        selected.insert(0, {
            "role": "system",
            "content": "[Earlier conversation compacted to stay within the model context budget.]",
        })
        logging.warning(
            "⚠️ ENGINE: Compacted chat history from %s messages to %s messages at %s%% context budget.",
            len(history_buffer or []),
            len(selected),
            int(CONTEXT_COMPACTION_THRESHOLD * 100),
        )
    return selected


def _build_ollama_history(history_buffer) -> list[dict]:
    history_buffer = _compact_history_for_model(history_buffer)
    ollama_history = []
    for msg in history_buffer:
        # Never retain references into the caller's history or into prior
        # request payloads. Tool calls are particularly easy to mutate while
        # assembling a later loop.
        clean_msg = {"role": msg["role"]}

        # Preserve tool calling fields if present in history
        if "tool_calls" in msg:
            clean_msg["tool_calls"] = copy.deepcopy(msg["tool_calls"])
        if "tool_call_id" in msg:
            clean_msg["tool_call_id"] = msg["tool_call_id"]
        if "name" in msg:
            clean_msg["name"] = msg["name"]

        content = msg.get("content")
        if content is not None:
            if isinstance(content, list):
                text_parts = [part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"]
                content_str = " ".join(text_parts) if text_parts else "[Sent an image]"
            elif isinstance(content, str):
                if len(content) > 5000 and not any(c.isspace() for c in content[1000:3000]):
                    content_str = "[Image base64 data removed]"
                else:
                    if msg.get("role") == "assistant":
                        content_str = clean_thinking_tags(content)
                    else:
                        content_str = content
            else:
                content_str = str(content)

            # Append message details (ID, Replies, Reactions) to the content for LLM awareness
            msg_details = []
            if msg.get("message_id"):
                msg_details.append(f"ID: {msg['message_id']}")
            if msg.get("reply_to_message_id"):
                msg_details.append(f"Replying to: {msg['reply_to_message_id']}")

            reactions = msg.get("reactions", {})
            reaction_parts = []
            if reactions.get("user"):
                reaction_parts.append(f"User: {', '.join(reactions['user'])}")
            if reactions.get("assistant"):
                reaction_parts.append(f"Emery: {', '.join(reactions['assistant'])}")
            if reaction_parts:
                msg_details.append(f"Reactions: {', '.join(reaction_parts)}")

            if msg_details:
                prefix = f"[{' | '.join(msg_details)}] "
                clean_msg["content"] = prefix + content_str
            else:
                clean_msg["content"] = content_str
        else:
            clean_msg["content"] = None if "tool_calls" in msg else ""

        ollama_history.append(clean_msg)

    return ollama_history


def _build_main_model_payload(
    *,
    history_buffer,
    model_to_use=MODEL_ID,
    max_tokens: int = 8192,
    temperature: float = 0.8,
    top_p: float = 0.95,
    top_k: int = 20,
    allow_tools: bool = True,
    stream: bool = None,
    session_context=None,
    turn_context=None,
    prompt_epoch: int | None = None,
    tools_schema_override=None,
) -> tuple[dict, list[dict]]:
    reasoning_effort = MAIN_MODEL_REASONING_EFFORT if THINK else "none"
    resolved_schema = tools_schema if tools_schema_override is None else tools_schema_override
    prompt_state = get_stable_prompt_state(
        stable_system_prompt=get_stable_system_prompt(),
        model=model_to_use,
        tool_schema=resolved_schema if allow_tools else [],
        session_context=session_context,
        prompt_epoch=prompt_epoch,
    )
    ollama_history = _build_ollama_history(history_buffer)

    # Turn context is request-local. Attach it to the latest user message in
    # the copied model history, never to the caller's stored history.
    if turn_context is not None:
        turn_text = turn_context if isinstance(turn_context, str) else json.dumps(
            turn_context, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )
        turn_text = str(turn_text).strip()
        if turn_text:
            for index in range(len(ollama_history) - 1, -1, -1):
                if ollama_history[index].get("role") == "user":
                    current = ollama_history[index].get("content") or ""
                    ollama_history[index]["content"] = (
                        f"{current}\n\n# Turn Context\n{turn_text}" if current else f"# Turn Context\n{turn_text}"
                    )
                    break

    payload = {
        "model": model_to_use,
        "messages": prompt_state.prefix_messages() + copy.deepcopy(ollama_history),
        "stream": ENABLE_LIVE_PROGRESS if stream is None else bool(stream),
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
        "top_k": top_k,
        "reasoning_effort": reasoning_effort,
    }

    if allow_tools and resolved_schema:
        payload["tools"] = prompt_state.tools()

    # These fields are intentionally absent for the custom local endpoint
    # unless an operator explicitly opts in through environment configuration.
    payload.update(endpoint_cache_options())
    logging.debug("ENGINE: Request assembly %s", format_cache_diagnostics(cache_diagnostics(payload, prompt_state)))

    return payload, ollama_history


async def _send_tool_status(fn: str, args: dict, on_event=None) -> None:
    if fn in ("react_to_message", "reply_to_message", "send_sticker", "send_gif") or fn in _INTERNAL_TOOL_NAMES:
        return

    status_msg = await _format_tool_status_message(fn, args)
    if on_event:
        await _emit_engine_event(
            on_event,
            {"type": "tool_started", "tool": fn, "text": status_msg, "source": "application"},
        )
        return

    chat_id = globals.TARGET_CHAT_ID.get()
    thread_id = normalize_message_thread_id(chat_id, globals.CURRENT_THREAD_ID.get())
    if chat_id is None:
        return
    try:
        await globals.application_bot.send_message(
            chat_id=chat_id,
            text=f"<i>{status_msg}</i>",
            parse_mode="HTML",
            message_thread_id=thread_id,
        )
    except BadRequest as e:
        logging.warning(
            "⚠️ ENGINE: Skipping tool status message for chat_id=%s thread_id=%s: %s",
            chat_id,
            thread_id,
            e,
        )
    except Exception as e:
        logging.error(
            "❌ ENGINE: Unexpected error sending tool status message to chat_id=%s thread_id=%s: %s",
            chat_id,
            thread_id,
            e,
            exc_info=True,
        )


async def _execute_tool_call(fn: str, args: dict, available_tools=None):
    logging.info("🔧 TOOL: %s | Args: %s", fn, format_logging_payload(args))
    registry = AVAILABLE_TOOLS if available_tools is None else available_tools
    tool = registry[fn]
    return await dispatch_tool_call(fn, args, tool)


async def warm_main_model_cache(
    history_buffer,
    model_to_use=MODEL_ID,
    reason: str = "",
    session_context=None,
) -> bool:
    if not history_buffer:
        return False

    if session_context is None:
        bound_context = globals.CURRENT_SESSION_CONTEXT.get()
        session_context = getattr(bound_context, "prompt", bound_context)

    _, active_tools_schema, _ = resolve_tooling(
        AVAILABLE_TOOLS,
        tools_schema,
        request_context=session_context,
    )
    payload, _ = _build_main_model_payload(
        history_buffer=history_buffer,
        model_to_use=model_to_use,
        max_tokens=1,
        temperature=0.0,
        top_p=1.0,
        top_k=1,
        allow_tools=True,
        stream=False,
        session_context=session_context,
        tools_schema_override=active_tools_schema,
    )

    try:
        label = f" ({reason})" if reason else ""
        logging.info("🤖 ENGINE: Warming main model cache%s...", label)
        async with globals.main_model_lock:
            request_started = time.perf_counter()
            r = await globals.http_client.post(MAIN_MODEL_URL, json=payload, timeout=900)
            request_wall_seconds = time.perf_counter() - request_started

        if r.status_code != 200:
            logging.warning("⚠️ ENGINE: Cache warmup returned %s — %s", r.status_code, r.text[:200])
            return False

        _log_main_model_perf(r.json(), request_wall_seconds)
        logging.info("🤖 ENGINE: Main model cache warmup complete%s.", label)
        return True
    except Exception as e:
        logging.error("❌ ENGINE: Cache warmup failed: %s", e, exc_info=True)
        return False


TOOL_STATUS_MESSAGES = {
    "delegate_to_coprocessor": f"{MODEL_NAME} is delegating a task to the coprocessor...",
    "save_user_memory": f"{MODEL_NAME} is writing this down in memory...",
    "jot_down_note": f"{MODEL_NAME} is jotting down a working note...",
    "read_scratchpad": f"{MODEL_NAME} is checking the scratchpad...",
    "clear_scratchpad": f"{MODEL_NAME} is clearing the scratchpad...",
    "web_search": f"{MODEL_NAME} is surfing the web...",
    "get_youtube_transcript": f"{MODEL_NAME} is reading the video transcript...",
    "get_calendar_events": f"{MODEL_NAME} is checking your calendar...",
    "get_nest_thermostats": f"{MODEL_NAME} is checking the Nest thermostat status...",
    "set_nest_thermostat_mode": f"{MODEL_NAME} is changing the Nest thermostat mode...",
    "set_nest_thermostat_temperature": f"{MODEL_NAME} is adjusting the Nest thermostat temperature...",
    "get_noaa_weather": f"{MODEL_NAME} is looking outside...",
    "set_weather_location_alias": f"{MODEL_NAME} is saving a weather location...",
    "remove_weather_location_alias": f"{MODEL_NAME} is clearing a weather location...",
    "list_weather_location_aliases": f"{MODEL_NAME} is checking saved weather locations...",
    "generate_image": f"{MODEL_NAME} is painting a picture...",
    "get_news_headlines": f"{MODEL_NAME} is reading the morning news...",
    "get_nasa_apod": f"{MODEL_NAME} is studying the stars...",
    "get_today_in_history": f"{MODEL_NAME} is dusting off the archives...",
    "speak_message": f"{MODEL_NAME} is recording a voice memo...",
    "overseer_search_movie": f"{MODEL_NAME} is searching for a movie...",
    "overseer_request_movie": f"{MODEL_NAME} is requesting a movie...",
    "overseer_search_tv": f"{MODEL_NAME} is searching for a TV show...",
    "overseer_request_tv_season": f"{MODEL_NAME} is requesting a TV season...",
    "fetch_web_content": f"{MODEL_NAME} is fetching a website...",
    "import_recipe_to_mealie": f"{MODEL_NAME} is importing a recipe to Mealie...",
    "search_fred_series": f"{MODEL_NAME} is searching the FRED database...",
    "get_fred_series_observations": f"{MODEL_NAME} is pulling FRED economic data...",
    "search_imf_indicators": f"{MODEL_NAME} is searching IMF indicators...",
    "get_imf_datamapper_series": f"{MODEL_NAME} is pulling IMF economic data...",
    "get_stock_snapshot": f"{MODEL_NAME} is checking the market...",
    "get_stock_price_history": f"{MODEL_NAME} is pulling stock price history...",
    "get_bond_market_dashboard": f"{MODEL_NAME} is assembling a bond market dashboard...",
    "get_inflation_dashboard": f"{MODEL_NAME} is assembling an inflation dashboard...",
    "get_us_macro_dashboard": f"{MODEL_NAME} is assembling a U.S. macro dashboard...",
    "get_equity_market_dashboard": f"{MODEL_NAME} is assembling an equity market dashboard...",
    "get_global_macro_dashboard": f"{MODEL_NAME} is assembling a global macro dashboard...",
    "get_housing_consumer_dashboard": f"{MODEL_NAME} is assembling a housing and consumer dashboard...",
    "get_labor_market_dashboard": f"{MODEL_NAME} is assembling a labor market dashboard...",
    "get_reolink_snapshot": f"{MODEL_NAME} is investigating a bump in the night...",
    "get_available_cameras": f"{MODEL_NAME} is reading your camera configuration...",
    "get_camera_security_log": f"{MODEL_NAME} is reviewing the security log...",
    "add_scheduled_job": f"{MODEL_NAME} is scheduling a job...",
    "list_scheduled_jobs": f"{MODEL_NAME} is retrieving scheduled jobs...",
    "remove_scheduled_job": f"{MODEL_NAME} is removing a scheduled job...",
    "list_portainer_environments": f"{MODEL_NAME} is retrieving Portainer environments...",
    "list_portainer_containers": f"{MODEL_NAME} is listing Portainer containers...",
    "update_portainer_container": f"{MODEL_NAME} is updating a container in Portainer...",
    "send_inter_agent_message": f"{MODEL_NAME} is consulting Hermes...",
}


# --- THE UNIFIED ENGINE ---
async def emery_engine(
    history_buffer,
    model_to_use=MODEL_ID,
    allow_tools=True,
    on_event=None,
    session_context=None,
    turn_context=None,
):
    url = MAIN_MODEL_URL
    # Find the latest sender info from the history buffer.
    sender_user_id = None
    for msg in reversed(history_buffer):
        if msg.get("role") == "user" and not msg.get("is_heartbeat_trigger") and not msg.get("is_reaction_trigger"):
            sender_user_id = msg.get("user_id")
            break
            
    if sender_user_id is not None:
        globals.current_user_id.set(sender_user_id)
    else:
        sender_user_id = globals.current_user_id.get()
        
    voice_sent_via_tool = False
    thinking_timeline = []
    active_tools, active_tools_schema, tooling_source = resolve_tooling(
        AVAILABLE_TOOLS,
        tools_schema,
        request_context=session_context,
    )
    logging.debug("ENGINE: Tool assembly source=%s tools=%s", tooling_source, len(active_tools_schema))
    payload, ollama_history = _build_main_model_payload(
        history_buffer=history_buffer,
        model_to_use=model_to_use,
        allow_tools=allow_tools,
        session_context=session_context,
        turn_context=turn_context,
        tools_schema_override=active_tools_schema,
    )
    request_diagnostics = {
        "epoch": current_prompt_epoch(),
        "model": payload.get("model"),
        "message_count": len(payload.get("messages") or []),
        "stable_prefix_hash": stable_hash((payload.get("messages") or [{}])[0]),
        "tool_schema_hash": stable_hash(payload.get("tools") or []),
        "request_shape_hash": prompt_request_shape_hash(payload),
    }
    stable_messages = copy.deepcopy(payload.get("messages", [])[:1])
    for loop_count in range(TOOL_LOOP):
        # Reuse the frozen prefix and copy only the changing conversation
        # tail for each request. This keeps tool-loop assembly deterministic.
        payload["messages"] = stable_messages + copy.deepcopy(ollama_history)
 
        try:
            logging.info(f"🤖 ENGINE: Thinking... (loop {loop_count+1}/{TOOL_LOOP})")
            async with globals.main_model_lock:
                request_started = time.perf_counter()
                streamed = bool(payload.get("stream"))
                if streamed:
                    try:
                        res, request_wall_seconds = await _stream_main_model_response(url, payload, on_event=on_event)
                    except _StreamingModelError as stream_error:
                        if stream_error.events_seen:
                            logging.error("❌ ENGINE: Main model stream failed after partial output: %s", stream_error)
                            await _emit_engine_event(
                                on_event,
                                {"type": "stream_error", "text": "Main model stream failed after partial output."},
                            )
                            return "Main model stream error.", False

                        logging.warning("⚠️ ENGINE: Streaming unavailable; retrying once without streaming: %s", stream_error)
                        payload["stream"] = False
                        r = await globals.http_client.post(url, json=payload, timeout=900)
                        request_wall_seconds = time.perf_counter() - request_started
                        if r.status_code != 200:
                            logging.error(f"❌ ENGINE: Main model returned {r.status_code} — {r.text[:200]}")
                            return "Main model connection error.", False
                        res = r.json()
                        _log_main_model_perf(res, request_wall_seconds)
                else:
                    r = await globals.http_client.post(url, json=payload, timeout=900)
                    request_wall_seconds = time.perf_counter() - request_started
                    if r.status_code != 200:
                        logging.error(f"❌ ENGINE: Main model returned {r.status_code} — {r.text[:200]}")
                        return "Main model connection error.", False
                    res = r.json()
                    _log_main_model_perf(res, request_wall_seconds)

            msg = _extract_response_message(res)
            logging.debug("ENGINE: Response diagnostics %s", format_cache_diagnostics(request_diagnostics, res))
            raw_content = message_content_to_text(msg.get("content"))
            content_thoughts, cleaned_msg_content = _extract_thinking_blocks(raw_content)
            had_progress_markup = bool(
                re.search(r"<progress>.*?</progress>", raw_content, flags=re.DOTALL | re.IGNORECASE)
            )
            cleaned_msg_content = re.sub(
                r"<progress>(.*?)</progress>",
                lambda match: _sanitize_model_preamble(match.group(1)),
                cleaned_msg_content,
                flags=re.DOTALL | re.IGNORECASE,
            ).strip()
            reasoning = message_content_to_text(
                msg.get("reasoning_content") or msg.get("thinking") or msg.get("reasoning")
            )
            turn_reasoning_parts = []
            if reasoning:
                reasoning = _strip_id_prefix(reasoning)
                turn_reasoning_parts.append(reasoning)
            for thought in content_thoughts:
                turn_reasoning_parts.append(_strip_id_prefix(thought))

            if turn_reasoning_parts:
                reasoning_summary = await _summarize_reasoning_block(
                    "\n\n".join(part for part in turn_reasoning_parts if part),
                    loop_count=loop_count,
                )
                if reasoning_summary:
                    thinking_timeline.append(
                        _format_thinking_turn(loop_count, "Summary", reasoning_summary)
                    )

            if allow_tools and msg.get("tool_calls"):
                if not had_progress_markup:
                    preamble = _sanitize_model_preamble(cleaned_msg_content)
                    if preamble:
                        await _emit_engine_event(
                            on_event,
                            {"type": "preamble", "text": preamble, "source": "model"},
                        )
                parsed_tool_calls = []
                for tc in msg["tool_calls"]:
                    function = tc.get("function") or {}
                    fn = function.get("name")
                    args = _parse_tool_arguments(function.get("arguments", {}))
                    if not fn or fn not in active_tools or args is None:
                        logging.error(
                            "❌ ENGINE: Refusing malformed or unknown streamed tool call: name=%r args=%r",
                            fn,
                            safe_preview(function.get("arguments", ""), max_len=240),
                        )
                        await _emit_engine_event(
                            on_event,
                            {"type": "stream_error", "text": "Model returned an invalid tool call."},
                        )
                        return "Main model tool-call error.", False
                    parsed_tool_calls.append((tc, fn, args))

                assistant_tool_msg = {
                    "role": msg.get("role", "assistant"),
                    "content": cleaned_msg_content,
                    "tool_calls": msg["tool_calls"],
                }
                history_buffer.append(assistant_tool_msg)
                ollama_history.append(assistant_tool_msg)
                for tc, fn, args in parsed_tool_calls:
                    
                    await _send_tool_status(fn, args, on_event=on_event)
                    _append_tool_timeline_entry(thinking_timeline, fn)
                    if fn == "speak_message": 
                        voice_sent_via_tool = True
                    
                    result = await _execute_tool_call(fn, args, available_tools=active_tools)
                    
                    tool_response = {
                        "role": "tool",
                        "content": str(result),
                        "name": fn
                    }
                    if "id" in tc:
                        tool_response["tool_call_id"] = tc["id"]
                        
                    history_buffer.append(tool_response)
                    ollama_history.append(tool_response)
                    await _emit_engine_event(
                        on_event,
                        {"type": "tool_finished", "tool": fn},
                    )
                continue
            
            content = cleaned_msg_content
            # Strip hallucinated [ID: ...] prefixes that the model imitated from history formatting
            content = re.sub(r'(</think>\s*)\[ID:\s*\d+[^\]]*\]\s*', r'\1', content, flags=re.IGNORECASE)
            content = re.sub(r'^\s*\[ID:\s*\d+[^\]]*\]\s*', '', content, flags=re.IGNORECASE)

            thinking_char_count = sum(len(entry) for entry in thinking_timeline if entry)
            logging.info(f"🤖 ENGINE: Response ready — {len(content)} chars" + (f", {thinking_char_count} chars reasoning" if thinking_char_count else ""))

            thinking_payload = "\n\n".join(entry for entry in thinking_timeline if entry)
            if thinking_payload:
                start_think_tag = "<" + "think" + ">"
                end_think_tag = "</" + "think" + ">"
                final_text = f"{start_think_tag}\n{thinking_payload}\n{end_think_tag}\n{content}"
            else:
                final_text = content

            return final_text, voice_sent_via_tool
            
        except Exception as e:
            logging.error(f"❌ ENGINE: Crash — {e}", exc_info=True)
            return "EMERYCHAT engine failure.", False
            
    return "Timeout.", False
