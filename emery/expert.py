import asyncio
import json
import logging
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

from emery.config import (
    ENABLE_FINANCE,
    EXPERT_ARCHIVE_DIR,
    EXPERT_INDEX_PATH,
    MAIN_MODEL_URL,
    MODEL_ID,
    MODEL_NAME,
    SEARXNG_URL,
    THINK,
    USER_TIMEZONE,
)
from emery.helpers import clean_thinking_tags, emery_format, query_fast_model, telegram_escape
from emery.logging_utils import safe_preview
from emery.telegram_delivery import send_rich_or_split_html_message
from emery.telegram_utils import normalize_message_thread_id
from emery.tools import (
    fetch_web_content,
    search_fred_series,
    get_fred_series_observations,
    search_imf_indicators,
    get_imf_datamapper_series,
    get_stock_snapshot,
    get_stock_price_history,
    get_bond_market_dashboard,
    get_inflation_dashboard,
    get_us_macro_dashboard,
    get_equity_market_dashboard,
    get_global_macro_dashboard,
    get_housing_consumer_dashboard,
    get_labor_market_dashboard,
)
import emery.globals as globals


ACTIVE_SESSIONS: dict[tuple[int, int | None], "ExpertSession"] = {}
SESSION_TASKS: dict[str, asyncio.Task] = {}

CALLBACK_PREFIX = "expert"
DEFAULT_TARGET_SOURCES = 20
DEFAULT_MAX_SOURCES = 30
DEFAULT_MAX_ROUNDS = 6
FETCHES_PER_ROUND = 6
SEARCH_RESULTS_PER_QUERY = 8
ECON_REQUESTS_PER_ROUND = 3

COMPLETED_WAITING_STATES = {"completed_pending_user", "waiting_for_answer"}

ECON_TOOL_FUNCTIONS = {
    "search_fred_series": search_fred_series,
    "get_fred_series_observations": get_fred_series_observations,
    "search_imf_indicators": search_imf_indicators,
    "get_imf_datamapper_series": get_imf_datamapper_series,
    "get_stock_snapshot": get_stock_snapshot,
    "get_stock_price_history": get_stock_price_history,
    "get_bond_market_dashboard": get_bond_market_dashboard,
    "get_inflation_dashboard": get_inflation_dashboard,
    "get_us_macro_dashboard": get_us_macro_dashboard,
    "get_equity_market_dashboard": get_equity_market_dashboard,
    "get_global_macro_dashboard": get_global_macro_dashboard,
    "get_housing_consumer_dashboard": get_housing_consumer_dashboard,
    "get_labor_market_dashboard": get_labor_market_dashboard,
}

ECON_TOOL_ALLOWED_ARGS = {
    "search_fred_series": {"query", "limit"},
    "get_fred_series_observations": {"series_id", "observation_start", "observation_end", "units", "frequency", "limit"},
    "search_imf_indicators": {"query", "limit"},
    "get_imf_datamapper_series": {"indicator", "countries", "start_year", "end_year"},
    "get_stock_snapshot": {"symbol"},
    "get_stock_price_history": {"symbol", "outputsize", "limit"},
    "get_global_macro_dashboard": {"countries", "start_year", "end_year"},
}


@dataclass
class ExpertQuestion:
    id: str
    prompt: str
    options: list[dict] = field(default_factory=list)
    critical: bool = True


@dataclass
class ExpertSession:
    id: str
    title: str
    topic: str
    chat_id: int
    message_thread_id: int | None
    user_id: int | None
    status: str = "running"
    created_at: str = field(default_factory=lambda: _now_iso())
    updated_at: str = field(default_factory=lambda: _now_iso())
    round: int = 0
    target_sources: int = DEFAULT_TARGET_SOURCES
    max_sources: int = DEFAULT_MAX_SOURCES
    max_rounds: int = DEFAULT_MAX_ROUNDS
    search_queries: list[str] = field(default_factory=list)
    search_results: list[dict] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    econ_results: list[dict] = field(default_factory=list)
    loop_events: list[dict] = field(default_factory=list)
    pending_questions: list[dict] = field(default_factory=list)
    pending_answers: dict[str, str] = field(default_factory=dict)
    user_inputs: list[dict] = field(default_factory=list)
    final_report: str = ""
    final_report_versions: list[dict] = field(default_factory=list)
    archive_path: str = ""
    followup_instruction: str = ""

    def key(self) -> tuple[int, int | None]:
        return self.chat_id, normalize_message_thread_id(self.chat_id, self.message_thread_id)

    def touch(self) -> None:
        self.updated_at = _now_iso()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ExpertSession":
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        clean = {key: value for key, value in data.items() if key in allowed}
        return cls(**clean)


def _now_iso() -> str:
    return datetime.now(USER_TIMEZONE).replace(microsecond=0).isoformat()


