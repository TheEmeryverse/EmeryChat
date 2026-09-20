"""Small persistent scratchpads scoped to the current Telegram chat/thread."""

import json
import logging
import os
import re
import threading
from datetime import datetime
from pathlib import Path

from emery.telegram_utils import normalize_message_thread_id
import emery.globals as globals


SCRATCHPAD_STORE_PATH = os.getenv("SCRATCHPAD_STORE_PATH", "data/scratchpad/scratchpad_store.json")
MAX_SCRATCHPAD_NOTES = 100
MAX_NOTE_CHARS = 800
MAX_SCRATCHPAD_CHARS = 24000
MAX_SCRATCHPAD_PROMPT_CHARS = 8000
_STORE_LOCK = threading.RLock()


def _scope() -> tuple[int | None, int | None]:
    chat_id = globals.TARGET_CHAT_ID.get()
    if chat_id is None:
        return None, None
    return chat_id, normalize_message_thread_id(chat_id, globals.CURRENT_THREAD_ID.get())


def _scope_key(chat_id: int, thread_id: int | None) -> str:
    return f"{chat_id}:{thread_id if thread_id is not None else 'main'}"


def _clean_note(note: str) -> str:
    clean = re.sub(r"\s+", " ", str(note or "")).strip()
    if len(clean) > MAX_NOTE_CHARS:
        clean = clean[: MAX_NOTE_CHARS - 1].rstrip() + "…"
    return clean


def _load_store() -> dict:
    path = Path(SCRATCHPAD_STORE_PATH).expanduser()
    try:
        if not path.exists():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception as exc:
        logging.warning("SCRATCHPAD: Unable to load store %s: %s", path, exc)
        return {}


