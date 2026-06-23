import asyncio
import contextlib
import html
import json
import logging
import os
import re
import shutil
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters
from telegram.error import BadRequest

from emery.config import (
    ENABLE_FINANCE,
    ENABLE_YOUTUBE_TRANSCRIPT,
    EXPERT_ALLOW_MIDLOOP_QUESTIONS,
    EXPERT_ARCHIVE_DIR,
    EXPERT_DEFAULT_TARGET_SOURCES,
    EXPERT_FAST_ENABLE_THINKING,
    EXPERT_FAST_MAX_TOKENS,
    EXPERT_FAST_MIN_P,
    EXPERT_FAST_PRESENCE_PENALTY,
    EXPERT_FAST_REPETITION_PENALTY,
    EXPERT_FAST_TEMPERATURE,
    EXPERT_FAST_TOP_K,
    EXPERT_FAST_TOP_P,
    EXPERT_INDEX_PATH,
    EXPERT_MAIN_ENABLE_THINKING,
    EXPERT_MAIN_MAX_TOKENS,
    EXPERT_MAIN_MIN_P,
    EXPERT_MAIN_PRESENCE_PENALTY,
    EXPERT_MAIN_REPETITION_PENALTY,
    EXPERT_MAIN_TEMPERATURE,
    EXPERT_MAIN_TOP_K,
    EXPERT_MAIN_TOP_P,
    EXPERT_MAX_AGENDA_QUESTIONS,
    EXPERT_MAX_NEW_QUESTIONS,
    EXPERT_MAX_SOURCES,
    EXPERT_MAX_SUBTASKS_PER_QUESTION,
    EXPERT_MIN_TARGET_SOURCES,
    MAIN_MODEL_URL,
    MODEL_ID,
    MODEL_NAME,
    SEARXNG_URL,
    USER_TIMEZONE,
)
from emery.helpers import clean_thinking_tags, emery_format, query_fast_model, telegram_escape
from emery.logging_utils import format_logging_payload, safe_preview
from emery.telegram_delivery import send_rich_or_split_html_message
from emery.telegram_utils import normalize_message_thread_id
from emery.tools import (
    fetch_web_content,
    get_youtube_transcript,
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
DEFAULT_TARGET_SOURCES = EXPERT_DEFAULT_TARGET_SOURCES
DEFAULT_MAX_SOURCES = EXPERT_MAX_SOURCES
DEFAULT_MAX_ROUNDS = 6
FETCHES_PER_ROUND = 6
SEARCH_RESULTS_PER_QUERY = 8
ECON_REQUESTS_PER_ROUND = 3
AGENDA_PRIORITY_ORDER = {"core": 0, "supporting": 1, "optional": 2}
EVALUATION_STATUSES = {"answered", "needs_more", "sufficient_with_gaps", "exhausted"}

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

ECON_TOOL_LABELS = {
    "search_fred_series": "FRED series search",
    "get_fred_series_observations": "FRED observations",
    "search_imf_indicators": "IMF indicator search",
    "get_imf_datamapper_series": "IMF DataMapper series",
    "get_stock_snapshot": "stock snapshot",
    "get_stock_price_history": "stock price history",
    "get_bond_market_dashboard": "bond market dashboard",
    "get_inflation_dashboard": "inflation dashboard",
    "get_us_macro_dashboard": "U.S. macro dashboard",
    "get_equity_market_dashboard": "equity market dashboard",
    "get_global_macro_dashboard": "global macro dashboard",
    "get_housing_consumer_dashboard": "housing and consumer dashboard",
    "get_labor_market_dashboard": "labor market dashboard",
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
    research_agenda: list[dict] = field(default_factory=list)
    research_packets: list[dict] = field(default_factory=list)
    new_questions_added: int = 0
    final_report: str = ""
    final_report_versions: list[dict] = field(default_factory=list)
    archive_path: str = ""
    followup_instruction: str = ""
    source_milestone_notice_count: int = 0
    archive_label: str = ""

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


def _is_youtube_url(url: str) -> bool:
    try:
        host = (urlparse(str(url or "").strip()).hostname or "").lower()
    except Exception:
        return False
    return host in {"youtu.be", "youtube.com", "www.youtube.com", "m.youtube.com"} or host.endswith(".youtube.com")


def _source_count(session: ExpertSession) -> int:
    return len([src for src in session.sources if src.get("fetch_success")])


def _econ_count(session: ExpertSession) -> int:
    return len([result for result in session.econ_results if result.get("success")])


def _source_target_bounds() -> tuple[int, int, int]:
    minimum = max(1, int(EXPERT_MIN_TARGET_SOURCES or 1))
    maximum = max(minimum, int(EXPERT_MAX_SOURCES or minimum))
    default = min(max(int(EXPERT_DEFAULT_TARGET_SOURCES or minimum), minimum), maximum)
    return minimum, default, maximum


def _normalize_source_target(raw_value) -> int:
    minimum, default, maximum = _source_target_bounds()
    try:
        target = int(raw_value)
    except (TypeError, ValueError):
        target = default
    return min(max(target, minimum), maximum)


def _round_budget_for_target(target_sources: int) -> int:
    per_round = max(1, FETCHES_PER_ROUND)
    return max(DEFAULT_MAX_ROUNDS, ((int(target_sources) + per_round - 1) // per_round) + 2)


def _normalize_priority(value: str) -> str:
    priority = str(value or "").strip().lower()
    return priority if priority in AGENDA_PRIORITY_ORDER else "supporting"


def _agenda_question_id(index: int) -> str:
    return f"Q{index}"


def _normalize_agenda_questions(raw_questions, *, existing_count: int = 0, limit: int | None = None) -> list[dict]:
    if not isinstance(raw_questions, list):
        return []
    limit = max(0, int(limit if limit is not None else EXPERT_MAX_AGENDA_QUESTIONS))
    normalized = []
    seen = set()
    for raw in raw_questions:
        if len(normalized) >= limit:
            break
        if isinstance(raw, str):
            question = raw.strip()
            priority = "supporting"
            why = ""
        elif isinstance(raw, dict):
            question = str(raw.get("question") or raw.get("prompt") or raw.get("research_question") or "").strip()
            priority = _normalize_priority(raw.get("priority"))
            why = str(raw.get("why") or raw.get("why_it_matters") or raw.get("rationale") or "").strip()[:500]
        else:
            continue
        question = re.sub(r"\s+", " ", question)
        if not question:
            continue
        dedupe_key = question.lower().rstrip("?")
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        item_id = _agenda_question_id(existing_count + len(normalized) + 1)
        normalized.append({
            "id": item_id,
            "question": question[:500],
            "priority": priority,
            "why": why,
            "status": "pending",
            "attempts": 0,
            "created_at": _now_label(),
            "answered_at": "",
            "answer_summary": "",
            "gap_summary": "",
            "confidence": "",
            "source_ids": [],
            "econ_result_ids": [],
            "research_need_level": 0,
        })
    return normalized


def _agenda_digest(session: ExpertSession) -> str:
    if not session.research_agenda:
        return "No agenda yet."
    lines = []
    for item in session.research_agenda:
        lines.append(
            f"{item.get('id')} | {item.get('priority')} | {item.get('status')} | attempts={item.get('attempts', 0)}\n"
            f"Question: {item.get('question')}\n"
            f"Answer: {item.get('answer_summary') or 'Not answered yet.'}\n"
            f"Gaps: {item.get('gap_summary') or 'No material gaps recorded.'}"
        )
    return "\n\n".join(lines)


def _research_packet_digest(session: ExpertSession, limit: int = 8) -> str:
    packets = session.research_packets[-limit:]
    if not packets:
        return "No research packets yet."
    chunks = []
    for packet in packets:
        chunks.append(
            f"{packet.get('id')} | {packet.get('question_id')} | {packet.get('question')}\n"
            f"Sources: {', '.join(packet.get('source_ids') or []) or 'none'} | "
            f"Structured tool results: {', '.join(packet.get('econ_result_ids') or []) or 'none'}\n"
            f"Summary: {packet.get('summary')}\n"
            f"Gaps: {'; '.join(str(gap) for gap in (packet.get('gaps') or [])[:4])}"
        )
    return "\n\n".join(chunks)


def _select_next_agenda_question(session: ExpertSession) -> dict | None:
    candidates = [
        item for item in session.research_agenda
        if item.get("status") in {"pending", "needs_more"}
        and int(item.get("attempts") or 0) < max(1, EXPERT_MAX_SUBTASKS_PER_QUESTION)
        and int(item.get("research_need_level") or 0) != 2
    ]
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda item: (
            AGENDA_PRIORITY_ORDER.get(item.get("priority"), 1),
            int(item.get("research_need_level") or 0),
            int(item.get("attempts") or 0),
            str(item.get("id") or ""),
        ),
    )[0]


def _agenda_has_open_core_questions(session: ExpertSession) -> bool:
    return any(
        item.get("priority") == "core"
        and item.get("status") in {"pending", "needs_more", "in_progress"}
        and int(item.get("attempts") or 0) < max(1, EXPERT_MAX_SUBTASKS_PER_QUESTION)
        and int(item.get("research_need_level") or 0) != 2
        for item in session.research_agenda
    )


def _normalize_evaluation_status(evaluation: dict, question: dict) -> str:
    raw_status = str(evaluation.get("status") or "").strip().lower()
    if raw_status in EVALUATION_STATUSES:
        status = raw_status
    elif bool(evaluation.get("answered")):
        status = "answered"
    else:
        status = "needs_more"

    if status == "needs_more" and int(question.get("attempts") or 0) >= max(1, EXPERT_MAX_SUBTASKS_PER_QUESTION):
        return "exhausted"
    return status



def _apply_agenda_coverage(session: ExpertSession, agenda_coverage: list, active_question_id: str) -> None:
    """Apply cross-question coverage updates from a research packet evaluation.

    Each entry in agenda_coverage must contain question_id, research_need_level,
    answer_summary, gap_summary, source_ids, and econ_result_ids.
    Malformed entries are silently skipped to degrade safely.
    """
    if not isinstance(agenda_coverage, list):
        return

    # Build a lookup of agenda items by ID for efficient matching
    agenda_by_id = {str(item.get("id") or ""): item for item in session.research_agenda}

    for entry in agenda_coverage:
        if not isinstance(entry, dict):
            continue
        qid = str(entry.get("question_id") or "").strip()
        if not qid:
            continue
        item = agenda_by_id.get(qid)
        if item is None:
            continue

        # research_need_level must be a valid integer in {0, 1, 2}
        rnl = entry.get("research_need_level")
        if not isinstance(rnl, int) or rnl not in (0, 1, 2):
            try:
                rnl = int(rnl)
                if rnl not in (0, 1, 2):
                    continue
            except (ValueError, TypeError):
                continue
        item["research_need_level"] = rnl

        # Refresh answer_summary when a newer value is supplied
        summary = str(entry.get("answer_summary") or "").strip()
        if summary:
            item["answer_summary"] = summary[:2000]

        # Refresh gap_summary when a newer value is supplied
        gap = str(entry.get("gap_summary") or "").strip()
        if gap:
            item["gap_summary"] = gap[:1000]

        # Merge source_ids without duplicating
        new_source_ids = entry.get("source_ids")
        if isinstance(new_source_ids, list):
            current = set(item.get("source_ids") or [])
            current.update(str(sid) for sid in new_source_ids if sid)
            item["source_ids"] = sorted(current)

        # Merge econ_result_ids without duplicating
        new_econ_ids = entry.get("econ_result_ids")
        if isinstance(new_econ_ids, list):
            current = set(item.get("econ_result_ids") or [])
            current.update(str(eid) for eid in new_econ_ids if eid)
            item["econ_result_ids"] = sorted(current)


def _apply_agenda_evaluation(session: ExpertSession, question: dict, packet: dict, evaluation: dict) -> list[dict]:
    status = _normalize_evaluation_status(evaluation, question)
    confidence = str(evaluation.get("confidence") or "").strip()[:80]
    answer_summary = str(evaluation.get("answer_summary") or packet.get("summary") or "").strip()[:2000]
    gap_summary = str(evaluation.get("gap_summary") or "").strip()[:1000]
    question["confidence"] = confidence
    question["answer_summary"] = answer_summary
    question["gap_summary"] = gap_summary
    question["source_ids"] = sorted(set((question.get("source_ids") or []) + (packet.get("source_ids") or [])))
    question["econ_result_ids"] = sorted(set((question.get("econ_result_ids") or []) + (packet.get("econ_result_ids") or [])))

    question["status"] = status
    if status in {"answered", "sufficient_with_gaps", "exhausted"}:
        question["answered_at"] = _now_label()

    additions_allowed = max(0, min(
        EXPERT_MAX_NEW_QUESTIONS - int(session.new_questions_added or 0),
        EXPERT_MAX_AGENDA_QUESTIONS - len(session.research_agenda),
    ))
    raw_new_questions = evaluation.get("new_questions") if isinstance(evaluation, dict) else []
    normalized_new = _normalize_agenda_questions(
        raw_new_questions,
        existing_count=len(session.research_agenda),
        limit=additions_allowed,
    )
    existing = {
        str(item.get("question") or "").lower().rstrip("?")
        for item in session.research_agenda
    }
    added_items = []
    for item in normalized_new:
        key = str(item.get("question") or "").lower().rstrip("?")
        if key in existing:
            continue
        session.research_agenda.append(item)
        session.new_questions_added += 1
        added_items.append(item)
        existing.add(key)
        _record_event(
            session,
            "agenda_add",
            f"{MODEL_NAME} added {item['id']}: {item['question']}",
            priority=item.get("priority"),
            why=item.get("why"),
        )

    # Step 4: Apply cross-question coverage updates from the evaluation
    _apply_agenda_coverage(
        session,
        evaluation.get("agenda_coverage", []) if isinstance(evaluation, dict) else [],
        question.get("id", ""),
    )

    return added_items


def _record_event(session: ExpertSession, event_type: str, message: str, **metadata) -> None:
    session.loop_events.append({
        "time": _now_label(),
        "type": event_type,
        "message": message,
        "metadata": metadata,
    })
    session.touch()
    logging.info("EXPERT MODE %s %s: %s", session.id, event_type.upper(), message)


def _model_display_name() -> str:
    return str(MODEL_NAME or "Emery").strip() or "Emery"


def _assistant_display_name() -> str:
    name = _model_display_name()
    suffix = "'" if name.endswith("s") else "'s"
    return f"{name}{suffix} Assistant"


def _model_possessive_name() -> str:
    name = _model_display_name()
    suffix = "'" if name.endswith("s") else "'s"
    return f"{name}{suffix}"


def _question_label(question_id: str | None) -> str:
    raw = str(question_id or "").strip()
    match = re.search(r"\d+", raw)
    return match.group(0) if match else raw


def _compact_notice_text(title: str, lines: list[str] | None = None, *, lines_are_html: bool = False, icon: str = "🧠") -> str:
    text_lines = [f"{telegram_escape(icon)} <b>{telegram_escape(title)}</b>"]
    for line in lines or []:
        clean = str(line or "").strip()
        if clean:
            text_lines.append(clean if lines_are_html else telegram_escape(clean))
    return "\n".join(text_lines)


def _source_total_line(session: ExpertSession) -> str:
    return f"📚 Sources Gathered: {_source_count(session)}"


def _question_line(question_id: str | None) -> str:
    label = _question_label(question_id)
    return f"🔎 Research Question: {label}" if label else ""


def _question_now_line(question_id: str | None) -> str:
    label = _question_label(question_id)
    return f"Now on 🔎 Research Question {label}" if label else ""


def _short_line(text: str, limit: int = 160) -> str:
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


def _agenda_lines(agenda: list[dict], *, html: bool = False) -> list[str]:
    lines = ["🔎 <b>Research Questions</b>" if html else "🔎 Research Questions:"]
    for item in agenda:
        label = _question_label(item.get("id")) or str(len(lines))
        question = re.sub(r"\s+", " ", str(item.get("question") or "")).strip()
        if not question:
            continue
        if html:
            lines.append(f"<b>{telegram_escape(label)}.</b> {telegram_escape(question)}")
        else:
            lines.append(f"{label}. {question}")
    return lines


async def _send_model_notice(
    bot,
    session: ExpertSession,
    title_suffix: str,
    lines: list[str] | None = None,
    *,
    reply_markup=None,
    lines_are_html: bool = False,
):
    logging.info(
        "EXPERT MODE %s NOTICE: %s%s",
        session.id,
        f"{_model_display_name()} {title_suffix}".strip(),
        f" | {safe_preview(' | '.join(str(line) for line in (lines or [])), max_len=180)}" if lines else "",
    )
    return await _send_status(
        bot,
        session,
        _compact_notice_text(f"{_model_display_name()} {title_suffix}".strip(), lines, lines_are_html=lines_are_html),
        reply_markup=reply_markup,
    )


async def _send_assistant_notice(
    bot,
    session: ExpertSession,
    title_suffix: str,
    lines: list[str] | None = None,
    *,
    lines_are_html: bool = False,
):
    logging.info(
        "EXPERT MODE %s NOTICE: %s%s",
        session.id,
        f"{_assistant_display_name()} {title_suffix}".strip(),
        f" | {safe_preview(' | '.join(str(line) for line in (lines or [])), max_len=180)}" if lines else "",
    )
    return await _send_status(
        bot,
        session,
        _compact_notice_text(f"{_assistant_display_name()} {title_suffix}".strip(), lines, lines_are_html=lines_are_html),
    )


async def _send_expert_started_notice(bot, session: ExpertSession) -> None:
    logging.info(
        "EXPERT MODE %s NOTICE: Expert Mode Activated! | %s and the Assistant have started researching...",
        session.id,
        _model_display_name(),
    )
    return await _send_status(
        bot,
        session,
        _compact_notice_text(
            "Expert Mode Activated!",
            [f"🧠 {_model_display_name()} and the Assistant have started researching..."],
            icon="🧐",
        ),
    )


async def _send_source_found_notice(bot, session: ExpertSession, source: dict, question_id: str | None) -> None:
    domain = source.get("domain") or _source_domain(source.get("url")) or "source"
    url = str(source.get("url") or "").strip()
    domain_line = (
        f'<a href="{html.escape(url, quote=True)}">{telegram_escape(domain)}</a>'
        if url
        else telegram_escape(domain)
    )
    await _send_assistant_notice(
        bot,
        session,
        "found a source!",
        [
            domain_line,
            _question_line(question_id),
            _source_total_line(session),
        ],
        lines_are_html=True,
    )


async def _maybe_send_source_milestone_notice(bot, session: ExpertSession) -> None:
    count = _source_count(session)
    if count < 10 or count % 10 != 0 or count <= int(session.source_milestone_notice_count or 0):
        return
    session.source_milestone_notice_count = count
    logging.info("EXPERT MODE %s NOTICE: %s hit %s sources", session.id, _assistant_display_name(), count)
    await _send_status(
        bot,
        session,
        _compact_notice_text(
            f"{_assistant_display_name()} is chugging coffee!",
            icon="☕️",
        ),
    )


async def _send_structured_context_notice(bot, session: ExpertSession, tool_label: str, question_id: str | None) -> None:
    await _send_model_notice(
        bot,
        session,
        "is getting background information",
        [
            tool_label,
            _question_line(question_id),
        ],
    )


async def _send_plan_ready_notice(bot, session: ExpertSession) -> None:
    await _send_model_notice(
        bot,
        session,
        "plan ready",
        [
            *_agenda_lines(session.research_agenda, html=True),
        ],
        lines_are_html=True,
    )


async def _send_question_added_notice(bot, session: ExpertSession, question: dict) -> None:
    label = _question_label(question.get("id"))
    await _send_model_notice(
        bot,
        session,
        "added a research question",
        [
            f"🔎 Research Question: {label}" if label else "🔎 Research Question",
            _short_line(question.get("question"), 220),
        ],
    )


async def _send_question_start_notice(bot, session: ExpertSession, question: dict) -> None:
    await _send_assistant_notice(
        bot,
        session,
        "is researching",
        [
            _question_now_line(question.get("id")),
            _source_total_line(session),
        ],
    )


async def _send_question_finished_notice(
    bot,
    session: ExpertSession,
    question: dict,
    next_question: dict | None = None,
) -> None:
    label = _question_label(question.get("id"))
    next_label = _question_label((next_question or {}).get("id"))
    lines = []
    if label:
        lines.append(f"✅ Submitting Research Question {label} report for review")
    else:
        lines.append("✅ Submitting research question report for review")
    if next_label:
        lines.append(f"➡ Moving onto Research Question {next_label} now")
    else:
        lines.append(f"➡ {_model_display_name()} is reviewing the final packet now")
    await _send_assistant_notice(bot, session, "finished a Research Question", lines)


async def _send_question_review_notice(bot, session: ExpertSession, question: dict, next_question: dict | None = None) -> None:
    label = _question_label(question.get("id"))
    next_label = _question_label((next_question or {}).get("id"))
    status = str(question.get("status") or "reviewed")

    if status == "needs_more":
        next_is_same_question = bool(label and next_label and label == next_label)
        line = (
            f"{_assistant_display_name()} is taking another pass ↩"
            if next_is_same_question
            else f"{_assistant_display_name()} will circle back if the remaining research budget allows ↩"
        )
        await _send_model_notice(
            bot,
            session,
            f"wants more information about Research Question {label}",
            [line],
        )
        return

    if status == "answered":
        lines = [f"Moving on to Research Question {next_label}..." if next_label else "All current research questions are resolved."]
        await _send_status(
            bot,
            session,
            _compact_notice_text(f"{_model_possessive_name()} Research Question {label} fully answered!", lines),
        )
        return

    if status == "sufficient_with_gaps":
        lines = ["Some details are still uncertain and will be noted in the report."]
        if next_label:
            lines.append(f"Moving on to Research Question {next_label}...")
        else:
            lines.append("Ready to synthesize with caveats.")
        await _send_model_notice(
            bot,
            session,
            f"has enough to continue from Research Question {label}",
            lines,
        )
        return

    if status == "exhausted":
        await _send_model_notice(
            bot,
            session,
            f"reached diminishing returns on Research Question {label}",
            ["Using the best available evidence in the final report."],
        )
        return

    await _send_model_notice(
        bot,
        session,
        f"reviewed Research Question {label}",
    )


def _synthesis_notice_lines(session: ExpertSession) -> list[str]:
    lines = ["Writing final report"]
    caveat_statuses = {"needs_more", "sufficient_with_gaps", "exhausted"}
    if any(item.get("status") in caveat_statuses for item in session.research_agenda):
        lines.append("Unresolved gaps will be included as uncertainty notes.")
    return lines


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


def _fallback_archive_label(title: str, topic: str = "") -> str:
    text = re.sub(r"\s+", " ", str(title or topic or "Research session")).strip()
    text = re.sub(r"^(research|investigate|analyze|explain|cover)\s+", "", text, flags=re.IGNORECASE).strip()
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'/-]*", text)
    return " ".join(words[:5]) or "Research session"


def _clean_archive_label(raw_label: str, *, title: str = "", topic: str = "") -> str:
    label = clean_thinking_tags(str(raw_label or "")).strip()
    label = re.sub(r"^```(?:json)?|```$", "", label, flags=re.IGNORECASE).strip()
    label = re.sub(r"^(label|topic)\s*:\s*", "", label, flags=re.IGNORECASE).strip()
    label = label.strip("\"'`*- \n\t")
    label = re.sub(r"\s+", " ", label)
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'/-]*", label)
    if not words:
        return _fallback_archive_label(title, topic)
    return " ".join(words[:5])


async def _generate_archive_label(title: str, topic: str) -> str:
    fallback = _fallback_archive_label(title, topic)
    prompt = (
        "Create a concise archive button label for this research session. "
        "Return only 2-5 words. No date, punctuation, quotes, markdown, or explanation.\n\n"
        f"Title: {title}\n"
        f"Original query: {topic}"
    )
    try:
        raw = await _query_expert_fast_model(
            prompt,
            f"You create short topic labels for {_model_display_name()} research archives. Return only the label.",
        )
    except Exception as exc:
        logging.warning("EXPERT: Archive label generation failed: %s", exc)
        return fallback
    return _clean_archive_label(raw, title=title, topic=topic)


async def _ensure_archive_label(session: ExpertSession) -> str:
    if session.archive_label:
        return session.archive_label
    session.archive_label = await _generate_archive_label(session.title, session.topic)
    session.touch()
    return session.archive_label


def _archive_date_label(created_at: str) -> str:
    try:
        dt = datetime.fromisoformat(str(created_at or "").replace("Z", "+00:00"))
        return f"{dt.strftime('%b')} {dt.day}, {dt.year}"
    except Exception:
        return "Unknown date"


def _archive_entry_topic_label(entry: dict) -> str:
    return str(entry.get("archive_label") or "").strip() or _fallback_archive_label(entry.get("title"), entry.get("topic"))


def _archive_entry_display_label(entry: dict) -> str:
    return f"{_archive_date_label(entry.get('created_at'))} · {_archive_entry_topic_label(entry)}"


def _telegram_button_text(text: str, limit: int = 64) -> str:
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


def _archive_root() -> Path:
    return Path(os.path.expanduser(EXPERT_ARCHIVE_DIR)).resolve()


def clear_expert_archives() -> dict:
    archive_root = _archive_root()
    existing_entries = _load_index()
    removed_items = 0

    if archive_root.exists():
        for child in archive_root.iterdir():
            try:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
                removed_items += 1
            except FileNotFoundError:
                continue
    archive_root.mkdir(parents=True, exist_ok=True)
    _save_index([])
    return {
        "archived_sessions": len(existing_entries),
        "removed_items": removed_items,
        "archive_dir": str(archive_root),
    }


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


async def _send_status(bot, session: ExpertSession, text: str, *, reply_markup=None, reply_to_message_id: int | None = None):
    reply_parameters = None
    if reply_to_message_id:
        reply_parameters = ReplyParameters(message_id=reply_to_message_id, allow_sending_without_reply=True)
    try:
        return await bot.send_message(
            chat_id=session.chat_id,
            text=text,
            parse_mode="HTML",
            message_thread_id=session.message_thread_id,
            reply_parameters=reply_parameters,
            reply_markup=reply_markup,
        )
    except BadRequest as exc:
        logging.warning("EXPERT: Telegram rejected status message: %s", exc)
    except Exception as exc:
        logging.error("EXPERT: Failed to send status message: %s", exc, exc_info=True)
    return None


async def _send_expert_typing_once(bot, session: ExpertSession) -> None:
    try:
        await bot.send_chat_action(
            chat_id=session.chat_id,
            action="typing",
            message_thread_id=session.message_thread_id,
        )
    except Exception as exc:
        logging.debug("EXPERT MODE %s: typing indicator failed: %s", session.id, exc)


async def _expert_typing_loop(bot, session: ExpertSession, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        await _send_expert_typing_once(bot, session)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=4)
        except asyncio.TimeoutError:
            continue


def _start_expert_typing(bot, session: ExpertSession) -> tuple[asyncio.Event, asyncio.Task]:
    stop_event = asyncio.Event()
    return stop_event, asyncio.create_task(_expert_typing_loop(bot, session, stop_event))


async def _stop_expert_typing(stop_event: asyncio.Event, task: asyncio.Task) -> None:
    stop_event.set()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


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
    logging.info(
        "EXPERT MODE: querying %s primary endpoint with max_tokens=%s temp=%s top_p=%s",
        MODEL_NAME,
        EXPERT_MAIN_MAX_TOKENS,
        EXPERT_MAIN_TEMPERATURE,
        EXPERT_MAIN_TOP_P,
    )
    payload = {
        "model": MODEL_ID,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "temperature": EXPERT_MAIN_TEMPERATURE,
        "top_p": EXPERT_MAIN_TOP_P,
        "top_k": EXPERT_MAIN_TOP_K,
        "min_p": EXPERT_MAIN_MIN_P,
        "presence_penalty": EXPERT_MAIN_PRESENCE_PENALTY,
        "repetition_penalty": EXPERT_MAIN_REPETITION_PENALTY,
        "max_tokens": EXPERT_MAIN_MAX_TOKENS,
        "chat_template_kwargs": {"enable_thinking": bool(EXPERT_MAIN_ENABLE_THINKING)},
    }
    try:
        async with globals.main_model_lock:
            response = await globals.http_client.post(MAIN_MODEL_URL, json=payload, timeout=900)
        if response.status_code != 200:
            logging.error("EXPERT: %s primary endpoint returned HTTP %s: %s", MODEL_NAME, response.status_code, safe_preview(response.text))
            return ""
        data = response.json()
        message = ((data.get("choices") or [{}])[0]).get("message", {})
        content = message.get("content") or ""
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return clean_thinking_tags(content).strip()
    except Exception as exc:
        logging.error("EXPERT: %s primary endpoint query failed: %s", MODEL_NAME, exc, exc_info=True)
        return ""


async def _query_expert_fast_model(prompt: str, system_prompt: str = None) -> str:
    logging.info(
        "EXPERT MODE: querying %s research endpoint with max_tokens=%s temp=%s top_p=%s",
        MODEL_NAME,
        EXPERT_FAST_MAX_TOKENS,
        EXPERT_FAST_TEMPERATURE,
        EXPERT_FAST_TOP_P,
    )
    return await query_fast_model(
        prompt,
        system_prompt=system_prompt,
        max_tokens=EXPERT_FAST_MAX_TOKENS,
        temperature=EXPERT_FAST_TEMPERATURE,
        top_p=EXPERT_FAST_TOP_P,
        top_k=EXPERT_FAST_TOP_K,
        min_p=EXPERT_FAST_MIN_P,
        presence_penalty=EXPERT_FAST_PRESENCE_PENALTY,
        repetition_penalty=EXPERT_FAST_REPETITION_PENALTY,
        enable_thinking=EXPERT_FAST_ENABLE_THINKING,
    )


async def _make_initial_plan(session: ExpertSession) -> dict:
    min_sources, default_sources, max_sources = _source_target_bounds()
    econ_instruction = ""
    if ENABLE_FINANCE:
        econ_instruction = (
            " Also include optional econ_requests when structured economic, market, trade, inflation, "
            "labor, GDP, rates, commodities-adjacent macro, country comparison, or stock context would materially improve the research. "
            f"{_available_econ_tool_text()}"
        )

    prompt = (
        "Create a deep research plan for this user request. Return strict JSON with keys: "
        "title, framing, target_source_count, agenda_questions, search_queries, source_targets, econ_requests. "
        "Choose target_source_count based on the topic's breadth and uncertainty rather than a fixed example count. "
        f"Use {default_sources} as the normal deep-research default, at least {min_sources} for narrow topics, "
        f"and up to {max_sources} for broad, fast-moving, or contested topics. "
        f"Include 4-8 agenda_questions, each with question, priority (core/supporting/optional), and why. "
        f"Keep the agenda bounded; maximum total agenda questions is {EXPERT_MAX_AGENDA_QUESTIONS}. "
        "Also include 3-6 seed search_queries for the first agenda item."
        f"{econ_instruction}\n\n"
        f"User request: {session.topic}"
    )
    system = (
        f"You are the lead research orchestrator for {_model_display_name()}'s /expert mode. "
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
    agenda_questions = _normalize_agenda_questions(
        parsed.get("agenda_questions") or parsed.get("research_questions"),
        limit=EXPERT_MAX_AGENDA_QUESTIONS,
    )
    if not agenda_questions:
        agenda_questions = _normalize_agenda_questions([
            {"question": f"What are the core facts and timeline for {session.topic}?", "priority": "core", "why": "Establishes the factual base."},
            {"question": f"Who are the key actors in {session.topic}, and what are their stated positions?", "priority": "core", "why": "Identifies actors and incentives."},
            {"question": f"What claims or interpretations about {session.topic} are disputed across sources?", "priority": "supporting", "why": "Surfaces uncertainty and disagreement."},
        ], limit=EXPERT_MAX_AGENDA_QUESTIONS)
    return {
        "title": str(parsed.get("title") or session.title),
        "framing": str(parsed.get("framing") or ""),
        "target_source_count": _normalize_source_target(
            parsed.get("target_source_count")
            or parsed.get("target_sources_count")
            or parsed.get("source_count")
        ),
        "agenda_questions": agenda_questions,
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
    parsed = _extract_json_object(await _query_expert_fast_model(prompt, system)) or {}
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


async def _fetch_expert_source_content(result: dict) -> dict:
    url = str(result.get("url") or "").strip()
    if ENABLE_YOUTUBE_TRANSCRIPT and _is_youtube_url(url):
        logging.info("🔧 EXPERT TOOL: get_youtube_transcript | Args: %s", format_logging_payload({"video_url_or_id": url}))
        transcript = await get_youtube_transcript(url, languages="en", include_timestamps=False)
        transcript_text = str(transcript or "").strip()
        if transcript_text and not transcript_text.lower().startswith("youtube transcript error"):
            return {
                "success": True,
                "title": result.get("title") or "YouTube transcript",
                "url": url,
                "content": transcript_text,
                "content_type": "youtube_transcript",
            }
        logging.info(
            "EXPERT: YouTube transcript unavailable for %s: %s",
            url,
            safe_preview(transcript_text, max_len=180),
        )
        return {
            "success": False,
            "title": result.get("title") or "YouTube video",
            "url": url,
            "error": transcript_text or "YouTube transcript unavailable.",
            "content": "",
            "content_type": "youtube_transcript",
        }

    logging.info("🔧 EXPERT TOOL: fetch_web_content | Args: %s", format_logging_payload({"url": url}))
    return await fetch_web_content(url, max_chars=12000, summarize_long=False)


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


def _source_items_digest(
    sources: list[dict],
    limit: int | None = None,
    include_failed: bool = True,
) -> str:
    """Return a human-readable digest of source summaries.

    Args:
        sources: list of source dicts (as stored in session.sources).
        limit: optional cap on number of items; ``None`` means no cap.
        include_failed: when ``False`` skip sources whose ``fetch_success`` is
            falsy — useful for the cumulative-evidence block in agenda scoring.
    """
    filtered = sources if include_failed else [s for s in sources if s.get("fetch_success")]
    items = filtered[-limit:] if limit is not None else filtered
    chunks = []
    for source in items:
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


def _econ_items_digest(
    results: list[dict],
    limit: int | None = None,
    include_failed: bool = True,
) -> str:
    """Return a human-readable digest of econ/finance tool results.

    Args:
        results: list of econ result dicts (as stored in session.econ_results).
        limit: optional cap on number of items; ``None`` means no cap.
        include_failed: when ``False`` skip results whose ``success`` is
            falsy — useful for the cumulative-evidence block in agenda scoring.
    """
    filtered = results if include_failed else [r for r in results if r.get("success")]
    items = filtered[-limit:] if limit is not None else filtered
    chunks = []
    for result in items:
        chunks.append(
            f"{result.get('id')} | {result.get('tool')} | {result.get('reason')} | "
            f"success={result.get('success')}\n"
            f"Summary: {result.get('summary')}\n"
            f"Content: {str(result.get('content') or '')[:1200]}"
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
    summary = await _query_expert_fast_model(prompt, "You summarize economic data for research synthesis.")
    return summary.strip() if summary else content[:1000]


async def _run_econ_requests(bot, session: ExpertSession, requests: list[dict], question_id: str | None = None) -> None:
    normalized_requests = _normalize_econ_requests(requests)
    if not normalized_requests:
        return

    for request in normalized_requests:
        tool = request["tool"]
        args = request["args"]
        function = ECON_TOOL_FUNCTIONS[tool]
        tool_label = ECON_TOOL_LABELS.get(tool, tool.replace("_", " "))
        logging.info("🔧 EXPERT TOOL: %s | Args: %s", tool, format_logging_payload(args))
        try:
            raw_content = await function(**args)
            success = not str(raw_content or "").lower().startswith((
                "fred error",
                "imf error",
                "stock data error",
                "stock history error",
            )) and "failed" not in str(raw_content or "").lower()
        except Exception as exc:
            logging.error("EXPERT: Structured data tool %s failed: %s", tool, exc, exc_info=True)
            raw_content = f"{tool} failed: {exc}"
            success = False

        content = str(raw_content or "").strip()
        summary = await _summarize_econ_result(session, tool, args, content) if content else ""
        result = {
            "id": f"E{len(session.econ_results) + 1}",
            "tool": tool,
            "args": args,
            "reason": request.get("reason") or "Structured data context",
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
        if success:
            await _send_structured_context_notice(bot, session, tool_label, question_id)


async def _plan_subagent_research(session: ExpertSession, question: dict) -> dict:
    econ_instruction = f" {_available_econ_tool_text()}" if ENABLE_FINANCE else ""
    attempted_queries = ", ".join(f'"{q}"' for q in session.search_queries[-15:]) if session.search_queries else "None"
    existing_domains = ", ".join(sorted(list(set(s.get("domain") for s in session.sources if s.get("domain"))))) if session.sources else "None"
    gap_summary = question.get("gap_summary") or "None recorded yet."
    research_need_level = int(question.get("research_need_level") or 0)

    system_prompt = (
        "You are a highly efficient research assistant that generates precise, keyword-based search query plans. "
        "Return only a compact JSON object. Avoid conversational filler."
    )

    if research_need_level == 1:
        # Reduced-pass: target only unresolved gaps, avoid already-covered broad angles
        prompt = (
            f"You are {_assistant_display_name()}. Plan a focused, gap-targeted research pass for the assigned question.\n"
            "Return strict JSON with keys: search_queries, econ_requests, focus.\n"
            "Do not decide the broader agenda. Generate 2-3 precise, narrow search queries that target ONLY the unresolved gaps.\n"
            "Use econ_requests only if structured read-only economic/financial data materially helps.\n"
            f"{econ_instruction}\n\n"
            "### Reduced-Pass Search Query Rules:\n"
            "1. Generate 2-3 distinct keyword search queries. Do NOT ask natural language questions.\n"
            "2. Target ONLY the specific unresolved gaps listed below. Do NOT re-search topics already covered by existing sources.\n"
            "3. Do NOT repeat search queries that have already been attempted.\n"
            "4. Avoid broad angles that have already been explored. Focus narrowly on what is missing.\n\n"
            f"User topic: {session.topic}\n"
            f"Previously Attempted Queries (Do NOT repeat): {attempted_queries}\n"
            f"Already Fetched Source Domains: {existing_domains}\n\n"
            f"Existing agenda:\n{_agenda_digest(session)}\n\n"
            f"Recent packets:\n{_research_packet_digest(session, limit=4)}\n\n"
            "### CURRENT ASSIGNED TASK (REDUCED PASS) ###\n"
            f"Assigned question: {question.get('id')} - {question.get('question')}\n"
            f"Why it matters: {question.get('why')}\n"
            f"Attempt: {int(question.get('attempts') or 0) + 1}/{EXPERT_MAX_SUBTASKS_PER_QUESTION}\n"
            f"Outstanding Gaps for this Question: {gap_summary}\n"
            f"NOTE: This is a reduced research pass. Only address gaps, do not re-search covered topics."
        )
        max_queries = 3
        fallback_queries = [
            f"{question.get('question')} {gap_summary[:60]}",
        ]
    else:
        # Full-pass (level 0): standard behavior
        prompt = (
            f"You are {_assistant_display_name()}. Plan one bounded research pass for the assigned question.\n"
            "Return strict JSON with keys: search_queries, econ_requests, focus.\n"
            "Do not decide the broader agenda. Generate 3-6 precise search queries that can answer this question.\n"
            "Use econ_requests only if structured read-only economic/financial data materially helps.\n"
            f"{econ_instruction}\n\n"
            "### Search Query Rules:\n"
            "1. Generate 3-6 distinct keyword search queries. Do NOT ask natural language questions (e.g., write 'company X CEO 2026' instead of 'Who is the CEO of company X in 2026?').\n"
            "2. Diversify search angles (e.g., use synonyms, target primary press releases, or search for reports/PDFs).\n"
            "3. Do NOT repeat search queries that have already been attempted.\n"
            "4. Target any outstanding gaps from prior attempts if applicable.\n\n"
            f"User topic: {session.topic}\n"
            f"Previously Attempted Queries (Do NOT repeat): {attempted_queries}\n"
            f"Already Fetched Source Domains: {existing_domains}\n\n"
            f"Existing agenda:\n{_agenda_digest(session)}\n\n"
            f"Recent packets:\n{_research_packet_digest(session, limit=4)}\n\n"
            "### CURRENT ASSIGNED TASK (DYNAMIC SUFFIX) ###\n"
            f"Assigned question: {question.get('id')} - {question.get('question')}\n"
            f"Why it matters: {question.get('why')}\n"
            f"Attempt: {int(question.get('attempts') or 0) + 1}/{EXPERT_MAX_SUBTASKS_PER_QUESTION}\n"
            f"Outstanding Gaps for this Question: {gap_summary}"
        )
        max_queries = 6
        fallback_queries = [
            f"{session.topic} {question.get('question')}",
            f"{question.get('question')} latest sources",
            f"{question.get('question')} timeline actors",
        ]

    parsed = _extract_json_object(await _query_expert_fast_model(
        prompt,
        system_prompt,
    )) or {}
    queries = parsed.get("search_queries") if isinstance(parsed.get("search_queries"), list) else []
    clean_queries = [str(query).strip() for query in queries if str(query).strip()][:max_queries]
    if not clean_queries:
        clean_queries = fallback_queries[:max_queries]
    return {
        "search_queries": clean_queries,
        "econ_requests": _normalize_econ_requests(parsed.get("econ_requests")),
        "focus": str(parsed.get("focus") or question.get("question") or "").strip()[:500],
    }


async def _summarize_research_packet(session: ExpertSession, question: dict, sources: list[dict], econ_results: list[dict]) -> dict:
    prompt = (
        f"Summarize this bounded research pass for {_model_display_name()}. Return strict JSON with keys: "
        "summary, key_findings, contradictions, gaps, confidence. Be concise and cite source IDs like [S3]. "
        "Do not decide the next agenda step.\n\n"
        f"User topic: {session.topic}\n"
        f"Question: {question.get('id')} - {question.get('question')}\n\n"
        f"New source notes:\n{_source_items_digest(sources) or 'No new sources.'}\n\n"
        f"New structured data notes:\n{_econ_items_digest(econ_results) or 'No new structured data results.'}"
    )
    parsed = _extract_json_object(await _query_expert_fast_model(
        prompt,
        f"You summarize bounded research packets for {MODEL_NAME}. Return only JSON.",
    )) or {}
    return {
        "summary": str(parsed.get("summary") or "No packet summary returned.").strip(),
        "key_findings": parsed.get("key_findings") if isinstance(parsed.get("key_findings"), list) else [],
        "contradictions": parsed.get("contradictions") if isinstance(parsed.get("contradictions"), list) else [],
        "gaps": parsed.get("gaps") if isinstance(parsed.get("gaps"), list) else [],
        "confidence": str(parsed.get("confidence") or "unlabeled").strip(),
    }


async def _run_research_subtask(bot, session: ExpertSession, question: dict) -> dict:
    question["status"] = "in_progress"
    question["attempts"] = int(question.get("attempts") or 0) + 1
    question_id = question.get("id") or "Q?"
    await _send_question_start_notice(bot, session, question)
    _record_event(
        session,
        "subtask_start",
        f"Started {question_id}: {question.get('question')}",
        attempts=question.get("attempts"),
    )

    plan = await _plan_subagent_research(session, question)
    source_start = len(session.sources)
    econ_start = len(session.econ_results)
    if plan.get("econ_requests"):
        await _run_econ_requests(bot, session, plan["econ_requests"], question_id)

    # Reduced fetch budget for level 1: cap at 3 new sources instead of full pass
    research_need_level = int(question.get("research_need_level") or 0)
    reduced_fetch = 3 if research_need_level == 1 else None
    await _fetch_round(bot, session, plan["search_queries"], question_id, max_fetches=reduced_fetch)
    new_sources = session.sources[source_start:]
    new_econ = session.econ_results[econ_start:]
    packet_notes = await _summarize_research_packet(session, question, new_sources, new_econ)
    packet = {
        "id": f"P{len(session.research_packets) + 1}",
        "question_id": question_id,
        "question": question.get("question"),
        "attempt": question.get("attempts"),
        "focus": plan.get("focus"),
        "search_queries": plan.get("search_queries") or [],
        "source_ids": [source.get("id") for source in new_sources if source.get("id")],
        "econ_result_ids": [result.get("id") for result in new_econ if result.get("id")],
        "created_at": _now_label(),
        **packet_notes,
    }
    session.research_packets.append(packet)
    _record_event(
        session,
        "research_packet",
        f"Completed {packet['id']} for {question_id}: {packet.get('summary')[:240]}",
        source_ids=packet["source_ids"],
        econ_result_ids=packet["econ_result_ids"],
    )
    return packet


async def _evaluate_research_packet(session: ExpertSession, question: dict, packet: dict) -> dict:
    remaining_new = max(0, EXPERT_MAX_NEW_QUESTIONS - int(session.new_questions_added or 0))
    remaining_total = max(0, EXPERT_MAX_AGENDA_QUESTIONS - len(session.research_agenda))
    question_instruction = (
        "Mid-loop user questions are disabled. Return critical_questions as an empty array and make the best research assumption yourself."
        if not EXPERT_ALLOW_MIDLOOP_QUESTIONS
        else "You may include critical_questions only when a user preference would materially change the research path."
    )
    prompt = (
        f"You are {_model_display_name()}. Evaluate whether {_assistant_display_name()} answered the assigned question.\n"
        "Return strict JSON with keys: status, confidence, answer_summary, gap_summary, new_questions, critical_questions, stop_now, agenda_coverage.\n"
        "status must be exactly one of: answered, needs_more, sufficient_with_gaps, exhausted.\n"
        "Use answered when the question is substantively answered with no material unresolved gaps.\n"
        "Use needs_more only when an important gap remains and another assistant pass is worthwhile.\n"
        "Use sufficient_with_gaps when the answer is good enough to continue or synthesize, but uncertainties should be noted in the report.\n"
        "Use exhausted when more research is unlikely to improve the answer or the available evidence is too limited.\n"
        "You own the agenda. Add new_questions only if new information materially changes the final answer.\n"
        "Do not add rabbit holes or background-only questions. Each new question must include question, priority "
        "(core/supporting/optional), and why. Respect the remaining budgets.\n"
        f"{question_instruction}\n\n"
        "### FULL AGENDA COVERAGE ###\n"
        "Score every question in the agenda based on all sources gathered so far (including this packet).\n"
        "Return agenda_coverage as a JSON array where each entry has: question_id (string), research_need_level (int), answer_summary (string), gap_summary (string), source_ids (array of strings), econ_result_ids (array of strings).\n"
        "research_need_level must be exactly 0, 1, or 2:\n"
        "- 0: no relevant sources gathered so far; run the full research loop\n"
        "- 1: current sources partially answer the question; run a reduced research loop targeting only unresolved gaps\n"
        "- 2: current sources fully answer the question; skip further research for this question\n"
        "For level 2 entries, you may omit answer_summary, gap_summary, and IDs.\n"
        "Only include agenda items that exist in the current agenda. Do not invent new question_ids.\n\n"
        f"User topic: {session.topic}\n\n"
        f"Existing agenda & answered status:\n{_agenda_digest(session)}\n\n"
        "### CUMULATIVE EVIDENCE GATHERED SO FAR ###\n"
        "Use the full evidence inventory below to score every agenda question. Do not base coverage decisions on only the latest packet.\n"
        f"Total sources fetched: {_source_count(session)}\n"
        f"Total structured data results: {len(session.econ_results)}\n\n"
        "All source summaries (titles, summaries, key claims):\n"
        # Bounded to most-recent successful evidence; prevents prompt bloat on large sessions
        f"{_source_items_digest(session.sources, limit=20, include_failed=False)}\n\n"
        "All structured data summaries:\n"
        f"{_econ_items_digest(session.econ_results, limit=10, include_failed=False)}\n\n"
        "### CURRENT TASK FOR EVALUATION (DYNAMIC SUFFIX) ###\n"
        f"Remaining new-question budget: {remaining_new}\n"
        f"Remaining total agenda slots: {remaining_total}\n"
        f"Assigned question: {question.get('id')} - {question.get('question')}\n"
        f"Question priority: {question.get('priority')}\n"
        f"Attempt: {question.get('attempts')}/{EXPERT_MAX_SUBTASKS_PER_QUESTION}\n\n"
        f"Research packet to evaluate:\n{json.dumps(packet, ensure_ascii=True, indent=2)}"
    )
    parsed = _extract_json_object(await _query_main_model(
        prompt,
        f"You are {MODEL_NAME}. Return only compact JSON.",
    )) or {}
    status = str(parsed.get("status") or "").strip().lower()
    if status not in EVALUATION_STATUSES:
        status = "answered" if bool(parsed.get("answered")) else "needs_more"
    return {
        "status": status,
        "answered": bool(parsed.get("answered")),
        "confidence": str(parsed.get("confidence") or packet.get("confidence") or "").strip(),
        "answer_summary": str(parsed.get("answer_summary") or packet.get("summary") or "").strip(),
        "gap_summary": str(parsed.get("gap_summary") or "").strip(),
        "new_questions": parsed.get("new_questions") if isinstance(parsed.get("new_questions"), list) else [],
        "critical_questions": _normalize_questions(parsed.get("critical_questions") if isinstance(parsed.get("critical_questions"), list) else []),
        "stop_now": bool(parsed.get("stop_now")),
        "agenda_coverage": parsed.get("agenda_coverage") if isinstance(parsed.get("agenda_coverage"), list) else [],
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


def _format_direction_prompt(prompt: str) -> str:
    text = re.sub(r"\s+", " ", str(prompt or "")).strip()
    if not text:
        return "Which direction should I take next?"
    if text.endswith("?"):
        return text

    cleaned = text.rstrip(".:; ")
    lowered = cleaned.lower()
    imperative_prefixes = (
        "provide ",
        "summarize ",
        "include ",
        "focus ",
        "compare ",
        "analyze ",
        "investigate ",
        "research ",
        "explain ",
        "cover ",
        "add ",
        "write ",
        "detail ",
    )
    if lowered.startswith(imperative_prefixes):
        return f"Should I {cleaned[:1].lower()}{cleaned[1:]} now, or keep researching?"
    return f"Should I use this direction for the next research branch: {cleaned}?"


def _questions_for_midloop_pause(session: ExpertSession, questions: list[dict]) -> list[dict]:
    if not questions:
        return []
    if EXPERT_ALLOW_MIDLOOP_QUESTIONS:
        return questions

    _record_event(
        session,
        "self_branch",
        f"{MODEL_NAME} proposed a mid-loop user question, but mid-loop questions are disabled; continuing autonomously.",
        questions=questions[:4],
    )
    return []


async def _ask_pending_questions(bot, session: ExpertSession) -> None:
    lines = [
        f"🧠 <b>{telegram_escape(_model_display_name())} needs direction</b>",
        "I found a choice that could change the next research branch.",
        "",
    ]
    for index, question in enumerate(session.pending_questions[:4], start=1):
        lines.append(f"<b>{index}. {telegram_escape(_format_direction_prompt(question.get('prompt')))}</b>")
        for option in question.get("options", [])[:4]:
            desc = f" - {option.get('description')}" if option.get("description") else ""
            lines.append(f"   • {telegram_escape(option.get('label'))}{telegram_escape(desc)}")
        lines.append("")
    lines.append("Tap an option, or reply in your own words.")
    await _send_status(bot, session, "\n".join(lines).strip(), reply_markup=_question_markup(session))


async def _fetch_round(bot, session: ExpertSession, queries: list[str], question_id: str | None = None, *, max_fetches: int | None = None) -> None:
    seen = {source.get("normalized_url") for source in session.sources}
    fetches_this_round = 0

    # 1. Parallelize web searches for all queries
    search_tasks = []
    queries_to_run = []
    for query in queries:
        if _source_count(session) >= session.max_sources:
            break
        if query not in session.search_queries:
            session.search_queries.append(query)
        queries_to_run.append(query)
        logging.info("🔧 EXPERT TOOL: web_search | Args: %s", format_logging_payload({"query": query}))
        search_tasks.append(_search_web(query))

    if search_tasks:
        search_results_list = await asyncio.gather(*search_tasks)
    else:
        search_results_list = []

    # 2. Record search events and collect all results
    all_results = []
    for query, results in zip(queries_to_run, search_results_list):
        session.search_results.extend(results)
        _record_event(session, "search", f"Search query: {query}", results=len(results))
        all_results.extend(results)

    # 3. Filter unique, unseen targets up to limits
    targets = []
    if max_fetches is not None:
        max_to_fetch = min(max_fetches, session.max_sources - _source_count(session), FETCHES_PER_ROUND - fetches_this_round)
    else:
        max_to_fetch = min(session.max_sources - _source_count(session), FETCHES_PER_ROUND - fetches_this_round)
    for result in all_results:
        if len(targets) >= max_to_fetch:
            break
        normalized = result.get("normalized_url")
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        targets.append(result)

    # 4. Fetch and summarize targets concurrently (bounded by a semaphore)
    if targets:
        llm_semaphore = asyncio.Semaphore(3)

        async def process_target(res: dict) -> tuple[dict, dict]:
            fetched = await _fetch_expert_source_content(res)
            if fetched.get("success") or fetched.get("fetch_success"):
                async with llm_semaphore:
                    source = await _summarize_source(session, res, fetched)
            else:
                # Bypass LLM summarization for failed fetches
                source = {
                    "id": "",  # To be assigned sequentially during commit
                    "title": fetched.get("title") or res.get("title") or "Untitled",
                    "url": fetched.get("url") or res.get("url"),
                    "normalized_url": res.get("normalized_url") or _normalize_url(fetched.get("url") or res.get("url")),
                    "domain": res.get("domain") or _source_domain(fetched.get("url") or res.get("url")),
                    "snippet": res.get("snippet", ""),
                    "fetch_success": False,
                    "fetch_error": fetched.get("error", "Fetch failed"),
                    "content": "",
                    "summary": f"Fetch failed: {fetched.get('error', 'Fetch failed')}",
                    "key_claims": [],
                    "dates": [],
                    "actors": [],
                    "perspective": "Unlabeled",
                    "reliability_label": "Unlabeled",
                    "useful_followups": [],
                }
            return fetched, source

        tasks = [process_target(t) for t in targets]
        completed = await asyncio.gather(*tasks)

        # 5. Commit completed sources sequentially to maintain state/ID order and trigger notifications
        for fetched, source in completed:
            source["id"] = f"S{len(session.sources) + 1}"
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
            if source.get("fetch_success"):
                await _send_source_found_notice(bot, session, source, question_id)
                await _maybe_send_source_milestone_notice(bot, session)


def _is_report_meta_footer(text: str) -> bool:
    clean = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    if not clean or len(clean) > 500:
        return False
    return any(
        phrase in clean
        for phrase in (
            "report generated",
            "provided research packets",
            "structured data notes",
            "source appendices separately",
            "source appendix separately",
            "sources separately",
            "appendix separately",
            "appendices separately",
            "emery will receive",
            "emery will send",
            "based on the research packets",
            "based on provided research",
        )
    )


def _strip_report_meta_footer(report: str) -> str:
    clean = str(report or "").strip()
    if not clean:
        return ""
    blocks = re.split(r"\n\s*\n", clean)
    while blocks and _is_report_meta_footer(blocks[-1]):
        blocks.pop()
    return "\n\n".join(block.strip() for block in blocks).strip()


async def _build_final_report(session: ExpertSession) -> str:
    prompt = (
        "Write the final research report as clean Telegram-friendly Markdown. "
        "Use headings, bullets, numbered lists, compact tables only if they fit, and citations like [S12]. "
        "Include: executive summary, timeline, key actors, core findings, competing interpretations, "
        "and uncertainty/confidence notes. Do not include a source appendix in this report. "
        "Do not include a source list; use citations only. "
        "Do not mention separate source messages, appendices, research packets, structured data notes, "
        "prompt inputs, or that the report was generated from provided materials. Be explicit about source "
        "reliability and perspective when it matters to the analysis.\n\n"
        f"User request: {session.topic}\n"
        f"User follow-up inputs: {json.dumps(session.user_inputs, ensure_ascii=True)}\n\n"
        f"{_model_display_name()} research agenda:\n{_agenda_digest(session)}\n\n"
        f"Research packets:\n{_research_packet_digest(session, limit=20)}\n\n"
        f"Source notes:\n{_source_digest(session, limit=40)}\n\n"
        f"Structured data notes:\n{_econ_digest(session, limit=20)}"
    )
    system = (
        f"You are {_model_display_name()}'s senior research writer. Produce only the finished report in Markdown. "
        "Do not include hidden reasoning, process notes, implementation notes, delivery notes, or meta disclaimers."
    )
    report = _strip_report_meta_footer(await _query_main_model(prompt, system))
    if report:
        return report

    lines = [
        f"# {_model_display_name()} Report: {session.title}",
        "",
        "## Executive Summary",
        "A final synthesis could not be generated from the available research.",
    ]
    return "\n".join(lines)


def _source_appendix_markdown(session: ExpertSession) -> str:
    lines = ["# Sources", ""]
    if not session.sources:
        lines.append("No web sources were fetched for this research session.")
        return "\n".join(lines)

    for source in session.sources:
        source_id = source.get("id") or "Source"
        title = source.get("title") or "Untitled source"
        url = source.get("url") or ""
        reliability = source.get("reliability_label") or "Reliability not labeled"
        perspective = source.get("perspective") or "Perspective not labeled"
        status = "Fetched" if source.get("fetch_success") else "Fetch failed"

        line = f"- [{source_id}] {title}"
        if url:
            line += f" - {url}"
        line += f"\n  {status}; {reliability}; {perspective}."
        lines.append(line)
    return "\n".join(lines)


def _archived_report_markdown(session: ExpertSession) -> str:
    report = (session.final_report or "").strip()
    sources = _source_appendix_markdown(session).strip()
    return f"{report}\n\n{sources}\n" if report else f"{sources}\n"


async def _deliver_final_report(bot, session: ExpertSession) -> None:
    fallback_html = emery_format(session.final_report)
    await send_rich_or_split_html_message(
        bot,
        session.chat_id,
        session.final_report,
        fallback_html_text=fallback_html,
        message_thread_id=session.message_thread_id,
    )
    source_appendix = _source_appendix_markdown(session)
    await send_rich_or_split_html_message(
        bot,
        session.chat_id,
        source_appendix,
        fallback_html_text=emery_format(source_appendix),
        message_thread_id=session.message_thread_id,
    )
    await _send_model_notice(
        bot,
        session,
        "research complete",
        ["What should I do next?"],
        reply_markup=_session_action_markup(session),
    )
    _schedule_normal_chat_warmup(session, "report complete")


async def _run_research_session(session: ExpertSession, bot) -> None:
    current_task = asyncio.current_task()
    typing_stop, typing_task = _start_expert_typing(bot, session)
    globals.register_foreground_loop(
        session.id,
        loop_type="expert",
        chat_id=session.chat_id,
        message_thread_id=session.message_thread_id,
    )
    try:
        ACTIVE_SESSIONS[session.key()] = session
        if session.round == 0 and not session.search_queries and not session.research_agenda:
            await _send_expert_started_notice(bot, session)
            plan = await _make_initial_plan(session)
            session.title = plan["title"]
            session.target_sources = _normalize_source_target(plan.get("target_source_count"))
            session.max_sources = max(session.target_sources, DEFAULT_MAX_SOURCES)
            session.max_rounds = max(session.max_rounds, _round_budget_for_target(session.target_sources))
            session.research_agenda = plan["agenda_questions"]
            session.search_queries.extend(plan["search_queries"])
            _record_event(
                session,
                "plan",
                plan.get("framing") or "Initial research plan created.",
                target_source_count=session.target_sources,
                max_sources=session.max_sources,
                max_rounds=session.max_rounds,
                agenda_count=len(session.research_agenda),
            )
            await _send_plan_ready_notice(bot, session)
            if plan.get("econ_requests"):
                await _run_econ_requests(bot, session, plan["econ_requests"])

        session.status = "running"
        while session.status == "running" and session.round < session.max_rounds and _source_count(session) < session.max_sources:
            if session.followup_instruction:
                additions_allowed = max(0, EXPERT_MAX_AGENDA_QUESTIONS - len(session.research_agenda))
                if additions_allowed:
                    added = _normalize_agenda_questions([{
                        "question": session.followup_instruction,
                        "priority": "core",
                        "why": f"User requested this direction while continuing the {_model_display_name()} research session.",
                    }], existing_count=len(session.research_agenda), limit=1)
                    session.research_agenda.extend(added)
                    for item in added:
                        _record_event(session, "agenda_add", f"User-added {item['id']}: {item['question']}")
                        await _send_question_added_notice(bot, session, item)
                session.followup_instruction = ""

            question = _select_next_agenda_question(session)
            if not question:
                _record_event(session, "agenda_done", "No open agenda questions remain within budget.")
                break
            if _source_count(session) >= session.target_sources and not _agenda_has_open_core_questions(session):
                _record_event(session, "stop", "Source target reached and core agenda questions are resolved.")
                break

            session.round += 1
            session.touch()

            packet = await _run_research_subtask(bot, session, question)
            evaluation = await _evaluate_research_packet(session, question, packet)
            added_questions = _apply_agenda_evaluation(session, question, packet, evaluation)
            _record_event(
                session,
                "agenda_eval",
                f"{MODEL_NAME} evaluated {question.get('id')}: status={question.get('status')} confidence={question.get('confidence')}",
                evaluation_status=evaluation.get("status"),
                stop_now=bool(evaluation.get("stop_now")),
            )
            next_question = _select_next_agenda_question(session)
            await _send_question_finished_notice(bot, session, question, next_question)
            await _send_question_review_notice(bot, session, question, next_question)
            for added_question in added_questions:
                await _send_question_added_notice(bot, session, added_question)
            questions_for_pause = _questions_for_midloop_pause(session, evaluation.get("critical_questions") or [])
            if questions_for_pause:
                session.pending_questions = questions_for_pause
                session.pending_answers = {}
                session.status = "waiting_for_answer"
                _record_event(session, "pause", f"{MODEL_NAME} paused for critical user direction.")
                await _ask_pending_questions(bot, session)
                return
            if (
                evaluation.get("stop_now")
                and not _agenda_has_open_core_questions(session)
                and _source_count(session) >= max(8, session.target_sources // 2)
            ):
                _record_event(session, "stop", f"{MODEL_NAME} decided the core research agenda is sufficiently answered.")
                break

        await _send_model_notice(
            bot,
            session,
            "is synthesizing",
            _synthesis_notice_lines(session),
        )
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
        _record_event(session, "error", f"Research loop failed: {exc}")
        logging.error("EXPERT: Session %s failed: %s", session.id, exc, exc_info=True)
        await _send_model_notice(bot, session, "research failed", [str(exc)])
    finally:
        await _stop_expert_typing(typing_stop, typing_task)
        globals.unregister_foreground_loop(session.id)
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

    if not session.archive_label:
        session.archive_label = _fallback_archive_label(session.title, session.topic)
    session.status = "archived"
    session.archive_path = str(folder)
    session.touch()

    (folder / "session.json").write_text(
        json.dumps(session.to_dict(), indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    (folder / "report.md").write_text(_archived_report_markdown(session), encoding="utf-8")
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
        "archive_label": session.archive_label,
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
        f"# {_model_display_name()} Research Loop: {session.title}",
        "",
        f"- Session ID: `{session.id}`",
        f"- Topic: {session.topic}",
        f"- Created: {session.created_at}",
        f"- Status: {session.status}",
        f"- Sources: {_source_count(session)}",
        f"- Structured tool results: {_econ_count(session)}",
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

    lines.extend(["", "## Research Agenda"])
    for item in session.research_agenda:
        lines.append(f"### {item.get('id')}: {item.get('question')}")
        lines.append(f"- Priority: {item.get('priority')}")
        lines.append(f"- Status: {item.get('status')}")
        lines.append(f"- Attempts: {item.get('attempts')}")
        lines.append(f"- Confidence: {item.get('confidence')}")
        if item.get("why"):
            lines.append(f"- Why: {item.get('why')}")
        if item.get("answer_summary"):
            lines.append("")
            lines.append(str(item.get("answer_summary")))
        lines.append("")

    lines.extend(["", "## Research Packets"])
    for packet in session.research_packets:
        lines.append(f"### {packet.get('id')}: {packet.get('question_id')}")
        lines.append(f"- Question: {packet.get('question')}")
        lines.append(f"- Sources: {', '.join(packet.get('source_ids') or [])}")
        lines.append(f"- Structured tool results: {', '.join(packet.get('econ_result_ids') or [])}")
        lines.append(f"- Confidence: {packet.get('confidence')}")
        lines.append("")
        lines.append(str(packet.get("summary") or ""))
        if packet.get("gaps"):
            lines.append("")
            lines.append("Gaps:")
            for gap in packet.get("gaps") or []:
                lines.append(f"- {gap}")
        lines.append("")

    lines.extend(["", "## Sources"])
    for source in session.sources:
        lines.append(f"### {source.get('id')}: {source.get('title')}")
        lines.append(f"- URL: {source.get('url')}")
        lines.append(f"- Reliability: {source.get('reliability_label')}")
        lines.append(f"- Perspective: {source.get('perspective')}")
        lines.append("")
        lines.append(str(source.get("summary") or ""))
        lines.append("")

    lines.extend(["", "## Structured Tool Results"])
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
    await _ensure_archive_label(session)
    folder = _archive_session(session)
    ACTIVE_SESSIONS.pop(session.key(), None)
    await _send_model_notice(
        bot,
        session,
        "archived research",
        [f"Saved to: {folder}"],
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


def _expert_help_text() -> str:
    return "\n".join([
        f"<b>{telegram_escape(_model_display_name())} research commands</b>",
        "",
        "/expert &lt;detailed research question&gt; - Start a focused research session.",
        "/expert help - Show this help.",
        "/expert list - Show archived research sessions.",
        "/expert status - Show the active research session status.",
        "/expert resume &lt;id&gt; - Resume an archived research session.",
        "/expert open &lt;id&gt; - Send an archived report.",
        "/expert clear - Delete all archived research reports.",
        "/expert cancel - Cancel the active research session.",
        "",
        "Use a complete research request, not a short command fragment.",
    ])


async def _send_expert_help(update) -> None:
    await update.message.reply_text(_expert_help_text(), parse_mode="HTML")


async def _clear_archived_reports(update) -> None:
    result = clear_expert_archives()
    await update.message.reply_text(
        f"Deleted archived {_model_display_name()} research reports.\n"
        f"Archived sessions cleared: {result['archived_sessions']}\n"
        f"Archive items removed: {result['removed_items']}"
    )


def _topic_has_enough_substance(topic: str) -> bool:
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'/-]*", str(topic or ""))
    if len(words) >= 4:
        return True
    if len(words) >= 3 and any(len(word) >= 8 for word in words):
        return True
    return False


def _expert_topic_label(text: str) -> str:
    return _normalize_intent_label(text, {"research_topic", "malformed"}, "unknown")


async def _classify_expert_topic_input(topic: str) -> str:
    prompt = (
        "Classify whether this /expert argument is a detailed research topic or a malformed/command-like fragment. "
        "Return exactly one label: research_topic or malformed.\n\n"
        "Use malformed for vague one- or two-word inputs, command-like fragments, or inputs such as: "
        "space, summary, research, question, help me, topic, list please. "
        "Use research_topic for complete requests with enough detail for a research session.\n\n"
        f"Input: {topic}"
    )
    try:
        result = await _query_expert_fast_model(
            prompt,
            f"You classify candidate {_model_display_name()} research requests. Return one label only.",
        )
    except Exception as exc:
        logging.warning("EXPERT: Topic classifier failed: %s", exc)
        return "research_topic" if _topic_has_enough_substance(topic) else "malformed"
    label = _expert_topic_label(result)
    if label in {"research_topic", "malformed"}:
        return label
    return "research_topic" if _topic_has_enough_substance(topic) else "malformed"


async def _send_malformed_expert_topic(update) -> None:
    await update.message.reply_text(
        "I'm sorry, I don't understand. Can you ask a detailed research question for me and my assistant to dive into?"
    )


async def _classify_expert_user_intent(session: ExpertSession, text: str) -> str:
    if session.status == "waiting_for_answer":
        allowed = {"close_archive", "cancel", "answer_question"}
        labels = "close_archive, cancel, answer_question"
        default = "answer_question"
        pending = json.dumps(session.pending_questions[:4], ensure_ascii=True)
        state_instruction = (
            f"{_model_display_name()} is waiting for the user to answer one or more pending questions. "
            "Use close_archive only if the user wants to end, close, archive, finish, or move on from the research session. "
            f"Use cancel only if the user wants to stop/discard the active {_model_display_name()} research session. "
            "Otherwise use answer_question, including free-form answers that do not match the button options."
        )
    else:
        allowed = {"close_archive", "cancel", "continue_research", "refine_report", "normal_message"}
        labels = "close_archive, cancel, continue_research, refine_report, normal_message"
        default = "normal_message"
        pending = "[]"
        state_instruction = (
            "The research loop has completed and the user is deciding what to do next. "
            "Use close_archive if the user wants to end, close, archive, finish, wrap up, or move on from the research session. "
            "Use continue_research if the user wants more research, more sources, a new branch, or deeper investigation. "
            "Use refine_report if the user asks to rewrite, shorten, expand, reorganize, polish, or otherwise alter the report. "
            "Use cancel if the user wants to discard/cancel the session. "
            "Use normal_message only if the message is not directing the research session."
        )

    prompt = (
        f"Classify this Telegram message for {_model_display_name()}'s /expert mode. "
        f"Return exactly one label from: {labels}.\n\n"
        f"Session status: {session.status}\n"
        f"Session title: {session.title}\n"
        f"Pending questions JSON: {pending}\n\n"
        f"{state_instruction}\n\n"
        f"User message: {text}"
    )
    result = await _query_expert_fast_model(
        prompt,
        f"You are a strict intent classifier for an active {_model_display_name()} research session. Return one label only.",
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
    typing_stop, typing_task = _start_expert_typing(bot, session)
    await _send_model_notice(bot, session, "is refining the report")
    try:
        prompt = (
            "Revise the existing research report according to the user instruction. Preserve source citations and "
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
    finally:
        await _stop_expert_typing(typing_stop, typing_task)


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
        await _send_expert_help(update)
        return

    subcommand = args[0].lower()
    if subcommand == "help":
        await _send_expert_help(update)
        return
    if subcommand == "list":
        await _send_expert_list(update, context)
        return
    if subcommand == "status":
        await _send_expert_status(update, context)
        return
    if subcommand == "clear":
        await _clear_archived_reports(update)
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
    if subcommand in {"resume", "open"}:
        await _send_expert_help(update)
        return

    key = _session_key(chat_id, thread_id)
    if key in ACTIVE_SESSIONS and ACTIVE_SESSIONS[key].status in {"running", "waiting_for_answer", "completed_pending_user"}:
        await update.message.reply_text(f"A {_model_display_name()} research session is already active here. Use /expert status, /expert cancel, or close/archive the current session first.")
        return

    topic = " ".join(args).strip()
    if await _classify_expert_topic_input(topic) == "malformed":
        await _send_malformed_expert_topic(update)
        return

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
    await _send_expert_typing_once(bot, session)
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
        _record_event(session, "answer", f"User answered research question: {text}")
        if is_refine_answer:
            session.status = "completed_pending_user"
            await _refine_report(bot, session, text)
            return True
        session.followup_instruction = text
        session.status = "running"
        await _send_model_notice(bot, session, "is resuming research", [_short_line(text, 220)])
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
    if not session and action in {"select", "resume", "open", "archive_cancel"} and len(parts) >= 4:
        archive_id = parts[3]
        if action == "select":
            await _send_archive_action_menu(update, archive_id)
        elif action == "resume":
            await _resume_archived_session(update, context, archive_id)
        elif action == "open":
            await _open_archived_report(update, context, archive_id)
        else:
            await query.message.reply_text(f"{_model_display_name()} is back to normal chat.")
        return
    if not session:
        await query.message.reply_text(f"That {_model_display_name()} research session is no longer active.")
        return

    if action == "noop":
        return

    if action == "cancel":
        await _cancel_session_object(context.bot, session)
        return

    if action in {"close", "continue", "refine"} and session.status != "completed_pending_user":
        await query.message.reply_text(f"That {_model_display_name()} research session is currently {session.status}. Use /expert status or /expert cancel.")
        return

    if action == "q" and session.status != "waiting_for_answer":
        await query.message.reply_text("That research question is no longer active.")
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
            await query.message.reply_text("Recorded. Answer the remaining research questions, or type a full response.")
        else:
            session.followup_instruction = "; ".join(session.pending_answers.values())
            session.pending_questions = []
            session.pending_answers = {}
            session.status = "running"
            await _send_model_notice(context.bot, session, "is resuming research", ["Using your selected options"])
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
    _record_event(session, "cancelled", f"User cancelled {_model_display_name()} research session.")
    ACTIVE_SESSIONS.pop(session.key(), None)
    await _send_model_notice(bot, session, "cancelled research")
    _schedule_normal_chat_warmup(session, "cancel")


async def _cancel_active_session(update, context) -> None:
    session = _active_session_for_update(update)
    if not session:
        await update.message.reply_text(f"No active {_model_display_name()} research session in this chat/thread.")
        return
    await _cancel_session_object(context.bot, session)


async def _send_expert_status(update, context) -> None:
    session = _active_session_for_update(update)
    if not session:
        await update.message.reply_text(f"No active {_model_display_name()} research session in this chat/thread.")
        return
    await update.message.reply_text(
        f"{_model_display_name()} research session {session.id}: {session.status}\n"
        f"Title: {session.title}\n"
        f"Round: {session.round}\n"
        f"Sources: {_source_count(session)}\n"
        f"Structured tool results: {_econ_count(session)}"
    )


async def _send_expert_list(update, context) -> None:
    all_entries = _load_index()
    entries = all_entries[:10]
    if not entries:
        await update.message.reply_text(f"No archived {_model_display_name()} research sessions yet.")
        return

    index_changed = False
    for entry in entries:
        if not str(entry.get("archive_label") or "").strip():
            entry["archive_label"] = await _generate_archive_label(entry.get("title", ""), entry.get("topic", ""))
            index_changed = True
    if index_changed:
        _save_index(all_entries)

    lines = [f"Archived {_model_display_name()} research sessions:"]
    rows = []
    for entry in entries:
        session_id = entry.get("id", "")
        display_label = _archive_entry_display_label(entry)
        econ_count = entry.get("econ_result_count", 0)
        data_label = f", {econ_count} structured tool results" if econ_count else ""
        lines.append(f"- {display_label} ({entry.get('source_count', 0)} sources{data_label})")
        rows.append([
            InlineKeyboardButton(
                _telegram_button_text(display_label),
                callback_data=_callback("select", "archive", session_id),
            ),
        ])
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))


async def _send_archive_action_menu(update, session_id: str) -> None:
    entry = _find_index_entry(session_id)
    target_message = update.message or (update.callback_query.message if update.callback_query else None)
    if not target_message:
        return
    if not entry:
        await target_message.reply_text(f"No archived {_model_display_name()} research session found for ID {session_id}.")
        return

    display_label = _archive_entry_display_label(entry)
    rows = [
        [
            InlineKeyboardButton("Resume", callback_data=_callback("resume", "archive", session_id)),
            InlineKeyboardButton("Open report", callback_data=_callback("open", "archive", session_id)),
        ],
        [
            InlineKeyboardButton("Cancel", callback_data=_callback("archive_cancel", "archive", session_id)),
        ],
    ]
    await target_message.reply_text(
        f"{_model_display_name()} archived report:\n{display_label}",
        reply_markup=InlineKeyboardMarkup(rows),
    )


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
            await target_message.reply_text(f"No archived {_model_display_name()} research session found for ID {session_id}.")
        return
    try:
        data = json.loads(Path(entry["session_path"]).expanduser().read_text(encoding="utf-8"))
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
                f"A {_model_display_name()} research session is already active in this chat/thread. Close, archive, or cancel it before resuming another one."
            )
        return

    session.chat_id = chat_id
    session.message_thread_id = thread_id
    session.status = "completed_pending_user"
    session.pending_questions = []
    session.pending_answers = {}
    session.followup_instruction = ""
    _record_event(session, "resume", f"Archived session resumed into active {_model_display_name()} research mode.")
    ACTIVE_SESSIONS[session.key()] = session
    if target_message:
        await target_message.reply_text(
            f"Resumed {_model_display_name()} research session {session.id}: {session.title}\nWhat should I do next?",
            reply_markup=_session_action_markup(session),
        )


async def _open_archived_report(update, context, session_id: str) -> None:
    entry = _find_index_entry(session_id)
    target_message = update.message or (update.callback_query.message if update.callback_query else None)
    if not entry:
        if target_message:
            await target_message.reply_text(f"No archived {_model_display_name()} research session found for ID {session_id}.")
        return
    try:
        report = Path(entry["report_path"]).expanduser().read_text(encoding="utf-8")
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
