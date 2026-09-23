"""Coordination between request-scoped image generation and normal chat."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from collections import deque
from typing import Any

from emery.config import LIVE_PROGRESS_HEARTBEAT_INTERVAL_SECONDS, MODEL_ID
from emery.image_profiles import DIRECT_IMAGE_DEFAULT_PROFILE
from emery.telegram_delivery import TelegramLiveProgress
from emery.telegram_utils import normalize_message_thread_id
import emery.globals as globals


log = logging.getLogger(__name__)
_ACTIVE: dict[tuple[int, int | None], "ImageGenerationState"] = {}


def _set_event() -> asyncio.Event:
    event = asyncio.Event()
    event.set()
    return event


@dataclass
class ImageGenerationJob:
    prompt: str
    batch_size: int
    bot: Any
    quality_profile: str = DIRECT_IMAGE_DEFAULT_PROFILE
    orientation: str | None = None
    input_image_bytes: bytes | None = None
    reply_to_message_id: int | None = None
    caption_prefix: str | None = None
    item_offset: int = 0
    batch_total: int | None = None


@dataclass
class ImageGenerationState:
    bot: Any
    chat_id: int
    thread_id: int | None
    total: int
    notifier: TelegramLiveProgress
    started_at: float = field(default_factory=time.monotonic)
    completed: int = 0
    active: bool = True
    pending_entries: list[dict[str, Any]] = field(default_factory=list)
    latest_update: Any = None
    latest_context: Any = None
    latest_user_id: int | None = None
    deferred_count: int = 0
    paused: bool = False
    cancel_requested: bool = False
    current_job_active: bool = False
    resume_event: asyncio.Event = field(default_factory=_set_event)
    pause_event: asyncio.Event = field(default_factory=asyncio.Event)
    pause_ready_event: asyncio.Event = field(default_factory=_set_event)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    progress_item: int = 0
    progress_value: int | None = None
    progress_max: int | None = None
    progress_status: str = "starting"
    progress_node: str | None = None
    pipeline_waiting: bool = False
    main_model_restored: bool = False
    image_jobs: deque[ImageGenerationJob] = field(default_factory=deque)
    queued_image_requests: int = 0
    accepting_jobs: bool = True
    worker_task: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    done_event: asyncio.Event = field(default_factory=asyncio.Event)
    heartbeat_task: asyncio.Task | None = None

    @property
    def key(self) -> tuple[int, int | None]:
        return self.chat_id, self.thread_id

    @property
    def queued_count(self) -> int:
        return len(self.pending_entries)


def _key(chat_id: int, thread_id: int | None) -> tuple[int, int | None]:
    return chat_id, normalize_message_thread_id(chat_id, thread_id)


def get_active_image_generation(chat_id: int, thread_id: int | None) -> ImageGenerationState | None:
    state = _ACTIVE.get(_key(chat_id, thread_id))
    return state if state and state.active else None


def _elapsed_text(state: ImageGenerationState) -> str:
    elapsed = max(0, int(time.monotonic() - state.started_at))
    minutes, seconds = divmod(elapsed, 60)
    return f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"


def _progress_text(
    state: ImageGenerationState,
    *,
    complete: bool = False,
    error: BaseException | None = None,
) -> str:
    if state.cancel_requested and complete:
        headline = "🛑 Image jobs cancelled."
    elif state.paused and error is None:
        headline = "⏸️ Image jobs paused."
    elif error is not None:
        headline = "⚠️ Image generation failed."
    elif complete:
        headline = "✅ Image generation complete."
    else:
        headline = f"🖼️ Image generation: {state.completed}/{state.total} ready."

    lines = [headline]
    if state.paused:
        if state.current_job_active:
            lines.append("The active image is stopping; completed images are kept and the batch continues with /image resume.")
        else:
            lines.append("Queued batches will start after /image resume.")
        if state.queued_image_requests:
            lines.append(f"Queued image batches: {state.queued_image_requests}")
        return "\n".join(lines)
    if state.cancel_requested and not complete:
        lines.append("Cancelling the current batch and removing queued batches…")
        if state.queued_image_requests:
            lines.append(f"Queued image batches to cancel: {state.queued_image_requests}")
        lines.append(f"Elapsed: {_elapsed_text(state)}")
        return "\n".join(lines)
    if not complete and error is None:
        lines.append("Emery will respond to messages when the images are done.")
        if state.pipeline_waiting:
            lines.append("Waiting for the B580 image pipeline…")
        elif state.progress_item:
            item_label = f"Image {state.progress_item}/{state.total}"
            if state.progress_value is not None and state.progress_max:
                percent = round(100 * state.progress_value / max(1, state.progress_max))
                lines.append(
                    f"{item_label}: sampler step {state.progress_value}/{state.progress_max} ({percent}%)"
                )
            elif state.progress_status == "complete":
                lines.append(f"{item_label}: finishing output…")
            elif state.progress_status == "starting":
                lines.append(f"{item_label}: starting ComfyUI execution…")
            elif state.progress_node:
                lines.append(f"{item_label}: running node {state.progress_node}…")
            else:
                lines.append(f"{item_label}: running ComfyUI…")
    if state.queued_count:
        lines.append(f"Queued chat messages: {state.queued_count}")
    if state.queued_image_requests:
        lines.append(f"Queued image requests: {state.queued_image_requests}")
    lines.append(f"Elapsed: {_elapsed_text(state)}")
    return "\n".join(lines)


async def _heartbeat(state: ImageGenerationState) -> None:
    try:
        while state.active:
            await asyncio.sleep(max(5.0, float(LIVE_PROGRESS_HEARTBEAT_INTERVAL_SECONDS)))
            if state.active:
                if state.paused:
                    await state.notifier.update(_progress_text(state), force=True)
                    await state.notifier.repost_at_bottom()
                else:
                    await state.notifier.update(_progress_text(state), force=True)
    except asyncio.CancelledError:
        return
    except Exception:
        log.debug("IMAGE LIFECYCLE: notifier heartbeat stopped", exc_info=True)


def start_image_generation(bot, chat_id: int, thread_id: int | None, total: int) -> ImageGenerationState:
    key = _key(chat_id, thread_id)
    existing = _ACTIVE.get(key)
    if existing and existing.active:
        raise RuntimeError("An image generation request is already active for this chat.")

    normalized_thread_id = key[1]
    notifier = TelegramLiveProgress(
        bot,
        chat_id,
        message_thread_id=normalized_thread_id,
        min_delay=0.0,
        edit_interval=1.0,
    )
    state = ImageGenerationState(
        bot=bot,
        chat_id=chat_id,
        thread_id=normalized_thread_id,
        total=max(1, int(total)),
        notifier=notifier,
    )
    _ACTIVE[key] = state
    state.heartbeat_task = asyncio.create_task(_heartbeat(state))
    asyncio.create_task(state.notifier.update(_progress_text(state), force=True))
    log.info(
        "IMAGE LIFECYCLE: started chat_id=%s thread_id=%s batch_size=%d",
        chat_id,
        normalized_thread_id,
        state.total,
    )
    return state


async def pause_image_generation(state: ImageGenerationState) -> bool:
    async with state.lock:
        if not state.active or state.cancel_requested or state.paused:
            return False
        state.paused = True
        state.resume_event.clear()
        state.pause_event.set()
        text = _progress_text(state)
    await state.notifier.update(text, force=True)
    await state.notifier.repost_at_bottom()
    return True


async def resume_image_generation(state: ImageGenerationState) -> bool:
    async with state.lock:
        if not state.active or state.cancel_requested or not state.paused:
            return False
        state.paused = False
        state.resume_event.set()
        state.pause_event.clear()
        state.pause_ready_event.clear()
        text = _progress_text(state)
    await state.notifier.update(text, force=True)
    await state.notifier.repost_at_bottom()
    return True


async def cancel_image_generation(state: ImageGenerationState) -> tuple[bool, int, bool]:
    async with state.lock:
        if not state.active or state.cancel_requested:
            return False, 0, False
        state.cancel_requested = True
        state.paused = False
        state.resume_event.set()
        state.cancel_event.set()
        queued = len(state.image_jobs)
        state.image_jobs.clear()
        state.queued_image_requests = 0
        should_interrupt = state.current_job_active and not state.pipeline_waiting
        state.pause_event.clear()
        text = _progress_text(state)
    await state.notifier.update(text, force=True)
    await state.notifier.repost_at_bottom()
    return True, queued, should_interrupt


async def note_image_request_queued(state: ImageGenerationState) -> None:
    async with state.lock:
        if not state.active:
            return
        text = _progress_text(state)
    await state.notifier.update(text, force=True)
    await state.notifier.repost_at_bottom()


async def set_image_pipeline_waiting(state: ImageGenerationState, waiting: bool) -> None:
    async with state.lock:
        if not state.active:
            return
        state.pipeline_waiting = bool(waiting)
        text = _progress_text(state)
    await state.notifier.update(text, force=True)


async def next_image_job(state: ImageGenerationState) -> ImageGenerationJob | None:
    async with state.lock:
        if not state.active or state.paused or state.cancel_requested or not state.image_jobs:
            return None
        job = state.image_jobs.popleft()
        state.current_job_active = True
        state.pause_ready_event.clear()
        state.queued_image_requests = max(0, state.queued_image_requests - 1)
        state.total = max(1, int(job.batch_total or job.batch_size))
        state.completed = max(0, int(job.item_offset))
        state.progress_item = 0
        state.progress_value = None
        state.progress_max = None
        state.progress_status = "starting"
        state.progress_node = None
        text = _progress_text(state)
    await state.notifier.update(text, force=True)
    return job


async def close_image_queue_if_empty(state: ImageGenerationState) -> bool:
    """Mark the queue as closing once the worker observes it empty."""
    async with state.lock:
        if not state.active:
            return True
        if state.paused:
            return False
        if state.image_jobs:
            return False
        state.accepting_jobs = False
        return True


async def image_item_ready(state: ImageGenerationState, index: int, total: int) -> None:
    async with state.lock:
        if not state.active:
            return
        state.completed = max(state.completed, min(int(index), int(total)))
        state.total = max(state.total, int(total))
        text = _progress_text(state)
    await state.notifier.update(text, force=True)
    await state.notifier.repost_at_bottom()


async def image_generation_progress(state: ImageGenerationState, progress: dict[str, Any]) -> None:
    """Update the persistent notifier from a brokered ComfyUI progress event."""
    async with state.lock:
        if not state.active:
            return
        state.progress_item = max(1, min(int(progress.get("item_index") or 1), state.total))
        state.progress_value = progress.get("value")
        state.progress_max = progress.get("max")
        state.progress_status = str(progress.get("status") or "running")
        state.progress_node = progress.get("node")
        text = _progress_text(state)
    await state.notifier.update(text, force=True)


async def set_current_image_job_active(state: ImageGenerationState, active: bool) -> None:
    async with state.lock:
        if not state.active:
            return
        state.current_job_active = bool(active)
        text = _progress_text(state)
    await state.notifier.update(text, force=True)


async def wait_for_image_resume(state: ImageGenerationState) -> bool:
    while True:
        async with state.lock:
            if not state.active or state.cancel_requested:
                return False
            paused = state.paused
        if not paused:
            return True
        await state.resume_event.wait()


async def defer_chat_message(state: ImageGenerationState, update, context, entry: dict[str, Any]) -> bool:
    async with state.lock:
        if not state.active:
            return False
        state.pending_entries.append(entry)
        state.latest_update = update
        state.latest_context = context
        state.latest_user_id = entry.get("user_id")
        text = _progress_text(state)

    await state.notifier.update(text, force=True)
    await state.notifier.repost_at_bottom()
    log.info(
        "IMAGE LIFECYCLE: deferred chat message chat_id=%s count=%d",
        state.chat_id,
        state.queued_count,
    )
    return True


async def defer_active_chat_message(update, context, entry: dict[str, Any]) -> bool:
    message = getattr(update, "message", None)
    chat = getattr(update, "effective_chat", None)
    if message is None or chat is None:
        return False
    thread_id = getattr(message, "message_thread_id", None)
    state = get_active_image_generation(chat.id, thread_id)
    if state is None:
        return False
    while True:
        async with state.lock:
            if not state.active:
                return False
            if not state.paused:
                return True
            pause_ready = state.pause_ready_event.is_set()
            pause_ready_event = state.pause_ready_event
        if pause_ready:
            # The image runtime has yielded the GPU to the text model.
            return False
        await pause_ready_event.wait()
    return await defer_chat_message(state, update, context, entry)


def _collapse_pending_history(state: ImageGenerationState) -> int:
    if not state.pending_entries:
        return 0

    history = globals.chat_histories.setdefault(state.chat_id, [])
    pending_ids = {id(entry) for entry in state.pending_entries}
    retained = [entry for entry in history if id(entry) not in pending_ids]
    messages = [str(entry.get("content") or "").strip() for entry in state.pending_entries]
    messages = [message for message in messages if message]
    if messages:
        latest = state.pending_entries[-1]
        combined = {
            "role": "user",
            "content": (
                "[Messages received while image generation was in progress]\n\n"
                + "\n\n".join(messages)
            ),
            "message_id": latest.get("message_id"),
            "message_ids": [entry.get("message_id") for entry in state.pending_entries if entry.get("message_id")],
            "user_id": latest.get("user_id"),
            "sender_name": latest.get("sender_name"),
            "message_thread_id": latest.get("message_thread_id"),
            "timestamp": latest.get("timestamp"),
        }
        retained.append(combined)
    history.clear()
    history.extend(retained)
    count = len(state.pending_entries)
    state.pending_entries.clear()
    return count


async def _resume_deferred_chat(state: ImageGenerationState) -> None:
    try:
        from emery.bot import run_engine_for_chat
        from emery.session_context import get_session_context, get_turn_context, set_current_context

        update = state.latest_update
        context = state.latest_context
        if update is None or context is None:
            return

        globals.TARGET_CHAT_ID.set(state.chat_id)
        globals.CURRENT_THREAD_ID.set(state.thread_id)
        globals.current_user_id.set(state.latest_user_id)
        session_context = await get_session_context(
            chat_id=state.chat_id,
            thread_id=state.thread_id,
            user_id=state.latest_user_id,
        )
        latest_text = globals.chat_histories[state.chat_id][-1].get("content", "")
        turn_context = await get_turn_context(
            latest_text,
            state.latest_user_id,
            session=session_context,
        )
        set_current_context(session_context, turn_context)
        if getattr(update, "message", None) is not None:
            globals.chat_reply_targets[state.chat_id] = update.message.message_id
        log.info(
            "IMAGE LIFECYCLE: resuming Emery chat_id=%s queued_messages=%d",
            state.chat_id,
            state.deferred_count,
        )
        await run_engine_for_chat(update, context, MODEL_ID, False)
    except Exception:
        log.exception("IMAGE LIFECYCLE: deferred chat resume failed chat_id=%s", state.chat_id)
    finally:
        await state.notifier.close_after_minimum(2.0)


async def _close_notifier(state: ImageGenerationState) -> None:
    await state.notifier.close_after_minimum(4.0)


async def finish_image_generation(state: ImageGenerationState, error: BaseException | None = None) -> None:
    async with state.lock:
        if not state.active:
            return
        state.active = False
        state.accepting_jobs = False
        queued_count = _collapse_pending_history(state)
        state.deferred_count = queued_count
        state.done_event.set()
        state.pause_ready_event.set()
        if _ACTIVE.get(state.key) is state:
            _ACTIVE.pop(state.key, None)
        text = _progress_text(
            state,
            complete=error is None or state.cancel_requested,
            error=None if state.cancel_requested else error,
        )
        has_deferred_chat = queued_count > 0 and state.latest_update is not None and state.latest_context is not None
        has_active_turn = state.key in globals.active_turns

    if state.heartbeat_task is not None:
        state.heartbeat_task.cancel()
    await state.notifier.update(text, force=True)

    if state.main_model_restored:
        try:
            await state.bot.send_message(
                chat_id=state.chat_id,
                text="Emery is back up.",
                message_thread_id=state.thread_id,
            )
        except Exception:
            log.exception("IMAGE LIFECYCLE: unable to send model-restored notice chat_id=%s", state.chat_id)

    if has_deferred_chat and not has_active_turn:
        asyncio.create_task(_resume_deferred_chat(state))
    else:
        asyncio.create_task(_close_notifier(state))

    log.info(
        "IMAGE LIFECYCLE: finished chat_id=%s completed=%d/%d deferred_messages=%d error=%s",
        state.chat_id,
        state.completed,
        state.total,
        queued_count,
        type(error).__name__ if error else "none",
    )


async def wait_for_active_image_generation(chat_id: int | None, thread_id: int | None) -> None:
    if chat_id is None:
        return
    state = get_active_image_generation(chat_id, thread_id)
    if state is not None:
        log.info("IMAGE LIFECYCLE: waiting for image runtime before main-model request chat_id=%s", chat_id)
        await state.done_event.wait()
