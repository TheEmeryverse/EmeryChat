from collections import deque
import contextvars
import asyncio

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


# Concurrency locks to protect Ollama endpoints from concurrent load
main_model_lock = asyncio.Semaphore(1)
fast_model_lock = asyncio.Semaphore(1)

learned_stickers = {}  # Tracks learned sticker file IDs: emoji -> file_id

