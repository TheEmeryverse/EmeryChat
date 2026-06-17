#!/usr/bin/env python3
import argparse
import json
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
BASE_DIR = REPO_DIR
BACKUPS_DIR = REPO_DIR / "backups"
CONFIG_DIR = REPO_DIR / "config"
ENV_PATH = REPO_DIR / ".env"
USERS_CONFIG_PATH = CONFIG_DIR / "users.json"
INTEGRATIONS_CONFIG_PATH = CONFIG_DIR / "integrations.json"
NEWS_FEEDS_CONFIG_PATH = CONFIG_DIR / "news_feeds.json"
WEATHER_LOCATIONS_PATH = CONFIG_DIR / "weather_locations.json"
CUSTOM_JOBS_PATH = CONFIG_DIR / "custom_jobs.json"
MEMORY_PATH = REPO_DIR / "memory.md"
CAMERA_LOG_PATH = REPO_DIR / "data" / "logs" / "camera_log.md"


DEFAULT_ENV = {
    "MODEL_NAME": "Emery",
    "MODEL_ID": "local",
    "FAST_MODEL_ID": "gemma4:e4b",
    "VISION_MODEL_ID": "gemma4:e4b",
    "EMBEDDING_MODEL_ID": "nomic-embed-text",
    "MAIN_MODEL_URL": "http://127.0.0.1:8081/v1/chat/completions",
    "FAST_MODEL_URL": "http://127.0.0.1:8082/v1/chat/completions",
    "VISION_OLLAMA_URL": "http://localhost:11434/api/chat",
    "EMBEDDING_OLLAMA_URL": "http://localhost:11434/api/embed",
    "OLLAMA_VISION_NUM_CTX": "65536",
    "OPEN_WEBUI_KEY": "YOUR_OPEN_WEBUI_KEY",
    "STT_URL": "http://localhost:3000/api/v1/audio/transcriptions",
    "TTS_URL": "http://localhost:8880/v1/audio/speech",
    "TTS_VOICE": "af_heart",
    "DOCLING_URL": "",
    "DOCLING_BEARER_TOKEN": "",
    "ENABLE_CALENDAR": "true",
    "ENABLE_WEATHER": "true",
    "ENABLE_NEWS": "true",
    "ENABLE_NASA": "true",
    "ENABLE_SEERR": "true",
    "ENABLE_HISTORY": "true",
    "ENABLE_VOICE": "true",
    "ENABLE_IMAGEGEN": "true",
    "ENABLE_SEARCH": "true",
    "ENABLE_WEB_SCRAPING": "true",
    "ENABLE_DOCLING": "true",
    "ENABLE_YOUTUBE_TRANSCRIPT": "true",
    "ENABLE_FINANCE": "false",
    "ENABLE_SYSTEM_STATS": "true",
    "ENABLE_NEST": "false",
    "ENABLE_REOLINK": "false",
    "ENABLE_SCHEDULER": "true",
    "ENABLE_HEARTBEAT": "true",
    "ENABLE_MEMORY": "true",
    "ALLOW_UNRESTRICTED_TELEGRAM_ACCESS": "false",
    "NASA_API_KEY": "YOUR_NASA_API_KEY",
    "GEMINI_API_KEY": "YOUR_GEMINI_API_KEY",
    "FRED_API_KEY": "YOUR_FRED_API_KEY",
    "ALPHA_VANTAGE_API_KEY": "YOUR_ALPHA_VANTAGE_API_KEY",
    "OVERSEER_URL": "http://localhost:5055/api/v1",
    "OVERSEER_KEY": "YOUR_OVERSEER_KEY",
    "OVERSEER_USER_ID": "1",
    "SEARXNG_URL": "http://localhost:8080/search",
    "ALLOW_PRIVATE_WEB_FETCH": "false",
    "NOAA_EMAIL": "example@example.com",
    "PORTAINER_URL": "",
    "PORTAINER_API_KEY": "",
    "PORTAINER_SSL_VERIFY": "true",
    "REOLINK_HOST": "",
    "REOLINK_USER": "",
    "REOLINK_PASSWORD": "",
    "GIPHY_API_KEY": "YOUR_GIPHY_API_KEY",
    "TENOR_API_KEY": "YOUR_TENOR_API_KEY",
    "MEMORY_STORE_PATH": "data/memory/memory_store.json",
    "CAMERA_LOG_FILE_PATH": "data/logs/camera_log.md",
    "GOOGLE_TOKEN_PATH": "secrets/google/token.json",
    "NEST_TOKEN_PATH": "secrets/google/nest_token.json",
    "TOOL_LOOP": "15",
    "CHAT_DEBOUNCE_DELAY": "4.0",
    "EXPERT_ARCHIVE_DIR": "data/expert",
    "EXPERT_INDEX_PATH": "config/expert_sessions.json",
    "EXPERT_MAIN_MAX_TOKENS": "32768",
    "EXPERT_FAST_MAX_TOKENS": "8192",
    "EXPERT_DEFAULT_TARGET_SOURCES": "24",
    "EXPERT_MIN_TARGET_SOURCES": "12",
    "EXPERT_MAX_SOURCES": "80",
    "EXPERT_ALLOW_MIDLOOP_QUESTIONS": "false",
    "EXPERT_MAX_AGENDA_QUESTIONS": "12",
    "EXPERT_MAX_NEW_QUESTIONS": "5",
    "EXPERT_MAX_SUBTASKS_PER_QUESTION": "3",
    "MEMORY_THRESHOLD": "4000",
    "HEARTBEAT_INTERVAL_SECONDS": "3600",
    "HEARTBEAT_SILENCE_THRESHOLD_SECONDS": "14400",
    "HEARTBEAT_SILENT_RETRY_SECONDS": "3600",
    "HEARTBEAT_PROACTIVE_COOLDOWN_SECONDS": "14400",
    "HEARTBEAT_DAILY_PROACTIVE_LIMIT": "2",
    "HEARTBEAT_SLEEP_START": "21:30",
    "HEARTBEAT_SLEEP_END": "03:30",
    "NOAA_LAT": "",
    "NOAA_LONG": "",
    "TELEGRAM_TOKEN": "",
}