def _now_label() -> str:
    return datetime.now(USER_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %Z")


def _session_key(chat_id: int, message_thread_id: int | None) -> tuple[int, int | None]:
    return chat_id, normalize_message_thread_id(chat_id, message_thread_id)


def _slugify(text: str, max_len: int = 50) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", str(text or "").lower()).strip("-")
    return (slug[:max_len].strip("-") or "expert-session")


def _short_id() -> str:
    return uuid.uuid4().hex[:8]


def _extract_json_object(text: str):
    text = clean_thinking_tags(str(text or "")).strip()
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


def _normalize_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    scheme = (parsed.scheme or "https").lower()
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/+$", "", parsed.path or "")
    return urlunparse((scheme, host, path, "", "", ""))


def _source_domain(url: str) -> str:
    host = urlparse(str(url or "")).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _source_count(session: ExpertSession) -> int:
    return len([src for src in session.sources if src.get("fetch_success")])


def _econ_count(session: ExpertSession) -> int:
    return len([result for result in session.econ_results if result.get("success")])


def _record_event(session: ExpertSession, event_type: str, message: str, **metadata) -> None:
    session.loop_events.append({
        "time": _now_label(),
        "type": event_type,
        "message": message,
        "metadata": metadata,
    })
    session.touch()


def _expert_status_prefix(session: ExpertSession) -> str:
    return (
        f"<b>Expert mode</b> "
        f"<code>{session.id}</code> "
        f"<b>R{session.round}/{session.max_rounds}</b>"
    )


def _expert_counts_text(session: ExpertSession) -> str:
    econ_text = f" | Econ {_econ_count(session)}" if ENABLE_FINANCE else ""
    return f"Sources {_source_count(session)}/{session.target_sources}{econ_text}"


async def _send_expert_progress(bot, session: ExpertSession, action: str, detail: str = "", *, reply_markup=None, detail_is_html: bool = False) -> None:
    lines = [_expert_status_prefix(session), f"<i>{telegram_escape(action)}</i>"]
    if detail:
        lines.append(detail if detail_is_html else telegram_escape(detail))
    lines.append(f"<code>{_expert_counts_text(session)}</code>")
    await _send_status(bot, session, "\n".join(lines), reply_markup=reply_markup)


def _load_index() -> list[dict]:
    path = Path(EXPERT_INDEX_PATH)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as exc:
        logging.warning("EXPERT: Unable to load index %s: %s", path, exc)
        return []


def _save_index(entries: list[dict]) -> None:
    path = Path(EXPERT_INDEX_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def _archive_root() -> Path:
    return Path(os.path.expanduser(EXPERT_ARCHIVE_DIR)).resolve()


def _callback(action: str, session_id: str, *parts: str) -> str:
    clean_parts = [str(part).replace(":", "_")[:16] for part in parts]
    return ":".join([CALLBACK_PREFIX, session_id, action, *clean_parts])[:64]


def _active_session_by_id(session_id: str) -> ExpertSession | None:
    for session in ACTIVE_SESSIONS.values():
        if session.id == session_id:
            return session
    return None


def _active_session_for_update(update) -> ExpertSession | None:
    if not update.effective_chat:
        return None
    chat_id = update.effective_chat.id
    thread_id = None
    if update.message:
        thread_id = update.message.message_thread_id
    elif update.callback_query and update.callback_query.message:
        thread_id = getattr(update.callback_query.message, "message_thread_id", None)
    return ACTIVE_SESSIONS.get(_session_key(chat_id, thread_id))


def _session_action_markup(session: ExpertSession) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Continue researching", callback_data=_callback("continue", session.id)),
            InlineKeyboardButton("Refine report", callback_data=_callback("refine", session.id)),
        ],
        [
            InlineKeyboardButton("Close and archive", callback_data=_callback("close", session.id)),
            InlineKeyboardButton("Cancel", callback_data=_callback("cancel", session.id)),
        ],
    ])


def _question_markup(session: ExpertSession) -> InlineKeyboardMarkup | None:
    rows = []
    for question in session.pending_questions[:4]:
        qid = str(question.get("id") or "")
        options = question.get("options") or []
        if not qid or not options:
            continue
        rows.append([InlineKeyboardButton(str(question.get("prompt", "Question"))[:64], callback_data=_callback("noop", session.id))])
        option_row = []
        for option in options[:4]:
            oid = str(option.get("id") or _slugify(option.get("label", "option"), 8))
            label = str(option.get("label") or oid)[:32]
            option_row.append(InlineKeyboardButton(label, callback_data=_callback("q", session.id, qid, oid)))
            if len(option_row) == 2:
                rows.append(option_row)
                option_row = []
        if option_row:
            rows.append(option_row)
    return InlineKeyboardMarkup(rows) if rows else None


async def _send_status(bot, session: ExpertSession, text: str, *, reply_markup=None) -> None:
    try:
        await bot.send_message(
            chat_id=session.chat_id,
            text=text,
            parse_mode="HTML",
            message_thread_id=session.message_thread_id,
            reply_markup=reply_markup,
        )
    except BadRequest as exc:
        logging.warning("EXPERT: Telegram rejected status message: %s", exc)
    except Exception as exc:
        logging.error("EXPERT: Failed to send status message: %s", exc, exc_info=True)


async def _warm_normal_chat_context(session: ExpertSession, reason: str) -> None:
    history = globals.chat_histories.get(session.chat_id)
    if not history:
        logging.debug("EXPERT: Skipping normal chat cache warmup after %s; no chat history for chat_id=%s.", reason, session.chat_id)
        return

    try:
        from emery.engine import warm_main_model_cache

        await warm_main_model_cache(
            history,
            reason=f"expert {reason}: chat_id={session.chat_id} thread_id={session.message_thread_id}",
        )
    except Exception as exc:
        logging.error("EXPERT: Normal chat cache warmup failed after %s: %s", reason, exc, exc_info=True)


def _schedule_normal_chat_warmup(session: ExpertSession, reason: str) -> None:
    asyncio.create_task(_warm_normal_chat_context(session, reason))


async def _query_main_model(prompt: str, system_prompt: str) -> str:
    payload = {
        "model": MODEL_ID,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "temperature": 0.35,
        "top_p": 0.9,
        "chat_template_kwargs": {"enable_thinking": bool(THINK)},
    }
    try:
        async with globals.main_model_lock:
            response = await globals.http_client.post(MAIN_MODEL_URL, json=payload, timeout=900)
        if response.status_code != 200:
            logging.error("EXPERT: Main model returned HTTP %s: %s", response.status_code, safe_preview(response.text))
            return ""
        data = response.json()
        message = ((data.get("choices") or [{}])[0]).get("message", {})
        content = message.get("content") or ""
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return clean_thinking_tags(content).strip()
    except Exception as exc:
        logging.error("EXPERT: Main model query failed: %s", exc, exc_info=True)
        return ""


async def _make_initial_plan(session: ExpertSession) -> dict:
    econ_instruction = ""
    if ENABLE_FINANCE:
        econ_instruction = (
            " Also include optional econ_requests when structured economic, market, trade, inflation, "
            "labor, GDP, rates, commodities-adjacent macro, country comparison, or stock context would materially improve the research. "
            f"{_available_econ_tool_text()}"
        )

    prompt = (
        "Create a deep research plan for this user request. Return strict JSON with keys: "
        "title, framing, search_queries, source_targets, econ_requests. Include 5-8 diverse web search queries."
        f"{econ_instruction}\n\n"
        f"User request: {session.topic}"
    )
    system = (
        "You are the lead research orchestrator for Emery's /expert mode. "
        "Return only compact JSON. Do not include hidden reasoning."
    )
    parsed = _extract_json_object(await _query_main_model(prompt, system)) or {}
    queries = parsed.get("search_queries") if isinstance(parsed, dict) else None
    if not isinstance(queries, list) or not queries:
        queries = [
            session.topic,
            f"{session.topic} latest developments",
            f"{session.topic} official statements",
            f"{session.topic} regional analysis",
            f"{session.topic} timeline",
        ]
    return {
        "title": str(parsed.get("title") or session.title),
        "framing": str(parsed.get("framing") or ""),
        "search_queries": [str(query).strip() for query in queries if str(query).strip()][:8],
        "source_targets": parsed.get("source_targets") if isinstance(parsed.get("source_targets"), list) else [],
        "econ_requests": _normalize_econ_requests(parsed.get("econ_requests")),
    }


