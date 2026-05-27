from collections import deque
import httpx

chat_histories = {}
TARGET_CHAT_ID = None
http_client = httpx.AsyncClient(timeout=900, verify=False, follow_redirects=True)
application_bot = None  # Populated dynamically by main.py
application = None      # Populated dynamically by main.py
reolink_thread_trackers = {}  # Tracks camera alerts: camera_name -> {"message_id": int, "timestamp": datetime}
chat_reply_targets = {}       # Tracks custom reply message ID per chat: chat_id -> message_id

# Concurrency locks to protect Ollama endpoints from concurrent load
import asyncio
main_model_lock = asyncio.Semaphore(1)
fast_model_lock = asyncio.Semaphore(1)

