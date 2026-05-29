import os
import sys
import logging
import pytz
from dotenv import load_dotenv

load_dotenv() # Load docker env variables

# --- LOGGING SETUP ---
class ColoredFormatter(logging.Formatter):
    GREY = "\x1b[90m"
    CYAN = "\x1b[36m"
    GREEN = "\x1b[32m"
    YELLOW = "\x1b[33m"
    RED = "\x1b[31m"
    BOLD_RED = "\x1b[1;31m"
    RESET = "\x1b[0m"

    LEVEL_COLORS = {
        logging.DEBUG: CYAN,
        logging.INFO: GREEN,
        logging.WARNING: YELLOW,
        logging.ERROR: RED,
        logging.CRITICAL: BOLD_RED
    }

    def __init__(self, use_color: bool = True):
        super().__init__()
        self.use_color = use_color

    def format(self, record):
        asctime = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        levelname = f"{record.levelname:<7}"
        message = record.getMessage()

        if self.use_color:
            color = self.LEVEL_COLORS.get(record.levelno, self.RESET)
            formatted_time = f"{self.GREY}{asctime}{self.RESET}"
            formatted_level = f"{color}{levelname}{self.RESET}"
            
            if record.exc_info:
                if not record.exc_text:
                    record.exc_text = self.formatException(record.exc_info)
            if record.exc_text:
                message = f"{message}\n{self.RED}{record.exc_text}{self.RESET}"
                
            return f"{formatted_time} | {formatted_level} | {message}"
        else:
            if record.exc_info:
                if not record.exc_text:
                    record.exc_text = self.formatException(record.exc_info)
            if record.exc_text:
                message = f"{message}\n{record.exc_text}"
            return f"{asctime} | {levelname} | {message}"

# Detect if stdout supports colors
use_color = sys.stdout.isatty() if hasattr(sys, 'stdout') else False

# Custom StreamHandler
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(ColoredFormatter(use_color=use_color))

# Root logger setup
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.handlers = []
root_logger.addHandler(handler)

# Mute noisy logs
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
CAMERA_LOG_FILE_PATH = os.getenv("CAMERA_LOG_FILE_PATH", "camera_log.md") # Path to camera_log.md
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
ENABLE_SCHEDULER = os.getenv("ENABLE_SCHEDULER", "true")
ENABLE_PORTAINER = os.getenv("ENABLE_PORTAINER", "false")
PORTAINER_URL = os.getenv("PORTAINER_URL", "").rstrip("/")
PORTAINER_API_KEY = os.getenv("PORTAINER_API_KEY", "")
PORTAINER_SSL_VERIFY = os.getenv("PORTAINER_SSL_VERIFY", "true").lower() == "true"
JOBS_FILE_PATH = os.getenv("JOBS_FILE_PATH", "custom_jobs.json")
CHAT_DEBOUNCE_DELAY = float(os.getenv("CHAT_DEBOUNCE_DELAY", "4.0"))
MAX_HISTORY_LEN = int(os.getenv("MAX_HISTORY_LEN", "200")) # Max chat history message count
ENABLE_HEARTBEAT = os.getenv("ENABLE_HEARTBEAT", "true").lower() == "true"
HEARTBEAT_INTERVAL_SECONDS = int(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "3600"))
HEARTBEAT_SILENCE_THRESHOLD_SECONDS = int(os.getenv("HEARTBEAT_SILENCE_THRESHOLD_SECONDS", "14400"))
HEARTBEAT_SLEEP_START = os.getenv("HEARTBEAT_SLEEP_START", "21:30")
HEARTBEAT_SLEEP_END = os.getenv("HEARTBEAT_SLEEP_END", "03:30")



# USER PROFILE
PRIMARY_USER_ID = int(os.getenv("PRIMARY_USER_ID", "0"))
SECONDARY_USER_ID = int(os.getenv("SECONDARY_USER_ID", "0"))

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

# SECONDARY USER PROFILE
USER_2_NAME = os.getenv("USER_2_NAME", "Wife")
USER_2_BIRTHDAY = os.getenv("USER_2_BIRTHDAY", "UNKNOWN")
USER_2_PROFESSION = os.getenv("USER_2_PROFESSION", "Unemployed")
USER_2_FAMILY = os.getenv("USER_2_FAMILY", "")
USER_RELATIONSHIP = os.getenv("USER_RELATIONSHIP", "")  # e.g. "married", "siblings", "friends"

def get_user_profile(user_id: int) -> dict:
    """Returns profile details for the given user ID. Fallbacks to primary user profile."""
    if SECONDARY_USER_ID != 0 and user_id == SECONDARY_USER_ID:
        return {
            "name": USER_2_NAME,
            "birthday": USER_2_BIRTHDAY,
            "profession": USER_2_PROFESSION,
            "family": USER_2_FAMILY
        }
    return {
        "name": USER_NAME,
        "birthday": USER_BIRTHDAY,
        "profession": USER_PROFESSION,
        "family": USER_FAMILY
    }

def get_memory_file_path(user_id: int) -> str:
    """Returns the path to the user's specific long-term memory file."""
    if not ENABLE_MEMORY:
        return ""
    if SECONDARY_USER_ID != 0 and user_id == SECONDARY_USER_ID:
        base, ext = os.path.splitext(MEMORY_FILE_PATH)
        name = USER_2_NAME.lower().replace(" ", "_")
        return f"{base}_{name}{ext}"
    return MEMORY_FILE_PATH

