import logging
import re
import asyncio
import html
import time
from types import SimpleNamespace

from telegram import ReplyParameters
from telegram.error import BadRequest

from emery.config import (
    ENABLE_TELEGRAM_RICH_MESSAGES,
    TELEGRAM_TOKEN,
    LIVE_PROGRESS_MIN_DELAY_SECONDS,
    LIVE_PROGRESS_EDIT_INTERVAL_SECONDS,
    LIVE_PROGRESS_HEARTBEAT_INTERVAL_SECONDS,
)
from emery.telegram_utils import normalize_message_thread_id


MAX_TELEGRAM_HTML_MESSAGE_LEN = 4000
MAX_TELEGRAM_RICH_MESSAGE_BYTES = 32768
MIN_TELEGRAM_SPLIT_LEN = 1000
HTML_TAG_RE = re.compile(r"</?([a-zA-Z][\w:-]*)(?:\s[^<>]*)?>")
VOID_HTML_TAGS = {"br", "hr", "img"}
_UNSET = object()


def _is_message_not_modified(exc: BaseException) -> bool:
    """Return whether Telegram rejected an edit because nothing changed."""
    return isinstance(exc, BadRequest) and "message is not modified" in str(exc).casefold()


class TelegramLiveProgress:
    """Render one throttled, best-effort temporary progress message per turn."""

    def __init__(
        self,
        bot,
        chat_id: int,
        *,
        message_thread_id: int = None,
        min_delay: float = LIVE_PROGRESS_MIN_DELAY_SECONDS,
        edit_interval: float = LIVE_PROGRESS_EDIT_INTERVAL_SECONDS,
    ):
        self.bot = bot
        self.chat_id = chat_id
        self.message_thread_id = normalize_message_thread_id(chat_id, message_thread_id)
        self.min_delay = max(0.0, float(min_delay))
        self.edit_interval = max(0.0, float(edit_interval))
        self.started_at = time.monotonic()
        self.last_sent_at = None
        self.message_id = None
        self.visible_since = None
        self.pending_text = None
        self.displayed_text = None
        self.displayed_html = None
        self.reply_markup = None
        self._operation_lock = asyncio.Lock()
        self.disabled = False

    async def run_heartbeat(
        self,
        stop_event: asyncio.Event,
        messages: list[str],
        *,
        interval: float = LIVE_PROGRESS_HEARTBEAT_INTERVAL_SECONDS,
        should_update=None,
        initial_index: int = 0,
    ) -> None:
        """Keep long model turns visibly active without exposing internal reasoning."""
        heartbeat_messages = [str(message).strip() for message in messages if str(message).strip()]
        if not heartbeat_messages:
            return

        interval = max(0.1, float(interval))
        message_index = int(initial_index) % len(heartbeat_messages)
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass

            if should_update is not None and not should_update():
                continue
            await self.update(heartbeat_messages[message_index % len(heartbeat_messages)])
            message_index += 1

    @staticmethod
    def _html_text(text: str) -> str:
        return f"<i>{html.escape(str(text or ''), quote=False)}</i>"

    async def update(
        self,
        text: str,
        *,
        force: bool = False,
        reply_markup=_UNSET,
        rendered_html: str | None = None,
    ) -> None:
        clean_text = str(text or "").strip()
        if not clean_text or self.disabled:
            return

        async with self._operation_lock:
            if self.disabled:
                return

            if reply_markup is not _UNSET:
                self.reply_markup = reply_markup
            self.pending_text = clean_text
            now = time.monotonic()
            if not force and self.message_id is None and now - self.started_at < self.min_delay:
                return

            if self.message_id is not None and self.last_sent_at is not None:
                remaining = self.edit_interval - (now - self.last_sent_at)
                if remaining > 0:
                    if not force:
                        return
                    await asyncio.sleep(remaining)

            text_to_send = self.pending_text
            html_to_send = rendered_html if rendered_html is not None else self._html_text(text_to_send)
            try:
                if self.message_id is None:
                    send_kwargs = dict(
                        chat_id=self.chat_id,
                        text=html_to_send,
                        parse_mode="HTML",
                        message_thread_id=self.message_thread_id,
                    )
                    if self.reply_markup is not None:
                        send_kwargs["reply_markup"] = self.reply_markup
                    sent = await self.bot.send_message(**send_kwargs)
                    self.message_id = getattr(sent, "message_id", None)
                    self.visible_since = time.monotonic()
                else:
                    await self.bot.edit_message_text(
                        chat_id=self.chat_id,
                        message_id=self.message_id,
                        text=html_to_send,
                        parse_mode="HTML",
                        reply_markup=self.reply_markup,
                    )
                self.displayed_text = text_to_send
                self.displayed_html = html_to_send
                self.pending_text = None
                self.last_sent_at = time.monotonic()
            except BadRequest as exc:
                # Telegram reports an unchanged edit as a BadRequest, but the
                # status message is still healthy and must remain usable for
                # later approval/browser updates.
                if _is_message_not_modified(exc):
                    self.displayed_text = text_to_send
                    self.displayed_html = html_to_send
                    self.pending_text = None
                    self.last_sent_at = time.monotonic()
                    logging.debug(
                        "TELEGRAM PROGRESS: unchanged update ignored chat_id=%s message_id=%s",
                        self.chat_id,
                        self.message_id,
                    )
                    return
                self.disabled = True
                logging.warning(
                    "⚠️ TELEGRAM PROGRESS: unable to update chat_id=%s thread_id=%s: %s",
                    self.chat_id,
                    self.message_thread_id,
                    exc,
                )
            except Exception as exc:
                self.disabled = True
                logging.warning(
                    "⚠️ TELEGRAM PROGRESS: unable to update chat_id=%s thread_id=%s: %s",
                    self.chat_id,
                    self.message_thread_id,
                    exc,
                )

    async def bring_to_bottom(self) -> None:
        """Re-post this one message below newer chat messages."""
        await self.repost_at_bottom()

    async def repost_at_bottom(self) -> None:
        """Replace this message when its current position is stale."""
        async with self._operation_lock:
            if self.disabled or self.message_id is None or not self.displayed_text:
                return

            old_message_id = self.message_id
            try:
                sent = await self.bot.send_message(
                    chat_id=self.chat_id,
                    text=self.displayed_html or self._html_text(self.displayed_text),
                    parse_mode="HTML",
                    message_thread_id=self.message_thread_id,
                    **({"reply_markup": self.reply_markup} if self.reply_markup is not None else {}),
                )
                new_message_id = getattr(sent, "message_id", None)
                if new_message_id is None:
                    raise RuntimeError("Telegram returned no message ID for the replacement progress message")

                self.message_id = new_message_id
                self.visible_since = time.monotonic()
                self.last_sent_at = time.monotonic()
                try:
                    await self.bot.delete_message(chat_id=self.chat_id, message_id=old_message_id)
                except Exception as exc:
                    logging.debug(
                        "TELEGRAM PROGRESS: unable to delete replaced message chat_id=%s message_id=%s: %s",
                        self.chat_id,
                        old_message_id,
                        exc,
                    )
            except Exception as exc:
                self.disabled = True
                logging.warning(
                    "⚠️ TELEGRAM PROGRESS: unable to move message to bottom chat_id=%s thread_id=%s: %s",
                    self.chat_id,
                    self.message_thread_id,
                    exc,
                )

    async def refresh_in_place(self) -> None:
        """Edit the existing status message without changing its position."""
        async with self._operation_lock:
            if self.disabled or self.message_id is None or not self.displayed_text:
                return
            displayed_text = self.displayed_text
            displayed_html = self.displayed_html
            reply_markup = self.reply_markup

        await self.update(
            displayed_text,
            force=True,
            reply_markup=reply_markup,
            rendered_html=displayed_html,
        )

    async def close_after_minimum(self, minimum_visible_seconds: float = 10.0) -> None:
        """Delete the message after it has been visible for the requested minimum."""
        async with self._operation_lock:
            message_id = self.message_id
            visible_since = self.visible_since
        if message_id is None:
            return

        minimum_visible_seconds = max(0.0, float(minimum_visible_seconds))
        if visible_since is not None:
            remaining = minimum_visible_seconds - (time.monotonic() - visible_since)
            if remaining > 0:
                await asyncio.sleep(remaining)
        await self.close()

    async def close(self) -> None:
        async with self._operation_lock:
            if self.message_id is None:
                return
            message_id = self.message_id
            try:
                await self.bot.delete_message(chat_id=self.chat_id, message_id=message_id)
            except Exception as exc:
                logging.debug(
                    "TELEGRAM PROGRESS: unable to delete temporary message chat_id=%s message_id=%s: %s",
                    self.chat_id,
                    message_id,
                    exc,
                )
            finally:
                self.message_id = None
                self.visible_since = None
                self.pending_text = None
                self.displayed_text = None
                self.displayed_html = None
                self.reply_markup = None


