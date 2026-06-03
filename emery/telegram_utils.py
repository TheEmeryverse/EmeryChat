def normalize_group_chat_id(chat_id):
    """Telegram supergroup IDs should be negative. Normalize likely forum/group IDs."""
    if chat_id is None:
        return None
    try:
        value = int(chat_id)
    except (TypeError, ValueError):
        return chat_id

    # Supergroup/channel IDs are typically large and negative (-100...).
    if value > 0 and value >= 10**12:
        return -value
    return value


def normalize_message_thread_id(chat_id, message_thread_id):
    """Private chats do not support forum topics, so drop any carried thread id."""
    if message_thread_id in (None, "", 0):
        return None

    try:
        chat_value = int(chat_id)
    except (TypeError, ValueError):
        chat_value = chat_id

    # Telegram private chats use positive IDs. Topic IDs in that context are invalid.
    if isinstance(chat_value, int) and chat_value > 0:
        return None

    try:
        return int(message_thread_id)
    except (TypeError, ValueError):
        return None
