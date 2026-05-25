import os
import logging
import pytz
from dotenv import load_dotenv

load_dotenv() # Load docker env variables

# --- LOGGING SETUP ---
logging.basicConfig(format='%(asctime)s - [EMERYCHAT] - %(levelname)s - %(message)s', level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

# --- GLOBAL CONFIGURATION ---
MODEL_NAME = os.getenv("MODEL_NAME", "Emery") # The name of the model to use for responses
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/chat") # Ollama URL
OPEN_WEBUI_KEY = os.getenv("OPEN_WEBUI_KEY", "blank") # Open WebUI API Key
THINK = os.getenv("ENABLE_THINKING", "true").lower() == "true" # Toggles the thinking engine
MODEL_ID = os.getenv("MODEL_ID", "qwen3.5:14b")  # Main model ID
VISION_MODEL_ID = os.getenv("VISION_MODEL_ID", "gemma4:e2b") # Coprocessor model ID
VISION_OLLAMA_URL = os.getenv("VISION_OLLAMA_URL", "http://192.168.1.129:11434/api/chat") # Coprocessor Ollama URL
ENABLE_MEMORY = os.getenv("ENABLE_MEMORY", "true").lower() == "true" # Toggles memory engine
MEMORY_FILE_PATH = os.getenv("MEMORY_FILE_PATH", "memory.md") # Path to memory.md
MEMORY_THRESHOLD = int(os.getenv("MEMORY_THRESHOLD", "4000")) # Memory context threshold
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://localhost:8080/search") # SearXNG URL
NASA_API_KEY = os.getenv("NASA_API_KEY", "blank") # NASA API Key
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "blank") # Gemini image API key
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "gemini-3.1-flash-image-preview") # Image model ID
NOAA_LAT = os.getenv("NOAA_LAT", "40.7128") # NOAA Lat
NOAA_LONG = os.getenv("NOAA_LONG", "74.0060") # NOAA Long
NOAA_EMAIL = os.getenv("NOAA_EMAIL", "example@example.com") # NOAA Email
raw_cal_string = os.getenv("GOOGLE_CALENDAR_IDS", "primary")
calendar_ids = [c.strip() for c in raw_cal_string.split(",")]
TOOL_LOOP = int(os.getenv("TOOL_LOOP", "15")) # Max LLM tool invocation turns
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "blank")
OVERSEER_URL = os.getenv("OVERSEER_URL", "http://localhost:5055/api/v1")
OVERSEER_KEY = os.getenv("OVERSEER_KEY", "blank")
OVERSEER_USER_ID = os.getenv("OVERSEER_USER_ID", "1")
STT_URL = os.getenv("STT_URL", "http://localhost:3000/api/v1/audio/transcriptions")
TTS_URL = os.getenv("TTS_URL", "http://localhost:8880/v1/audio/speech")
TTS_VOICE = os.getenv("TTS_VOICE", "af_heart")
NEWS_FEEDS = os.getenv("NEWS_FEEDS", "REUTERS|https://news.google.com/rss/search?q=when:24h+source:reuters&hl=en-US&gl=US&ceid=US:en, FOX|http://feeds.foxnews.com/foxnews/latest, TECH|https://news.google.com/rss/search?q=when:24h+technology&hl=en-US&gl=US&ceid=US:en, LOCAL|https://news.google.com/rss/search?q=when:24h+Milwaukee+Wisconsin&hl=en-US&gl=US&ceid=US:en")

# --- ENABLE TOOLS ---
ENABLE_CALENDAR = os.getenv("ENABLE_CALENDAR", "false")
ENABLE_OVERSEER = os.getenv("ENABLE_OVERSEER", "false")
ENABLE_NEWS = os.getenv("ENABLE_NEWS", "false")
ENABLE_NASA = os.getenv("ENABLE_NASA", "false")
ENABLE_SEERR = os.getenv("ENABLE_SEERR", "false")
ENABLE_HISTORY = os.getenv("ENABLE_HISTORY", "false")
ENABLE_VOICE = os.getenv("ENABLE_VOICE", "false")
ENABLE_IMAGEGEN = os.getenv("ENABLE_IMAGEGEN", "false")
ENABLE_WEATHER = os.getenv("ENABLE_WEATHER", "false")
ENABLE_SEARCH = os.getenv("ENABLE_SEARCH", "false")
ENABLE_WEB_SCRAPING = os.getenv("ENABLE_WEB_SCRAPING", "false")

# USER PROFILE
USER_NAME = os.getenv("USER_NAME", "User")
USER_LOCATION = os.getenv("USER_LOCATION", "Earth")
USER_TIMEZONE = pytz.timezone(os.getenv("USER_TIMEZONE", "America/New_York"))
USER_BIRTHDAY = os.getenv("USER_BIRTHDAY", "UNKNOWN")
USER_FAMILY = os.getenv("USER_FAMILY", "")
USER_PROFESSION = os.getenv("USER_PROFESSION", "Unemployed")
USER_BIO = f"""User's name: {USER_NAME}.
            {USER_NAME}'s location: {USER_LOCATION}.
            {USER_NAME}'s timezone: {USER_TIMEZONE}.
            {USER_NAME}'s family: {USER_FAMILY}.
            {USER_NAME}'s profession: {USER_PROFESSION}."""
