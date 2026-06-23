from collections import deque
import contextvars
import asyncio
import logging

import httpx

from emery.config import TELEGRAM_GROUP_CHAT_ID

chat_histories = {}
group_chat_id = TELEGRAM_GROUP_CHAT_ID

TARGET_CHAT_ID = contextvars.ContextVar("TARGET_CHAT_ID", default=group_chat_id)
CURRENT_THREAD_ID = contextvars.ContextVar("CURRENT_THREAD_ID", default=None)

http_client = httpx.AsyncClient(timeout=900, verify=False, follow_redirects=True)
application_bot = None  # Populated dynamically by main.py
application = None      # Populated dynamically by main.py
reolink_thread_trackers = {}  # Tracks camera alerts: camera_name -> {"message_id": int, "timestamp": datetime}
chat_reply_targets = {}       # Tracks custom reply message ID per chat: chat_id -> message_id
current_user_id = contextvars.ContextVar("current_user_id", default=None)
chat_debounce_tasks = {}  # Tracks active debounce timers: chat_id -> asyncio.Task
active_foreground_loops = {}  # Tracks foreground agent loops: loop_id -> metadata


# Concurrency locks to protect Ollama endpoints from concurrent load
main_model_lock = asyncio.Semaphore(1)
fast_model_lock = asyncio.Semaphore(1)
reolink_snapshot_lock = asyncio.Lock()

learned_stickers = {}  # Tracks learned sticker file IDs: emoji -> file_id


def register_foreground_loop(loop_id: str, **metadata) -> None:
    if not loop_id:
        return
    active_foreground_loops[loop_id] = dict(metadata or {})


def unregister_foreground_loop(loop_id: str) -> bool:
    if not loop_id:
        return False

    removed = active_foreground_loops.pop(loop_id, None)
    became_idle = removed is not None and not active_foreground_loops
    if not became_idle:
        return False

    try:
        from emery.scheduler import trigger_deferred_job_drain

        trigger_deferred_job_drain(reason=f"foreground loop completed: {loop_id}")
    except Exception as exc:
        logging.debug("SCHEDULER: Unable to trigger deferred drain after loop exit: %s", exc)
    return True


def has_active_foreground_loops() -> bool:
    return bool(active_foreground_loops)
