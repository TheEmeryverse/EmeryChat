import logging

from telegram import ReplyParameters
from telegram.error import BadRequest

from emery.telegram_utils import normalize_message_thread_id


MAX_TELEGRAM_HTML_MESSAGE_LEN = 4000


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

    if len(text) <= MAX_TELEGRAM_HTML_MESSAGE_LEN:
        sent_msg = await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_parameters=reply_params,
            message_thread_id=message_thread_id,
        )
        return [sent_msg]

    while text:
        if len(text) <= MAX_TELEGRAM_HTML_MESSAGE_LEN:
            chunk = text
            text = ""
        else:
            split_index = text.rfind("\n", 0, MAX_TELEGRAM_HTML_MESSAGE_LEN)
            if split_index == -1 or split_index < 3000:
                split_index = MAX_TELEGRAM_HTML_MESSAGE_LEN

            chunk = text[:split_index]
            text = text[split_index:].strip()

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
