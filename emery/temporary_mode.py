"""Per-chat/thread controls for privacy-preserving temporary conversations."""

import copy
from collections import deque

import emery.globals as globals
from emery.telegram_utils import normalize_message_thread_id


_temporary_scopes: set[tuple[int, int | None]] = set()
_temporary_history_backups: dict[tuple[int, int | None], list[dict]] = {}


def scope_key(chat_id: int | None = None, thread_id: int | None = None) -> tuple[int, int | None] | None:
    if chat_id is None:
        chat_id = globals.TARGET_CHAT_ID.get()
    if chat_id is None:
        return None
    try:
        chat_id = int(chat_id)
    except (TypeError, ValueError):
        return None
    return chat_id, normalize_message_thread_id(chat_id, thread_id if thread_id is not None else globals.CURRENT_THREAD_ID.get())


def is_temporary_mode(chat_id: int | None = None, thread_id: int | None = None) -> bool:
    key = scope_key(chat_id, thread_id)
    return key in _temporary_scopes if key is not None else False


def is_chat_temporary_mode(chat_id: int | None = None) -> bool:
    key = scope_key(chat_id, None)
    if key is None:
        return False
    return any(chat_key == key[0] for chat_key, _thread_key in _temporary_scopes)


def set_temporary_mode(chat_id: int, thread_id: int | None, enabled: bool) -> bool:
    key = scope_key(chat_id, thread_id)
    if key is None:
        return False
    if enabled:
        if key in _temporary_scopes:
            return True
        history = globals.chat_histories.get(key[0])
        _temporary_history_backups[key] = copy.deepcopy(list(history or []))
        _temporary_scopes.add(key)
        clear_temporary_history(*key)
    else:
        if key not in _temporary_scopes:
            return True
        _temporary_scopes.discard(key)
        history = globals.chat_histories.get(key[0])
        if history is None:
            history = deque()
            globals.chat_histories[key[0]] = history
        history.clear()
        history.extend(copy.deepcopy(_temporary_history_backups.pop(key, [])))
    return True


def clear_temporary_history(chat_id: int, thread_id: int | None) -> int:
    """Remove history for this temporary scope so it cannot cross the boundary."""
    history = globals.chat_histories.get(chat_id)
    if history is None:
        return 0
    normalized_thread_id = normalize_message_thread_id(chat_id, thread_id)
    kept = [
        message for message in history
        if normalize_message_thread_id(chat_id, message.get("message_thread_id")) != normalized_thread_id
    ]
    removed = len(history) - len(kept)
    history.clear()
    history.extend(kept)
    return removed


def temporary_history(history, chat_id: int, thread_id: int | None):
    """Return only the active temporary scope's history for model input."""
    normalized_thread_id = normalize_message_thread_id(chat_id, thread_id)
    return type(history)(
        message for message in history
        if normalize_message_thread_id(chat_id, message.get("message_thread_id")) == normalized_thread_id
    )
