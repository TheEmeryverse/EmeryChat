import asyncio
import json
import logging
import re
import time
from urllib.parse import urlparse

from telegram.error import BadRequest

from emery.config import (
    AGENTIC_FAST_ALLOWED_TOOLS,
    AGENTIC_FAST_MAX_TOOL_CALLS,
    AGENTIC_FAST_TOOLS_ENABLED,
    FAST_MODEL_ID,
    FAST_MODEL_URL,
    MAIN_MODEL_URL,
    MODEL_ID,
    TOOL_LOOP,
    MODEL_NAME,
    THINK,
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
from emery.logging_utils import format_logging_payload, format_llama_perf_line, safe_preview
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


def _format_tool_timeline_entry(fn: str) -> str:
    return f"{MODEL_NAME} used {fn}"


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


def _extract_json_object(text: str):
    text = clean_thinking_tags(normalize_gemma_thinking(str(text or ""))).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _log_main_model_perf(response_json: dict, wall_seconds: float) -> None:
    logging.info(format_llama_perf_line("MAIN", response_json, wall_seconds))


def _build_ollama_history(history_buffer) -> list[dict]:
    ollama_history = []
    for msg in history_buffer:
        clean_msg = {"role": msg["role"]}

        # Preserve tool calling fields if present in history
        if "tool_calls" in msg:
            clean_msg["tool_calls"] = msg["tool_calls"]
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
) -> tuple[dict, list[dict]]:
    chat_template_kwargs = {"enable_thinking": bool(THINK)}
    stable_prefix = [{"role": "system", "content": get_stable_system_prompt()}]
    ollama_history = _build_ollama_history(history_buffer)

    payload = {
        "model": model_to_use,
        "messages": stable_prefix + ollama_history,
        "stream": False,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
        "top_k": top_k,
        "chat_template_kwargs": chat_template_kwargs,
    }

    if allow_tools and tools_schema:
        payload["tools"] = tools_schema

    return payload, ollama_history


FAST_AGENTIC_DENYLIST = {
    "set_nest_thermostat_mode",
    "set_nest_thermostat_temperature",
    "set_weather_location_alias",
    "remove_weather_location_alias",
    "overseer_request_movie",
    "overseer_request_tv_season",
    "generate_image",
    "get_reolink_snapshot",
    "speak_message",
    "save_user_memory",
    "react_to_message",
    "reply_to_message",
    "send_sticker",
    "send_gif",
    "add_scheduled_job",
    "remove_scheduled_job",
    "update_portainer_container",
}


def _tool_schema_by_name() -> dict[str, dict]:
    schemas = {}
    for schema in tools_schema:
        function = schema.get("function", {}) if isinstance(schema, dict) else {}
        name = function.get("name")
        if name:
            schemas[name] = schema
    return schemas


def _fast_agentic_tool_names() -> list[str]:
    if not AGENTIC_FAST_TOOLS_ENABLED or AGENTIC_FAST_MAX_TOOL_CALLS <= 0:
        return []

    configured = set(AGENTIC_FAST_ALLOWED_TOOLS or ())
    schemas = _tool_schema_by_name()
    names = []
    for schema in tools_schema:
        function = schema.get("function", {}) if isinstance(schema, dict) else {}
        name = function.get("name")
        if (
            name
            and name in configured
            and name in AVAILABLE_TOOLS
            and name in schemas
            and name not in FAST_AGENTIC_DENYLIST
        ):
            names.append(name)
    return names


def _compact_tool_schema(schema: dict) -> dict:
    function = schema.get("function", {}) if isinstance(schema, dict) else {}
    parameters = function.get("parameters") or {"type": "object", "properties": {}}
    return {
        "name": function.get("name", ""),
        "description": function.get("description", ""),
        "parameters": parameters,
    }


