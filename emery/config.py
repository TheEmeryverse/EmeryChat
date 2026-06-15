import json
import logging
import os
import sys
from pathlib import Path

import pytz
from dotenv import load_dotenv
from emery.telegram_utils import normalize_group_chat_id

load_dotenv()


def _to_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_optional_int(value):
    if value in (None, "", 0, "0"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_reolink_channel_map(raw_cameras):
    cameras = {}
    if not isinstance(raw_cameras, dict):
        return cameras

    for name, channel in raw_cameras.items():
        clean_name = str(name).strip()
        clean_channel = str(channel).strip()
        if not clean_name or not clean_channel:
            continue

        try:
            cameras[clean_name] = str(int(clean_channel))
        except (TypeError, ValueError):
            logging.warning(
                "Skipping Reolink camera '%s' because channel value is not an integer: %r",
                clean_name,
                channel,
            )

    return cameras


BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
USERS_CONFIG_PATH = Path(os.getenv("USERS_CONFIG_PATH", str(CONFIG_DIR / "users.json")))
INTEGRATIONS_CONFIG_PATH = Path(os.getenv("INTEGRATIONS_CONFIG_PATH", str(CONFIG_DIR / "integrations.json")))
NEWS_FEEDS_CONFIG_PATH = Path(os.getenv("NEWS_FEEDS_CONFIG_PATH", str(CONFIG_DIR / "news_feeds.json")))
WEATHER_LOCATIONS_FILE_PATH = os.getenv("WEATHER_LOCATIONS_FILE_PATH", str(CONFIG_DIR / "weather_locations.json"))
JOBS_FILE_PATH = os.getenv("JOBS_FILE_PATH", str(CONFIG_DIR / "custom_jobs.json"))
EXPERT_ARCHIVE_DIR = os.getenv("EXPERT_ARCHIVE_DIR", "~/expert")
EXPERT_INDEX_PATH = os.getenv("EXPERT_INDEX_PATH", str(CONFIG_DIR / "expert_sessions.json"))


DEFAULT_NEWS_FEEDS = [
    {
        "name": "Reuters",
        "url": "https://news.google.com/rss/search?q=when:24h+source:reuters&hl=en-US&gl=US&ceid=US:en",
    },
    {"name": "Fox", "url": "http://feeds.foxnews.com/foxnews/latest"},
    {
        "name": "Tech",
        "url": "https://news.google.com/rss/search?q=when:24h+technology&hl=en-US&gl=US&ceid=US:en",
    },
    {
        "name": "Local",
        "url": "https://news.google.com/rss/search?q=when:24h+Milwaukee+Wisconsin&hl=en-US&gl=US&ceid=US:en",
    },
]


def _default_users_config():
    return {
        "allowed_user_ids": [],
        "primary_user": {
            "id": 0,
            "name": "User",
            "location": "Earth",
            "timezone": "America/New_York",
            "birthday": "UNKNOWN",
            "family": "",
            "profession": "Unemployed",
        },
        "secondary_user": {
            "id": 0,
            "name": "Wife",
            "birthday": "UNKNOWN",
            "family": "",
            "profession": "Unemployed",
        },
        "relationship": "",
    }


def _default_integrations_config():
    return {
        "google_calendar_ids": ["primary"],
        "telegram": {
            "group_chat_id": None,
            "security_topic_id": None,
            "routines_topic_id": None,
            "chat_topic_id": None,
            "sticker_set_name": None,
        },
        "reolink": {
            "cameras": {},
            "camera_descriptions": {},
            "silent_alerts": True,
            "threading_enabled": True,
            "thread_window_minutes": 10.0,
            "polling_enabled": False,
        },
        "nest": {
            "project_id": "",
        },
    }


def _ensure_json_file(path: Path, default_data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(json.dumps(default_data, indent=2) + "\n", encoding="utf-8")
        return default_data

    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        repaired = default_data
        path.write_text(json.dumps(repaired, indent=2) + "\n", encoding="utf-8")
        return repaired


def _normalize_users_config(raw):
    default = _default_users_config()
    primary = raw.get("primary_user", {}) if isinstance(raw, dict) else {}
    secondary = raw.get("secondary_user", {}) if isinstance(raw, dict) else {}

    return {
        "allowed_user_ids": [
            user_id
            for user_id in (_to_int(value, 0) for value in raw.get("allowed_user_ids", []))
            if user_id != 0
        ] if isinstance(raw, dict) else [],
        "primary_user": {
            "id": _to_int(primary.get("id"), default["primary_user"]["id"]),
            "name": str(primary.get("name", default["primary_user"]["name"])).strip() or default["primary_user"]["name"],
            "location": str(primary.get("location", default["primary_user"]["location"])).strip() or default["primary_user"]["location"],
            "timezone": str(primary.get("timezone", default["primary_user"]["timezone"])).strip() or default["primary_user"]["timezone"],
            "birthday": str(primary.get("birthday", default["primary_user"]["birthday"])).strip() or default["primary_user"]["birthday"],
            "family": str(primary.get("family", default["primary_user"]["family"])).strip(),
            "profession": str(primary.get("profession", default["primary_user"]["profession"])).strip() or default["primary_user"]["profession"],
        },
        "secondary_user": {
            "id": _to_int(secondary.get("id"), default["secondary_user"]["id"]),
            "name": str(secondary.get("name", default["secondary_user"]["name"])).strip() or default["secondary_user"]["name"],
            "birthday": str(secondary.get("birthday", default["secondary_user"]["birthday"])).strip() or default["secondary_user"]["birthday"],
            "family": str(secondary.get("family", default["secondary_user"]["family"])).strip(),
            "profession": str(secondary.get("profession", default["secondary_user"]["profession"])).strip() or default["secondary_user"]["profession"],
        },
        "relationship": str(raw.get("relationship", default["relationship"])).strip() if isinstance(raw, dict) else "",
    }


def _normalize_integrations_config(raw):
    default = _default_integrations_config()
    telegram = raw.get("telegram", {}) if isinstance(raw, dict) else {}
    reolink = raw.get("reolink", {}) if isinstance(raw, dict) else {}
    nest = raw.get("nest", {}) if isinstance(raw, dict) else {}

    cameras = _normalize_reolink_channel_map(reolink.get("cameras", {}))

    camera_descriptions = {}
    for name, description in reolink.get("camera_descriptions", {}).items():
        clean_name = str(name).strip()
        clean_description = str(description).strip()
        if clean_name and clean_description:
            camera_descriptions[clean_name] = clean_description

    calendar_ids = []
    for calendar_id in raw.get("google_calendar_ids", default["google_calendar_ids"]):
        clean_calendar_id = str(calendar_id).strip()
        if clean_calendar_id:
            calendar_ids.append(clean_calendar_id)
    if not calendar_ids:
        calendar_ids = default["google_calendar_ids"]

    return {
        "google_calendar_ids": calendar_ids,
        "telegram": {
            "group_chat_id": normalize_group_chat_id(_normalize_optional_int(telegram.get("group_chat_id"))),
            "security_topic_id": _normalize_optional_int(telegram.get("security_topic_id")),
            "routines_topic_id": _normalize_optional_int(telegram.get("routines_topic_id")),
            "chat_topic_id": _normalize_optional_int(telegram.get("chat_topic_id")),
            "sticker_set_name": str(telegram.get("sticker_set_name") or "").strip() or None,
        },
        "reolink": {
            "cameras": cameras,
            "camera_descriptions": camera_descriptions,
            "silent_alerts": _to_bool(reolink.get("silent_alerts"), default["reolink"]["silent_alerts"]),
            "threading_enabled": _to_bool(reolink.get("threading_enabled"), default["reolink"]["threading_enabled"]),
            "thread_window_minutes": _to_float(reolink.get("thread_window_minutes"), default["reolink"]["thread_window_minutes"]),
            "polling_enabled": _to_bool(reolink.get("polling_enabled"), default["reolink"]["polling_enabled"]),
        },
        "nest": {
            "project_id": str(nest.get("project_id", default["nest"]["project_id"])).strip(),
        },
    }


def _normalize_news_feeds(raw):
    feeds = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            url = str(item.get("url", "")).strip()
            if name and url:
                feeds.append({"name": name, "url": url})
    return feeds or DEFAULT_NEWS_FEEDS


def _write_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


_raw_users = _ensure_json_file(USERS_CONFIG_PATH, _default_users_config())
_raw_integrations = _ensure_json_file(INTEGRATIONS_CONFIG_PATH, _default_integrations_config())
_raw_news_feeds = _ensure_json_file(NEWS_FEEDS_CONFIG_PATH, DEFAULT_NEWS_FEEDS)
_ensure_json_file(Path(WEATHER_LOCATIONS_FILE_PATH), {})
_ensure_json_file(Path(JOBS_FILE_PATH), [])
_ensure_json_file(Path(EXPERT_INDEX_PATH), [])

USERS_CONFIG = _normalize_users_config(_raw_users)
INTEGRATIONS_CONFIG = _normalize_integrations_config(_raw_integrations)
NEWS_FEEDS = _normalize_news_feeds(_raw_news_feeds)

_write_json(USERS_CONFIG_PATH, USERS_CONFIG)
_write_json(INTEGRATIONS_CONFIG_PATH, INTEGRATIONS_CONFIG)
_write_json(NEWS_FEEDS_CONFIG_PATH, NEWS_FEEDS)


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
        logging.CRITICAL: BOLD_RED,
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

            if record.exc_info and not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
            if record.exc_text:
                message = f"{message}\n{self.RED}{record.exc_text}{self.RESET}"

            return f"{formatted_time} | {formatted_level} | {message}"

        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            message = f"{message}\n{record.exc_text}"
        return f"{asctime} | {levelname} | {message}"


def _resolve_log_level(raw_level: str) -> int:
    level_name = str(raw_level or "INFO").strip().upper()
    return getattr(logging, level_name, logging.INFO)


use_color = sys.stdout.isatty() if hasattr(sys, "stdout") else False
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(ColoredFormatter(use_color=use_color))

root_logger = logging.getLogger()
root_logger.setLevel(_resolve_log_level(os.getenv("LOG_LEVEL", "INFO")))
root_logger.handlers = []
root_logger.addHandler(handler)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)


MODEL_NAME = os.getenv("MODEL_NAME", "Emery")
MAIN_MODEL_URL = os.getenv("MAIN_MODEL_URL") or os.getenv("OLLAMA_URL", "http://127.0.0.1:8081/v1/chat/completions")
OPEN_WEBUI_KEY = os.getenv("OPEN_WEBUI_KEY", "blank")
THINK = _to_bool(os.getenv("ENABLE_THINKING") or os.getenv("THINK"), True)
MODEL_ID = os.getenv("MODEL_ID", "local")
VISION_MODEL_ID = os.getenv("VISION_MODEL_ID", "gemma4:e2b")
VISION_OLLAMA_URL = os.getenv("VISION_OLLAMA_URL", "http://192.168.1.129:11434/api/chat")
FAST_MODEL_ID = os.getenv("FAST_MODEL_ID", VISION_MODEL_ID)
FAST_MODEL_URL = os.getenv("FAST_MODEL_URL", "http://127.0.0.1:8082/v1/chat/completions")
EXPERT_MAIN_MAX_TOKENS = _to_int(os.getenv("EXPERT_MAIN_MAX_TOKENS"), 32768)
EXPERT_FAST_MAX_TOKENS = _to_int(os.getenv("EXPERT_FAST_MAX_TOKENS"), 8192)
EXPERT_DEFAULT_TARGET_SOURCES = _to_int(os.getenv("EXPERT_DEFAULT_TARGET_SOURCES"), 24)
EXPERT_MIN_TARGET_SOURCES = _to_int(os.getenv("EXPERT_MIN_TARGET_SOURCES"), 12)
EXPERT_MAX_SOURCES = _to_int(os.getenv("EXPERT_MAX_SOURCES"), 80)
EXPERT_ALLOW_MIDLOOP_QUESTIONS = _to_bool(os.getenv("EXPERT_ALLOW_MIDLOOP_QUESTIONS"), False)
EXPERT_MAX_AGENDA_QUESTIONS = _to_int(os.getenv("EXPERT_MAX_AGENDA_QUESTIONS"), 12)
EXPERT_MAX_NEW_QUESTIONS = _to_int(os.getenv("EXPERT_MAX_NEW_QUESTIONS"), 5)
EXPERT_MAX_SUBTASKS_PER_QUESTION = _to_int(os.getenv("EXPERT_MAX_SUBTASKS_PER_QUESTION"), 3)
EXPERT_MAIN_ENABLE_THINKING = _to_bool(os.getenv("EXPERT_MAIN_ENABLE_THINKING"), THINK)
EXPERT_MAIN_TEMPERATURE = _to_float(os.getenv("EXPERT_MAIN_TEMPERATURE"), 1.0)
EXPERT_MAIN_TOP_P = _to_float(os.getenv("EXPERT_MAIN_TOP_P"), 0.95)
EXPERT_MAIN_TOP_K = _to_int(os.getenv("EXPERT_MAIN_TOP_K"), 20)
EXPERT_MAIN_MIN_P = _to_float(os.getenv("EXPERT_MAIN_MIN_P"), 0.0)
EXPERT_MAIN_PRESENCE_PENALTY = _to_float(os.getenv("EXPERT_MAIN_PRESENCE_PENALTY"), 1.5)
EXPERT_MAIN_REPETITION_PENALTY = _to_float(os.getenv("EXPERT_MAIN_REPETITION_PENALTY"), 1.0)
EXPERT_FAST_ENABLE_THINKING = _to_bool(os.getenv("EXPERT_FAST_ENABLE_THINKING"), False)
EXPERT_FAST_TEMPERATURE = _to_float(os.getenv("EXPERT_FAST_TEMPERATURE"), 0.2)
EXPERT_FAST_TOP_P = _to_float(os.getenv("EXPERT_FAST_TOP_P"), 0.9)
EXPERT_FAST_TOP_K = _to_int(os.getenv("EXPERT_FAST_TOP_K"), 40)
EXPERT_FAST_MIN_P = _to_float(os.getenv("EXPERT_FAST_MIN_P"), 0.0)
EXPERT_FAST_PRESENCE_PENALTY = _to_float(os.getenv("EXPERT_FAST_PRESENCE_PENALTY"), 0.0)
EXPERT_FAST_REPETITION_PENALTY = _to_float(os.getenv("EXPERT_FAST_REPETITION_PENALTY"), 1.0)
EMBEDDING_MODEL_ID = os.getenv("EMBEDDING_MODEL_ID", "nomic-embed-text")
EMBEDDING_OLLAMA_URL = os.getenv("EMBEDDING_OLLAMA_URL", "http://localhost:11434/api/embed")
OLLAMA_VISION_NUM_CTX = _to_int(os.getenv("OLLAMA_VISION_NUM_CTX"), 65536)
ENABLE_MEMORY = _to_bool(os.getenv("ENABLE_MEMORY"), True)
MEMORY_STORE_PATH = os.getenv("MEMORY_STORE_PATH", "data/memory/memory_store.json")
CAMERA_LOG_FILE_PATH = os.getenv("CAMERA_LOG_FILE_PATH", "data/logs/camera_log.md")
MEMORY_THRESHOLD = _to_int(os.getenv("MEMORY_THRESHOLD"), 4000)
ALLOW_UNRESTRICTED_TELEGRAM_ACCESS = _to_bool(os.getenv("ALLOW_UNRESTRICTED_TELEGRAM_ACCESS"), False)
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://localhost:8080/search")
ALLOW_PRIVATE_WEB_FETCH = _to_bool(os.getenv("ALLOW_PRIVATE_WEB_FETCH"), False)
NASA_API_KEY = os.getenv("NASA_API_KEY", "blank")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "blank")
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "gemini-3.1-flash-image-preview")
NOAA_LAT = os.getenv("NOAA_LAT", "").strip()
NOAA_LONG = os.getenv("NOAA_LONG", "").strip()
NOAA_EMAIL = os.getenv("NOAA_EMAIL", "example@example.com").strip()
TOOL_LOOP = _to_int(os.getenv("TOOL_LOOP"), 15)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "blank")
OVERSEER_URL = os.getenv("OVERSEER_URL", "http://localhost:5055/api/v1")
OVERSEER_KEY = os.getenv("OVERSEER_KEY", "blank")
OVERSEER_USER_ID = os.getenv("OVERSEER_USER_ID", "1")
STT_URL = os.getenv("STT_URL", "http://localhost:3000/api/v1/audio/transcriptions")
TTS_URL = os.getenv("TTS_URL", "http://localhost:8880/v1/audio/speech")
TTS_VOICE = os.getenv("TTS_VOICE", "af_heart")
FRED_API_KEY = os.getenv("FRED_API_KEY", "")
ALPHA_VANTAGE_API_KEY = os.getenv("ALPHA_VANTAGE_API_KEY", "")
GOOGLE_TOKEN_PATH = os.getenv("GOOGLE_TOKEN_PATH", "secrets/google/token.json")
NEST_TOKEN_PATH = os.getenv("NEST_TOKEN_PATH", "secrets/google/nest_token.json")
GIPHY_API_KEY = os.getenv("GIPHY_API_KEY", "dc6zaTOxFJmzC")
TENOR_API_KEY = os.getenv("TENOR_API_KEY", "LIVDTRZ9VRJH")

