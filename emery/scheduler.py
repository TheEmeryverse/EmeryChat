import os
import json
import uuid
import logging
import re
import asyncio
import copy
from datetime import datetime, time, timedelta
from collections import deque
from pathlib import Path

from emery.config import (
    USER_TIMEZONE, JOBS_FILE_PATH, TELEGRAM_GROUP_CHAT_ID,
    ROUTINES_TOPIC_ID, CHAT_TOPIC_ID, ROUTINE_HISTORY_DEFER_SECONDS,
    ENABLE_ROUTINE_CACHE_WARMUP
)
import emery.globals as globals
from emery.telegram_delivery import try_send_rich_or_split_html_message, try_send_split_html_message
from emery.telegram_utils import normalize_message_thread_id

WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6
}

LEGACY_JOBS_FILE_PATH = str(Path(JOBS_FILE_PATH).resolve().parent.parent / "custom_jobs.json")
RECURRING_SCHEDULE_TYPES = {"daily", "weekly", "monthly", "yearly", "interval"}
SHARED_TARGET_ALIASES = {"both", "us", "family", "everyone", "all", "we", "our"}
CREATOR_TARGET_ALIASES = {"me", "myself", "my"}
SECONDARY_TARGET_ALIASES = {"wife", "spouse", "partner", "her", "husband", "him"}
SCHEDULE_TYPE_ORDER = ("once", "daily", "weekly", "monthly", "yearly", "interval")
_PENDING_ROUTINE_HISTORY: dict[tuple[int, int | None], list[dict]] = {}
_ROUTINE_HISTORY_FLUSH_JOB_PREFIX = "routine_history_flush"
_ROUTINE_HISTORY_RESULT_CHARS = 1800
_ROUTINE_HISTORY_PROMPT_CHARS = 500
_ROUTINE_HISTORY_MAX_PENDING_ENTRIES = 5
_PENDING_SCHEDULED_RUNS: deque[dict] = deque()
_PENDING_SCHEDULED_DRAIN_TASK: asyncio.Task | None = None


def _truncate_for_history(text: str, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "").strip())
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0].strip() + "..."


def _routine_history_key(chat_id: int, message_thread_id: int | None) -> tuple[int, int | None]:
    return chat_id, normalize_message_thread_id(chat_id, message_thread_id)


def _is_routine_history_candidate(
    *,
    delivery_scope: str | None,
    schedule_type: str | None,
    route_to_routines: bool,
    prompt: str | None,
    description: str | None,
) -> bool:
    if delivery_scope == "routine":
        return True
    if delivery_scope:
        return False
    return (
        str(schedule_type or "").lower() in RECURRING_SCHEDULE_TYPES
        and route_to_routines
        and not _looks_like_reminder(prompt or "", description or "")
    )


def _routine_destination_for_job_data(job_data: dict) -> tuple[int | None, int | None] | None:
    if not isinstance(job_data, dict):
        return None

    delivery_scope = job_data.get("delivery_scope")
    schedule_type = str(job_data.get("schedule_type") or "").lower()
    route_to_routines = job_data.get("route_to_routines", True)
    prompt = job_data.get("prompt")
    description = job_data.get("description")

    if not _is_routine_history_candidate(
        delivery_scope=delivery_scope,
        schedule_type=schedule_type,
        route_to_routines=route_to_routines,
        prompt=prompt,
        description=description,
    ):
        return None

    chat_id = job_data.get("chat_id") or job_data.get("origin_chat_id")
    message_thread_id = job_data.get("message_thread_id") or job_data.get("origin_message_thread_id")

    if TELEGRAM_GROUP_CHAT_ID is not None:
        chat_id = TELEGRAM_GROUP_CHAT_ID
    if ROUTINES_TOPIC_ID is not None:
        message_thread_id = ROUTINES_TOPIC_ID

    if chat_id is None:
        return None
    return chat_id, normalize_message_thread_id(chat_id, message_thread_id)


def _job_next_time(job):
    next_t = getattr(job, "next_t", None)
    if callable(next_t):
        next_t = next_t()
    if next_t is None:
        return None
    if next_t.tzinfo is None:
        if hasattr(USER_TIMEZONE, "localize"):
            return USER_TIMEZONE.localize(next_t)
        return next_t.replace(tzinfo=USER_TIMEZONE)
    return next_t.astimezone(USER_TIMEZONE)


def _job_data_next_time(job_data: dict, now: datetime):
    if not isinstance(job_data, dict):
        return None

    schedule_type = str(job_data.get("schedule_type") or "").lower()
    schedule_value = str(job_data.get("schedule_value") or "").strip()
    try:
        if schedule_type == "interval":
            return now + timedelta(seconds=parse_duration_to_seconds(schedule_value))
    except Exception as exc:
        logging.debug("📅 SCHEDULER: Unable to infer next routine time from job data: %s", exc)
    return None


def _next_routine_eta_seconds(
    *,
    chat_id: int,
    message_thread_id: int | None,
    now: datetime,
) -> float | None:
    if not globals.application or not globals.application.job_queue:
        return None

    jobs_getter = getattr(globals.application.job_queue, "jobs", None)
    if not callable(jobs_getter):
        return None

    target_key = _routine_history_key(chat_id, message_thread_id)
    soonest_eta = None
    try:
        jobs = jobs_getter()
    except Exception as exc:
        logging.debug("📅 SCHEDULER: Unable to inspect JobQueue for nearby routines: %s", exc)
        return None

    for queued_job in jobs:
        job_data = getattr(queued_job, "data", None)
        destination = _routine_destination_for_job_data(job_data)
        if destination != target_key:
            continue

        next_time = _job_next_time(queued_job)
        eta_seconds = (next_time - now).total_seconds() if next_time is not None else None
        if eta_seconds is None or eta_seconds <= 0:
            fallback_next_time = _job_data_next_time(job_data, now)
            eta_seconds = (fallback_next_time - now).total_seconds() if fallback_next_time is not None else None
        if eta_seconds is None or eta_seconds <= 0:
            continue
        if soonest_eta is None or eta_seconds < soonest_eta:
            soonest_eta = eta_seconds

    return soonest_eta


def _format_routine_history_summary(entries: list[dict]) -> str:
    lines = ["Recent scheduled routine results were delivered to Telegram."]
    for index, entry in enumerate(entries, start=1):
        lines.extend([
            "",
            f"{index}. {entry['description']} ({entry['job_id']})",
            f"   Time: {entry['timestamp']}",
        ])
        if entry.get("prompt"):
            lines.append(f"   Prompt: {entry['prompt']}")
        if entry.get("result"):
            lines.append(f"   Delivered result excerpt: {entry['result']}")
    return "\n".join(lines).strip()