def _compact_agentic_history(history_buffer, limit: int = 8) -> list[dict]:
    compact = []
    recent_messages = list(history_buffer)[-limit:]
    for msg in recent_messages:
        role = msg.get("role", "")
        if role not in {"system", "user", "assistant", "tool"}:
            continue

        content = msg.get("content", "")
        if isinstance(content, list):
            text_parts = [
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            content = " ".join(text_parts) if text_parts else "[Sent an image]"
        elif content is None:
            content = ""
        else:
            content = str(content)

        compact.append({
            "role": role,
            "name": msg.get("name", ""),
            "content": clean_thinking_tags(content)[:1600],
        })
    return compact


def _validate_fast_tool_call(call: dict, allowed_names: set[str], schemas: dict[str, dict]) -> dict | None:
    if not isinstance(call, dict):
        return None

    name = str(call.get("name") or call.get("function", {}).get("name") or "").strip()
    if name not in allowed_names or name not in AVAILABLE_TOOLS:
        return None

    raw_args = call.get("arguments")
    if raw_args is None and isinstance(call.get("function"), dict):
        raw_args = call["function"].get("arguments")
    args = _normalize_tool_arguments(raw_args)
    if not isinstance(args, dict):
        return None

    function = schemas.get(name, {}).get("function", {})
    parameters = function.get("parameters") or {}
    properties = parameters.get("properties") or {}
    allowed_arg_names = set(properties.keys())
    if allowed_arg_names:
        args = {key: value for key, value in args.items() if key in allowed_arg_names}
    else:
        args = {}

    missing = [
        required
        for required in parameters.get("required", [])
        if required not in args or args.get(required) in (None, "")
    ]
    if missing:
        logging.info("⚡ AGENTIC: Skipping %s because required args are missing: %s", name, missing)
        return None

    return {"name": name, "arguments": args}


def _parse_fast_agentic_calls(message: dict, allowed_names: set[str], schemas: dict[str, dict]) -> list[dict]:
    calls = []
    for tool_call in message.get("tool_calls") or []:
        function = tool_call.get("function") or {}
        parsed = _validate_fast_tool_call(
            {"name": function.get("name"), "arguments": function.get("arguments")},
            allowed_names,
            schemas,
        )
        if parsed:
            calls.append(parsed)

    if calls:
        return calls

    content = message.get("content") or ""
    if isinstance(content, list):
        content = "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    data = _extract_json_object(content)
    if not isinstance(data, dict):
        return []

    raw_calls = data.get("calls") or data.get("tool_calls") or []
    if isinstance(raw_calls, dict):
        raw_calls = [raw_calls]
    if not isinstance(raw_calls, list):
        return []

    for raw_call in raw_calls:
        parsed = _validate_fast_tool_call(raw_call, allowed_names, schemas)
        if parsed:
            calls.append(parsed)
    return calls


async def _query_fast_agentic_tool_calls(history_buffer, user_query: str, allowed_names: list[str]) -> list[dict]:
    parsed_url = urlparse((FAST_MODEL_URL or "").strip())
    if not parsed_url.path.rstrip("/").endswith("/chat/completions"):
        logging.warning("⚡ AGENTIC: FAST_MODEL_URL is not an OpenAI-compatible chat-completions endpoint: %s", FAST_MODEL_URL)
        return []

    schemas = _tool_schema_by_name()
    allowed_set = set(allowed_names)
    selected_schemas = [schemas[name] for name in allowed_names if name in schemas]
    max_calls = max(1, min(AGENTIC_FAST_MAX_TOOL_CALLS, 6))
    tool_catalog = [_compact_tool_schema(schema) for schema in selected_schemas]
    history = _compact_agentic_history(history_buffer)

    system_prompt = (
        "You are Emery's fast tool strategist. Decide whether read-only tool calls would materially help "
        "answer the latest user request before the main model writes. Use zero calls for ordinary chat, "
        "opinion, creative writing, or requests already answerable from context. Never call tools that change "
        "state, send messages, save memory, schedule work, request media, or modify devices. Prefer one precise "
        f"call; use at most {max_calls}. If using native tool calls, call the provided tools directly. If not, "
        'return strict JSON: {"calls":[{"name":"tool_name","arguments":{}}]}.'
    )
    user_prompt = json.dumps(
        {
            "latest_user_request": user_query,
            "recent_conversation": history,
            "allowed_read_only_tools": tool_catalog,
            "max_calls": max_calls,
        },
        ensure_ascii=False,
    )

    payload = {
        "model": FAST_MODEL_ID,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "tools": selected_schemas,
        "tool_choice": "auto",
        "stream": False,
        "temperature": 0.0,
        "max_tokens": 1200,
        "chat_template_kwargs": {"enable_thinking": False},
    }

    try:
        logging.info("⚡ AGENTIC: Asking %s for read-only tool preflight...", FAST_MODEL_ID)
        request_started = time.perf_counter()
        async with globals.fast_model_lock:
            response = await globals.http_client.post(FAST_MODEL_URL.rstrip("/"), json=payload, timeout=120)
        wall_seconds = time.perf_counter() - request_started
        if response.status_code != 200:
            logging.warning(
                "⚡ AGENTIC: Fast tool preflight returned HTTP %s: %s",
                response.status_code,
                safe_preview(response.text, max_len=240),
            )
            return []

        data = response.json()
        logging.info(format_llama_perf_line("FAST-AGENTIC", data, wall_seconds))
        message = _extract_response_message(data)
        calls = _parse_fast_agentic_calls(message, allowed_set, schemas)
        return calls[:max_calls]
    except Exception as exc:
        logging.warning("⚡ AGENTIC: Fast tool preflight failed: %s", exc, exc_info=True)
        return []


async def _send_tool_status(fn: str, args: dict) -> None:
    if fn in ("react_to_message", "reply_to_message", "send_sticker", "send_gif"):
        return

    chat_id = globals.TARGET_CHAT_ID.get()
    thread_id = normalize_message_thread_id(chat_id, globals.CURRENT_THREAD_ID.get())
    if chat_id is None:
        return

    status_msg = await _format_tool_status_message(fn, args)
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


async def _execute_tool_call(fn: str, args: dict):
    logging.info("🔧 TOOL: %s | Args: %s", fn, format_logging_payload(args))
    return await AVAILABLE_TOOLS[fn](**args) if args else await AVAILABLE_TOOLS[fn]()


async def _run_fast_agentic_preflight(history_buffer, ollama_history, user_query: str, thinking_timeline: list[str]) -> bool:
    if not user_query or not AGENTIC_FAST_TOOLS_ENABLED or not tools_schema:
        return False

    allowed_names = _fast_agentic_tool_names()
    if not allowed_names:
        return False

    calls = await _query_fast_agentic_tool_calls(history_buffer, user_query, allowed_names)
    if not calls:
        return False

    tool_calls = []
    for index, call in enumerate(calls, start=1):
        tool_calls.append({
            "id": f"fast_agentic_{int(time.time() * 1000)}_{index}",
            "type": "function",
            "function": {
                "name": call["name"],
                "arguments": json.dumps(call.get("arguments", {})),
            },
        })

    assistant_tool_msg = {
        "role": "assistant",
        "content": "",
        "tool_calls": tool_calls,
    }
    history_buffer.append(assistant_tool_msg)
    ollama_history.append(assistant_tool_msg)

    for tool_call, call in zip(tool_calls, calls):
        fn = call["name"]
        args = call.get("arguments", {})
        await _send_tool_status(fn, args)
        thinking_timeline.append(f"{FAST_MODEL_ID} preflight used {fn}")
        try:
            result = await _execute_tool_call(fn, args)
        except Exception as exc:
            logging.error("❌ TOOL: %s failed during fast preflight: %s", fn, exc, exc_info=True)
            result = f"Tool {fn} failed during fast preflight: {exc}"

        tool_response = {
            "role": "tool",
            "content": str(result),
            "name": fn,
            "tool_call_id": tool_call["id"],
        }
        history_buffer.append(tool_response)
        ollama_history.append(tool_response)

    return True


async def warm_main_model_cache(history_buffer, model_to_use=MODEL_ID, reason: str = "") -> bool:
    if not history_buffer:
        return False

    payload, _ = _build_main_model_payload(
        history_buffer=history_buffer,
        model_to_use=model_to_use,
        max_tokens=1,
        temperature=0.0,
        top_p=1.0,
        top_k=1,
        allow_tools=True,
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
    "update_portainer_container": f"{MODEL_NAME} is updating a container in Portainer..."
}


# --- THE UNIFIED ENGINE ---
async def emery_engine(history_buffer, model_to_use=MODEL_ID, allow_tools=True):
    url = MAIN_MODEL_URL
    # Find the latest user query and sender info from the history buffer
    user_query = ""
    sender_user_id = None
    for msg in reversed(history_buffer):
        if msg.get("role") == "user" and not msg.get("is_heartbeat_trigger") and not msg.get("is_reaction_trigger"):
            user_query = msg.get("content", "")
            sender_user_id = msg.get("user_id")
            break
            
    if sender_user_id is not None:
        globals.current_user_id.set(sender_user_id)
    else:
        sender_user_id = globals.current_user_id.get()
        
    voice_sent_via_tool = False
    thinking_timeline = []
    payload, ollama_history = _build_main_model_payload(
        history_buffer=history_buffer,
        model_to_use=model_to_use,
        allow_tools=allow_tools,
    )
    if allow_tools:
        await _run_fast_agentic_preflight(history_buffer, ollama_history, user_query, thinking_timeline)
    
    for loop_count in range(TOOL_LOOP):
        payload["messages"] = [{"role": "system", "content": get_stable_system_prompt()}] + ollama_history
 
        try:
            logging.info(f"🤖 ENGINE: Thinking... (loop {loop_count+1}/{TOOL_LOOP})")
            async with globals.main_model_lock:
                request_started = time.perf_counter()
                r = await globals.http_client.post(url, json=payload, timeout=900)
                request_wall_seconds = time.perf_counter() - request_started
            
            if r.status_code != 200:
                logging.error(f"❌ ENGINE: Main model returned {r.status_code} — {r.text[:200]}")
                return "Main model connection error.", False
 
            res = r.json()
            _log_main_model_perf(res, request_wall_seconds)
            msg = _extract_response_message(res)
            raw_content = message_content_to_text(msg.get("content"))
            content_thoughts, cleaned_msg_content = _extract_thinking_blocks(raw_content)
            reasoning = message_content_to_text(
                msg.get("reasoning_content") or msg.get("thinking") or msg.get("reasoning")
            )
            if reasoning:
                reasoning = _strip_id_prefix(reasoning)
                thinking_timeline.append(_format_thinking_turn(loop_count, "Reasoning", reasoning))
            for thought in content_thoughts:
                thinking_timeline.append(_format_thinking_turn(loop_count, "Inline thought", thought))

            if allow_tools and msg.get("tool_calls"):
                assistant_tool_msg = {
                    "role": msg.get("role", "assistant"),
                    "content": cleaned_msg_content,
                    "tool_calls": msg["tool_calls"],
                }
                history_buffer.append(assistant_tool_msg)
                ollama_history.append(assistant_tool_msg)
                for tc in assistant_tool_msg['tool_calls']:
                    fn = tc['function']['name']
                    args = _normalize_tool_arguments(tc['function'].get('arguments', {}))
                    
                    await _send_tool_status(fn, args)
                    thinking_timeline.append(_format_tool_timeline_entry(fn))
                    if fn == "speak_message": 
                        voice_sent_via_tool = True
                    
                    result = await _execute_tool_call(fn, args)
                    
                    tool_response = {
                        "role": "tool",
                        "content": str(result),
                        "name": fn
                    }
                    if "id" in tc:
                        tool_response["tool_call_id"] = tc["id"]
                        
                    history_buffer.append(tool_response)
                    ollama_history.append(tool_response)
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