ENABLE_CALENDAR = _to_bool(os.getenv("ENABLE_CALENDAR"))
ENABLE_OVERSEER = _to_bool(os.getenv("ENABLE_OVERSEER"))
ENABLE_NEWS = _to_bool(os.getenv("ENABLE_NEWS"))
ENABLE_NASA = _to_bool(os.getenv("ENABLE_NASA"))
ENABLE_SEERR = _to_bool(os.getenv("ENABLE_SEERR"))
ENABLE_HISTORY = _to_bool(os.getenv("ENABLE_HISTORY"))
ENABLE_VOICE = _to_bool(os.getenv("ENABLE_VOICE"))
ENABLE_IMAGEGEN = _to_bool(os.getenv("ENABLE_IMAGEGEN"))
ENABLE_WEATHER = _to_bool(os.getenv("ENABLE_WEATHER"))
ENABLE_SEARCH = _to_bool(os.getenv("ENABLE_SEARCH"))
ENABLE_WEB_SCRAPING = _to_bool(os.getenv("ENABLE_WEB_SCRAPING"))
ENABLE_FINANCE = _to_bool(os.getenv("ENABLE_FINANCE"))
ENABLE_SCHEDULER = _to_bool(os.getenv("ENABLE_SCHEDULER"), True)
ENABLE_PORTAINER = _to_bool(os.getenv("ENABLE_PORTAINER"))
ENABLE_HEARTBEAT = _to_bool(os.getenv("ENABLE_HEARTBEAT"), True)
ENABLE_NEST = _to_bool(os.getenv("ENABLE_NEST"))
ENABLE_REOLINK = _to_bool(os.getenv("ENABLE_REOLINK"))
ENABLE_SYSTEM_STATS = _to_bool(os.getenv("ENABLE_SYSTEM_STATS"))
ENABLE_TELEGRAM_RICH_MESSAGES = _to_bool(os.getenv("ENABLE_TELEGRAM_RICH_MESSAGES"), True)
ENABLE_ROUTINE_CACHE_WARMUP = _to_bool(os.getenv("ENABLE_ROUTINE_CACHE_WARMUP"), True)