def _flush_pending_routine_history(key: tuple[int, int | None], *, reason: str) -> bool:
    entries = _PENDING_ROUTINE_HISTORY.pop(key, [])
    if not entries:
        return False

    chat_id, message_thread_id = key
    if chat_id not in globals.chat_histories:
        globals.chat_histories[chat_id] = deque()

    globals.chat_histories[chat_id].append({
        "role": "assistant",
        "content": _format_routine_history_summary(entries),
        "message_thread_id": message_thread_id,
        "timestamp": datetime.now(USER_TIMEZONE),
        "is_routine_history_summary": True,
    })
    logging.info(
        "📅 SCHEDULER: Flushed %s compact routine history entr%s for chat_id=%s thread_id=%s (%s)",
        len(entries),
        "y" if len(entries) == 1 else "ies",
        chat_id,
        message_thread_id,
        reason,
    )
    return True


async def _warm_chat_history_after_routines(key: tuple[int, int | None], *, reason: str) -> None:
    if not ENABLE_ROUTINE_CACHE_WARMUP:
        return

    chat_id, message_thread_id = key
    history = globals.chat_histories.get(chat_id)
    if not history:
        return

    from emery.engine import warm_main_model_cache

    warmed = await warm_main_model_cache(
        history,
        reason=f"routine batch complete: chat_id={chat_id} thread_id={message_thread_id} {reason}",
    )
    if warmed:
        logging.info(
            "📅 SCHEDULER: Warmed chat cache after routine batch for chat_id=%s thread_id=%s (%s)",
            chat_id,
            message_thread_id,
            reason,
        )


def _schedule_pending_routine_history_flush(key: tuple[int, int | None]) -> None:
    if ROUTINE_HISTORY_DEFER_SECONDS <= 0:
        return
    if not globals.application or not globals.application.job_queue:
        return

    chat_id, message_thread_id = key
    globals.application.job_queue.run_once(
        _flush_pending_routine_history_job,
        when=ROUTINE_HISTORY_DEFER_SECONDS,
        data={"key": key},
        name=f"{_ROUTINE_HISTORY_FLUSH_JOB_PREFIX}:{chat_id}:{message_thread_id or 0}",
    )


async def _flush_pending_routine_history_job(context):
    key = (context.job.data or {}).get("key")
    if not isinstance(key, tuple) or len(key) != 2:
        return

    chat_id, message_thread_id = key
    now = datetime.now(USER_TIMEZONE)
    eta_seconds = _next_routine_eta_seconds(
        chat_id=chat_id,
        message_thread_id=message_thread_id,
        now=now,
    )
    if eta_seconds is not None and eta_seconds <= ROUTINE_HISTORY_DEFER_SECONDS:
        logging.info(
            "📅 SCHEDULER: Keeping compact routine history deferred for chat_id=%s thread_id=%s; next routine in %.0fs",
            chat_id,
            message_thread_id,
            eta_seconds,
        )
        _schedule_pending_routine_history_flush(key)
        return

    if _flush_pending_routine_history(key, reason="timer"):
        await _warm_chat_history_after_routines(key, reason="timer")


def _record_routine_history(
    *,
    chat_id: int,
    message_thread_id: int | None,
    job_id: str,
    description: str,
    prompt: str | None,
    result_text: str,
    now: datetime,
) -> bool:
    from emery.helpers import clean_thinking_tags

    key = _routine_history_key(chat_id, message_thread_id)
    entry = {
        "job_id": job_id or "unknown",
        "description": description or "Scheduled routine",
        "timestamp": now.strftime("%A, %B %d, %Y at %I:%M %p"),
        "prompt": _truncate_for_history(prompt or "", _ROUTINE_HISTORY_PROMPT_CHARS),
        "result": _truncate_for_history(clean_thinking_tags(result_text or ""), _ROUTINE_HISTORY_RESULT_CHARS),
    }
    pending_entries = _PENDING_ROUTINE_HISTORY.setdefault(key, [])
    pending_entries.append(entry)

    eta_seconds = _next_routine_eta_seconds(
        chat_id=chat_id,
        message_thread_id=message_thread_id,
        now=now,
    )
    should_defer = (
        ROUTINE_HISTORY_DEFER_SECONDS > 0
        and eta_seconds is not None
        and eta_seconds <= ROUTINE_HISTORY_DEFER_SECONDS
    )

    if len(pending_entries) >= _ROUTINE_HISTORY_MAX_PENDING_ENTRIES:
        _flush_pending_routine_history(key, reason="pending limit")
        if should_defer:
            _schedule_pending_routine_history_flush(key)
        return not should_defer

    if should_defer:
        logging.info(
            "📅 SCHEDULER: Deferred compact routine history for job '%s' (%s); next routine in %.0fs",
            description,
            job_id,
            eta_seconds,
        )
        _schedule_pending_routine_history_flush(key)
        return False

    return _flush_pending_routine_history(key, reason="no nearby routine")


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _foreground_loop_active() -> bool:
    return globals.has_active_foreground_loops()


def _defer_scheduled_job(job_data: dict, *, reason: str) -> None:
    payload = copy.deepcopy(job_data or {})
    payload["_deferred_enqueued_at"] = _now_iso()
    payload["_deferred_reason"] = reason
    _PENDING_SCHEDULED_RUNS.append(payload)
    logging.info(
        "📅 SCHEDULER: Deferred job '%s' (%s) because %s. Pending scheduled runs=%s",
        payload.get("description"),
        payload.get("id"),
        reason,
        len(_PENDING_SCHEDULED_RUNS),
    )


def _now_iso() -> str:
    return datetime.now(USER_TIMEZONE).replace(microsecond=0).isoformat()


def trigger_deferred_job_drain(*, reason: str = "") -> bool:
    global _PENDING_SCHEDULED_DRAIN_TASK

    if _foreground_loop_active():
        return False
    if not _PENDING_SCHEDULED_RUNS:
        return False
    if _PENDING_SCHEDULED_DRAIN_TASK and not _PENDING_SCHEDULED_DRAIN_TASK.done():
        return False
    if globals.application_bot is None:
        logging.warning(
            "⚠️ SCHEDULER: Deferred scheduled jobs are waiting, but application_bot is unavailable."
        )
        return False

    _PENDING_SCHEDULED_DRAIN_TASK = asyncio.create_task(
        _drain_deferred_scheduled_jobs(reason=reason or "foreground loop idle")
    )
    return True


async def _drain_deferred_scheduled_jobs(*, reason: str) -> None:
    global _PENDING_SCHEDULED_DRAIN_TASK

    try:
        logging.info(
            "📅 SCHEDULER: Starting deferred scheduled-job drain (%s pending, reason=%s)",
            len(_PENDING_SCHEDULED_RUNS),
            reason,
        )
        while _PENDING_SCHEDULED_RUNS:
            if _foreground_loop_active():
                logging.info(
                    "📅 SCHEDULER: Pausing deferred scheduled-job drain because a foreground loop became active again."
                )
                return
            job_data = _PENDING_SCHEDULED_RUNS.popleft()
            await _execute_custom_job(globals.application_bot, job_data, deferred_reason=reason)
    finally:
        _PENDING_SCHEDULED_DRAIN_TASK = None
        if _PENDING_SCHEDULED_RUNS and not _foreground_loop_active():
            trigger_deferred_job_drain(reason="deferred drain follow-up")


