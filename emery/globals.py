from collections import deque
import contextvars
import asyncio
import heapq
import itertools
import logging
import time

import httpx

from emery.config import TELEGRAM_GROUP_CHAT_ID

chat_histories = {}
group_chat_id = TELEGRAM_GROUP_CHAT_ID

TARGET_CHAT_ID = contextvars.ContextVar("TARGET_CHAT_ID", default=group_chat_id)
CURRENT_THREAD_ID = contextvars.ContextVar("CURRENT_THREAD_ID", default=None)
# Runtime context is deliberately kept out of ``chat_histories``.  The engine
# consumes these values for the active task; history remains durable, compact,
# and compatible with entries written by older Emery versions.
CURRENT_SESSION_CONTEXT = contextvars.ContextVar("CURRENT_SESSION_CONTEXT", default=None)
CURRENT_TURN_CONTEXT = contextvars.ContextVar("CURRENT_TURN_CONTEXT", default=None)
CURRENT_MEDIA_TURN = contextvars.ContextVar("CURRENT_MEDIA_TURN", default=None)

# Immutable SessionContext instances are cached by emery.session_context.  The
# cache is process-local and keyed by chat/thread/variant; group-chat keys do
# not contain a user ID, so one user's private profile can never be returned
# as another user's group context.
SESSION_CONTEXT_CACHE = {}
session_context_cache = SESSION_CONTEXT_CACHE

http_client = httpx.AsyncClient(timeout=900, verify=False, follow_redirects=True)
application_bot = None  # Populated dynamically by main.py
application = None      # Populated dynamically by main.py
reolink_thread_trackers = {}  # Tracks camera alerts: camera_name -> {"message_id": int, "timestamp": datetime}
chat_reply_targets = {}       # Tracks custom reply message ID per chat: chat_id -> message_id
current_user_id = contextvars.ContextVar("current_user_id", default=None)
chat_debounce_tasks = {}  # Tracks active debounce timers: chat_id -> asyncio.Task
active_turns = {}  # Tracks active normal chat turns: (chat_id, thread_id) -> ActiveTurnState
background_image_tasks = set()
active_foreground_loops = {}  # Tracks foreground agent loops: loop_id -> metadata
# One ordered three-message status stack per chat/thread while a turn runs.
# Its messages are edited in place and deleted after the final response.
persistent_status_messages = {}
# Active controllers let command approvals render inside the same ordered
# status message instead of posting a second, interleaved Telegram message.
persistent_status_controllers = {}


# Concurrency locks to protect Ollama endpoints from concurrent load
main_model_lock = asyncio.Semaphore(1)
fast_model_lock = asyncio.Semaphore(1)
reolink_snapshot_lock = asyncio.Lock()
image_generation_pipeline_lock = asyncio.Lock()


class PriorityModelScheduler:
    """Serialize local-model work while letting foreground requests jump ahead."""

    _PRIORITIES = {"user": 0, "background": 10}

    def __init__(self):
        self._condition = asyncio.Condition()
        self._queue = []
        self._sequence = itertools.count()
        self._worker = None

    async def submit(self, operation, *, priority: str = "user", label: str = "model"):
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        enqueued_at = time.perf_counter()
        priority_value = self._PRIORITIES.get(str(priority or "user").lower(), 0)
        async with self._condition:
            heapq.heappush(
                self._queue,
                (priority_value, next(self._sequence), future, operation, label, enqueued_at),
            )
            if self._worker is None or self._worker.done():
                self._worker = asyncio.create_task(self._drain())
        try:
            return await future
        except asyncio.CancelledError:
            future.cancel()
            raise

    async def _drain(self):
        while True:
            async with self._condition:
                item = None
                while self._queue:
                    candidate = heapq.heappop(self._queue)
                    if not candidate[2].cancelled():
                        item = candidate
                        break
                if item is None:
                    self._worker = None
                    return

            _, _, future, operation, label, enqueued_at = item
            queue_wait = time.perf_counter() - enqueued_at
            logging.info(
                "🧠 MODEL QUEUE: starting label=%s queue_wait=%.2fs priority=%s pending=%s",
                label,
                queue_wait,
                "user" if item[0] == 0 else "background",
                len(self._queue),
            )
            try:
                result = await operation()
            except Exception as exc:
                if not future.cancelled():
                    future.set_exception(exc)
            else:
                if not future.cancelled():
                    future.set_result(result)


priority_model_scheduler = PriorityModelScheduler()

learned_stickers = {}  # Tracks learned sticker file IDs: emoji -> file_id


class ActiveTurnState:
    """Mutable coordination state for one steerable normal-chat turn."""

    def __init__(self, *, chat_id: int, thread_id=None, max_pending: int = 4):
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.pending_messages = deque()
        self.max_pending = max(1, int(max_pending))
        self.steer_event = asyncio.Event()
        self.completion_id_event = asyncio.Event()
        self.completion_id = None
        self.control_sent = False
        self.stream_active = False
        self.accepting = True
        self.notify = None
        self.refresh_progress = None


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