PORTAINER_URL = os.getenv("PORTAINER_URL", "").rstrip("/")
PORTAINER_API_KEY = os.getenv("PORTAINER_API_KEY", "")
PORTAINER_SSL_VERIFY = _to_bool(os.getenv("PORTAINER_SSL_VERIFY"), True)
CHAT_DEBOUNCE_DELAY = _to_float(os.getenv("CHAT_DEBOUNCE_DELAY"), 4.0)
HEARTBEAT_INTERVAL_SECONDS = _to_int(os.getenv("HEARTBEAT_INTERVAL_SECONDS"), 3600)
HEARTBEAT_SILENCE_THRESHOLD_SECONDS = _to_int(os.getenv("HEARTBEAT_SILENCE_THRESHOLD_SECONDS"), 14400)
HEARTBEAT_SILENT_RETRY_SECONDS = _to_int(os.getenv("HEARTBEAT_SILENT_RETRY_SECONDS"), 3600)
HEARTBEAT_PROACTIVE_COOLDOWN_SECONDS = _to_int(os.getenv("HEARTBEAT_PROACTIVE_COOLDOWN_SECONDS"), 14400)
HEARTBEAT_DAILY_PROACTIVE_LIMIT = _to_int(os.getenv("HEARTBEAT_DAILY_PROACTIVE_LIMIT"), 2)
HEARTBEAT_SLEEP_START = os.getenv("HEARTBEAT_SLEEP_START", "21:30")
HEARTBEAT_SLEEP_END = os.getenv("HEARTBEAT_SLEEP_END", "03:30")
ROUTINE_HISTORY_DEFER_SECONDS = _to_int(os.getenv("ROUTINE_HISTORY_DEFER_SECONDS"), 600)