def _combined_job_text(prompt: str, description: str) -> str:
    return _normalize_text(f"{description or ''} {prompt or ''}")


def _strip_runtime_context_from_user_message(content: str) -> str | None:
    """Recover the actual user request from a dynamic-context wrapped message."""
    content = (content or "").strip()
    if "# New User Message" not in content:
        return content or None

    newest = content.split("# New User Message", 1)[1].strip()
    match = re.search(r"\]\s*[^:]+:\s*(.*)", newest, flags=re.DOTALL)
    if match:
        return match.group(1).strip() or None
    return newest or None


def _looks_like_reminder(prompt: str, description: str) -> bool:
    text = _combined_job_text(prompt, description)
    return bool(re.search(r"\b(remind|reminder|remember to|don't forget|dont forget)\b", text))


def _looks_like_routine(prompt: str, description: str) -> bool:
    text = _combined_job_text(prompt, description)
    return bool(
        re.search(r"\b(routine|briefing|digest|report|dashboard|monitor|monitoring|check|status|summary)\b", text)
        or re.search(r"\b(weather|news|market|security|camera|system|stats)\b.*\b(briefing|check|summary|report)\b", text)
    )


def _requests_voice_memo(prompt: str | None, description: str | None, source_request: str | None = None) -> bool:
    text = _normalize_text(f"{description or ''} {prompt or ''} {source_request or ''}")
    return bool(
        re.search(r"\b(voice memo|voice message|spoken briefing|spoken adaptation|audio briefing)\b", text)
        or re.search(r"\b(send|create|generate|record|speak)\b.{0,40}\b(voice|audio|spoken)\b", text)
    )


def build_scheduled_execution_prompt(prompt: str | None, description: str | None, *, voice_memo_requested: bool = False) -> str:
    """Wrap routine prompts with guardrails that keep scheduled outputs fresh and single-pass."""
    base_prompt = (prompt or "").strip()
    job_label = (description or "Scheduled Job").strip()
    voice_directive = ""
    if voice_memo_requested:
        voice_directive = (
            "\n- A voice memo was requested for this scheduled job. Write the briefing as natural spoken prose from the start: "
            "no markdown, headings, titles, bullets, numbered lists, tables, or section labels. "
            "Use conversational transitions between topics instead of labels like 'Today's News' or 'Domestic Job Market'. "
            "The scheduler will generate and send the voice memo after your response. Do not call `speak_message`."
        )

    return (
        f"Scheduled job to run now: {job_label}\n\n"
        f"{base_prompt}\n\n"
        "[SCHEDULED JOB OUTPUT RULES]\n"
        "- Produce exactly one polished final response for this run.\n"
        "- Start with the greeting or opening sentence if you use one; do not place the intro at the end.\n"
        "- Do not include drafts, alternate versions, meta commentary, or a second rewritten briefing.\n"
        "- Avoid repeating the same story, section, or conclusion unless a brief callback is necessary.\n"
        "- Use today's runtime context and tool results as authoritative; do not continue from prior scheduled-job outputs.\n"
        "- For long reports or briefings, use rich Markdown structure when useful: clear headings, bullets or numbered lists, block quotes, code blocks, and simple tables. Keep tables compact."
        f"{voice_directive}"
    )


def _mentions_shared_target(prompt: str, description: str) -> bool:
    text = _combined_job_text(prompt, description)
    return bool(
        re.search(r"\bremind\s+(us|everyone|everybody|all|both|the family)\b", text)
        or re.search(r"\b(for|to)\s+(us|everyone|everybody|all|both of us|the family)\b", text)
    )


def _mentions_creator_target(prompt: str, description: str) -> bool:
    text = _combined_job_text(prompt, description)
    return bool(
        re.search(r"\bremind\s+(me|myself)\b", text)
        or re.search(r"\b(my|me|myself)\b", text)
    )


def _has_explicit_single_user_target(target_user: str, prompt: str, description: str) -> bool:
    from emery.config import USER_NAME, USER_2_NAME

    if target_user:
        clean_name = _normalize_text(target_user)
        return clean_name not in SHARED_TARGET_ALIASES

    text = _combined_job_text(prompt, description)
    if _mentions_creator_target(prompt, description):
        return True
    if USER_NAME and re.search(rf"\bremind\s+{re.escape(USER_NAME.lower())}\b", text):
        return True
    if USER_2_NAME and re.search(rf"\bremind\s+{re.escape(USER_2_NAME.lower())}\b", text):
        return True
    return False


def _is_duration_value(value: str) -> bool:
    clean = str(value or "").strip().lower()
    return bool(re.match(r"^\d+(?:[hmsd])?$", clean))


def _missing_once_time_message(schedule_value: str) -> str:
    value = str(schedule_value or "").strip()
    label = value or "that date"
    return f"What time on {label} should I remind you?"


def _resolve_target_user(target_user: str, creator_user_id: int | None, prompt: str, description: str) -> tuple[int | None, str]:
    from emery.config import PRIMARY_USER_ID, SECONDARY_USER_ID, USER_NAME, USER_2_NAME, get_user_profile

    target_user_id = creator_user_id
    target_name = get_user_profile(creator_user_id)["name"] if creator_user_id else "User"

    if target_user:
        clean_name = _normalize_text(target_user)
        if clean_name in SHARED_TARGET_ALIASES:
            return -1, "both"
        if clean_name in CREATOR_TARGET_ALIASES:
            return creator_user_id, target_name
        if clean_name == USER_NAME.lower():
            return PRIMARY_USER_ID, USER_NAME
        if SECONDARY_USER_ID != 0 and clean_name in {USER_2_NAME.lower(), *SECONDARY_TARGET_ALIASES}:
            return SECONDARY_USER_ID, USER_2_NAME
        return target_user_id, target_name

    text = _combined_job_text(prompt, description)
    if USER_NAME and re.search(rf"\bremind\s+{re.escape(USER_NAME.lower())}\b", text):
        return PRIMARY_USER_ID, USER_NAME
    if SECONDARY_USER_ID != 0 and USER_2_NAME and re.search(rf"\bremind\s+{re.escape(USER_2_NAME.lower())}\b", text):
        return SECONDARY_USER_ID, USER_2_NAME

    if _mentions_shared_target(prompt, description):
        return -1, "both"

    if _mentions_creator_target(prompt, description):
        return creator_user_id, target_name

    return target_user_id, target_name


def _determine_delivery_scope(
    *,
    schedule_type: str,
    route_to_routines: bool,
    origin_chat_id: int,
    target_user_id: int | None,
    explicit_single_user_target: bool,
    prompt: str,
    description: str,
) -> str:
    if target_user_id == -1:
        return "shared"

    if explicit_single_user_target:
        return "personal"

    if _looks_like_reminder(prompt, description):
        return "personal"

    if origin_chat_id > 0:
        return "personal"

    if schedule_type in RECURRING_SCHEDULE_TYPES and (route_to_routines or _looks_like_routine(prompt, description)):
        return "routine"

    return "shared"


