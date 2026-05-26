from collections import deque
import httpx

chat_histories = {}
TARGET_CHAT_ID = None
http_client = httpx.AsyncClient(timeout=300, verify=False, follow_redirects=True)
application_bot = None  # Populated dynamically by main.py
application = None      # Populated dynamically by main.py