def _save_store(store: dict) -> None:
    path = Path(SCRATCHPAD_STORE_PATH).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(store, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _current_notes(store: dict) -> tuple[str | None, list[dict] | None]:
    chat_id, thread_id = _scope()
    if chat_id is None:
        return None, None
    key = _scope_key(chat_id, thread_id)
    bucket = store.get(key) or {}
    notes = bucket.get("notes") if isinstance(bucket, dict) else []
    return key, notes if isinstance(notes, list) else []


def _current_turn_count() -> int:
    """Count user turns in the active chat/thread history."""
    chat_id, thread_id = _scope()
    if chat_id is None:
        return 0

    history = globals.chat_histories.get(chat_id, [])
    count = 0
    for message in history:
        if message.get("role") != "user":
            continue
        message_thread_id = normalize_message_thread_id(
            chat_id, message.get("message_thread_id")
        )
        if message_thread_id == thread_id:
            count += 1
    return count


def _parse_created_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def get_recent_scratchpad_reminder(max_turns: int = 10) -> str:
    """Return a reminder when a note was written within recent chat turns."""
    from emery.temporary_mode import is_temporary_mode
    if is_temporary_mode():
        return ""
    if max_turns < 1:
        return ""

    with _STORE_LOCK:
        store = _load_store()
        key, notes = _current_notes(store)
    if key is None or not notes:
        return ""

    chat_id, thread_id = _scope()
    current_turn_count = _current_turn_count()
    recent_turn_floor = max(0, current_turn_count - max_turns)
    recent_turns = [
        message for message in globals.chat_histories.get(chat_id, [])
        if message.get("role") == "user"
        and normalize_message_thread_id(chat_id, message.get("message_thread_id")) == thread_id
    ][-max_turns:]
    if not recent_turns:
        return ""
    oldest_timestamp = recent_turns[0].get("timestamp")

    for note in reversed(notes):
        note_turn_count = note.get("turn_count")
        if isinstance(note_turn_count, int):
            # A turn count greater than the current history count indicates
            # that the process history was reset after this note was written.
            if note_turn_count < 0 or note_turn_count > current_turn_count:
                continue
            if note_turn_count >= recent_turn_floor:
                created_at = _parse_created_at(note.get("created_at"))
                if created_at is not None and isinstance(oldest_timestamp, datetime):
                    try:
                        if created_at < oldest_timestamp:
                            continue
                    except TypeError:
                        continue
                return "The chat/thread scratchpad has been used recently; call `read_scratchpad` if its working notes may help."
            continue

        # Backward-compatible fallback for notes created before turn_count
        # was recorded: compare the note timestamp with the oldest recent turn.
        created_at = _parse_created_at(note.get("created_at"))
        if created_at is None:
            continue
        if isinstance(oldest_timestamp, datetime):
            try:
                if created_at >= oldest_timestamp:
                    return "The chat/thread scratchpad has been used recently; call `read_scratchpad` if its working notes may help."
            except TypeError:
                # Avoid treating incomparable naive/aware timestamps as recent.
                continue
    return ""


def get_scratchpad_snapshot() -> str:
    """Return a bounded prompt section for the current chat/thread."""
    from emery.temporary_mode import is_temporary_mode
    if is_temporary_mode():
        return ""
    with _STORE_LOCK:
        store = _load_store()
        key, notes = _current_notes(store)
    if key is None or not notes:
        return ""

    lines = [f"\n\n# Current Chat Scratchpad\nThese are temporary working notes for this chat/thread ({len(notes)} notes):"]
    selected = []
    used_chars = len(lines[0])
    for note in reversed(notes):
        label = f" [{note.get('title')}]" if note.get("title") else ""
        line = f"- {note.get('id', 'N?')}{label}: {note.get('text', '')}"
        if selected and used_chars + len(line) + 1 > MAX_SCRATCHPAD_PROMPT_CHARS:
            break
        selected.append(line)
        used_chars += len(line) + 1
    lines.extend(reversed(selected))
    omitted = len(notes) - len(selected)
    if omitted:
        lines.append(f"- {omitted} older note(s) are omitted from this automatic snapshot; call `read_scratchpad` to load all notes.")
    lines.append("Use these as working context, verify them when freshness or accuracy matters, and do not treat them as durable personal memory.")
    return "\n".join(lines)


async def jot_down_note(note: str, title: str = "") -> str:
    """Save one short scratchpad note for the current chat/thread."""
    from emery.temporary_mode import is_temporary_mode
    if is_temporary_mode():
        return "The scratchpad is disabled in temporary mode."
    clean = _clean_note(note)
    if not clean:
        return "No note was saved because the note was empty."

    title = _clean_note(title)[:120]
    normalized = re.sub(r"[^a-z0-9]+", " ", clean.lower()).strip()
    with _STORE_LOCK:
        store = _load_store()
        key, notes = _current_notes(store)
        if key is None:
            return "No note was saved because there is no active chat context."
        notes = notes or []
        for existing in notes:
            if existing.get("normalized") == normalized:
                return f"That note is already on the scratchpad as {existing.get('id', 'an existing note')}."
        current_chars = sum(len(str(existing.get("text") or "")) for existing in notes)
        if len(notes) >= MAX_SCRATCHPAD_NOTES:
            return f"The scratchpad is full ({MAX_SCRATCHPAD_NOTES} notes). Read or clear it before adding more."
        if current_chars + len(clean) > MAX_SCRATCHPAD_CHARS:
            return f"The scratchpad has reached its {MAX_SCRATCHPAD_CHARS}-character limit. Read or clear it before adding more."

        entry = {
            "id": f"N{len(notes) + 1}",
            "title": title,
            "text": clean,
            "normalized": normalized,
            "created_at": datetime.now().astimezone().replace(microsecond=0).isoformat(),
            "turn_count": _current_turn_count(),
        }
        notes.append(entry)
        store[key] = {
            "chat_id": key.split(":", 1)[0],
            "thread_id": key.split(":", 1)[1],
            "notes": notes,
        }
        _save_store(store)
    return f"Saved scratchpad note {entry['id']}."


async def read_scratchpad() -> str:
    """Return the current chat/thread scratchpad in model-readable form."""
    from emery.temporary_mode import is_temporary_mode
    if is_temporary_mode():
        return "The scratchpad is disabled in temporary mode."
    with _STORE_LOCK:
        store = _load_store()
        key, notes = _current_notes(store)
    if key is None:
        return "No active chat context; the scratchpad is unavailable."
    if not notes:
        return "The scratchpad is empty."

    lines = [f"Scratchpad ({len(notes)} notes):"]
    for note in notes:
        label = f" — {note.get('title')}" if note.get("title") else ""
        lines.append(f"{note.get('id', 'N?')}{label}: {note.get('text', '')}")
    return "\n".join(lines)


async def clear_scratchpad() -> str:
    """Clear only the current chat/thread scratchpad."""
    with _STORE_LOCK:
        store = _load_store()
        key, notes = _current_notes(store)
        if key is None:
            return "There is no active chat context to clear."
        if not notes:
            return "The scratchpad is already empty."
        store.pop(key, None)
        _save_store(store)
    return "Cleared the current chat/thread scratchpad."