PRIMARY_USER_ID = USERS_CONFIG["primary_user"]["id"]
SECONDARY_USER_ID = USERS_CONFIG["secondary_user"]["id"]
ALLOWED_USER_IDS = USERS_CONFIG["allowed_user_ids"]
USER_NAME = USERS_CONFIG["primary_user"]["name"]
USER_LOCATION = USERS_CONFIG["primary_user"]["location"]
USER_TIMEZONE = pytz.timezone(USERS_CONFIG["primary_user"]["timezone"])
USER_BIRTHDAY = USERS_CONFIG["primary_user"]["birthday"]
USER_FAMILY = USERS_CONFIG["primary_user"]["family"]
USER_PROFESSION = USERS_CONFIG["primary_user"]["profession"]
USER_2_NAME = USERS_CONFIG["secondary_user"]["name"]
USER_2_BIRTHDAY = USERS_CONFIG["secondary_user"]["birthday"]
USER_2_PROFESSION = USERS_CONFIG["secondary_user"]["profession"]
USER_2_FAMILY = USERS_CONFIG["secondary_user"]["family"]
USER_RELATIONSHIP = USERS_CONFIG["relationship"]
USER_BIO = (
    f"User's name: {USER_NAME}.\n"
    f"{USER_NAME}'s location: {USER_LOCATION}.\n"
    f"{USER_NAME}'s timezone: {USER_TIMEZONE}.\n"
    f"{USER_NAME}'s family: {USER_FAMILY}.\n"
    f"{USER_NAME}'s profession: {USER_PROFESSION}."
)