DEFAULT_USERS = {
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


DEFAULT_INTEGRATIONS = {
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


BANNER = r"""
 ______ __  __ ______ _______   __   ______ _    _       _______ 
|  ____|  \/  |  ____|  __ \ \ / /  / ____| |  | |   /\|__   __|
| |__  | \  / | |__  | |__) \ V /  | |    | |__| |  /  \  | |   
|  __| | |\/| |  __| |  _  / > <   | |    |  __  | / /\ \ | |   
| |____| |  | | |____| | \ \/ . \  | |____| |  | |/ ____ \| |   
|______|_|  |_|______|_|  \_/_/ \_\  \_____|_|  |_/_/    \_\_|   
"""


def load_dotenv_like(path: Path) -> dict:
    data = {}
    if not path.exists():
        return data
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.split(" #", 1)[0].split("#", 1)[0].strip()
        value = value.strip().strip('"').strip("'")
        data[key.strip()] = value
    return data


def load_json(path: Path, default):
    if not path.exists():
        return json.loads(json.dumps(default))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return json.loads(json.dumps(default))


def clone_default(value):
    return json.loads(json.dumps(value))


def parse_bool(value, default=False):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_int(value, default=0):
    try:
        return int(str(value).strip())
    except Exception:
        return default


def parse_float(value, default=0.0):
    try:
        return float(str(value).strip())
    except Exception:
        return default


def parse_csv_ints(value):
    if not value:
        return []
    output = []
    for item in str(value).split(","):
        item = item.strip()
        if item:
            try:
                output.append(int(item))
            except ValueError:
                pass
    return output


def parse_calendar_ids(value):
    if not value:
        return ["primary"]
    items = [item.strip() for item in str(value).split(",") if item.strip()]
    return items or ["primary"]


def parse_news_feeds(value):
    feeds = []
    if not value:
        return feeds
    for item in str(value).split(","):
        if "|" not in item:
            continue
        name, url = item.split("|", 1)
        name = name.strip()
        url = url.strip()
        if name and url:
            feeds.append({"name": name, "url": url})
    return feeds


def parse_name_map(value):
    output = {}
    if not value:
        return output
    pattern = re.compile(r"(?:^|,)\s*([^:,]+?)\s*:\s*(.*?)(?=(?:,\s*[^:,]+?\s*:)|$)")
    for name, mapped in pattern.findall(str(value)):
        name = str(name).strip()
        mapped = str(mapped).strip()
        if name and mapped:
            output[name] = mapped
    return output


def normalize_optional_int(value):
    if value in (None, "", 0, "0"):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def normalize_reolink_channels(raw_cameras):
    cameras = {}
    if not isinstance(raw_cameras, dict):
        return cameras

    for name, channel in raw_cameras.items():
        clean_name = str(name).strip()
        if not clean_name:
            continue
        try:
            cameras[clean_name] = str(int(str(channel).strip()))
        except (TypeError, ValueError):
            continue
    return cameras


def normalize_descriptions(raw_descriptions):
    descriptions = {}
    if not isinstance(raw_descriptions, dict):
        return descriptions

    for name, description in raw_descriptions.items():
        clean_name = str(name).strip()
        clean_description = str(description).strip()
        if clean_name and clean_description:
            descriptions[clean_name] = clean_description
    return descriptions


def normalize_integrations_seed(raw):
    integrations = clone_default(DEFAULT_INTEGRATIONS)
    if not isinstance(raw, dict):
        return integrations

    telegram = raw.get("telegram", {})
    reolink = raw.get("reolink", {})
    nest = raw.get("nest", {})

    calendar_ids = []
    for calendar_id in raw.get("google_calendar_ids", integrations["google_calendar_ids"]):
        clean_calendar_id = str(calendar_id).strip()
        if clean_calendar_id:
            calendar_ids.append(clean_calendar_id)
    integrations["google_calendar_ids"] = calendar_ids or integrations["google_calendar_ids"]

    if isinstance(telegram, dict):
        integrations["telegram"]["group_chat_id"] = normalize_optional_int(telegram.get("group_chat_id"))
        integrations["telegram"]["security_topic_id"] = normalize_optional_int(telegram.get("security_topic_id"))
        integrations["telegram"]["routines_topic_id"] = normalize_optional_int(telegram.get("routines_topic_id"))
        integrations["telegram"]["chat_topic_id"] = normalize_optional_int(telegram.get("chat_topic_id"))
        sticker_set_name = str(telegram.get("sticker_set_name") or "").strip()
        integrations["telegram"]["sticker_set_name"] = sticker_set_name or None

    if isinstance(reolink, dict):
        integrations["reolink"]["cameras"] = normalize_reolink_channels(reolink.get("cameras", {}))
        integrations["reolink"]["camera_descriptions"] = normalize_descriptions(reolink.get("camera_descriptions", {}))
        integrations["reolink"]["silent_alerts"] = parse_bool(
            reolink.get("silent_alerts"),
            integrations["reolink"]["silent_alerts"],
        )
        integrations["reolink"]["threading_enabled"] = parse_bool(
            reolink.get("threading_enabled"),
            integrations["reolink"]["threading_enabled"],
        )
        integrations["reolink"]["thread_window_minutes"] = parse_float(
            reolink.get("thread_window_minutes"),
            integrations["reolink"]["thread_window_minutes"],
        )
        integrations["reolink"]["polling_enabled"] = parse_bool(
            reolink.get("polling_enabled"),
            integrations["reolink"]["polling_enabled"],
        )

    if isinstance(nest, dict):
        integrations["nest"]["project_id"] = str(nest.get("project_id", "")).strip()

    return integrations


def normalize_users_seed(raw):
    users = clone_default(DEFAULT_USERS)
    if not isinstance(raw, dict):
        return users

    primary = raw.get("primary_user", {})
    secondary = raw.get("secondary_user", {})

    if isinstance(raw.get("allowed_user_ids"), list):
        users["allowed_user_ids"] = [
            user_id
            for user_id in (parse_int(value, 0) for value in raw.get("allowed_user_ids", []))
            if user_id != 0
        ]

    if isinstance(primary, dict):
        users["primary_user"]["id"] = parse_int(primary.get("id"), users["primary_user"]["id"])
        for key in ("name", "location", "timezone", "birthday", "family", "profession"):
            value = str(primary.get(key, users["primary_user"][key])).strip()
            users["primary_user"][key] = value or users["primary_user"][key]

    if isinstance(secondary, dict):
        users["secondary_user"]["id"] = parse_int(secondary.get("id"), users["secondary_user"]["id"])
        for key in ("name", "birthday", "family", "profession"):
            value = str(secondary.get(key, users["secondary_user"][key])).strip()
            users["secondary_user"][key] = value or users["secondary_user"][key]

    users["relationship"] = str(raw.get("relationship", users["relationship"])).strip()
    return users


def normalize_news_seed(raw):
    feeds = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            url = str(item.get("url", "")).strip()
            if name and url:
                feeds.append({"name": name, "url": url})
    return feeds or clone_default(DEFAULT_NEWS_FEEDS)


def seeded_text(current_value, env_value, default_value=""):
    current = str(current_value or "").strip()
    env = str(env_value or "").strip()
    default = str(default_value or "").strip()
    if not current or current == default:
        return env or current or default
    return current


def seeded_int(current_value, env_value, default_value=0):
    current = parse_int(current_value, default_value)
    if current == default_value:
        env_parsed = parse_int(env_value, default_value)
        if env_parsed != default_value:
            return env_parsed
    return current


def seeded_list(current_value, env_value, default_value=None, parser=None):
    default = default_value or []
    current = list(current_value) if isinstance(current_value, list) else []
    env_parsed = parser(env_value) if parser else []
    if not current or current == default:
        return env_parsed or current or list(default)
    return current


def validate_url(value):
    parsed = urlparse(value)
    return bool(parsed.scheme and parsed.netloc)


def prompt_text(label, default=None, required=False, validator=None, help_text=None):
    while True:
        suffix = f" [{default}]" if default not in (None, "") else ""
        raw = input(f"{label}{suffix}: ").strip()
        if not raw:
            raw = default if default is not None else ""
        if required and str(raw).strip() == "":
            print("  This field is required.")
            continue
        if validator and str(raw).strip():
            valid, message = validator(str(raw).strip())
            if not valid:
                print(f"  {message}")
                if help_text:
                    print(f"  {help_text}")
                continue
        return str(raw).strip()


def prompt_yes_no(label, default=True):
    default_label = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{label} [{default_label}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes"}:
            return True
        if raw in {"n", "no"}:
            return False
        print("  Enter y or n.")


def prompt_int(label, default=None, allow_blank=False):
    while True:
        suffix = f" [{default}]" if default not in (None, "") else ""
        raw = input(f"{label}{suffix}: ").strip()
        if not raw:
            if allow_blank:
                return None
            raw = str(default) if default is not None else ""
        if raw == "" and allow_blank:
            return None
        try:
            return int(raw)
        except ValueError:
            print("  Enter a whole number.")


def prompt_float(label, default=None):
    while True:
        suffix = f" [{default}]" if default not in (None, "") else ""
        raw = input(f"{label}{suffix}: ").strip()
        if not raw:
            raw = str(default) if default is not None else ""
        try:
            return float(raw)
        except ValueError:
            print("  Enter a number.")


def prompt_csv_ids(label, default_ids):
    default_text = ",".join(str(item) for item in default_ids) if default_ids else ""
    raw = prompt_text(label, default_text)
    return parse_csv_ints(raw)


def validate_timezone(value):
    try:
        ZoneInfo(value)
        return True, ""
    except Exception:
        return False, "Enter a valid IANA timezone like America/Chicago."


def validate_url_prompt(value):
    if validate_url(value):
        return True, ""
    return False, "Enter a full URL such as http://localhost:11434/api/chat."


def validate_reolink_channel(value):
    try:
        parsed = int(str(value).strip())
    except ValueError:
        return False, "Enter a whole-number camera channel like 0, 1, or 5."
    if parsed < 0:
        return False, "Camera channel must be 0 or greater."
    return True, ""


def print_banner():
    print(BANNER.rstrip())
    print("Interactive setup for EmeryChat")
    print("=" * 56)


def print_section(title, subtitle=None):
    print(f"\n== {title} ==")
    if subtitle:
        print(subtitle)


def backup_if_needed(path: Path):
    if path.exists():
        relative_path = path.relative_to(REPO_DIR)
        backup_name = relative_path.name + ".bak"
        backup_path = BACKUPS_DIR / relative_path.parent / backup_name
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup_path)


def write_text_file(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_if_needed(path)
    path.write_text(content, encoding="utf-8")


def write_json_file(path: Path, data):
    write_text_file(path, json.dumps(data, indent=2) + "\n")


def ensure_file(path: Path, content: str = ""):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def bool_to_env(value: bool) -> str:
    return "true" if value else "false"


def env_value(value):
    return "" if value is None else str(value)


def maybe_quote(value: str) -> str:
    value = env_value(value)
    if any(ch in value for ch in [" ", "#"]):
        return f'"{value}"'
    return value


def build_env_content(env_data):
    lines = [
        "TELEGRAM_TOKEN=" + maybe_quote(env_data["TELEGRAM_TOKEN"]),
        "ALLOW_UNRESTRICTED_TELEGRAM_ACCESS=" + env_value(env_data["ALLOW_UNRESTRICTED_TELEGRAM_ACCESS"]),
        "",
        "# Model information",
        "MODEL_NAME=" + maybe_quote(env_data["MODEL_NAME"]),
        "MODEL_ID=" + maybe_quote(env_data["MODEL_ID"]),
        "FAST_MODEL_ID=" + maybe_quote(env_data["FAST_MODEL_ID"]),
        "VISION_MODEL_ID=" + maybe_quote(env_data["VISION_MODEL_ID"]),
        "EMBEDDING_MODEL_ID=" + maybe_quote(env_data["EMBEDDING_MODEL_ID"]),
        "MAIN_MODEL_URL=" + maybe_quote(env_data["MAIN_MODEL_URL"]),
        "FAST_MODEL_URL=" + maybe_quote(env_data["FAST_MODEL_URL"]),
        "VISION_OLLAMA_URL=" + maybe_quote(env_data["VISION_OLLAMA_URL"]),
        "EMBEDDING_OLLAMA_URL=" + maybe_quote(env_data["EMBEDDING_OLLAMA_URL"]),
        "OLLAMA_VISION_NUM_CTX=" + env_value(env_data["OLLAMA_VISION_NUM_CTX"]),
        "",
        "OPEN_WEBUI_KEY=" + maybe_quote(env_data["OPEN_WEBUI_KEY"]),
        "STT_URL=" + maybe_quote(env_data["STT_URL"]),
        "TTS_URL=" + maybe_quote(env_data["TTS_URL"]),
        "TTS_VOICE=" + maybe_quote(env_data["TTS_VOICE"]),
        "DOCLING_URL=" + maybe_quote(env_data["DOCLING_URL"]),
        "DOCLING_BEARER_TOKEN=" + maybe_quote(env_data["DOCLING_BEARER_TOKEN"]),
        "",
        "# Feature flags",
    ]

    feature_keys = [
        "ENABLE_CALENDAR", "ENABLE_WEATHER", "ENABLE_NEWS", "ENABLE_NASA",
        "ENABLE_SEERR", "ENABLE_HISTORY", "ENABLE_VOICE", "ENABLE_IMAGEGEN",
        "ENABLE_SEARCH", "ENABLE_WEB_SCRAPING", "ENABLE_YOUTUBE_TRANSCRIPT",
        "ENABLE_DOCLING",
        "ENABLE_FINANCE", "ENABLE_SYSTEM_STATS", "ENABLE_NEST", "ENABLE_REOLINK",
        "ENABLE_SCHEDULER", "ENABLE_HEARTBEAT", "ENABLE_MEMORY", "ENABLE_PORTAINER",
    ]
    lines.extend(f"{key}={env_value(env_data[key])}" for key in feature_keys)
    lines.extend([
        "",
        "# Secrets / API keys",
        "NASA_API_KEY=" + maybe_quote(env_data["NASA_API_KEY"]),
        "GEMINI_API_KEY=" + maybe_quote(env_data["GEMINI_API_KEY"]),
        "FRED_API_KEY=" + maybe_quote(env_data["FRED_API_KEY"]),
        "ALPHA_VANTAGE_API_KEY=" + maybe_quote(env_data["ALPHA_VANTAGE_API_KEY"]),
        "OVERSEER_URL=" + maybe_quote(env_data["OVERSEER_URL"]),
        "OVERSEER_KEY=" + maybe_quote(env_data["OVERSEER_KEY"]),
        "OVERSEER_USER_ID=" + env_value(env_data["OVERSEER_USER_ID"]),
        "SEARXNG_URL=" + maybe_quote(env_data["SEARXNG_URL"]),
        "ALLOW_PRIVATE_WEB_FETCH=" + env_value(env_data["ALLOW_PRIVATE_WEB_FETCH"]),
        "NOAA_EMAIL=" + maybe_quote(env_data["NOAA_EMAIL"]),
        "PORTAINER_URL=" + maybe_quote(env_data["PORTAINER_URL"]),
        "PORTAINER_API_KEY=" + maybe_quote(env_data["PORTAINER_API_KEY"]),
        "PORTAINER_SSL_VERIFY=" + env_value(env_data["PORTAINER_SSL_VERIFY"]),
        "REOLINK_HOST=" + maybe_quote(env_data["REOLINK_HOST"]),
        "REOLINK_USER=" + maybe_quote(env_data["REOLINK_USER"]),
        "REOLINK_PASSWORD=" + maybe_quote(env_data["REOLINK_PASSWORD"]),
        "GIPHY_API_KEY=" + maybe_quote(env_data["GIPHY_API_KEY"]),
        "TENOR_API_KEY=" + maybe_quote(env_data["TENOR_API_KEY"]),
        "",
        "# File paths",
        "MEMORY_STORE_PATH=" + maybe_quote(env_data["MEMORY_STORE_PATH"]),
        "CAMERA_LOG_FILE_PATH=" + maybe_quote(env_data["CAMERA_LOG_FILE_PATH"]),
        "GOOGLE_TOKEN_PATH=" + maybe_quote(env_data["GOOGLE_TOKEN_PATH"]),
        "NEST_TOKEN_PATH=" + maybe_quote(env_data["NEST_TOKEN_PATH"]),
        "",
        "# Runtime tuning",
        "TOOL_LOOP=" + env_value(env_data["TOOL_LOOP"]),
        "CHAT_DEBOUNCE_DELAY=" + env_value(env_data["CHAT_DEBOUNCE_DELAY"]),
        "EXPERT_ARCHIVE_DIR=" + maybe_quote(env_data["EXPERT_ARCHIVE_DIR"]),
        "EXPERT_INDEX_PATH=" + maybe_quote(env_data["EXPERT_INDEX_PATH"]),
        "EXPERT_MAIN_MAX_TOKENS=" + env_value(env_data["EXPERT_MAIN_MAX_TOKENS"]),
        "EXPERT_FAST_MAX_TOKENS=" + env_value(env_data["EXPERT_FAST_MAX_TOKENS"]),
        "EXPERT_DEFAULT_TARGET_SOURCES=" + env_value(env_data["EXPERT_DEFAULT_TARGET_SOURCES"]),
        "EXPERT_MIN_TARGET_SOURCES=" + env_value(env_data["EXPERT_MIN_TARGET_SOURCES"]),
        "EXPERT_MAX_SOURCES=" + env_value(env_data["EXPERT_MAX_SOURCES"]),
        "EXPERT_ALLOW_MIDLOOP_QUESTIONS=" + env_value(env_data["EXPERT_ALLOW_MIDLOOP_QUESTIONS"]),
        "EXPERT_MAX_AGENDA_QUESTIONS=" + env_value(env_data["EXPERT_MAX_AGENDA_QUESTIONS"]),
        "EXPERT_MAX_NEW_QUESTIONS=" + env_value(env_data["EXPERT_MAX_NEW_QUESTIONS"]),
        "EXPERT_MAX_SUBTASKS_PER_QUESTION=" + env_value(env_data["EXPERT_MAX_SUBTASKS_PER_QUESTION"]),
        "MEMORY_THRESHOLD=" + env_value(env_data["MEMORY_THRESHOLD"]),
        "HEARTBEAT_INTERVAL_SECONDS=" + env_value(env_data["HEARTBEAT_INTERVAL_SECONDS"]),
        "HEARTBEAT_SILENCE_THRESHOLD_SECONDS=" + env_value(env_data["HEARTBEAT_SILENCE_THRESHOLD_SECONDS"]),
        "HEARTBEAT_SILENT_RETRY_SECONDS=" + env_value(env_data["HEARTBEAT_SILENT_RETRY_SECONDS"]),
        "HEARTBEAT_PROACTIVE_COOLDOWN_SECONDS=" + env_value(env_data["HEARTBEAT_PROACTIVE_COOLDOWN_SECONDS"]),
        "HEARTBEAT_DAILY_PROACTIVE_LIMIT=" + env_value(env_data["HEARTBEAT_DAILY_PROACTIVE_LIMIT"]),
        "HEARTBEAT_SLEEP_START=" + maybe_quote(env_data["HEARTBEAT_SLEEP_START"]),
        "HEARTBEAT_SLEEP_END=" + maybe_quote(env_data["HEARTBEAT_SLEEP_END"]),
        "",
        "# Optional NOAA fallback if no saved home alias exists",
        "NOAA_LAT=" + maybe_quote(env_data["NOAA_LAT"]),
        "NOAA_LONG=" + maybe_quote(env_data["NOAA_LONG"]),
        "",
        "# App-managed JSON lives under ./config and is auto-generated by setup_emery.py and the runtime.",
    ])
    return "\n".join(lines) + "\n"


def derive_seed(args):
    env_seed = dict(DEFAULT_ENV)
    env_seed.update(load_dotenv_like(ENV_PATH))
    if args.import_env:
        env_seed.update(load_dotenv_like(Path(args.import_env).expanduser()))
    env_seed["MAIN_MODEL_URL"] = env_seed.get("MAIN_MODEL_URL") or env_seed.get("OLLAMA_URL", DEFAULT_ENV["MAIN_MODEL_URL"])
    env_seed["FAST_MODEL_URL"] = env_seed.get("FAST_MODEL_URL", DEFAULT_ENV["FAST_MODEL_URL"])

    if args.fresh:
        users_seed = clone_default(DEFAULT_USERS)
        integrations_seed = clone_default(DEFAULT_INTEGRATIONS)
        news_seed = clone_default(DEFAULT_NEWS_FEEDS)
    else:
        users_seed = normalize_users_seed(load_json(USERS_CONFIG_PATH, DEFAULT_USERS))
        integrations_seed = normalize_integrations_seed(load_json(INTEGRATIONS_CONFIG_PATH, DEFAULT_INTEGRATIONS))
        news_seed = normalize_news_seed(load_json(NEWS_FEEDS_CONFIG_PATH, DEFAULT_NEWS_FEEDS))

    users_seed["allowed_user_ids"] = seeded_list(
        users_seed.get("allowed_user_ids"),
        env_seed.get("TELEGRAM_ALLOWED_USERS"),
        DEFAULT_USERS["allowed_user_ids"],
        parser=parse_csv_ints,
    )
    users_seed["primary_user"]["id"] = seeded_int(
        users_seed["primary_user"].get("id"),
        env_seed.get("PRIMARY_USER_ID"),
        DEFAULT_USERS["primary_user"]["id"],
    )
    users_seed["primary_user"]["name"] = seeded_text(
        users_seed["primary_user"].get("name"),
        env_seed.get("USER_NAME"),
        DEFAULT_USERS["primary_user"]["name"],
    )
    users_seed["primary_user"]["location"] = seeded_text(
        users_seed["primary_user"].get("location"),
        env_seed.get("USER_LOCATION"),
        DEFAULT_USERS["primary_user"]["location"],
    )
    users_seed["primary_user"]["timezone"] = seeded_text(
        users_seed["primary_user"].get("timezone"),
        env_seed.get("USER_TIMEZONE"),
        DEFAULT_USERS["primary_user"]["timezone"],
    )
    users_seed["primary_user"]["birthday"] = seeded_text(
        users_seed["primary_user"].get("birthday"),
        env_seed.get("USER_BIRTHDAY"),
        DEFAULT_USERS["primary_user"]["birthday"],
    )
    users_seed["primary_user"]["family"] = seeded_text(
        users_seed["primary_user"].get("family"),
        env_seed.get("USER_FAMILY"),
        DEFAULT_USERS["primary_user"]["family"],
    )
    users_seed["primary_user"]["profession"] = seeded_text(
        users_seed["primary_user"].get("profession"),
        env_seed.get("USER_PROFESSION"),
        DEFAULT_USERS["primary_user"]["profession"],
    )
    users_seed["secondary_user"]["id"] = seeded_int(
        users_seed["secondary_user"].get("id"),
        env_seed.get("SECONDARY_USER_ID"),
        DEFAULT_USERS["secondary_user"]["id"],
    )
    users_seed["secondary_user"]["name"] = seeded_text(
        users_seed["secondary_user"].get("name"),
        env_seed.get("USER_2_NAME"),
        DEFAULT_USERS["secondary_user"]["name"],
    )
    users_seed["secondary_user"]["birthday"] = seeded_text(
        users_seed["secondary_user"].get("birthday"),
        env_seed.get("USER_2_BIRTHDAY"),
        DEFAULT_USERS["secondary_user"]["birthday"],
    )
    users_seed["secondary_user"]["family"] = seeded_text(
        users_seed["secondary_user"].get("family"),
        env_seed.get("USER_2_FAMILY"),
        DEFAULT_USERS["secondary_user"]["family"],
    )
    users_seed["secondary_user"]["profession"] = seeded_text(
        users_seed["secondary_user"].get("profession"),
        env_seed.get("USER_2_PROFESSION"),
        DEFAULT_USERS["secondary_user"]["profession"],
    )
    users_seed["relationship"] = seeded_text(
        users_seed.get("relationship"),
        env_seed.get("USER_RELATIONSHIP"),
        DEFAULT_USERS["relationship"],
    )

    integrations_seed["google_calendar_ids"] = seeded_list(
        integrations_seed.get("google_calendar_ids"),
        env_seed.get("GOOGLE_CALENDAR_IDS"),
        DEFAULT_INTEGRATIONS["google_calendar_ids"],
        parser=parse_calendar_ids,
    )
    integrations_seed["telegram"]["group_chat_id"] = integrations_seed["telegram"].get("group_chat_id") or parse_int(env_seed.get("TELEGRAM_GROUP_CHAT_ID"), 0) or None
    integrations_seed["telegram"]["security_topic_id"] = integrations_seed["telegram"].get("security_topic_id") or parse_int(env_seed.get("SECURITY_TOPIC_ID"), 0) or None
    integrations_seed["telegram"]["routines_topic_id"] = integrations_seed["telegram"].get("routines_topic_id") or parse_int(env_seed.get("ROUTINES_TOPIC_ID"), 0) or None
    integrations_seed["telegram"]["chat_topic_id"] = integrations_seed["telegram"].get("chat_topic_id") or parse_int(env_seed.get("CHAT_TOPIC_ID"), 0) or None
    integrations_seed["telegram"]["sticker_set_name"] = integrations_seed["telegram"].get("sticker_set_name") or env_seed.get("TELEGRAM_STICKER_SET") or None
    integrations_seed["reolink"]["cameras"] = integrations_seed["reolink"].get("cameras") or normalize_reolink_channels(parse_name_map(env_seed.get("REOLINK_CAMERAS")))
    integrations_seed["reolink"]["camera_descriptions"] = integrations_seed["reolink"].get("camera_descriptions") or parse_name_map(env_seed.get("REOLINK_CAMERA_DESCRIPTIONS"))
    integrations_seed["reolink"]["silent_alerts"] = parse_bool(env_seed.get("REOLINK_SILENT_ALERTS"), integrations_seed["reolink"].get("silent_alerts", True))
    integrations_seed["reolink"]["threading_enabled"] = parse_bool(env_seed.get("ENABLE_REOLINK_THREADING"), integrations_seed["reolink"].get("threading_enabled", True))
    integrations_seed["reolink"]["thread_window_minutes"] = parse_float(env_seed.get("REOLINK_THREAD_WINDOW_MINUTES"), integrations_seed["reolink"].get("thread_window_minutes", 10.0))
    integrations_seed["reolink"]["polling_enabled"] = parse_bool(env_seed.get("ENABLE_REOLINK_POLLING"), integrations_seed["reolink"].get("polling_enabled", False))
    integrations_seed["nest"]["project_id"] = integrations_seed["nest"].get("project_id") or env_seed.get("NEST_PROJECT_ID", "")

    parsed_feeds = parse_news_feeds(env_seed.get("NEWS_FEEDS"))
    if parsed_feeds:
        news_seed = parsed_feeds

    return env_seed, users_seed, integrations_seed, news_seed


def ask_core(env_seed):
    print_section("Core Bot Setup")
    env_seed["TELEGRAM_TOKEN"] = prompt_text("Telegram bot token", env_seed.get("TELEGRAM_TOKEN"), required=True)
    env_seed["MODEL_NAME"] = prompt_text("Assistant display name", env_seed.get("MODEL_NAME"))
    env_seed["MODEL_ID"] = prompt_text("Primary model ID", env_seed.get("MODEL_ID"), required=True)
    env_seed["MAIN_MODEL_URL"] = prompt_text("Primary model URL", env_seed.get("MAIN_MODEL_URL"), required=True, validator=validate_url_prompt)
    use_fast = prompt_yes_no("Configure a separate fast text coprocessor model", True)
    if use_fast:
        env_seed["FAST_MODEL_ID"] = prompt_text("Fast model ID", env_seed.get("FAST_MODEL_ID"), required=True)
        env_seed["FAST_MODEL_URL"] = prompt_text("Fast model URL", env_seed.get("FAST_MODEL_URL"), required=True, validator=validate_url_prompt)
    else:
        env_seed["FAST_MODEL_ID"] = DEFAULT_ENV["FAST_MODEL_ID"]
        env_seed["FAST_MODEL_URL"] = DEFAULT_ENV["FAST_MODEL_URL"]

    use_vision = prompt_yes_no("Configure a separate vision model", True)
    if use_vision:
        env_seed["VISION_MODEL_ID"] = prompt_text("Vision model ID", env_seed.get("VISION_MODEL_ID"), required=True)
        env_seed["VISION_OLLAMA_URL"] = prompt_text("Vision model URL", env_seed.get("VISION_OLLAMA_URL"), required=True, validator=validate_url_prompt)
    else:
        env_seed["VISION_MODEL_ID"] = env_seed.get("FAST_MODEL_ID", env_seed.get("MODEL_ID", DEFAULT_ENV["MODEL_ID"]))
        env_seed["VISION_OLLAMA_URL"] = DEFAULT_ENV["VISION_OLLAMA_URL"]

    use_embeddings = prompt_yes_no("Configure a separate embedding model", True)
    if use_embeddings:
        env_seed["EMBEDDING_MODEL_ID"] = prompt_text("Embedding model ID", env_seed.get("EMBEDDING_MODEL_ID"), required=True)
        env_seed["EMBEDDING_OLLAMA_URL"] = prompt_text("Embedding model URL", env_seed.get("EMBEDDING_OLLAMA_URL"), required=True, validator=validate_url_prompt)
    else:
        env_seed["EMBEDDING_MODEL_ID"] = env_seed.get("FAST_MODEL_ID", DEFAULT_ENV["FAST_MODEL_ID"])
        env_seed["EMBEDDING_OLLAMA_URL"] = DEFAULT_ENV["EMBEDDING_OLLAMA_URL"]
    return env_seed


def ask_users(users_seed):
    print_section("Primary User Profile")
    primary = users_seed["primary_user"]
    primary["name"] = prompt_text("Primary user name", primary.get("name"), required=True)
    primary["location"] = prompt_text("Primary user location", primary.get("location"), required=True)
    primary["timezone"] = prompt_text("Primary user timezone", primary.get("timezone"), required=True, validator=validate_timezone)
    primary["birthday"] = prompt_text("Primary user birthday", primary.get("birthday"))
    primary["family"] = prompt_text("Primary user family/context", primary.get("family"))
    primary["profession"] = prompt_text("Primary user profession", primary.get("profession"))
    primary["id"] = prompt_int("Primary Telegram user ID (optional)", primary.get("id") or None, allow_blank=True) or 0

    users_seed["allowed_user_ids"] = prompt_csv_ids("Allowed Telegram user IDs, comma-separated (required unless unrestricted access is enabled)", users_seed.get("allowed_user_ids", []))
    if not users_seed["allowed_user_ids"]:
        print("No allowed user IDs configured. The bot will ignore Telegram users unless ALLOW_UNRESTRICTED_TELEGRAM_ACCESS=true is set in .env.")

    if prompt_yes_no("Configure a secondary user / family mode", users_seed.get("secondary_user", {}).get("id", 0) != 0):
        print_section("Secondary User Profile")
        secondary = users_seed["secondary_user"]
        secondary["name"] = prompt_text("Secondary user name", secondary.get("name"), required=True)
        secondary["birthday"] = prompt_text("Secondary user birthday", secondary.get("birthday"))
        secondary["family"] = prompt_text("Secondary user family/context", secondary.get("family"))
        secondary["profession"] = prompt_text("Secondary user profession", secondary.get("profession"))
        secondary["id"] = prompt_int("Secondary Telegram user ID (optional)", secondary.get("id") or None, allow_blank=True) or 0
        users_seed["relationship"] = prompt_text("Relationship between users", users_seed.get("relationship"))
    else:
        users_seed["secondary_user"] = dict(DEFAULT_USERS["secondary_user"])
        users_seed["relationship"] = ""

    return users_seed


def ask_telegram(integrations_seed):
    print_section("Telegram Routing")
    if prompt_yes_no("Configure a group chat / forum routing setup", integrations_seed["telegram"].get("group_chat_id") is not None):
        telegram = integrations_seed["telegram"]
        telegram["group_chat_id"] = prompt_int("Telegram group chat ID", telegram.get("group_chat_id"), allow_blank=True)
        if telegram["group_chat_id"] and telegram["group_chat_id"] > 0 and telegram["group_chat_id"] >= 10**12:
            telegram["group_chat_id"] = -telegram["group_chat_id"]
        telegram["chat_topic_id"] = prompt_int("General chat topic ID (optional)", telegram.get("chat_topic_id"), allow_blank=True)
        telegram["routines_topic_id"] = prompt_int("Routines topic ID (optional)", telegram.get("routines_topic_id"), allow_blank=True)
        telegram["security_topic_id"] = prompt_int("Security topic ID (optional)", telegram.get("security_topic_id"), allow_blank=True)
    else:
        integrations_seed["telegram"]["group_chat_id"] = None
        integrations_seed["telegram"]["chat_topic_id"] = None
        integrations_seed["telegram"]["routines_topic_id"] = None
        integrations_seed["telegram"]["security_topic_id"] = None

    integrations_seed["telegram"]["sticker_set_name"] = prompt_text(
        "Sticker set name to preload (optional)",
        integrations_seed["telegram"].get("sticker_set_name") or "",
    ) or None
    return integrations_seed


def ask_features(env_seed):
    print_section("Feature Toggles")
    feature_prompts = {
        "ENABLE_CALENDAR": "Enable Google Calendar",
        "ENABLE_NEST": "Enable Google Nest thermostat",
        "ENABLE_WEATHER": "Enable NOAA weather",
        "ENABLE_NEWS": "Enable RSS/news feeds",
        "ENABLE_NASA": "Enable NASA APOD",
        "ENABLE_SEERR": "Enable Overseerr",
        "ENABLE_HISTORY": "Enable Today in History",
        "ENABLE_VOICE": "Enable voice I/O",
        "ENABLE_IMAGEGEN": "Enable image generation",
        "ENABLE_SEARCH": "Enable web search",
        "ENABLE_WEB_SCRAPING": "Enable web content fetch",
        "ENABLE_DOCLING": "Enable Docling document extraction",
        "ENABLE_YOUTUBE_TRANSCRIPT": "Enable YouTube transcript fetch",
        "ENABLE_FINANCE": "Enable finance tools",
        "ENABLE_SYSTEM_STATS": "Enable system stats tool",
        "ENABLE_REOLINK": "Enable Reolink cameras",
        "ENABLE_PORTAINER": "Enable Portainer tools",
        "ENABLE_SCHEDULER": "Enable scheduler",
        "ENABLE_HEARTBEAT": "Enable inactivity heartbeat",
        "ENABLE_MEMORY": "Enable persistent memory",
    }
    for key, label in feature_prompts.items():
        env_seed[key] = bool_to_env(prompt_yes_no(label, parse_bool(env_seed.get(key), DEFAULT_ENV.get(key, "false") == "true")))
    return env_seed


def ask_integrations(env_seed, integrations_seed, news_seed):
    print_section("Integration Details")

    if parse_bool(env_seed["ENABLE_CALENDAR"]):
        raw_default = ",".join(integrations_seed.get("google_calendar_ids", ["primary"]))
        integrations_seed["google_calendar_ids"] = parse_calendar_ids(prompt_text("Google Calendar IDs (comma-separated)", raw_default))
        env_seed["GOOGLE_TOKEN_PATH"] = prompt_text("Google Calendar token path", env_seed.get("GOOGLE_TOKEN_PATH"))

    if parse_bool(env_seed["ENABLE_NEST"]):
        integrations_seed["nest"]["project_id"] = prompt_text(
            "Nest Device Access project ID",
            integrations_seed["nest"].get("project_id", ""),
            required=True,
        )
        env_seed["NEST_TOKEN_PATH"] = prompt_text("Nest token path", env_seed.get("NEST_TOKEN_PATH"))

    if parse_bool(env_seed["ENABLE_WEATHER"]):
        env_seed["NOAA_EMAIL"] = prompt_text("NOAA contact email", env_seed.get("NOAA_EMAIL"), required=True)
        if prompt_yes_no("Set a saved 'home' weather location", True):
            home_location = prompt_text("Home weather location (e.g. Houston, TX)", "", required=True)
        else:
            home_location = ""
            env_seed["NOAA_LAT"] = prompt_text("Optional NOAA fallback latitude", env_seed.get("NOAA_LAT"))
            env_seed["NOAA_LONG"] = prompt_text("Optional NOAA fallback longitude", env_seed.get("NOAA_LONG"))
        weather_locations = {"home": {"label": home_location}} if home_location else {}
    else:
        weather_locations = {}

    if parse_bool(env_seed["ENABLE_NEWS"]):
        use_defaults = prompt_yes_no("Use default news feeds", True if news_seed == DEFAULT_NEWS_FEEDS else False)
        if use_defaults:
            news_seed = list(DEFAULT_NEWS_FEEDS)
        else:
            custom_feeds = []
            print("Enter custom news feeds. Leave the name blank when finished.")
            while True:
                name = prompt_text("Feed name", "")
                if not name:
                    break
                url = prompt_text("Feed URL", "", required=True, validator=validate_url_prompt)
                custom_feeds.append({"name": name, "url": url})
            news_seed = custom_feeds or list(DEFAULT_NEWS_FEEDS)

    if parse_bool(env_seed["ENABLE_SEERR"]):
        env_seed["OVERSEER_URL"] = prompt_text("Overseerr URL", env_seed.get("OVERSEER_URL"), required=True, validator=validate_url_prompt)
        env_seed["OVERSEER_KEY"] = prompt_text("Overseerr API key", env_seed.get("OVERSEER_KEY"), required=True)
        env_seed["OVERSEER_USER_ID"] = str(prompt_int("Overseerr user ID", parse_int(env_seed.get("OVERSEER_USER_ID"), 1)))

    if parse_bool(env_seed["ENABLE_IMAGEGEN"]):
        env_seed["GEMINI_API_KEY"] = prompt_text("Gemini API key", env_seed.get("GEMINI_API_KEY"), required=True)

    if parse_bool(env_seed["ENABLE_NASA"]):
        env_seed["NASA_API_KEY"] = prompt_text("NASA API key", env_seed.get("NASA_API_KEY"), required=True)

    if parse_bool(env_seed["ENABLE_FINANCE"]):
        env_seed["FRED_API_KEY"] = prompt_text("FRED API key", env_seed.get("FRED_API_KEY"))
        env_seed["ALPHA_VANTAGE_API_KEY"] = prompt_text("Alpha Vantage API key", env_seed.get("ALPHA_VANTAGE_API_KEY"))

    if parse_bool(env_seed["ENABLE_SEARCH"]):
        env_seed["SEARXNG_URL"] = prompt_text("SearXNG URL", env_seed.get("SEARXNG_URL"), required=True, validator=validate_url_prompt)

    if parse_bool(env_seed["ENABLE_WEB_SCRAPING"]):
        env_seed["ALLOW_PRIVATE_WEB_FETCH"] = bool_to_env(prompt_yes_no("Allow web fetches to private/local network addresses", parse_bool(env_seed.get("ALLOW_PRIVATE_WEB_FETCH"), False)))

    if parse_bool(env_seed["ENABLE_DOCLING"]):
        env_seed["DOCLING_URL"] = prompt_text("Docling URL", env_seed.get("DOCLING_URL"), required=True, validator=validate_url_prompt)
        env_seed["DOCLING_BEARER_TOKEN"] = prompt_text("Optional Docling bearer token", env_seed.get("DOCLING_BEARER_TOKEN"))

    if parse_bool(env_seed["ENABLE_VOICE"]):
        env_seed["OPEN_WEBUI_KEY"] = prompt_text("Open WebUI / STT auth key", env_seed.get("OPEN_WEBUI_KEY"))
        env_seed["STT_URL"] = prompt_text("STT URL", env_seed.get("STT_URL"), required=True, validator=validate_url_prompt)
        env_seed["TTS_URL"] = prompt_text("TTS URL", env_seed.get("TTS_URL"), required=True, validator=validate_url_prompt)
        env_seed["TTS_VOICE"] = prompt_text("TTS voice", env_seed.get("TTS_VOICE"), required=True)

    if parse_bool(env_seed["ENABLE_PORTAINER"]):
        env_seed["PORTAINER_URL"] = prompt_text("Portainer URL", env_seed.get("PORTAINER_URL"), required=True, validator=validate_url_prompt)
        env_seed["PORTAINER_API_KEY"] = prompt_text("Portainer API key", env_seed.get("PORTAINER_API_KEY"), required=True)
        env_seed["PORTAINER_SSL_VERIFY"] = bool_to_env(prompt_yes_no("Verify Portainer SSL certificates", parse_bool(env_seed.get("PORTAINER_SSL_VERIFY"), True)))

    if parse_bool(env_seed["ENABLE_REOLINK"]):
        print_section(
            "Reolink Cameras",
            "Camera channels must be numeric. Example: frontdoor -> 0",
        )
        env_seed["REOLINK_HOST"] = prompt_text("Reolink NVR host or IP", env_seed.get("REOLINK_HOST"), required=True)
        env_seed["REOLINK_USER"] = prompt_text("Reolink username", env_seed.get("REOLINK_USER"), required=True)
        env_seed["REOLINK_PASSWORD"] = prompt_text("Reolink password", env_seed.get("REOLINK_PASSWORD"), required=True)
        integrations_seed["reolink"]["silent_alerts"] = prompt_yes_no(
            "Send Reolink alerts silently",
            integrations_seed["reolink"].get("silent_alerts", True),
        )
        integrations_seed["reolink"]["threading_enabled"] = prompt_yes_no(
            "Thread repeated Reolink alerts together",
            integrations_seed["reolink"].get("threading_enabled", True),
        )
        integrations_seed["reolink"]["thread_window_minutes"] = prompt_float(
            "Reolink thread window in minutes",
            integrations_seed["reolink"].get("thread_window_minutes", 10.0),
        )
        integrations_seed["reolink"]["polling_enabled"] = prompt_yes_no(
            "Enable background Reolink polling",
            integrations_seed["reolink"].get("polling_enabled", False),
        )
        print("Enter Reolink cameras. Leave the camera name blank when finished.")
        cameras = {}
        descriptions = {}
        while True:
            camera_name = prompt_text("Camera name", "")
            if not camera_name:
                break
            normalized_name = camera_name.strip()
            if normalized_name.lower() in {name.lower() for name in cameras}:
                print(f"  Camera '{normalized_name}' is already configured. Choose a different name.")
                continue
            camera_channel = prompt_text(
                "Camera channel",
                "",
                required=True,
                validator=validate_reolink_channel,
                help_text="This should match the numeric channel shown in your Reolink/NVR config.",
            )
            camera_description = prompt_text("Camera description (optional)", "")
            cameras[normalized_name] = str(int(camera_channel))
            if camera_description:
                descriptions[normalized_name] = camera_description
            print(f"  Added '{normalized_name}' on channel {cameras[normalized_name]}.")
        integrations_seed["reolink"]["cameras"] = cameras
        integrations_seed["reolink"]["camera_descriptions"] = descriptions

    return env_seed, integrations_seed, news_seed, weather_locations


def ask_runtime(env_seed):
    print_section("Runtime Tuning")
    env_seed["CHAT_DEBOUNCE_DELAY"] = str(prompt_float("Chat debounce delay seconds", parse_float(env_seed.get("CHAT_DEBOUNCE_DELAY"), 4.0)))
    env_seed["TOOL_LOOP"] = str(prompt_int("Max tool loop count", parse_int(env_seed.get("TOOL_LOOP"), 15)))
    env_seed["MEMORY_THRESHOLD"] = str(prompt_int("Memory threshold characters", parse_int(env_seed.get("MEMORY_THRESHOLD"), 4000)))
    env_seed["HEARTBEAT_INTERVAL_SECONDS"] = str(prompt_int("Heartbeat check interval seconds", parse_int(env_seed.get("HEARTBEAT_INTERVAL_SECONDS"), 3600)))
    env_seed["HEARTBEAT_SILENCE_THRESHOLD_SECONDS"] = str(prompt_int("Heartbeat silence threshold seconds", parse_int(env_seed.get("HEARTBEAT_SILENCE_THRESHOLD_SECONDS"), 14400)))
    env_seed["HEARTBEAT_SILENT_RETRY_SECONDS"] = str(prompt_int("Heartbeat silent retry cooldown seconds", parse_int(env_seed.get("HEARTBEAT_SILENT_RETRY_SECONDS"), 3600)))
    env_seed["HEARTBEAT_PROACTIVE_COOLDOWN_SECONDS"] = str(prompt_int("Heartbeat proactive message cooldown seconds", parse_int(env_seed.get("HEARTBEAT_PROACTIVE_COOLDOWN_SECONDS"), 14400)))
    env_seed["HEARTBEAT_DAILY_PROACTIVE_LIMIT"] = str(prompt_int("Heartbeat daily proactive message limit", parse_int(env_seed.get("HEARTBEAT_DAILY_PROACTIVE_LIMIT"), 2)))
    env_seed["HEARTBEAT_SLEEP_START"] = prompt_text("Heartbeat quiet-hours start", env_seed.get("HEARTBEAT_SLEEP_START"))
    env_seed["HEARTBEAT_SLEEP_END"] = prompt_text("Heartbeat quiet-hours end", env_seed.get("HEARTBEAT_SLEEP_END"))
    return env_seed


def write_setup(env_data, users_data, integrations_data, news_data, weather_locations):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    write_text_file(ENV_PATH, build_env_content(env_data))
    write_json_file(USERS_CONFIG_PATH, users_data)
    write_json_file(INTEGRATIONS_CONFIG_PATH, integrations_data)
    write_json_file(NEWS_FEEDS_CONFIG_PATH, news_data)
    write_json_file(WEATHER_LOCATIONS_PATH, weather_locations)
    if not CUSTOM_JOBS_PATH.exists():
        write_json_file(CUSTOM_JOBS_PATH, [])

    ensure_file(CAMERA_LOG_PATH, "# Camera Security Log\n")


def print_next_steps(env_data):
    print_section("Setup Complete")
    print("- Wrote .env")
    print("- Wrote config/users.json")
    print("- Wrote config/integrations.json")
    print("- Wrote config/news_feeds.json")
    print("- Initialized config/weather_locations.json and config/custom_jobs.json")
    print("- Ensured camera log file exists")

    if parse_bool(env_data["ENABLE_CALENDAR"]) or parse_bool(env_data["ENABLE_NEST"]):
        print("\nGoogle integrations next step:")
        print("- Place credentials.json in secrets/google/credentials.json")
        print("- For Nest, optionally place secrets/google/nest_credentials.json too")
        print("- Run: python scripts/generate_google_token.py")

    print("\nYou can now start EmeryChat with:")
    print("python main.py")


def main():
    parser = argparse.ArgumentParser(description="Interactive first-time setup for EmeryChat.")
    parser.add_argument(
        "--import-env",
        help="Optional path to a legacy .env file to use as defaults during setup.",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Ignore existing JSON config files and rebuild them from defaults plus env/imported env values.",
    )
    args = parser.parse_args()

    print_banner()
    if args.import_env:
        print(f"Using legacy env defaults from: {Path(args.import_env).expanduser()}")
    elif ENV_PATH.exists():
        print(f"Using existing .env defaults from: {ENV_PATH}")
    if args.fresh:
        print("Ignoring existing JSON config files for this setup run.")

    env_seed, users_seed, integrations_seed, news_seed = derive_seed(args)

    env_seed = ask_core(env_seed)
    users_seed = ask_users(users_seed)
    integrations_seed = ask_telegram(integrations_seed)
    env_seed = ask_features(env_seed)
    env_seed, integrations_seed, news_seed, weather_locations = ask_integrations(env_seed, integrations_seed, news_seed)
    env_seed = ask_runtime(env_seed)

    write_setup(env_seed, users_seed, integrations_seed, news_seed, weather_locations)
    print_next_steps(env_seed)


if __name__ == "__main__":
    main()