def _resolve_delivery_destination(
    *,
    delivery_scope: str,
    target_user_id: int | None,
    origin_chat_id: int,
    origin_message_thread_id: int | None,
) -> tuple[int | None, int | None]:
    if delivery_scope == "personal":
        return target_user_id or origin_chat_id, None

    if delivery_scope == "routine":
        chat_id = TELEGRAM_GROUP_CHAT_ID if TELEGRAM_GROUP_CHAT_ID is not None else origin_chat_id
        thread_id = ROUTINES_TOPIC_ID if ROUTINES_TOPIC_ID is not None else origin_message_thread_id
        return chat_id, thread_id

    chat_id = TELEGRAM_GROUP_CHAT_ID if TELEGRAM_GROUP_CHAT_ID is not None else origin_chat_id
    thread_id = CHAT_TOPIC_ID if CHAT_TOPIC_ID is not None else origin_message_thread_id
    return chat_id, thread_id


def parse_duration_to_seconds(val: str) -> int:
    """
    Parses duration strings like '1h', '30m', '10s', '1d', or a raw integer string
    into seconds.
    """
    val = val.strip().lower()
    if val.isdigit():
        return int(val)
    match = re.match(r'^(\d+)([hmsd])$', val)
    if match:
        num, unit = match.groups()
        num = int(num)
        if unit == 's':
            return num
        elif unit == 'm':
            return num * 60
        elif unit == 'h':
            return num * 3600
        elif unit == 'd':
            return num * 86400
    raise ValueError(f"Invalid duration format: '{val}'. Use e.g. '1h', '30m', '10s', '1d', or a number of seconds.")


def _format_schedule_type_counts(counts: dict[str, int]) -> str:
    parts = [
        f"{schedule_type}={counts[schedule_type]}"
        for schedule_type in SCHEDULE_TYPE_ORDER
        if counts.get(schedule_type)
    ]
    parts.extend(
        f"{schedule_type}={count}"
        for schedule_type, count in sorted(counts.items())
        if schedule_type not in SCHEDULE_TYPE_ORDER and count
    )
    return ", ".join(parts) if parts else "none"


def _log_job_scheduled(job_id: str, schedule_type: str, schedule_value: str, detail: str):
    logging.info(
        "📅 SCHEDULER: Scheduled %s job '%s' (%s) %s",
        schedule_type,
        job_id,
        schedule_value,
        detail,
    )


def build_reminder_execution_prompt(
    prompt: str | None,
    description: str | None,
    source_request: str | None,
    user_name: str,
    target_user_id: int | None,
) -> str:
    """Build a structured execution prompt so reminder jobs retain their concrete details."""
    base_prompt = (prompt or "").strip()
    details = (description or "").strip()
    original_request = (source_request or "").strip()

    shared_directive = (
        "Deliver the reminder directly and concisely as a message to the intended recipient. "
        "Use the saved prompt, job description, and original user scheduling request below as authoritative context. "
        "If any stored field is vague, use the original user scheduling request to recover the intended reminder content. "
        "Do not ask follow-up questions. Do not say you are unsure what the reminder means. "
        "Do not say 'I have set a reminder'. Do not add conversational filler."
    )

    if target_user_id == -1:
        audience_directive = "Address both intended users naturally."
    else:
        audience_directive = f"Address the recipient by name as '{user_name}'."

    return (
        "Reminder job to deliver now.\n\n"
        f"Saved reminder prompt:\n{base_prompt}\n\n"
        f"Job description:\n{details}\n\n"
        f"Original user scheduling request:\n{original_request or '[Not captured]'}\n\n"
        f"[SYSTEM DIRECTIVE: {shared_directive} {audience_directive}]"
    )


def get_latest_user_request(chat_id: int | None) -> str | None:
    """Capture the user message that caused a scheduled job to be created."""
    if chat_id is None:
        return None

    for message in reversed(globals.chat_histories.get(chat_id, [])):
        if message.get("role") == "user":
            content = _strip_runtime_context_from_user_message(message.get("content") or "")
            return content or None
    return None

def load_jobs_from_file() -> list:
    """Loads scheduled jobs from the custom_jobs.json file."""
    current_path = Path(JOBS_FILE_PATH)
    legacy_path = Path(LEGACY_JOBS_FILE_PATH)

    if not current_path.exists() and not legacy_path.exists():
        return []

    try:
        current_jobs = []
        if current_path.exists():
            with current_path.open("r", encoding="utf-8") as f:
                current_jobs = json.load(f)
        if not isinstance(current_jobs, list):
            current_jobs = []

        legacy_jobs = []
        if legacy_path.exists():
            with legacy_path.open("r", encoding="utf-8") as f:
                legacy_jobs = json.load(f)
        if not isinstance(legacy_jobs, list):
            legacy_jobs = []

        if (not current_jobs) and legacy_jobs:
            save_jobs_to_file(legacy_jobs)
            logging.info(
                "📅 SCHEDULER: Migrated %s legacy jobs from %s to %s",
                len(legacy_jobs),
                legacy_path,
                current_path,
            )
            current_jobs = legacy_jobs

        deduped_jobs = []
        seen_ids = set()
        for job in reversed(current_jobs):
            job_id = str(job.get("id") or "").strip()
            if not job_id:
                logging.warning("⚠️ SCHEDULER: Skipping persisted job without an id: %r", job)
                continue
            if job_id in seen_ids:
                logging.warning("⚠️ SCHEDULER: Dropping duplicate persisted job id '%s'", job_id)
                continue
            job["message_thread_id"] = normalize_message_thread_id(
                job.get("chat_id"),
                job.get("message_thread_id"),
            )
            seen_ids.add(job_id)
            deduped_jobs.append(job)
        deduped_jobs.reverse()

        if deduped_jobs != current_jobs:
            save_jobs_to_file(deduped_jobs)

        return deduped_jobs
    except Exception as e:
        logging.error(f"❌ SCHEDULER: Error loading jobs file: {e}", exc_info=True)
        return []

