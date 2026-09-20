"""Session-scoped and turn-scoped context for Emery.

History is a record of the conversation, not a snapshot of the current clock,
memory search, or scratchpad.  This module keeps those two kinds of data
separate so the engine can add the appropriate context at request time.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from emery.config import (
    ENABLE_MEMORY,
    USER_LOCATION,
    USER_RELATIONSHIP,
    USER_TIMEZONE,
    USER_NAME,
    USER_2_NAME,
    SECONDARY_USER_ID,
    get_user_profile,
)
from emery.telegram_utils import normalize_message_thread_id
import emery.globals as globals
from emery.temporary_mode import is_temporary_mode


@dataclass(frozen=True)
class SessionContext:
    """Query-independent context for one chat/thread/session variant."""

    cache_key: tuple
    chat_id: int | None
    thread_id: int | None
    user_id: int | None
    session_variant: str
    is_group: bool
    stable_prompt: str
    prompt: str


@dataclass(frozen=True)
class TurnContext:
    """Volatile context computed for one model turn."""

    session: SessionContext
    user_query: str
    user_id: int | None
    created_at: datetime
    prompt: str


def _coerce_chat_id(chat_id: int | None) -> int | None:
    if chat_id is None:
        return None
    try:
        return int(chat_id)
    except (TypeError, ValueError):
        return None


def _coerce_user_id(user_id: int | None) -> int | None:
    if user_id is None:
        return None
    try:
        return int(user_id)
    except (TypeError, ValueError):
        return None


def _variant(session_variant: Any) -> str:
    """Return a bounded, deterministic cache-key variant."""
    value = str(session_variant or "default").strip()
    return value[:120] or "default"


def make_session_context_key(
    chat_id: int | None = None,
    thread_id: int | None = None,
    user_id: int | None = None,
    session_variant: str | None = None,
) -> tuple:
    """Build a privacy-safe cache key.

    Telegram group IDs are negative.  A group key intentionally excludes the
    requester ID because group session context is shared and must not contain
    requester-specific data.  Private-chat keys include the user ID so profile
    context cannot cross user sessions.
    """
    chat_id = _coerce_chat_id(chat_id)
    thread_id = normalize_message_thread_id(chat_id, thread_id) if chat_id is not None else None
    variant = _variant(session_variant)
    if chat_id is not None and chat_id < 0:
        return ("group", chat_id, thread_id, variant)
    return ("private", chat_id, thread_id, _coerce_user_id(user_id), variant)


def clear_session_context_cache(
    chat_id: int | None = None,
    thread_id: int | None = None,
) -> int:
    """Clear all cached session variants, optionally limited to a scope."""
    if chat_id is None:
        removed = len(globals.session_context_cache)
        globals.session_context_cache.clear()
        return removed

    chat_id = _coerce_chat_id(chat_id)
    thread_id = normalize_message_thread_id(chat_id, thread_id)
    keys = [
        key for key in globals.session_context_cache
        if len(key) >= 3 and key[1] == chat_id and key[2] == thread_id
    ]
    for key in keys:
        globals.session_context_cache.pop(key, None)
    return len(keys)


def _group_privacy_prompt() -> str:
    return (
        "\n- This is a shared group chat. Do not disclose private or sensitive "
        "information from any user's one-on-one conversations or memories."
    )


def _build_session_prompt(
    chat_id: int | None,
    user_id: int | None,
    is_group: bool,
    *,
    temporary: bool = False,
) -> str:
    """Build only data that is stable for the session and safe for its scope."""
    if temporary:
        return (
            "# Session Context\n"
            "Temporary mode is active. Do not use, reveal, or create long-term memory, "
            "persistent scratchpad notes, or topic summaries. Use only this turn's conversation context."
        )

    # A group session is intentionally generic.  Private profile data and
    # timezone belong in the cache-friendly private session context.
    if is_group:
        return "# Session Context\n" + _group_privacy_prompt()

    profile = get_user_profile(user_id)
    relationship_line = ""
    if SECONDARY_USER_ID != 0 and USER_RELATIONSHIP:
        relationship_line = f"\n- {USER_NAME} and {USER_2_NAME} are {USER_RELATIONSHIP}."

    return (
        "# Session Context\n"
        "This context is independent of the newest user message.\n\n"
        "# Context & Profile\n"
        f"- Location: {USER_LOCATION}\n"
        f"- Timezone: {USER_TIMEZONE}\n"
        f"- User's name: {profile['name']}\n"
        f"- User's birthday: {profile['birthday']}\n"
        f"- User's family: {profile['family']}\n"
        f"- User's profession: {profile['profession']}"
        f"{relationship_line}"
    )


async def get_session_context(
    chat_id: int | None = None,
    thread_id: int | None = None,
    user_id: int | None = None,
    *,
    session_variant: str | None = None,
    force_refresh: bool = False,
) -> SessionContext:
    """Get cached query-independent context for a chat/thread.

    ``chat_id``, ``thread_id``, and ``user_id`` default to the active context
    vars, which makes this safe for scheduled and background tasks as well as
    Telegram handlers.
    """
    if chat_id is None:
        chat_id = globals.TARGET_CHAT_ID.get()
    if thread_id is None:
        thread_id = globals.CURRENT_THREAD_ID.get()
    if user_id is None:
        user_id = globals.current_user_id.get()

    chat_id = _coerce_chat_id(chat_id)
    thread_id = normalize_message_thread_id(chat_id, thread_id) if chat_id is not None else None
    user_id = _coerce_user_id(user_id)
    temporary = is_temporary_mode(chat_id, thread_id)
    variant = _variant(session_variant or ("temporary" if temporary else "default"))
    key = make_session_context_key(chat_id, thread_id, user_id, variant)
    if not force_refresh:
        cached = globals.session_context_cache.get(key)
        if cached is not None:
            return cached

    is_group = chat_id is not None and chat_id < 0
    # Never store requester-specific data in a group SessionContext.  The
    # context object itself also reports no user ID for that scope.
    from emery.helpers import get_stable_system_prompt

    context = SessionContext(
        cache_key=key,
        chat_id=chat_id,
        thread_id=thread_id,
        user_id=None if is_group else user_id,
        session_variant=variant,
        is_group=is_group,
        stable_prompt=get_stable_system_prompt(temporary=temporary),
        prompt=_build_session_prompt(chat_id, user_id, is_group, temporary=temporary),
    )
    globals.session_context_cache[key] = context
    return context


async def _load_relevant_memories(query: str, user_id: int | None) -> str:
    if not ENABLE_MEMORY or user_id is None:
        return ""
    from emery.memory import retrieve_relevant_memories
    return await retrieve_relevant_memories(query, user_id)


async def get_turn_context(
    user_query: str = "",
    user_id: int | None = None,
    *,
    session: SessionContext | None = None,
    chat_id: int | None = None,
    thread_id: int | None = None,
) -> TurnContext:
    """Build volatile per-turn context without caching query/user data."""
    if user_id is None:
        user_id = globals.current_user_id.get()
    user_id = _coerce_user_id(user_id)
    if session is None:
        session = await get_session_context(chat_id, thread_id, user_id)

    now = datetime.now(USER_TIMEZONE)
    sections = [
        "# Dynamic Runtime Context",
        "This context is current for this request. It is not the user's newest message.",
        f"- Current date and time: {now.strftime('%A, %B %d, %Y at %I:%M %p')}",
    ]

    # Profile details and timezone live in the cache-friendly session context.
    # Group sessions deliberately omit requester-specific memory details.
    from emery.helpers import get_active_holiday_info

    holiday_info = get_active_holiday_info(now.date())
    if holiday_info:
        sections.append("\n# Dynamic Event Alerts" + holiday_info)

    if not session.is_group:
        memories = await _load_relevant_memories(str(user_query or ""), user_id)
        if memories:
            sections.append(f"\n# Long-Term Persistent Memory\n{memories}")

    # Scratchpad contents are intentionally tool-only.  A recent-use reminder
    # preserves discoverability without copying working notes into every prompt.
    if not is_temporary_mode(session.chat_id, session.thread_id):
        from emery.scratchpad import get_recent_scratchpad_reminder
        scratchpad_reminder = get_recent_scratchpad_reminder()
        if scratchpad_reminder:
            sections.append(f"\n# Working Context Reminder\n- {scratchpad_reminder}")

    return TurnContext(
        session=session,
        user_query=str(user_query or ""),
        user_id=user_id,
        created_at=now,
        prompt="\n".join(sections),
    )


def get_current_session_context() -> SessionContext | None:
    return globals.CURRENT_SESSION_CONTEXT.get()


def get_current_turn_context() -> TurnContext | None:
    return globals.CURRENT_TURN_CONTEXT.get()


def set_current_context(session: SessionContext, turn: TurnContext):
    """Bind context to the current async task and return reset tokens."""
    return (
        globals.CURRENT_SESSION_CONTEXT.set(session),
        globals.CURRENT_TURN_CONTEXT.set(turn),
    )