class PersistentStatusStack:
    """Own the ordered live-status messages at the chat bottom.

    Telegram cannot move a message by editing it. Status updates therefore
    keep the original message IDs and edit the three messages in place:

    1. live reasoning
    2. most recently called tool, created only when needed
    3. browser or terminal state, created only when needed

    The reasoning message is created for every live turn. The tool and
    browser/terminal messages are created lazily only when that state exists.
    Transient messages are deleted after the final response; the finalized
    reasoning message can be preserved as a collapsed summary.
    """

    SLOT_ORDER = ("reasoning", "tool", "environment")
    LEGACY_SLOT_MAP = {
        "command": "environment",
        "browser": "environment",
        "approval": "environment",
    }
    DEFAULT_REASONING = "💭 I’m thinking through your request…"
    DEFAULT_TOOL = "🔧 No tool call yet."
    DEFAULT_ENVIRONMENT = "🖥️ No browser or terminal activity yet."

    def __init__(self, bot, chat_id: int, *, message_thread_id: int = None):
        self.bot = bot
        self.chat_id = chat_id
        self.message_thread_id = normalize_message_thread_id(chat_id, message_thread_id)
        self.messages = {
            slot: TelegramLiveProgress(
                bot,
                chat_id,
                message_thread_id=self.message_thread_id,
                min_delay=0.0,
                edit_interval=0.0,
            )
            for slot in self.SLOT_ORDER
        }
        self.slots = {
            "reasoning": self.DEFAULT_REASONING,
            "tool": None,
            "environment": None,
        }
        self._base_environment = None
        self._approval_text = None
        self.browser_urls = {}
        self.mode = None
        self._approval_markup = None
        self._lock = asyncio.Lock()
        self._at_bottom = True

    @property
    def message_id(self):
        return self.messages["reasoning"].message_id

    @property
    def message(self):
        """Compatibility alias for callers that used the old single message."""
        return self.messages["reasoning"]

    @property
    def disabled(self):
        return any(message.disabled for message in self.messages.values())

    def _render_slot(self, slot: str) -> str:
        if slot == "environment" and self._approval_text:
            return self._approval_text
        value = str(self.slots.get(slot) or "").strip()
        if slot == "reasoning" and value and not value.startswith("💭"):
            return f"💭 {value}"
        return value

    def _environment_visible(self) -> bool:
        return bool(self._approval_text or self._base_environment)

    def _tool_visible(self) -> bool:
        return bool(self.slots.get("tool"))

    def _reply_markup_for(self, slot: str):
        return self._approval_markup if slot == "environment" and self._approval_text else None

    async def note_newer_message(self) -> None:
        """Record that a normal chat message is newer than this status block."""
        async with self._lock:
            if any(message.message_id is not None for message in self.messages.values()):
                self._at_bottom = False

    async def _ensure_messages_locked(self) -> None:
        """Create the currently required messages in their fixed order."""
        for slot in self.SLOT_ORDER:
            message = self.messages[slot]
            if slot == "tool" and not self._tool_visible():
                if message.message_id is not None:
                    await message.close()
                continue
            if slot == "environment" and not self._environment_visible():
                if message.message_id is not None:
                    await message.close()
                continue
            if message.message_id is not None:
                continue
            await message.update(
                self._render_slot(slot),
                force=True,
                reply_markup=self._reply_markup_for(slot),
            )

    def _canonical_slot(self, slot: str) -> str:
        if slot in self.SLOT_ORDER:
            return slot
        if slot in self.LEGACY_SLOT_MAP:
            return self.LEGACY_SLOT_MAP[slot]
        raise ValueError(f"unknown persistent status slot: {slot}")

    async def set_slot(
        self,
        slot: str,
        value: str | None,
        *,
        reply_markup=_UNSET,
        rendered_html: str | None = None,
        force: bool = True,
        mode: str | None = None,
    ) -> None:
        original_slot = slot
        slot = self._canonical_slot(slot)
        async with self._lock:
            if mode is not None:
                if mode not in {"terminal", "browser"}:
                    raise ValueError(f"unknown persistent status mode: {mode}")
                self.mode = mode
            if original_slot == "approval":
                self._approval_text = str(value).strip() if value else None
                if reply_markup is not _UNSET:
                    self._approval_markup = reply_markup
            elif slot == "environment":
                self._base_environment = str(value).strip() if value else None
                self.slots[slot] = self._base_environment
            elif slot == "tool":
                self.slots[slot] = str(value).strip() if value else None
            else:
                self.slots[slot] = str(value).strip() if value else self.DEFAULT_REASONING

            await self._ensure_messages_locked()
            if not self._at_bottom:
                await self._repost_locked()
            if (slot != "tool" or self._tool_visible()) and (
                slot != "environment" or self._environment_visible()
            ):
                await self.messages[slot].update(
                    self._render_slot(slot),
                    force=force,
                    reply_markup=self._reply_markup_for(slot),
                    rendered_html=rendered_html,
                )

    async def clear_slot(self, slot: str) -> None:
        if slot == "approval":
            await self.clear_approval()
            return
        slot = self._canonical_slot(slot)
        if slot == "environment":
            await self.set_slot("environment", None, reply_markup=None)
        else:
            await self.set_slot(slot, None, reply_markup=None)

    async def clear_approval(self) -> None:
        async with self._lock:
            self._approval_text = None
            self._approval_markup = None
            await self._ensure_messages_locked()
            if self._environment_visible():
                await self.messages["environment"].update(
                    self._base_environment,
                    force=True,
                    reply_markup=None,
                )

    async def clear_slots(
        self,
        *slots: str,
        final_reasoning: bool = False,
        reasoning_value: str | None = None,
    ) -> None:
        """Clear only legacy transient overlays; never remove a status message."""
        async with self._lock:
            if reasoning_value:
                self.slots["reasoning"] = str(reasoning_value).strip()
            if "approval" in slots:
                self._approval_text = None
                self._approval_markup = None
            await self._ensure_messages_locked()
            if reasoning_value:
                await self.messages["reasoning"].update(
                    self._render_slot("reasoning"),
                    force=True,
                )
            if "approval" in slots and self._environment_visible():
                await self.messages["environment"].update(
                    self._base_environment,
                    force=True,
                    reply_markup=None,
                )

    async def ensure_visible(self) -> None:
        """Create the currently required status messages if missing."""
        async with self._lock:
            await self._ensure_messages_locked()

    async def _repost_locked(self) -> None:
        for slot in self.SLOT_ORDER:
            await self.messages[slot].repost_at_bottom()
        self._at_bottom = True

    async def bring_to_bottom(self) -> None:
        """Edit in place when current; repost only when marked stale."""
        async with self._lock:
            await self._ensure_messages_locked()
            if not self._at_bottom:
                await self._repost_locked()
            else:
                for slot in self.SLOT_ORDER:
                    await self.messages[slot].refresh_in_place()

    async def close(self, *, preserve_slots: set[str] | None = None) -> None:
        """Delete transient messages, optionally preserving finalized slots."""
        preserve_slots = set(preserve_slots or ())
        async with self._lock:
            for slot, message in self.messages.items():
                if slot in preserve_slots:
                    continue
                await message.close()