def save_jobs_to_file(jobs: list):
    """Saves scheduled jobs to the custom_jobs.json file."""
    try:
        with open(JOBS_FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(jobs, f, indent=2)
    except Exception as e:
        logging.error(f"❌ SCHEDULER: Error saving jobs file: {e}", exc_info=True)

def remove_job_from_store(job_id: str):
    """Removes a job definition from the persistent JSON storage."""
    jobs = load_jobs_from_file()
    updated_jobs = [j for j in jobs if j.get("id") != job_id]
    if len(jobs) != len(updated_jobs):
        save_jobs_to_file(updated_jobs)
        logging.info(f"📅 SCHEDULER: Removed job {job_id} from store")

def _markdown_escape(text: str) -> str:
    return str(text or "").replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _markdown_link(label: str, url: str) -> str:
    return f"[{_markdown_escape(label)}]({url.replace(')', '%29')})"


async def send_safe_job_message(bot, chat_id: int, text: str, message_thread_id: int = None, rich_markdown_text: str = None):
    """Splits large messages to fit Telegram's character limits."""
    if rich_markdown_text is not None:
        return await try_send_rich_or_split_html_message(
            bot,
            chat_id,
            rich_markdown_text,
            fallback_html_text=text,
            message_thread_id=message_thread_id,
            log_prefix="SCHEDULER",
        )

    return await try_send_split_html_message(
        bot,
        chat_id,
        text,
        message_thread_id=message_thread_id,
        log_prefix="SCHEDULER",
    )


async def _send_scheduled_voice_memo(bot, chat_id: int, text: str, message_thread_id: int = None) -> bool:
    """Generate and send a voice memo for scheduled jobs that explicitly request one."""
    from emery.helpers import clean_thinking_tags
    from emery.tools import get_voice_audio, prepare_voice_memo_script

    clean_text = clean_thinking_tags(text or "").strip()
    if not clean_text:
        return False

    voice_text = await prepare_voice_memo_script(clean_text)

    audio = await get_voice_audio(voice_text)
    if not audio:
        logging.warning("⚠️ SCHEDULER: Voice memo TTS returned no audio for chat_id=%s", chat_id)
        return False

    await bot.send_voice(
        chat_id=chat_id,
        voice=audio,
        caption="Voice memo",
        message_thread_id=normalize_message_thread_id(chat_id, message_thread_id),
    )
    return True


async def _execute_custom_job(bot, job_data: dict, *, deferred_reason: str | None = None):
    job_id = job_data.get("id")

    # Restore user context for this task
    user_id = job_data.get("user_id")
    target_user_id = job_data.get("target_user_id")
    target_user_name = job_data.get("target_user_name")

    if target_user_id is not None and target_user_id != -1:
        globals.current_user_id.set(target_user_id)
    elif user_id is not None:
        globals.current_user_id.set(user_id)

    prompt = job_data.get("prompt")
    description = job_data.get("description")
    source_request = job_data.get("source_request")
    chat_id = job_data.get("chat_id")
    message_thread_id = job_data.get("message_thread_id")
    origin_chat_id = job_data.get("origin_chat_id", chat_id)
    origin_message_thread_id = job_data.get("origin_message_thread_id", message_thread_id)
    delivery_scope = job_data.get("delivery_scope")
    schedule_type = job_data.get("schedule_type")
    route_to_routines = job_data.get("route_to_routines", True)

    if delivery_scope:
        chat_id = chat_id or origin_chat_id
        if chat_id is None:
            chat_id, message_thread_id = _resolve_delivery_destination(
                delivery_scope=delivery_scope,
                target_user_id=target_user_id,
                origin_chat_id=origin_chat_id,
                origin_message_thread_id=origin_message_thread_id,
            )
    else:
        # Legacy persisted jobs did not store delivery_scope. Preserve their old routing behavior.
        is_routine = schedule_type in RECURRING_SCHEDULE_TYPES
        if is_routine and route_to_routines:
            if TELEGRAM_GROUP_CHAT_ID is not None:
                chat_id = TELEGRAM_GROUP_CHAT_ID
            else:
                logging.warning(
                    "⚠️ SCHEDULER: Job '%s' (%s) is marked route_to_routines=True, "
                    "but telegram.group_chat_id is not configured. Falling back to stored chat_id=%s.",
                    description,
                    job_id,
                    chat_id,
                )
            if ROUTINES_TOPIC_ID is not None:
                message_thread_id = ROUTINES_TOPIC_ID
            else:
                logging.warning(
                    "⚠️ SCHEDULER: Job '%s' (%s) is marked route_to_routines=True, "
                    "but telegram.routines_topic_id is not configured. Using thread_id=%s.",
                    description,
                    job_id,
                    message_thread_id,
                )
        elif message_thread_id is None and CHAT_TOPIC_ID is not None:
            message_thread_id = CHAT_TOPIC_ID

    if not chat_id:
        chat_id = globals.TARGET_CHAT_ID.get()

    if not chat_id:
        logging.warning("⚠️ Custom job '%s' (%s) triggered without chat_id.", description, job_id)
        return

    globals.TARGET_CHAT_ID.set(chat_id)
    message_thread_id = normalize_message_thread_id(chat_id, message_thread_id)
    globals.CURRENT_THREAD_ID.set(message_thread_id)

    logging.info(
        "📅 SCHEDULER: Resolved destination for job '%s' (%s) -> chat_id=%s thread_id=%s "
        "[schedule_type=%s route_to_routines=%s delivery_scope=%s origin_chat_id=%s origin_thread_id=%s deferred_reason=%s]",
        description,
        job_id,
        chat_id,
        message_thread_id,
        schedule_type,
        route_to_routines,
        delivery_scope or "legacy",
        origin_chat_id,
        origin_message_thread_id,
        deferred_reason or "immediate",
    )

    if schedule_type == "yearly":
        try:
            parts = job_data.get("schedule_value", "").split()
            if parts:
                target_month = int(parts[0].split("-")[0])
                now = datetime.now(USER_TIMEZONE)
                if now.month != target_month:
                    logging.info(
                        "📅 SCHEDULER: Skipping yearly job '%s' (month mismatch: %s != %s)",
                        job_id,
                        now.month,
                        target_month,
                    )
                    return
        except Exception as e:
            logging.error("❌ SCHEDULER: Error filtering month for yearly job %s: %s", job_id, e, exc_info=True)

    logging.info(
        "📅 SCHEDULER: Running custom job '%s' (%s)%s",
        description,
        job_id,
        f" after deferral ({deferred_reason})" if deferred_reason else "",
    )
    from emery.engine import emery_engine
    from emery.helpers import clean_thinking_tags, emery_format, telegram_escape, get_current_system_prompt

    try:
        active_user_id = globals.current_user_id.get() or user_id
        from emery.config import get_user_profile
        profile = get_user_profile(active_user_id)
        user_name = profile["name"]

        mention_prefix = ""
        rich_mention_prefix = ""
        if target_user_id == -1:
            from emery.config import PRIMARY_USER_ID, SECONDARY_USER_ID, USER_NAME, USER_2_NAME
            if SECONDARY_USER_ID != 0:
                mention_prefix = (
                    f"<a href=\"tg://user?id={PRIMARY_USER_ID}\">{telegram_escape(USER_NAME)}</a> & "
                    f"<a href=\"tg://user?id={SECONDARY_USER_ID}\">{telegram_escape(USER_2_NAME)}</a>: "
                )
                rich_mention_prefix = (
                    f"{_markdown_link(USER_NAME, f'tg://user?id={PRIMARY_USER_ID}')} & "
                    f"{_markdown_link(USER_2_NAME, f'tg://user?id={SECONDARY_USER_ID}')}: "
                )
            else:
                mention_prefix = f"<a href=\"tg://user?id={PRIMARY_USER_ID}\">{telegram_escape(USER_NAME)}</a>: "
                rich_mention_prefix = f"{_markdown_link(USER_NAME, f'tg://user?id={PRIMARY_USER_ID}')}: "
        elif target_user_id and target_user_id != 0:
            mention_prefix = f"<a href=\"tg://user?id={target_user_id}\">{telegram_escape(target_user_name)}</a>: "
            rich_mention_prefix = f"{_markdown_link(target_user_name, f'tg://user?id={target_user_id}')}: "

        voice_memo_requested = _requests_voice_memo(prompt, description, source_request)
        exec_prompt = build_scheduled_execution_prompt(
            prompt,
            description,
            voice_memo_requested=voice_memo_requested,
        )
        prompt_text = prompt or ""
        description_text = description or ""
        if schedule_type == "once" or "remind" in description_text.lower() or "remind" in prompt_text.lower():
            exec_prompt = build_reminder_execution_prompt(
                prompt=prompt,
                description=description,
                source_request=source_request,
                user_name=user_name,
                target_user_id=target_user_id,
            )

        if chat_id not in globals.chat_histories:
            globals.chat_histories[chat_id] = deque()

        use_compact_routine_history = _is_routine_history_candidate(
            delivery_scope=delivery_scope,
            schedule_type=schedule_type,
            route_to_routines=route_to_routines,
            prompt=prompt,
            description=description,
        )

        runtime_context = await get_current_system_prompt(exec_prompt, active_user_id)
        now_dt = datetime.now(USER_TIMEZONE)
        scheduled_trigger = {
            "role": "user",
            "content": (
                f"{runtime_context}\n\n"
                "# Scheduled Job Trigger\n"
                f"[{now_dt.strftime('%A, %B %d, %Y at %I:%M %p')}] "
                f"Run scheduled job '{description}' ({job_id}): {exec_prompt}"
            ),
            "user_id": active_user_id,
            "is_scheduled_job_trigger": True,
            "message_thread_id": message_thread_id,
            "timestamp": now_dt,
        }
        job_history = deque([scheduled_trigger])

        res_text, voice_sent_via_tool = await emery_engine(job_history)
        if not use_compact_routine_history:
            globals.chat_histories[chat_id].extend(job_history)
        clean_res_text = clean_thinking_tags(res_text).strip()
        job_html_text = f"🛡️ <b>EMERYCHAT JOB: {telegram_escape(description)}</b>\n\n{mention_prefix}{emery_format(res_text)}"
        job_rich_text = f"# 🛡️ EMERYCHAT JOB: {_markdown_escape(description)}\n\n{rich_mention_prefix}{clean_res_text}"
        delivered_chat_id = chat_id
        delivered_thread_id = message_thread_id
        sent_ok = await send_safe_job_message(
            bot,
            chat_id=chat_id,
            text=job_html_text,
            message_thread_id=message_thread_id,
            rich_markdown_text=job_rich_text,
        )
        if (
            not sent_ok
            and delivery_scope == "personal"
            and origin_chat_id
            and origin_chat_id != chat_id
        ):
            fallback_thread_id = normalize_message_thread_id(origin_chat_id, origin_message_thread_id)
            logging.warning(
                "⚠️ SCHEDULER: Personal reminder '%s' (%s) failed in DM chat_id=%s. "
                "Falling back to origin chat_id=%s thread_id=%s.",
                description,
                job_id,
                chat_id,
                origin_chat_id,
                fallback_thread_id,
            )
            sent_ok = await send_safe_job_message(
                bot,
                chat_id=origin_chat_id,
                text=job_html_text,
                message_thread_id=fallback_thread_id,
                rich_markdown_text=job_rich_text,
            )
            if sent_ok:
                delivered_chat_id = origin_chat_id
                delivered_thread_id = fallback_thread_id
        if not sent_ok:
            logging.warning(
                "⚠️ SCHEDULER: Job '%s' (%s) executed, but delivery to chat_id=%s thread_id=%s failed.",
                description,
                job_id,
                chat_id,
                message_thread_id,
            )
        if voice_memo_requested and not voice_sent_via_tool:
            try:
                voice_ok = await _send_scheduled_voice_memo(
                    bot,
                    delivered_chat_id if sent_ok else (origin_chat_id or chat_id),
                    res_text,
                    delivered_thread_id if sent_ok else (origin_message_thread_id or message_thread_id),
                )
                if not voice_ok:
                    logging.warning(
                        "⚠️ SCHEDULER: Job '%s' (%s) requested a voice memo, but none was sent.",
                        description,
                        job_id,
                    )
            except Exception as e:
                logging.error("❌ SCHEDULER: Failed to send voice memo for job %s: %s", job_id, e, exc_info=True)
        if use_compact_routine_history:
            should_warm_cache = _record_routine_history(
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                job_id=job_id,
                description=description,
                prompt=prompt,
                result_text=res_text,
                now=datetime.now(USER_TIMEZONE),
            )
            if should_warm_cache:
                await _warm_chat_history_after_routines(
                    _routine_history_key(chat_id, message_thread_id),
                    reason="no nearby routine",
                )
        else:
            globals.chat_histories[chat_id].append({
                "role": "assistant",
                "content": res_text,
                "message_thread_id": message_thread_id,
                "timestamp": datetime.now(USER_TIMEZONE),
            })
    except Exception as e:
        logging.error("❌ CUSTOM JOB Error executing job %s: %s", job_id, e, exc_info=True)

    if schedule_type == "once":
        remove_job_from_store(job_id)


async def run_custom_job(context):
    """Callback triggered by APScheduler/JobQueue when a job fires."""
    job_data = context.job.data or {}
    if _foreground_loop_active():
        _defer_scheduled_job(job_data, reason="foreground agent loop active")
        return
    await _execute_custom_job(context.bot, job_data)

def remove_job_from_queue(job_id: str):
    """Removes a job from the active Telegram JobQueue."""
    if globals.application and globals.application.job_queue:
        current_jobs = globals.application.job_queue.get_jobs_by_name(job_id)
        if current_jobs:
            for j in current_jobs:
                j.schedule_removal()
            logging.info(f"📅 SCHEDULER: Cancelled job '{job_id}' in queue")

def schedule_in_tg_queue(job_data: dict, log_registration: bool = True) -> bool:
    """Registers the job in the active Telegram JobQueue based on its configuration."""
    if not globals.application or not globals.application.job_queue:
        logging.warning("⚠️ SCHEDULER: Telegram JobQueue is not initialized yet.")
        return False
        
    job_id = job_data["id"]
    stype = job_data["schedule_type"]
    sval = job_data["schedule_value"]
    
    # Clean up existing instance of job first
    remove_job_from_queue(job_id)
    
    try:
        if stype == "daily":
            # HH:MM format
            hour, minute = map(int, sval.split(":"))
            time_obj = time(hour, minute, tzinfo=USER_TIMEZONE)
            globals.application.job_queue.run_daily(
                run_custom_job,
                time=time_obj,
                data=job_data,
                name=job_id
            )
            if log_registration:
                _log_job_scheduled(job_id, stype, sval, f"at {sval}")
            return True
            
        elif stype == "interval":
            seconds = parse_duration_to_seconds(sval)
            globals.application.job_queue.run_repeating(
                run_custom_job,
                interval=seconds,
                first=seconds,
                data=job_data,
                name=job_id
            )
            if log_registration:
                _log_job_scheduled(job_id, stype, sval, f"every {seconds}s")
            return True
            
        elif stype == "once":
            if "-" in sval or ":" in sval:
                dt_naive = datetime.strptime(sval, "%Y-%m-%d %H:%M:%S")
                if hasattr(USER_TIMEZONE, "localize"):
                    dt_localized = USER_TIMEZONE.localize(dt_naive)
                else:
                    dt_localized = dt_naive.replace(tzinfo=USER_TIMEZONE)
            else:
                seconds = parse_duration_to_seconds(sval)
                dt_localized = datetime.now(USER_TIMEZONE) + timedelta(seconds=seconds)
                
            if dt_localized < datetime.now(USER_TIMEZONE):
                logging.warning(f"⚠️ SCHEDULER: Job '{job_id}' scheduled in the past ({dt_localized}), skipping.")
                remove_job_from_store(job_id)
                return False
                
            globals.application.job_queue.run_once(
                run_custom_job,
                when=dt_localized,
                data=job_data,
                name=job_id
            )
            if log_registration:
                _log_job_scheduled(
                    job_id,
                    stype,
                    sval,
                    f"at {dt_localized.strftime('%Y-%m-%d %H:%M:%S')}",
                )
            return True
            
        elif stype == "weekly":
            # format: "Monday 08:30"
            parts = sval.split()
            day_name, time_str = parts
            weekday_index = WEEKDAYS[day_name.lower()]
            hour, minute = map(int, time_str.split(":"))
            time_obj = time(hour, minute, tzinfo=USER_TIMEZONE)
            globals.application.job_queue.run_daily(
                run_custom_job,
                time=time_obj,
                days=(weekday_index,),
                data=job_data,
                name=job_id
            )
            if log_registration:
                _log_job_scheduled(job_id, stype, sval, f"on {day_name}s at {time_str}")
            return True
            
        elif stype == "monthly":
            # format: "1 08:30"
            parts = sval.split()
            dom_str, time_str = parts
            day_of_month = int(dom_str)
            hour, minute = map(int, time_str.split(":"))
            time_obj = time(hour, minute, tzinfo=USER_TIMEZONE)
            globals.application.job_queue.run_monthly(
                run_custom_job,
                time=time_obj,
                day=day_of_month,
                data=job_data,
                name=job_id
            )
            if log_registration:
                _log_job_scheduled(job_id, stype, sval, f"on day {day_of_month} at {time_str}")
            return True
            
        elif stype == "yearly":
            # format: "12-19 08:30"
            parts = sval.split()
            date_str, time_str = parts
            month, day = map(int, date_str.split("-"))
            hour, minute = map(int, time_str.split(":"))
            time_obj = time(hour, minute, tzinfo=USER_TIMEZONE)
            globals.application.job_queue.run_monthly(
                run_custom_job,
                time=time_obj,
                day=day,
                data=job_data,
                name=job_id
            )
            if log_registration:
                _log_job_scheduled(job_id, stype, sval, f"on {month}-{day} at {time_str}")
            return True
            
    except Exception as e:
        logging.error(f"❌ SCHEDULER: Failed to schedule job {job_id} in Telegram queue: {e}", exc_info=True)
        return False
        
    return False

# --- LLM TOOL CALLABLE FUNCTIONS ---

async def add_scheduled_job(schedule_type: str, schedule_value: str, prompt: str, description: str = None, target_user: str = None, route_to_routines: bool = None) -> str:
    """
    Schedule a new automated job/task.
    
    Parameters:
    - schedule_type: 'daily' (e.g. at 08:30), 'interval' (repeating, e.g. every '1h'), or 'once' (one-time date or offset).
    - schedule_value: The scheduling specification. E.g. '08:30' for daily, '30m' or '3600' for interval, or '2026-05-26 15:30:00' / '15m' for once.
    - prompt: The text prompt the bot executes when the job runs.
    - description: A short, user-friendly label/description of the job.
    - target_user: Optional name or alias this job/reminder is targeted at (e.g. 'Alice', 'Bob', 'me', 'us', or 'both').
    - route_to_routines: Optional boolean. Use True for true routines/automation such as briefings, checks, and monitoring.
    """
    chat_id = globals.TARGET_CHAT_ID.get()
    if not chat_id:
        return "Error: No active chat session to associate with this job. Run this command from within a chat."
    origin_chat_id = chat_id
    origin_message_thread_id = globals.CURRENT_THREAD_ID.get()
        
    if not description:
        description = f"Reminder: {prompt[:30]}..." if len(prompt) > 30 else f"Reminder: {prompt}"

    if route_to_routines is None:
        route_to_routines = False

    stype = schedule_type.lower().strip()
    if stype not in ("daily", "interval", "once", "weekly", "monthly", "yearly"):
        return f"Error: Invalid schedule_type '{schedule_type}'. Must be 'daily', 'interval', 'once', 'weekly', 'monthly', or 'yearly'."
        
    job_id = f"job_{uuid.uuid4().hex[:8]}"
    
    # Basic input validations before writing to store
    try:
        if stype == "daily":
            if ":" not in schedule_value:
                return "Error: For 'daily' job, schedule_value must be in 'HH:MM' 24-hour format."
            hour, minute = map(int, schedule_value.split(":"))
            if not (0 <= hour <= 23) or not (0 <= minute <= 59):
                return "Error: Hours must be 0-23 and minutes 0-59."
        elif stype == "interval":
            parse_duration_to_seconds(schedule_value)
        elif stype == "once":
            clean_schedule_value = str(schedule_value or "").strip()
            if re.match(r"^\d{4}-\d{2}-\d{2}$", clean_schedule_value):
                return _missing_once_time_message(clean_schedule_value)
            if "-" in clean_schedule_value or ":" in clean_schedule_value:
                datetime.strptime(schedule_value, "%Y-%m-%d %H:%M:%S")
            else:
                if not _is_duration_value(clean_schedule_value):
                    return _missing_once_time_message(clean_schedule_value)
                parse_duration_to_seconds(clean_schedule_value)
        elif stype == "weekly":
            parts = schedule_value.split()
            if len(parts) != 2:
                return "Error: For 'weekly' job, schedule_value must be in '<day_name> <HH:MM>' format (e.g. 'Monday 08:30')."
            day_name, time_str = parts
            if day_name.lower() not in WEEKDAYS:
                return f"Error: Invalid weekday name '{day_name}'."
            if ":" not in time_str:
                return "Error: Time portion must be in 'HH:MM' 24-hour format."
            hour, minute = map(int, time_str.split(":"))
            if not (0 <= hour <= 23) or not (0 <= minute <= 59):
                return "Error: Hours must be 0-23 and minutes 0-59."
        elif stype == "monthly":
            parts = schedule_value.split()
            if len(parts) != 2:
                return "Error: For 'monthly' job, schedule_value must be in '<day_of_month> <HH:MM>' format (e.g. '1 12:00')."
            dom_str, time_str = parts
            if not dom_str.isdigit():
                return "Error: Day of month must be a number."
            dom = int(dom_str)
            if not (1 <= dom <= 31):
                return "Error: Day of month must be between 1 and 31."
            if ":" not in time_str:
                return "Error: Time portion must be in 'HH:MM' 24-hour format."
            hour, minute = map(int, time_str.split(":"))
            if not (0 <= hour <= 23) or not (0 <= minute <= 59):
                return "Error: Hours must be 0-23 and minutes 0-59."
        elif stype == "yearly":
            parts = schedule_value.split()
            if len(parts) != 2:
                return "Error: For 'yearly' job, schedule_value must be in '<MM-DD> <HH:MM>' format (e.g. '12-19 08:30')."
            date_str, time_str = parts
            if "-" not in date_str:
                return "Error: Date portion must be in 'MM-DD' format."
            month, day = map(int, date_str.split("-"))
            if not (1 <= month <= 12):
                return "Error: Month must be between 1 and 12."
            if not (1 <= day <= 31):
                return "Error: Day must be between 1 and 31."
            if ":" not in time_str:
                return "Error: Time portion must be in 'HH:MM' 24-hour format."
            hour, minute = map(int, time_str.split(":"))
            if not (0 <= hour <= 23) or not (0 <= minute <= 59):
                return "Error: Hours must be 0-23 and minutes 0-59."
    except Exception as e:
        return f"Error validating schedule_value '{schedule_value}': {e}"
        
    creator_user_id = globals.current_user_id.get()
    
    # Resolve target user
    target_user_id, target_name = _resolve_target_user(target_user, creator_user_id, prompt, description)
    explicit_single_user_target = _has_explicit_single_user_target(target_user, prompt, description)
    delivery_scope = _determine_delivery_scope(
        schedule_type=stype,
        route_to_routines=route_to_routines,
        origin_chat_id=origin_chat_id,
        target_user_id=target_user_id,
        explicit_single_user_target=explicit_single_user_target,
        prompt=prompt,
        description=description,
    )
    delivery_chat_id, delivery_thread_id = _resolve_delivery_destination(
        delivery_scope=delivery_scope,
        target_user_id=target_user_id,
        origin_chat_id=origin_chat_id,
        origin_message_thread_id=origin_message_thread_id,
    )

    source_request = get_latest_user_request(chat_id)

    job_data = {
        "id": job_id,
        "schedule_type": stype,
        "schedule_value": schedule_value,
        "prompt": prompt,
        "description": description,
        "chat_id": delivery_chat_id,
        "message_thread_id": delivery_thread_id,
        "origin_chat_id": origin_chat_id,
        "origin_message_thread_id": origin_message_thread_id,
        "delivery_scope": delivery_scope,
        "route_to_routines": route_to_routines,
        "user_id": creator_user_id,
        "target_user_id": target_user_id,
        "target_user_name": target_name,
        "source_request": source_request,
        "created_at": datetime.now(USER_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    }
    
    # Save definition to persistent store
    jobs = load_jobs_from_file()
    jobs.append(job_data)
    save_jobs_to_file(jobs)
    
    # Schedule inside Telegram's job queue
    success = schedule_in_tg_queue(job_data)
    if success:
        return f"Successfully scheduled job '{description}' (ID: {job_id}) to run with trigger '{stype}' and schedule '{schedule_value}'."
    else:
        # Rollback from store if scheduling failed
        remove_job_from_store(job_id)
        return "Error: Failed to register job in Telegram's active job queue."

async def list_scheduled_jobs() -> str:
    """Lists all active custom scheduled jobs."""
    jobs = load_jobs_from_file()
    if not jobs:
        return "No custom scheduled jobs are currently configured."
        
    lines = ["Here are the currently configured custom scheduled jobs:"]
    for j in jobs:
        lines.append(
            f"📌 **{j['description']}** (ID: `{j['id']}`)\n"
            f"   - **Trigger**: {j['schedule_type'].upper()} ({j['schedule_value']})\n"
            f"   - **Prompt**: \"{j['prompt']}\"\n"
            f"   - **Created At**: {j['created_at']}"
        )
    return "\n\n".join(lines)

async def remove_scheduled_job(job_id: str) -> str:
    """Cancels and removes a custom job by its unique ID."""
    job_id = job_id.strip()
    jobs = load_jobs_from_file()
    job_to_remove = next((j for j in jobs if j["id"] == job_id), None)
    
    if not job_to_remove:
        return f"Error: No scheduled job found with ID '{job_id}'."
        
    # Cancel in telegram queue
    remove_job_from_queue(job_id)
    
    # Remove from persistent JSON store
    remove_job_from_store(job_id)
    
    return f"Successfully cancelled and removed job '{job_to_remove['description']}' (ID: `{job_id}`)."

# --- STARTUP FUNCTION ---

def load_and_register_all_jobs():
    """Loads all saved custom jobs from file and schedules them in the queue on boot."""
    jobs = load_jobs_from_file()
    count = 0
    schedule_counts = {}
    for job_data in jobs:
        if schedule_in_tg_queue(job_data, log_registration=False):
            count += 1
            schedule_type = str(job_data.get("schedule_type") or "unknown").lower()
            schedule_counts[schedule_type] = schedule_counts.get(schedule_type, 0) + 1
    if jobs:
        logging.info(
            "📅 SCHEDULER: Loaded %s/%s persistent jobs | %s",
            count,
            len(jobs),
            _format_schedule_type_counts(schedule_counts),
        )
    else:
        logging.info("📅 SCHEDULER: No persistent jobs configured")

def update_jobs_with_chat_id(chat_id: int):
    """
    Checks if any persistent scheduled jobs have a null/missing chat_id,
    updates them with the provided chat_id, saves the file, and reschedules them.
    This dynamically registers placeholder default jobs once the first user message is received.
    """
    jobs = load_jobs_from_file()
    updated = False
    for job_data in jobs:
        if job_data.get("delivery_scope") and job_data.get("origin_chat_id") is None:
            job_data["origin_chat_id"] = chat_id
            logging.info(f"📅 SCHEDULER: Associated scoped job '{job_data['id']}' origin with chat {chat_id}")
            updated = True
        elif job_data.get("delivery_scope") is None and job_data.get("chat_id") is None:
            job_data["chat_id"] = chat_id
            logging.info(f"📅 SCHEDULER: Associated job '{job_data['id']}' with chat {chat_id}")
            # Reschedule it so it uses the updated chat_id in memory
            schedule_in_tg_queue(job_data)
            updated = True
            
    if updated:
        save_jobs_to_file(jobs)
