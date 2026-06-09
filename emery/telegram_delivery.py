import logging
import re

from telegram import ReplyParameters
from telegram.error import BadRequest

from emery.telegram_utils import normalize_message_thread_id


MAX_TELEGRAM_HTML_MESSAGE_LEN = 4000
MIN_TELEGRAM_SPLIT_LEN = 1000
HTML_TAG_RE = re.compile(r"</?([a-zA-Z][\w:-]*)(?:\s[^<>]*)?>")
VOID_HTML_TAGS = {"br", "hr", "img"}


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