calendar_ids = INTEGRATIONS_CONFIG["google_calendar_ids"]
raw_cal_string = ",".join(calendar_ids)
TELEGRAM_GROUP_CHAT_ID = INTEGRATIONS_CONFIG["telegram"]["group_chat_id"]
SECURITY_TOPIC_ID = INTEGRATIONS_CONFIG["telegram"]["security_topic_id"]
ROUTINES_TOPIC_ID = INTEGRATIONS_CONFIG["telegram"]["routines_topic_id"]
CHAT_TOPIC_ID = INTEGRATIONS_CONFIG["telegram"]["chat_topic_id"]
TELEGRAM_STICKER_SET = INTEGRATIONS_CONFIG["telegram"]["sticker_set_name"]

REOLINK_CAMERAS = INTEGRATIONS_CONFIG["reolink"]["cameras"]
REOLINK_CAMERA_DESCRIPTIONS = INTEGRATIONS_CONFIG["reolink"]["camera_descriptions"]
REOLINK_SILENT_ALERTS = INTEGRATIONS_CONFIG["reolink"]["silent_alerts"]
ENABLE_REOLINK_THREADING = INTEGRATIONS_CONFIG["reolink"]["threading_enabled"]
REOLINK_THREAD_WINDOW_MINUTES = INTEGRATIONS_CONFIG["reolink"]["thread_window_minutes"]
ENABLE_REOLINK_POLLING = INTEGRATIONS_CONFIG["reolink"]["polling_enabled"]
NEST_PROJECT_ID = INTEGRATIONS_CONFIG["nest"]["project_id"]

REOLINK_HOST = os.getenv("REOLINK_HOST", "").strip()
REOLINK_USER = os.getenv("REOLINK_USER", "").strip()
REOLINK_PASSWORD = os.getenv("REOLINK_PASSWORD", "").strip()


def get_user_profile(user_id: int) -> dict:
    if SECONDARY_USER_ID != 0 and user_id == SECONDARY_USER_ID:
        return {
            "name": USER_2_NAME,
            "birthday": USER_2_BIRTHDAY,
            "profession": USER_2_PROFESSION,
            "family": USER_2_FAMILY,
        }
    return {
        "name": USER_NAME,
        "birthday": USER_BIRTHDAY,
        "profession": USER_PROFESSION,
        "family": USER_FAMILY,
    }
