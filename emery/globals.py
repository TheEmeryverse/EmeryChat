from collections import deque
import httpx

chat_histories = {}
import os
from dotenv import load_dotenv
load_dotenv()

group_chat_id_env = os.getenv("TELEGRAM_GROUP_CHAT_ID")
if group_chat_id_env:
    try:
        TARGET_CHAT_ID = int(group_chat_id_env)
    except ValueError:
        TARGET_CHAT_ID = None
else:
    TARGET_CHAT_ID = None

CURRENT_THREAD_ID = None  # Tracks the topic/thread ID of the active conversation

http_client = httpx.AsyncClient(timeout=900, verify=False, follow_redirects=True)
application_bot = None  # Populated dynamically by main.py
application = None      # Populated dynamically by main.py
reolink_thread_trackers = {}  # Tracks camera alerts: camera_name -> {"message_id": int, "timestamp": datetime}
chat_reply_targets = {}       # Tracks custom reply message ID per chat: chat_id -> message_id

# Concurrency locks to protect Ollama endpoints from concurrent load
import asyncio
main_model_lock = asyncio.Semaphore(1)
fast_model_lock = asyncio.Semaphore(1)

learned_stickers = {}  # Tracks learned sticker file IDs: emoji -> file_id


