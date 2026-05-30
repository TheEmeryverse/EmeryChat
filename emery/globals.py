from collections import deque
import httpx

chat_histories = {}
import os
from dotenv import load_dotenv
load_dotenv()

import contextvars

group_chat_id = None
group_chat_id_env = os.getenv("TELEGRAM_GROUP_CHAT_ID")
if group_chat_id_env:
    try:
        group_chat_id = int(group_chat_id_env)
    except ValueError:
        pass

TARGET_CHAT_ID = contextvars.ContextVar("TARGET_CHAT_ID", default=group_chat_id)
CURRENT_THREAD_ID = contextvars.ContextVar("CURRENT_THREAD_ID", default=None)

http_client = httpx.AsyncClient(timeout=900, verify=False, follow_redirects=True)
scheduler = None              # Global APScheduler instance, populated by main.py
reolink_thread_trackers = {}  # Tracks camera alerts: camera_name -> {"message_id": int, "timestamp": datetime}

import contextvars
current_user_id = contextvars.ContextVar("current_user_id", default=None)
outgoing_responses = contextvars.ContextVar("outgoing_responses", default=None) # Request-scoped list of generated responses/media

# Concurrency locks to protect Ollama endpoints from concurrent load
import asyncio
main_model_lock = asyncio.Semaphore(1)
fast_model_lock = asyncio.Semaphore(1)