async def _search_web(query: str) -> list[dict]:
    try:
        response = await globals.http_client.get(SEARXNG_URL, params={"q": query, "format": "json"}, timeout=40)
        payload = response.json()
        results = payload.get("results") or []
    except Exception as exc:
        logging.warning("EXPERT: Search failed for %r: %s", query, exc)
        return []

    clean_results = []
    for item in results[:SEARCH_RESULTS_PER_QUERY]:
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or "").strip()
        if not url or not title:
            continue
        clean_results.append({
            "query": query,
            "title": title,
            "url": url,
            "normalized_url": _normalize_url(url),
            "domain": _source_domain(url),
            "snippet": str(item.get("content") or item.get("snippet") or "").strip(),
        })
    return clean_results


async def _summarize_source(session: ExpertSession, source: dict, fetched: dict) -> dict:
    content = str(fetched.get("content") or "")
    prompt = (
        "Summarize this source for a deep research dossier. Return strict JSON with keys: "
        "summary, key_claims, dates, actors, perspective, reliability_label, useful_followups. "
        "Keep it factual and label uncertainty.\n\n"
        f"Research topic: {session.topic}\n"
        f"Title: {fetched.get('title') or source.get('title')}\n"
        f"URL: {fetched.get('url') or source.get('url')}\n\n"
        f"Content:\n{content[:9000]}"
    )
    system = "You distill research sources into compact JSON notes. Return only JSON."
    parsed = _extract_json_object(await query_fast_model(prompt, system)) or {}
    return {
        "id": f"S{len(session.sources) + 1}",
        "title": fetched.get("title") or source.get("title") or "Untitled",
        "url": fetched.get("url") or source.get("url"),
        "normalized_url": source.get("normalized_url") or _normalize_url(fetched.get("url") or source.get("url")),
        "domain": source.get("domain") or _source_domain(fetched.get("url") or source.get("url")),
        "snippet": source.get("snippet", ""),
        "fetch_success": bool(fetched.get("success")),
        "fetch_error": fetched.get("error", ""),
        "content": content[:12000],
        "summary": str(parsed.get("summary") or content[:800]),
        "key_claims": parsed.get("key_claims") if isinstance(parsed.get("key_claims"), list) else [],
        "dates": parsed.get("dates") if isinstance(parsed.get("dates"), list) else [],
        "actors": parsed.get("actors") if isinstance(parsed.get("actors"), list) else [],
        "perspective": str(parsed.get("perspective") or "Unlabeled"),
        "reliability_label": str(parsed.get("reliability_label") or "Unlabeled"),
        "useful_followups": parsed.get("useful_followups") if isinstance(parsed.get("useful_followups"), list) else [],
    }


def _source_digest(session: ExpertSession, limit: int = 24) -> str:
    chunks = []
    for source in session.sources[:limit]:
        claims = "; ".join(str(claim) for claim in source.get("key_claims", [])[:4])
        chunks.append(
            f"{source.get('id')} | {source.get('title')} | {source.get('domain')} | "
            f"{source.get('reliability_label')} | {source.get('perspective')}\n"
            f"Summary: {source.get('summary')}\n"
            f"Claims: {claims}"
        )
    return "\n\n".join(chunks)


def _econ_digest(session: ExpertSession, limit: int = 12) -> str:
    chunks = []
    for result in session.econ_results[:limit]:
        chunks.append(
            f"{result.get('id')} | {result.get('tool')} | {result.get('reason')} | "
            f"success={result.get('success')}\n"
            f"Args: {json.dumps(result.get('args') or {}, ensure_ascii=True)}\n"
            f"Summary: {result.get('summary')}\n"
            f"Content: {str(result.get('content') or '')[:1800]}"
        )
    return "\n\n".join(chunks)


def _available_econ_tool_text() -> str:
    return (
        "Available read-only econ tools: "
        "get_bond_market_dashboard, get_inflation_dashboard, get_us_macro_dashboard, "
        "get_equity_market_dashboard, get_global_macro_dashboard(countries,start_year,end_year), "
        "get_housing_consumer_dashboard, get_labor_market_dashboard, "
        "search_fred_series(query,limit), get_fred_series_observations(series_id,observation_start,observation_end,units,frequency,limit), "
        "search_imf_indicators(query,limit), get_imf_datamapper_series(indicator,countries,start_year,end_year), "
        "get_stock_snapshot(symbol), get_stock_price_history(symbol,outputsize,limit)."
    )


def _normalize_econ_requests(raw_requests, limit: int = ECON_REQUESTS_PER_ROUND) -> list[dict]:
    if not ENABLE_FINANCE:
        return []
    if not isinstance(raw_requests, list):
        return []

    normalized = []
    seen = set()
    for raw in raw_requests:
        if not isinstance(raw, dict):
            continue
        tool = str(raw.get("tool") or raw.get("name") or "").strip()
        if tool not in ECON_TOOL_FUNCTIONS:
            continue

        args = raw.get("args") or raw.get("arguments") or {}
        args = args if isinstance(args, dict) else {}
        allowed_args = ECON_TOOL_ALLOWED_ARGS.get(tool, set())
        clean_args = {
            str(key): value
            for key, value in args.items()
            if str(key) in allowed_args and value not in (None, "")
        }

        if "limit" in clean_args:
            try:
                clean_args["limit"] = max(1, min(int(clean_args["limit"]), 24))
            except (TypeError, ValueError):
                clean_args.pop("limit", None)
        for year_key in ("start_year", "end_year"):
            if year_key in clean_args:
                try:
                    clean_args[year_key] = int(clean_args[year_key])
                except (TypeError, ValueError):
                    clean_args.pop(year_key, None)

        request_key = json.dumps({"tool": tool, "args": clean_args}, sort_keys=True, ensure_ascii=True)
        if request_key in seen:
            continue
        seen.add(request_key)

        normalized.append({
            "tool": tool,
            "args": clean_args,
            "reason": str(raw.get("reason") or raw.get("purpose") or "Economic context").strip()[:240],
        })
        if len(normalized) >= limit:
            break
    return normalized