def _is_inside_html_syntax(text: str, index: int) -> bool:
    before = text[:index]
    last_lt = before.rfind("<")
    last_gt = before.rfind(">")
    if last_lt > last_gt:
        return True

    last_amp = before.rfind("&")
    last_semicolon = before.rfind(";")
    last_space = max(before.rfind(" "), before.rfind("\n"), before.rfind("\t"))
    return last_amp > last_semicolon and last_amp > last_space


def _find_safe_split_index(text: str, limit: int) -> int:
    if len(text) <= limit:
        return len(text)

    limit = max(1, min(limit, len(text)))
    min_index = min(MIN_TELEGRAM_SPLIT_LEN, max(1, limit // 2))
    for delimiter in ("\n", " "):
        split_index = text.rfind(delimiter, 0, limit)
        if split_index >= min_index and not _is_inside_html_syntax(text, split_index):
            return split_index + (1 if delimiter == "\n" else 0)

    for split_index in range(limit, min_index, -1):
        if not _is_inside_html_syntax(text, split_index):
            return split_index

    return limit


def _apply_html_tag_events(open_tags: list[tuple[str, str]], html_text: str) -> list[tuple[str, str]]:
    stack = list(open_tags)
    for match in HTML_TAG_RE.finditer(html_text):
        raw_tag = match.group(0)
        tag_name = match.group(1).lower()
        if tag_name in VOID_HTML_TAGS or raw_tag.endswith("/>"):
            continue

        if raw_tag.startswith("</"):
            for index in range(len(stack) - 1, -1, -1):
                if stack[index][0] == tag_name:
                    del stack[index:]
                    break
            continue

        stack.append((tag_name, raw_tag))
    return stack


def _close_tags(open_tags: list[tuple[str, str]]) -> str:
    return "".join(f"</{tag_name}>" for tag_name, _raw_tag in reversed(open_tags))


def split_telegram_html(text: str, limit: int = MAX_TELEGRAM_HTML_MESSAGE_LEN) -> list[str]:
    """Split Telegram HTML text without cutting inside tags/entities and keep chunks balanced."""
    remaining = str(text or "")
    chunks = []
    open_tags: list[tuple[str, str]] = []

    while remaining:
        prefix = "".join(raw_tag for _tag_name, raw_tag in open_tags)
        suffix_budget = len(_close_tags(open_tags))
        content_limit = max(1, limit - len(prefix) - suffix_budget)
        split_index = _find_safe_split_index(remaining, content_limit)

        while True:
            raw_chunk = remaining[:split_index].rstrip()
            next_open_tags = _apply_html_tag_events(open_tags, raw_chunk)
            chunk = prefix + raw_chunk + _close_tags(next_open_tags)
            if len(chunk) <= limit or split_index <= 1:
                break
            overflow = len(chunk) - limit
            split_index = _find_safe_split_index(remaining, max(1, split_index - overflow - 8))

        chunks.append(chunk)
        remaining = remaining[split_index:].lstrip()
        open_tags = next_open_tags

    return chunks or [""]


def build_telegram_rich_message_payload(
    chat_id: int,
    rich_text: str,
    *,
    rich_format: str = "markdown",
    reply_to_message_id: int = None,
    message_thread_id: int = None,
) -> dict:
    """Build the raw Bot API payload for sendRichMessage."""
    if rich_format not in {"markdown", "html"}:
        raise ValueError("rich_format must be 'markdown' or 'html'")

    payload = {
        "chat_id": chat_id,
        "rich_message": {
            rich_format: str(rich_text or ""),
        },
    }

    normalized_thread_id = normalize_message_thread_id(chat_id, message_thread_id)
    if normalized_thread_id is not None:
        payload["message_thread_id"] = normalized_thread_id

    if reply_to_message_id:
        payload["reply_parameters"] = {
            "message_id": reply_to_message_id,
            "allow_sending_without_reply": True,
        }

    return payload


def _rich_message_within_limits(rich_text: str) -> bool:
    return len(str(rich_text or "").encode("utf-8")) <= MAX_TELEGRAM_RICH_MESSAGE_BYTES


async def _send_native_rich_message(bot, payload: dict):
    method = getattr(bot, "send_rich_message", None) or getattr(bot, "sendRichMessage", None)
    if not callable(method):
        return None

    reply_params = None
    reply_payload = payload.get("reply_parameters")
    if reply_payload:
        reply_params = ReplyParameters(
            message_id=reply_payload["message_id"],
            allow_sending_without_reply=reply_payload.get("allow_sending_without_reply", True),
        )

    kwargs = {
        "chat_id": payload["chat_id"],
        "rich_message": payload["rich_message"],
        "reply_parameters": reply_params,
    }
    if "message_thread_id" in payload:
        kwargs["message_thread_id"] = payload["message_thread_id"]

    try:
        return await method(**kwargs)
    except TypeError as e:
        logging.debug("TELEGRAM RICH: native send_rich_message signature was incompatible: %s", e)
        return None


async def _send_raw_rich_message(payload: dict):
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "blank":
        raise RuntimeError("TELEGRAM_TOKEN is not configured")

    import emery.globals as globals

    response = await globals.http_client.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendRichMessage",
        json=payload,
    )

    try:
        data = response.json()
    except Exception as e:
        raise RuntimeError(f"Telegram sendRichMessage returned non-JSON response: HTTP {response.status_code}") from e

    if response.status_code >= 400 or not data.get("ok"):
        raise BadRequest(data.get("description") or f"Telegram sendRichMessage failed with HTTP {response.status_code}")

    result = data.get("result") or {}
    return SimpleNamespace(message_id=result.get("message_id"), raw=result)


async def send_rich_or_split_html_message(
    bot,
    chat_id: int,
    markdown_text: str,
    *,
    fallback_html_text: str = None,
    reply_to_message_id: int = None,
    message_thread_id: int = None,
):
    """Send a final reply as Telegram rich Markdown, falling back to legacy split HTML."""
    fallback_text = fallback_html_text if fallback_html_text is not None else str(markdown_text or "")
    payload = build_telegram_rich_message_payload(
        chat_id,
        markdown_text,
        rich_format="markdown",
        reply_to_message_id=reply_to_message_id,
        message_thread_id=message_thread_id,
    )

    if ENABLE_TELEGRAM_RICH_MESSAGES and _rich_message_within_limits(markdown_text):
        try:
            sent_msg = await _send_native_rich_message(bot, payload)
            if sent_msg is None:
                sent_msg = await _send_raw_rich_message(payload)
            return [sent_msg]
        except BadRequest as e:
            logging.warning(
                "TELEGRAM RICH: sendRichMessage rejected final reply for chat_id=%s thread_id=%s; falling back to HTML: %s",
                chat_id,
                payload.get("message_thread_id"),
                e,
            )
        except Exception as e:
            logging.warning(
                "TELEGRAM RICH: sendRichMessage failed for chat_id=%s thread_id=%s; falling back to HTML: %s",
                chat_id,
                payload.get("message_thread_id"),
                e,
                exc_info=True,
            )
    elif ENABLE_TELEGRAM_RICH_MESSAGES:
        logging.info(
            "TELEGRAM RICH: final reply is over %s bytes; falling back to split HTML.",
            MAX_TELEGRAM_RICH_MESSAGE_BYTES,
        )

    return await send_split_html_message(
        bot,
        chat_id,
        fallback_text,
        reply_to_message_id=reply_to_message_id,
        message_thread_id=message_thread_id,
    )


async def send_rich_html_or_split_html_message(
    bot,
    chat_id: int,
    rich_html_text: str,
    *,
    fallback_html_text: str = None,
    reply_to_message_id: int = None,
    message_thread_id: int = None,
):
    """Send Telegram Rich HTML, falling back to legacy split HTML."""
    fallback_text = fallback_html_text if fallback_html_text is not None else str(rich_html_text or "")
    payload = build_telegram_rich_message_payload(
        chat_id,
        rich_html_text,
        rich_format="html",
        reply_to_message_id=reply_to_message_id,
        message_thread_id=message_thread_id,
    )

    if ENABLE_TELEGRAM_RICH_MESSAGES and _rich_message_within_limits(rich_html_text):
        try:
            sent_msg = await _send_native_rich_message(bot, payload)
            if sent_msg is None:
                sent_msg = await _send_raw_rich_message(payload)
            return [sent_msg]
        except BadRequest as e:
            logging.warning(
                "TELEGRAM RICH: sendRichMessage rejected rich HTML for chat_id=%s thread_id=%s; falling back to HTML: %s",
                chat_id,
                payload.get("message_thread_id"),
                e,
            )
        except Exception as e:
            logging.warning(
                "TELEGRAM RICH: sendRichMessage rich HTML failed for chat_id=%s thread_id=%s; falling back to HTML: %s",
                chat_id,
                payload.get("message_thread_id"),
                e,
                exc_info=True,
            )
    elif ENABLE_TELEGRAM_RICH_MESSAGES:
        logging.info(
            "TELEGRAM RICH: rich HTML is over %s bytes; falling back to split HTML.",
            MAX_TELEGRAM_RICH_MESSAGE_BYTES,
        )

    return await send_split_html_message(
        bot,
        chat_id,
        fallback_text,
        reply_to_message_id=reply_to_message_id,
        message_thread_id=message_thread_id,
    )


async def send_split_html_message(
    bot,
    chat_id: int,
    text: str,
    *,
    reply_to_message_id: int = None,
    message_thread_id: int = None,
):
    """Send HTML text in Telegram-safe chunks and return sent Message objects."""
    message_thread_id = normalize_message_thread_id(chat_id, message_thread_id)
    reply_params = None
    if reply_to_message_id:
        reply_params = ReplyParameters(message_id=reply_to_message_id, allow_sending_without_reply=True)

    sent_msgs = []

    chunks = split_telegram_html(text)

    if len(chunks) == 1:
        sent_msg = await bot.send_message(
            chat_id=chat_id,
            text=chunks[0],
            parse_mode="HTML",
            reply_parameters=reply_params,
            message_thread_id=message_thread_id,
        )
        return [sent_msg]

    for chunk in chunks:
        sent_msg = await bot.send_message(
            chat_id=chat_id,
            text=chunk,
            parse_mode="HTML",
            reply_parameters=reply_params,
            message_thread_id=message_thread_id,
        )
        sent_msgs.append(sent_msg)

    return sent_msgs


async def try_send_split_html_message(
    bot,
    chat_id: int,
    text: str,
    *,
    message_thread_id: int = None,
    log_prefix: str = "TELEGRAM",
) -> bool:
    """Send split HTML text and convert Telegram delivery errors to False."""
    normalized_thread_id = normalize_message_thread_id(chat_id, message_thread_id)
    try:
        await send_split_html_message(
            bot,
            chat_id,
            text,
            message_thread_id=normalized_thread_id,
        )
        return True
    except BadRequest as e:
        logging.warning(
            "⚠️ %s: Telegram rejected message for chat_id=%s thread_id=%s: %s",
            log_prefix,
            chat_id,
            normalized_thread_id,
            e,
        )
        return False
    except Exception as e:
        logging.error(
            "❌ %s: Unexpected error while sending message to chat_id=%s thread_id=%s: %s",
            log_prefix,
            chat_id,
            normalized_thread_id,
            e,
            exc_info=True,
        )
        return False


async def try_send_rich_or_split_html_message(
    bot,
    chat_id: int,
    markdown_text: str,
    *,
    fallback_html_text: str = None,
    message_thread_id: int = None,
    log_prefix: str = "TELEGRAM",
) -> bool:
    """Send rich Markdown with legacy HTML fallback and convert delivery errors to False."""
    normalized_thread_id = normalize_message_thread_id(chat_id, message_thread_id)
    try:
        await send_rich_or_split_html_message(
            bot,
            chat_id,
            markdown_text,
            fallback_html_text=fallback_html_text,
            message_thread_id=normalized_thread_id,
        )
        return True
    except BadRequest as e:
        logging.warning(
            "⚠️ %s: Telegram rejected rich/fallback message for chat_id=%s thread_id=%s: %s",
            log_prefix,
            chat_id,
            normalized_thread_id,
            e,
        )
        return False
    except Exception as e:
        logging.error(
            "❌ %s: Unexpected error while sending rich/fallback message to chat_id=%s thread_id=%s: %s",
            log_prefix,
            chat_id,
            normalized_thread_id,
            e,
            exc_info=True,
        )
        return False