async def _summarize_econ_result(session: ExpertSession, tool: str, args: dict, content: str) -> str:
    prompt = (
        "Summarize this structured economic/financial tool result for a deep research dossier. "
        "Keep key data points, dates, levels, changes, caveats, and why it matters. "
        "Return concise prose, not JSON.\n\n"
        f"Research topic: {session.topic}\n"
        f"Tool: {tool}\n"
        f"Args: {json.dumps(args, ensure_ascii=True)}\n\n"
        f"Result:\n{content[:9000]}"
    )
    summary = await query_fast_model(prompt, "You summarize economic data for research synthesis.")
    return summary.strip() if summary else content[:1000]


async def _run_econ_requests(bot, session: ExpertSession, requests: list[dict]) -> None:
    normalized_requests = _normalize_econ_requests(requests)
    if not normalized_requests:
        return

    for request in normalized_requests:
        tool = request["tool"]
        args = request["args"]
        function = ECON_TOOL_FUNCTIONS[tool]
        try:
            raw_content = await function(**args)
            success = not str(raw_content or "").lower().startswith((
                "fred error",
                "imf error",
                "stock data error",
                "stock history error",
            )) and "failed" not in str(raw_content or "").lower()
        except Exception as exc:
            logging.error("EXPERT: Econ tool %s failed: %s", tool, exc, exc_info=True)
            raw_content = f"{tool} failed: {exc}"
            success = False

        content = str(raw_content or "").strip()
        summary = await _summarize_econ_result(session, tool, args, content) if content else ""
        result = {
            "id": f"E{len(session.econ_results) + 1}",
            "tool": tool,
            "args": args,
            "reason": request.get("reason") or "Economic context",
            "success": success,
            "content": content[:12000],
            "summary": summary,
            "created_at": _now_label(),
        }
        session.econ_results.append(result)
        _record_event(
            session,
            "econ_tool",
            f"Ran {tool} as {result['id']}: {result['reason']}",
            args=args,
            success=success,
        )
        await _send_expert_progress(
            bot,
            session,
            f"ran structured econ tool {result['id']}",
            f"<b>{telegram_escape(tool)}</b>: {telegram_escape(result['reason'])}",
            detail_is_html=True,
        )


async def _plan_next_round(session: ExpertSession) -> dict:
    econ_instruction = ""
    if ENABLE_FINANCE:
        econ_instruction = (
            " You may include econ_requests for read-only structured economic/financial tools if they would close a gap. "
            f"{_available_econ_tool_text()}"
        )

    prompt = (
        "Review the research state and decide the next step. Return strict JSON with keys: "
        "stop_now (boolean), next_queries (array), econ_requests (array), critical_questions (array). "
        "Each critical question may include prompt and options. Ask user questions only if their answer "
        f"materially changes the research path; otherwise self-branch.{econ_instruction}\n\n"
        f"Topic: {session.topic}\n"
        f"Completed rounds: {session.round}\n"
        f"Fetched source count: {_source_count(session)} target {session.target_sources}\n"
        f"Econ result count: {_econ_count(session)}\n"
        f"Recent user inputs: {json.dumps(session.user_inputs[-4:], ensure_ascii=True)}\n\n"
        f"Source digest:\n{_source_digest(session)}\n\n"
        f"Econ digest:\n{_econ_digest(session)}"
    )
    system = "You are Emery's expert research planner. Return only compact JSON."
    parsed = _extract_json_object(await query_fast_model(prompt, system)) or {}
    next_queries = parsed.get("next_queries") if isinstance(parsed.get("next_queries"), list) else []
    questions = parsed.get("critical_questions") if isinstance(parsed.get("critical_questions"), list) else []
    return {
        "stop_now": bool(parsed.get("stop_now")) and _source_count(session) >= max(8, session.target_sources // 2),
        "next_queries": [str(query).strip() for query in next_queries if str(query).strip()][:6],
        "econ_requests": _normalize_econ_requests(parsed.get("econ_requests")),
        "critical_questions": _normalize_questions(questions),
    }


def _normalize_questions(raw_questions: list) -> list[dict]:
    questions = []
    for index, raw in enumerate(raw_questions[:4], start=1):
        if not isinstance(raw, dict):
            continue
        prompt = str(raw.get("prompt") or raw.get("question") or "").strip()
        if not prompt:
            continue
        options = []
        for option_index, option in enumerate((raw.get("options") or [])[:4], start=1):
            if isinstance(option, dict):
                label = str(option.get("label") or option.get("text") or "").strip()
                description = str(option.get("description") or "").strip()
            else:
                label = str(option).strip()
                description = ""
            if label:
                options.append({
                    "id": f"o{option_index}",
                    "label": label[:48],
                    "description": description[:180],
                })
        questions.append(asdict(ExpertQuestion(id=f"q{index}", prompt=prompt, options=options)))
    return questions


async def _ask_pending_questions(bot, session: ExpertSession) -> None:
    lines = ["<b>Expert needs direction</b>", ""]
    for index, question in enumerate(session.pending_questions[:4], start=1):
        lines.append(f"{index}. {question.get('prompt')}")
        for option in question.get("options", [])[:4]:
            desc = f" - {option.get('description')}" if option.get("description") else ""
            lines.append(f"   - {option.get('label')}{desc}")
        lines.append("")
    lines.append("Tap options below, or type your answer in your own words.")
    await _send_status(bot, session, "\n".join(lines).strip(), reply_markup=_question_markup(session))


async def _fetch_round(bot, session: ExpertSession, queries: list[str]) -> None:
    seen = {source.get("normalized_url") for source in session.sources}
    fetches_this_round = 0

    for query in queries:
        if _source_count(session) >= session.max_sources or fetches_this_round >= FETCHES_PER_ROUND:
            break
        if query not in session.search_queries:
            session.search_queries.append(query)
        results = await _search_web(query)
        session.search_results.extend(results)
        _record_event(session, "search", f"Search query: {query}", results=len(results))

        for result in results:
            if _source_count(session) >= session.max_sources or fetches_this_round >= FETCHES_PER_ROUND:
                break
            normalized = result.get("normalized_url")
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            fetched = await fetch_web_content(result["url"], max_chars=12000, summarize_long=False)
            source = await _summarize_source(session, result, fetched)
            session.sources.append(source)
            fetches_this_round += 1
            label = source["id"] if source.get("fetch_success") else "failed"
            _record_event(
                session,
                "fetch",
                f"Fetched {label}: {source.get('title')}",
                url=source.get("url"),
                success=source.get("fetch_success"),
            )
            source_label = source["id"] if source.get("fetch_success") else f"failed fetch from {source.get('domain')}"
            await _send_expert_progress(
                bot,
                session,
                f"read source {source_label}",
                f"<b>{telegram_escape(source.get('domain') or 'unknown source')}</b>: {telegram_escape(source.get('title'))}",
                detail_is_html=True,
            )

    await _send_expert_progress(
        bot,
        session,
        f"completed research round {session.round}",
    )


async def _build_final_report(session: ExpertSession) -> str:
    prompt = (
        "Write the final /expert research report as clean Telegram-friendly Markdown. "
        "Use headings, bullets, numbered lists, compact tables only if they fit, and citations like [S12]. "
        "Include: executive summary, timeline, key actors, core findings, competing interpretations, "
        "uncertainty/confidence notes, and source appendix. Be explicit about source reliability and perspective.\n\n"
        f"User request: {session.topic}\n"
        f"User follow-up inputs: {json.dumps(session.user_inputs, ensure_ascii=True)}\n\n"
        f"Source notes:\n{_source_digest(session, limit=40)}\n\n"
        f"Structured econ/finance notes:\n{_econ_digest(session, limit=20)}"
    )
    system = (
        "You are Emery's senior research writer. Produce only the finished report in Markdown. "
        "Do not include hidden reasoning or process notes."
    )
    report = await _query_main_model(prompt, system)
    if report:
        return report

    lines = [
        f"# Expert Report: {session.title}",
        "",
        "## Executive Summary",
        "The expert research loop completed, but the final synthesis model did not return a report. Source notes are preserved below.",
        "",
        "## Source Appendix",
    ]
    for source in session.sources:
        lines.append(f"- [{source.get('id')}] {source.get('title')} - {source.get('url')}")
    return "\n".join(lines)


async def _deliver_final_report(bot, session: ExpertSession) -> None:
    fallback_html = emery_format(session.final_report)
    await send_rich_or_split_html_message(
        bot,
        session.chat_id,
        session.final_report,
        fallback_html_text=fallback_html,
        message_thread_id=session.message_thread_id,
    )
    await _send_expert_progress(
        bot,
        session,
        "research loop complete",
        "The full loop is still active. What should I do next?",
        reply_markup=_session_action_markup(session),
    )
    _schedule_normal_chat_warmup(session, "report complete")


async def _run_research_session(session: ExpertSession, bot) -> None:
    current_task = asyncio.current_task()
    try:
        ACTIVE_SESSIONS[session.key()] = session
        if session.round == 0 and not session.search_queries:
            plan = await _make_initial_plan(session)
            session.title = plan["title"]
            session.search_queries.extend(plan["search_queries"])
            _record_event(session, "plan", plan.get("framing") or "Initial research plan created.")
            if plan.get("econ_requests"):
                await _run_econ_requests(bot, session, plan["econ_requests"])
            await _send_expert_progress(
                bot,
                session,
                "started deep research",
                f"<b>{telegram_escape(session.title)}</b>\n"
                f"{telegram_escape(f'Targeting {session.target_sources}+ sources across multiple rounds')}"
                f"{' with structured econ tools enabled.' if ENABLE_FINANCE else '.'}",
                detail_is_html=True,
            )

        session.status = "running"
        while session.status == "running" and session.round < session.max_rounds and _source_count(session) < session.target_sources:
            session.round += 1
            session.touch()
            if session.followup_instruction:
                _record_event(session, "user_followup", session.followup_instruction)

            if session.round == 1:
                queries = session.search_queries[:6]
            else:
                next_plan = await _plan_next_round(session)
                if next_plan["critical_questions"]:
                    session.pending_questions = next_plan["critical_questions"]
                    session.pending_answers = {}
                    session.status = "waiting_for_answer"
                    _record_event(session, "pause", "Paused for critical user direction.")
                    await _ask_pending_questions(bot, session)
                    return
                if next_plan["stop_now"]:
                    break
                queries = next_plan["next_queries"] or [
                    f"{session.topic} latest analysis",
                    f"{session.topic} timeline actors",
                    f"{session.topic} regional sources",
                ]
                if next_plan.get("econ_requests"):
                    await _run_econ_requests(bot, session, next_plan["econ_requests"])

            if session.followup_instruction:
                queries = [f"{session.topic} {session.followup_instruction}", *queries]
                session.followup_instruction = ""

            await _send_expert_progress(
                bot,
                session,
                f"running research round {session.round}",
                "Searching, reading, and updating the research map.",
            )
            await _fetch_round(bot, session, queries[:6])

        session.final_report = await _build_final_report(session)
        session.final_report_versions.append({"time": _now_label(), "report": session.final_report})
        session.status = "completed_pending_user"
        _record_event(session, "complete", "Research loop completed and final report generated.")
        await _deliver_final_report(bot, session)
    except asyncio.CancelledError:
        session.status = "cancelled"
        _record_event(session, "cancelled", "Research task cancelled.")
        raise
    except Exception as exc:
        session.status = "error"
        _record_event(session, "error", f"Expert loop failed: {exc}")
        logging.error("EXPERT: Session %s failed: %s", session.id, exc, exc_info=True)
        await _send_expert_progress(bot, session, "failed", str(exc))
    finally:
        if SESSION_TASKS.get(session.id) is current_task:
            SESSION_TASKS.pop(session.id, None)


def _start_session_task(session: ExpertSession, bot) -> None:
    existing = SESSION_TASKS.get(session.id)
    if existing and not existing.done():
        existing.cancel()
    task = asyncio.create_task(_run_research_session(session, bot))
    SESSION_TASKS[session.id] = task


def _archive_session(session: ExpertSession) -> Path:
    archive_root = _archive_root()
    archive_root.mkdir(parents=True, exist_ok=True)
    folder_name = f"{datetime.now(USER_TIMEZONE).strftime('%Y%m%d-%H%M%S')}-{_slugify(session.title)}-{session.id}"
    folder = archive_root / folder_name
    folder.mkdir(parents=True, exist_ok=False)

    session.status = "archived"
    session.archive_path = str(folder)
    session.touch()

    (folder / "session.json").write_text(
        json.dumps(session.to_dict(), indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    (folder / "report.md").write_text(session.final_report or "", encoding="utf-8")
    (folder / "sources.json").write_text(
        json.dumps(session.sources, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    (folder / "econ_results.json").write_text(
        json.dumps(session.econ_results, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    (folder / "loop.md").write_text(_render_loop_markdown(session), encoding="utf-8")

    entries = [entry for entry in _load_index() if entry.get("id") != session.id]
    entries.insert(0, {
        "id": session.id,
        "title": session.title,
        "topic": session.topic,
        "created_at": session.created_at,
        "closed_at": _now_iso(),
        "source_count": _source_count(session),
        "econ_result_count": _econ_count(session),
        "archive_path": str(folder),
        "report_path": str(folder / "report.md"),
        "session_path": str(folder / "session.json"),
        "status": session.status,
    })
    _save_index(entries[:200])
    return folder


def _render_loop_markdown(session: ExpertSession) -> str:
    lines = [
        f"# Expert Loop: {session.title}",
        "",
        f"- Session ID: `{session.id}`",
        f"- Topic: {session.topic}",
        f"- Created: {session.created_at}",
        f"- Status: {session.status}",
        f"- Sources: {_source_count(session)}",
        f"- Econ results: {_econ_count(session)}",
        "",
        "## User Inputs",
    ]
    for item in session.user_inputs:
        lines.append(f"- {item.get('time')}: {item.get('text')}")

    lines.extend(["", "## Events"])
    for event in session.loop_events:
        lines.append(f"- {event.get('time')} [{event.get('type')}] {event.get('message')}")

    lines.extend(["", "## Search Queries"])
    for query in session.search_queries:
        lines.append(f"- {query}")

    lines.extend(["", "## Sources"])
    for source in session.sources:
        lines.append(f"### {source.get('id')}: {source.get('title')}")
        lines.append(f"- URL: {source.get('url')}")
        lines.append(f"- Reliability: {source.get('reliability_label')}")
        lines.append(f"- Perspective: {source.get('perspective')}")
        lines.append("")
        lines.append(str(source.get("summary") or ""))
        lines.append("")

    lines.extend(["", "## Structured Econ/Finance Results"])
    for result in session.econ_results:
        lines.append(f"### {result.get('id')}: {result.get('tool')}")
        lines.append(f"- Args: `{json.dumps(result.get('args') or {}, ensure_ascii=True)}`")
        lines.append(f"- Reason: {result.get('reason')}")
        lines.append(f"- Success: {result.get('success')}")
        lines.append("")
        lines.append(str(result.get("summary") or ""))
        lines.append("")
        if result.get("content"):
            lines.append("```text")
            lines.append(str(result.get("content") or "")[:12000])
            lines.append("```")
            lines.append("")

    lines.extend(["", "## Final Report", "", session.final_report or ""])
    return "\n".join(lines).strip() + "\n"


async def _close_and_archive(bot, session: ExpertSession) -> None:
    task = SESSION_TASKS.pop(session.id, None)
    if task and not task.done():
        task.cancel()
    folder = _archive_session(session)
    ACTIVE_SESSIONS.pop(session.key(), None)
    await _send_expert_progress(
        bot,
        session,
        "archived session",
        f"Saved to:\n<code>{telegram_escape(folder)}</code>",
        detail_is_html=True,
    )
    _schedule_normal_chat_warmup(session, "archive")


def _normalize_intent_label(text: str, allowed_labels: set[str], default: str) -> str:
    lowered = str(text or "").strip().lower()
    raw = re.sub(r"[^a-z_]", "", lowered)
    if raw in allowed_labels:
        return raw
    for label in sorted(allowed_labels, key=len, reverse=True):
        if re.search(rf"\b{re.escape(label)}\b", lowered):
            return label
    return default


async def _classify_expert_user_intent(session: ExpertSession, text: str) -> str:
    if session.status == "waiting_for_answer":
        allowed = {"close_archive", "cancel", "answer_question"}
        labels = "close_archive, cancel, answer_question"
        default = "answer_question"
        pending = json.dumps(session.pending_questions[:4], ensure_ascii=True)
        state_instruction = (
            "The expert agent is waiting for the user to answer one or more pending questions. "
            "Use close_archive only if the user wants to end, close, archive, finish, or move on from expert mode. "
            "Use cancel only if the user wants to stop/discard the active expert session. "
            "Otherwise use answer_question, including free-form answers that do not match the button options."
        )
    else:
        allowed = {"close_archive", "cancel", "continue_research", "refine_report", "normal_message"}
        labels = "close_archive, cancel, continue_research, refine_report, normal_message"
        default = "normal_message"
        pending = "[]"
        state_instruction = (
            "The expert research loop has completed and the user is deciding what to do next. "
            "Use close_archive if the user wants to end, close, archive, finish, wrap up, or move on from expert mode. "
            "Use continue_research if the user wants more research, more sources, a new branch, or deeper investigation. "
            "Use refine_report if the user asks to rewrite, shorten, expand, reorganize, polish, or otherwise alter the report. "
            "Use cancel if the user wants to discard/cancel the session. "
            "Use normal_message only if the message is not directing the expert session."
        )

    prompt = (
        "Classify this Telegram message for Emery's /expert mode. "
        f"Return exactly one label from: {labels}.\n\n"
        f"Session status: {session.status}\n"
        f"Session title: {session.title}\n"
        f"Pending questions JSON: {pending}\n\n"
        f"{state_instruction}\n\n"
        f"User message: {text}"
    )
    result = await query_fast_model(
        prompt,
        "You are a strict intent classifier for an active expert research session. Return one label only.",
    )
    return _normalize_intent_label(result, allowed, default)


async def _exit_waiting_session(bot, session: ExpertSession, action: str) -> bool:
    if action == "close_archive":
        if session.final_report:
            await _close_and_archive(bot, session)
        else:
            await _cancel_session_object(bot, session)
        return True
    if action == "cancel":
        await _cancel_session_object(bot, session)
        return True
    return False


async def _refine_report(bot, session: ExpertSession, instruction: str) -> None:
    await _send_expert_progress(bot, session, "refining final report", "Rewriting from the retained expert context.")
    prompt = (
        "Revise the existing expert report according to the user instruction. Preserve source citations and "
        "Telegram-friendly Markdown. Return only the revised report.\n\n"
        f"Instruction: {instruction}\n\n"
        f"Existing report:\n{session.final_report}\n\n"
        f"Source notes:\n{_source_digest(session, limit=40)}"
    )
    report = await _query_main_model(prompt, "You revise research reports. Return only Markdown.")
    if report:
        session.final_report = report
        session.final_report_versions.append({"time": _now_label(), "instruction": instruction, "report": report})
        _record_event(session, "refine", f"Final report refined: {instruction}")
    await _deliver_final_report(bot, session)


async def handle_expert_command(update, context) -> None:
    if not update.message or not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    thread_id = normalize_message_thread_id(chat_id, update.message.message_thread_id)
    globals.TARGET_CHAT_ID.set(chat_id)
    globals.CURRENT_THREAD_ID.set(thread_id)
    globals.current_user_id.set(update.effective_user.id if update.effective_user else None)

    args = list(getattr(context, "args", []) or [])
    if not args:
        await update.message.reply_text("Usage: /expert <research topic>, /expert list, /expert status, /expert resume <id>, /expert open <id>, or /expert cancel.")
        return

    subcommand = args[0].lower()
    if subcommand == "list":
        await _send_expert_list(update, context)
        return
    if subcommand == "status":
        await _send_expert_status(update, context)
        return
    if subcommand == "cancel":
        await _cancel_active_session(update, context)
        return
    if subcommand == "resume" and len(args) >= 2:
        await _resume_archived_session(update, context, args[1])
        return
    if subcommand == "open" and len(args) >= 2:
        await _open_archived_report(update, context, args[1])
        return

    key = _session_key(chat_id, thread_id)
    if key in ACTIVE_SESSIONS and ACTIVE_SESSIONS[key].status in {"running", "waiting_for_answer", "completed_pending_user"}:
        await update.message.reply_text("An expert session is already active here. Use /expert status, /expert cancel, or close/archive the current session first.")
        return

    topic = " ".join(args).strip()
    session = ExpertSession(
        id=_short_id(),
        title=topic[:80],
        topic=topic,
        chat_id=chat_id,
        message_thread_id=thread_id,
        user_id=update.effective_user.id if update.effective_user else None,
    )
    ACTIVE_SESSIONS[key] = session
    _start_session_task(session, context.bot)


async def handle_expert_message(update, context, content_text: str) -> bool:
    session = _active_session_for_update(update)
    if not session or session.status not in COMPLETED_WAITING_STATES:
        return False
    if not update.message:
        return False

    text = str(content_text or update.message.text or "").strip()
    if not text:
        return False

    bot = context.bot
    session.user_inputs.append({"time": _now_label(), "text": text, "state": session.status})

    if session.status == "waiting_for_answer":
        waiting_action = await _classify_expert_user_intent(session, text)
        if waiting_action in {"close_archive", "cancel"}:
            await _exit_waiting_session(bot, session, waiting_action)
            return True

        is_refine_answer = any(question.get("id") == "refine" for question in session.pending_questions)
        session.pending_answers["typed"] = text
        session.pending_questions = []
        session.pending_answers = {}
        _record_event(session, "answer", f"User answered expert question: {text}")
        if is_refine_answer:
            session.status = "completed_pending_user"
            await _refine_report(bot, session, text)
            return True
        session.followup_instruction = text
        session.status = "running"
        await _send_expert_progress(bot, session, "resuming with your direction", text)
        _start_session_task(session, bot)
        return True

    action = await _classify_expert_user_intent(session, text)
    if action == "close_archive":
        await _close_and_archive(bot, session)
        return True
    if action == "cancel":
        await _cancel_session_object(bot, session)
        return True
    if action == "continue_research":
        session.followup_instruction = text
        session.target_sources = min(session.max_sources, max(session.target_sources + 6, _source_count(session) + 6))
        session.max_rounds += 2
        session.status = "running"
        _record_event(session, "continue", f"User requested continued research: {text}")
        _start_session_task(session, bot)
        return True
    if action == "refine_report":
        await _refine_report(bot, session, text)
        return True
    return False


async def handle_expert_callback(update, context) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    parts = query.data.split(":")
    if len(parts) < 3 or parts[0] != CALLBACK_PREFIX:
        return
    session_id = parts[1]
    action = parts[2]

    session = _active_session_by_id(session_id)
    if not session and action in {"resume", "open"} and len(parts) >= 4:
        archive_id = parts[3]
        if action == "resume":
            await _resume_archived_session(update, context, archive_id)
        else:
            await _open_archived_report(update, context, archive_id)
        return
    if not session:
        await query.message.reply_text("That expert session is no longer active.")
        return

    if action == "noop":
        return

    if action == "cancel":
        await _cancel_session_object(context.bot, session)
        return

    if action in {"close", "continue", "refine"} and session.status != "completed_pending_user":
        await query.message.reply_text(f"That expert session is currently {session.status}. Use /expert status or /expert cancel.")
        return

    if action == "q" and session.status != "waiting_for_answer":
        await query.message.reply_text("That expert question is no longer active.")
        return

    if action == "close":
        await _close_and_archive(context.bot, session)
    elif action == "continue":
        session.status = "running"
        session.target_sources = min(session.max_sources, max(session.target_sources + 6, _source_count(session) + 6))
        session.max_rounds += 2
        _record_event(session, "continue", "User tapped Continue researching.")
        _start_session_task(session, context.bot)
    elif action == "refine":
        session.status = "waiting_for_answer"
        session.pending_questions = [asdict(ExpertQuestion(
            id="refine",
            prompt="How should I refine the final report?",
            options=[
                {"id": "o1", "label": "Shorter", "description": "Condense the report."},
                {"id": "o2", "label": "More detail", "description": "Expand analysis and source usage."},
                {"id": "o3", "label": "Sharper thesis", "description": "Make the argument more direct."},
            ],
        ))]
        await _ask_pending_questions(context.bot, session)
    elif action == "q" and len(parts) >= 5:
        qid, oid = parts[3], parts[4]
        _record_question_answer(session, qid, oid)
        if qid == "refine" and qid in session.pending_answers:
            instruction = session.pending_answers[qid]
            session.pending_questions = []
            session.pending_answers = {}
            session.status = "completed_pending_user"
            await _refine_report(context.bot, session, instruction)
            return
        unanswered = [
            q for q in session.pending_questions
            if q.get("id") not in session.pending_answers
        ]
        if unanswered:
            await query.message.reply_text("Recorded. Answer the remaining expert questions, or type a full response.")
        else:
            session.followup_instruction = "; ".join(session.pending_answers.values())
            session.pending_questions = []
            session.pending_answers = {}
            session.status = "running"
            await _send_expert_progress(context.bot, session, "resuming with selected options")
            _start_session_task(session, context.bot)


def _record_question_answer(session: ExpertSession, qid: str, oid: str) -> None:
    for question in session.pending_questions:
        if question.get("id") != qid:
            continue
        for option in question.get("options", []):
            if option.get("id") == oid:
                answer = f"{question.get('prompt')}: {option.get('label')}"
                session.pending_answers[qid] = answer
                session.user_inputs.append({"time": _now_label(), "text": answer, "state": "waiting_for_answer"})
                _record_event(session, "answer", f"User selected: {answer}")
                return


async def _cancel_session_object(bot, session: ExpertSession) -> None:
    task = SESSION_TASKS.pop(session.id, None)
    if task and not task.done():
        task.cancel()
    session.status = "cancelled"
    _record_event(session, "cancelled", "User cancelled expert session.")
    ACTIVE_SESSIONS.pop(session.key(), None)
    await _send_expert_progress(bot, session, "cancelled session")
    _schedule_normal_chat_warmup(session, "cancel")


async def _cancel_active_session(update, context) -> None:
    session = _active_session_for_update(update)
    if not session:
        await update.message.reply_text("No active expert session in this chat/thread.")
        return
    await _cancel_session_object(context.bot, session)


async def _send_expert_status(update, context) -> None:
    session = _active_session_for_update(update)
    if not session:
        await update.message.reply_text("No active expert session in this chat/thread.")
        return
    await update.message.reply_text(
        f"Expert session {session.id}: {session.status}\n"
        f"Title: {session.title}\n"
        f"Round: {session.round}/{session.max_rounds}\n"
        f"Sources: {_source_count(session)}/{session.target_sources}\n"
        f"Econ results: {_econ_count(session)}"
    )


async def _send_expert_list(update, context) -> None:
    entries = _load_index()[:10]
    if not entries:
        await update.message.reply_text("No archived expert sessions yet.")
        return

    lines = ["Archived expert sessions:"]
    rows = []
    for entry in entries:
        session_id = entry.get("id", "")
        title = entry.get("title", "Untitled")
        econ_count = entry.get("econ_result_count", 0)
        econ_label = f", {econ_count} econ" if econ_count else ""
        lines.append(f"- {session_id}: {title} ({entry.get('source_count', 0)} sources{econ_label})")
        rows.append([
            InlineKeyboardButton(f"Resume {session_id}", callback_data=_callback("resume", "archive", session_id)),
            InlineKeyboardButton(f"Open {session_id}", callback_data=_callback("open", "archive", session_id)),
        ])
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))


def _find_index_entry(session_id: str) -> dict | None:
    for entry in _load_index():
        if str(entry.get("id")) == str(session_id):
            return entry
    return None


async def _resume_archived_session(update, context, session_id: str) -> None:
    entry = _find_index_entry(session_id)
    target_message = update.message or (update.callback_query.message if update.callback_query else None)
    if not entry:
        if target_message:
            await target_message.reply_text(f"No archived expert session found for ID {session_id}.")
        return
    try:
        data = json.loads(Path(entry["session_path"]).read_text(encoding="utf-8"))
        session = ExpertSession.from_dict(data)
    except Exception as exc:
        if target_message:
            await target_message.reply_text(f"Unable to load archived session {session_id}: {exc}")
        return

    chat_id = update.effective_chat.id
    thread_id = normalize_message_thread_id(chat_id, getattr(target_message, "message_thread_id", None))
    active = ACTIVE_SESSIONS.get(_session_key(chat_id, thread_id))
    if active and active.status in {"running", "waiting_for_answer", "completed_pending_user"}:
        if target_message:
            await target_message.reply_text(
                "An expert session is already active in this chat/thread. Close, archive, or cancel it before resuming another one."
            )
        return

    session.chat_id = chat_id
    session.message_thread_id = thread_id
    session.status = "completed_pending_user"
    session.pending_questions = []
    session.pending_answers = {}
    session.followup_instruction = ""
    _record_event(session, "resume", "Archived session resumed into active expert mode.")
    ACTIVE_SESSIONS[session.key()] = session
    if target_message:
        await target_message.reply_text(
            f"Resumed expert session {session.id}: {session.title}\nWhat should I do next?",
            reply_markup=_session_action_markup(session),
        )


async def _open_archived_report(update, context, session_id: str) -> None:
    entry = _find_index_entry(session_id)
    target_message = update.message or (update.callback_query.message if update.callback_query else None)
    if not entry:
        if target_message:
            await target_message.reply_text(f"No archived expert session found for ID {session_id}.")
        return
    try:
        report = Path(entry["report_path"]).read_text(encoding="utf-8")
    except Exception as exc:
        if target_message:
            await target_message.reply_text(f"Unable to open report for {session_id}: {exc}")
        return

    chat_id = update.effective_chat.id
    thread_id = normalize_message_thread_id(chat_id, getattr(target_message, "message_thread_id", None))
    await send_rich_or_split_html_message(
        context.bot,
        chat_id,
        report,
        fallback_html_text=emery_format(report),
        message_thread_id=thread_id,
    )
