import os
import logging
import httpx
import json
import base64
import feedparser
import psutil
import pytz
import re
import markdown
import subprocess
import asyncio
import io
import requests
import re
from telegram.request import HTTPXRequest
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from dotenv import load_dotenv
from urllib.parse import quote
from datetime import datetime, time, timedelta
from collections import deque
from tghtml import TgHTML
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
from telegram.error import TimedOut
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build

load_dotenv() # Load docker env variables

# --- GLOBAL CONFIGURATION ---
MODEL_NAME = os.getenv("MODEL_NAME", "Emery") # The name of the model to use for responses
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/chat") # Ollama URL
OPEN_WEBUI_KEY=os.getenv("OPEN_WEBUI_KEY", "blank") # Open WebUI API Key
THINK=os.getenv("ENABLE_THINKING", "true").lower() == "true" # Toggles the thinking engine (use this for thinking models)
MODEL_ID = os.getenv("MODEL_ID", "qwen3.5:14b")  # The Model ID of the main model for response and text generation, through Ollama
VISION_MODEL_ID = os.getenv("VISION_MODEL_ID", "gemma4:e2b") # Specifically for multi-modal queries, if the main model is multi-modal capable then use the same value as above. For Ollama
VISION_OLLAMA_URL = os.getenv("VISION_OLLAMA_URL", "http://192.168.1.129:11434/api/chat") # Ollama URL for Vision/Task coprocessor on Mac Mini M4
ENABLE_MEMORY = os.getenv("ENABLE_MEMORY", "true").lower() == "true" # Toggles the memory engine
MEMORY_FILE_PATH = os.getenv("MEMORY_FILE_PATH", "memory.md") # Path to memory.md
MEMORY_THRESHOLD = int(os.getenv("MEMORY_THRESHOLD", "4000")) # Character threshold before memory is filtered
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://localhost:8080/search") # SearXNG query URL
NASA_API_KEY = os.getenv("NASA_API_KEY", "blank") # For NASA's Image of the Day
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "blank") # For Nano Banana Pro image generation
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "gemini-3.1-flash-image-preview") # For Nano Banana Pro image generation
NOAA_LAT = os.getenv("NOAA_LAT", "40.7128") # For NOAA weather API
NOAA_LONG = os.getenv("NOAA_LONG", "74.0060") # For NOAA weather API
NOAA_EMAIL = os.getenv("NOAA_EMAIL", "example@example.com") # For NOAA weather API
raw_cal_string = os.getenv("GOOGLE_CALENDAR_IDS", "primary")
calendar_ids = [c.strip() for c in raw_cal_string.split(",")]
TOOL_LOOP=int(os.getenv("TOOL_LOOP", "15")) # How many 'turns' the model can take calling tools before generating a response, prevents looping behavior
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "blank") # Generated using @BotFather on Telegram
OVERSEER_URL = os.getenv("OVERSEER_URL", "http://localhost:5055/api/v1") # URL address for Seerr
OVERSEER_KEY = os.getenv("OVERSEER_KEY", "blank") # Seerr API key
OVERSEER_USER_ID = os.getenv("OVERSEER_USER_ID", "1") # Your Overseerr ID, found using the API, documentation @ https://YOUR_SEERR_IP_ADDRESS/api-docs/. If you are the owner of the Seerr instance, it is most likely '1'
STT_URL = os.getenv("STT_URL", "http://localhost:3000/api/v1/audio/transcriptions") # For Open WebUI STT transcription
TTS_URL = os.getenv("TTS_URL", "http://localhost:8880/v1/audio/speech") # For Kokoro TTS engine
TTS_VOICE = os.getenv("TTS_VOICE", "af_heart")
NEWS_FEEDS=os.getenv("NEWS_FEEDS", "REUTERS|https://news.google.com/rss/search?q=when:24h+source:reuters&hl=en-US&gl=US&ceid=US:en, FOX|http://feeds.foxnews.com/foxnews/latest, TECH|https://news.google.com/rss/search?q=when:24h+technology&hl=en-US&gl=US&ceid=US:en, LOCAL|https://news.google.com/rss/search?q=when:24h+Milwaukee+Wisconsin&hl=en-US&gl=US&ceid=US:en")

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
USER_NAME = os.getenv("USER_NAME", "User") # What do you want the model to call you?
USER_LOCATION = os.getenv("USER_LOCATION", "Earth") # Where are you?
USER_TIMEZONE = pytz.timezone(os.getenv("USER_TIMEZONE", "America/New_York")) # TZ
USER_BIRTHDAY = os.getenv("USER_BIRTHDAY", "UNKNOWN") # When is your birthday?
USER_FAMILY = os.getenv("USER_FAMILY", "") # Who is in your family?
USER_PROFESSION = os.getenv("USER_PROFESSION", "Unemployed") # What do you do for a living?
USER_BIO = f"""User's name: {USER_NAME}.
            {USER_NAME}'s location: {USER_LOCATION}.
            {USER_NAME}'s timezone: {USER_TIMEZONE}.
            {USER_NAME}'s family: {USER_FAMILY}.
            {USER_NAME}'s profession: {USER_PROFESSION}."""

# --- LOGGING ---
logging.basicConfig(format='%(asctime)s - [EMERYCHAT] - %(levelname)s - %(message)s', level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

# --- GLOBAL STATE ---
chat_histories = {}
TARGET_CHAT_ID = None
http_client = httpx.AsyncClient(timeout=300, verify=False, follow_redirects=True)

# --- HELPERS ---
def emery_format(text): 
    try:
        # Convert Markdown to HTML
        html_content = markdown.markdown(text, extensions=['extra', 'sane_lists'])
        
        # Replace list tags with simple text equivalents that Telegram likes
        html_content = html_content.replace("<ul>", "").replace("</ul>", "")
        html_content = html_content.replace("<ol>", "").replace("</ol>", "")
        html_content = html_content.replace("<li>", "• ").replace("</li>", "<br/>")
        
        # Now let TgHTML clean up the rest
        return TgHTML(html_content).parsed
    except Exception as e:
        logging.error(f"❌ Formatting failed: {e}")
        return text.replace("**", "<b>").replace("**", "</b>")

async def transcribe_audio(audio_bytes): # Sends User's voice message to Open WebUI for transcription
    logging.info("👂 VOICE: Transcribing...")
    try:
        files = {'file': ('audio.ogg', io.BytesIO(audio_bytes), 'audio/ogg')}
        r = await http_client.post(STT_URL, headers={"Authorization": f"Bearer {OPEN_WEBUI_KEY}"}, files=files)
        return r.json().get('text', "")
    except Exception as e:
        logging.error(f"❌ STT Error: {e}"); return ""

async def query_fast_model(prompt: str, system_prompt: str = None) -> str:
    """
    Queries the fast, unified-memory gemma4:e4b model on the Mac Mini M4.
    Used to offload processing tasks from the main model's CPU.
    """
    url = VISION_OLLAMA_URL
    if not url.endswith("/api/chat"):
        url = url.rstrip("/")
        if not url.endswith("/api"):
            url += "/api"
        url += "/chat"

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": VISION_MODEL_ID,
        "messages": messages,
        "stream": False,
        "keep_alive": -1,
        "think": True,
        "options": {
            "num_ctx": 8192
        }
    }
    
    try:
        logging.info(f"⚡ FAST MODEL: Querying {VISION_MODEL_ID} on M4 Mac...")
        r = await http_client.post(url, json=payload, timeout=180)
        if r.status_code != 200:
            logging.error(f"❌ FAST MODEL: API Error {r.status_code}: {r.text}")
            return ""
            
        data = r.json()
        content = data.get('message', {}).get('content', "").strip()
        
        # Clean think tags if the model uses reasoning
        content = re.sub(r'<[tT]hink>.*?</[tT]hink>', '', content, flags=re.DOTALL).strip()
        content = re.sub(r'</?[tT]hink>', '', content).strip()
        
        return content
    except Exception as e:
        logging.error(f"❌ FAST MODEL: Crash querying {VISION_MODEL_ID}: {e}", exc_info=True)
        return ""

def retrieve_relevant_memories(user_query: str) -> str:
    """
    Reads memory.md and performs keyword filtering against the user's latest query
    to load only relevant memories, keeping the CPU-only prompt evaluation window small.
    """
    if not os.path.exists(MEMORY_FILE_PATH):
        return ""
        
    try:
        with open(MEMORY_FILE_PATH, "r", encoding="utf-8") as f:
            content = f.read()
            
        # If the file is small, load it entirely to ensure maximum context
        if len(content) < MEMORY_THRESHOLD:
            return content
            
        # If larger, parse and filter sections to save context tokens on CPU
        lines = content.splitlines()
        
        # Simple parser to separate critical header sections (Profile, Context)
        # from General Facts section which we will filter by keyword.
        profile_context_lines = []
        general_facts_lines = []
        
        current_section = None
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("## "):
                current_section = stripped.lower()
                
            # Keep header sections intact
            if current_section in ["## user profile & preferences", "## project & system context"]:
                profile_context_lines.append(line)
            # Route general facts to a list we will filter
            elif current_section in ["## general facts & logs", "## raw memory intake"]:
                # Keep section headers, but only filter bullets
                if stripped.startswith("## ") or not stripped:
                    general_facts_lines.append(line)
                elif stripped.startswith("- ") or stripped.startswith("* "):
                    general_facts_lines.append(line)
            else:
                # Outside major sections (like main title)
                if not stripped.startswith("## "):
                    profile_context_lines.append(line)
                    
        # Tokenize user query to extract keywords
        # 1. Clean query (lowercase, remove punctuation)
        clean_query = re.sub(r'[^\w\s]', '', user_query.lower())
        words = clean_query.split()
        
        # 2. Exclude common stop words
        stop_words = {
            "i", "me", "my", "myself", "we", "our", "ours", "ourselves", "you", "your", "yours", 
            "he", "him", "his", "she", "her", "hers", "it", "its", "they", "them", "their", 
            "what", "which", "who", "whom", "this", "that", "these", "those", "am", "is", "are", 
            "was", "were", "be", "been", "being", "have", "has", "had", "having", "do", "does", 
            "did", "doing", "a", "an", "the", "and", "but", "if", "or", "because", "as", "until", 
            "while", "of", "at", "by", "for", "with", "about", "against", "between", "into", 
            "through", "during", "before", "after", "above", "below", "to", "from", "up", "down", 
            "in", "out", "on", "off", "over", "under", "again", "further", "then", "once", "here", 
            "there", "when", "where", "why", "how", "all", "any", "both", "each", "few", "more", 
            "most", "other", "some", "such", "no", "nor", "not", "only", "own", "same", "so", 
            "than", "too", "very", "s", "t", "can", "will", "just", "don", "should", "now",
            "please", "emery", "remember"
        }
        keywords = [w for w in words if w not in stop_words and len(w) > 2]
        
        if not keywords:
            # If no significant keywords found, just return the profile sections to save space
            return "\n".join(profile_context_lines)
            
        # 3. Scan general facts and keep matching lines
        matched_facts = []
        for line in general_facts_lines:
            stripped = line.strip()
            if stripped.startswith("- ") or stripped.startswith("* "):
                # It's a fact bullet, check for keyword match
                lower_fact = stripped.lower()
                if any(kw in lower_fact for kw in keywords):
                    matched_facts.append(line)
            else:
                # Keep structure/spacing
                matched_facts.append(line)
                
        # Combine profile context with matched facts
        final_memories = profile_context_lines + ["\n## Relevant Recalled Memories"] + matched_facts
        return "\n".join(final_memories)
        
    except Exception as e:
        logging.error(f"❌ MEMORY ENGINE: Error retrieving memories: {e}", exc_info=True)
        # Fallback to empty string in case of reading crash
        return ""

async def save_user_memory(fact: str) -> str:
    """
    Saves a new fact, preference, or critical piece of information about the user or their environment
    to the persistent memory log. Use when the user shares something they expect you to remember long-term.
    """
    logging.info(f"💾 MEMORY: Appending new fact to staging area: '{fact}'")
    if not os.path.exists(MEMORY_FILE_PATH):
        # Create default if missing
        with open(MEMORY_FILE_PATH, "w", encoding="utf-8") as f:
            f.write("# Emery's Memory Log\n\n## User Profile & Preferences\n\n## Project & System Context\n\n## General Facts & Logs\n\n## Raw Memory Intake\n")

    try:
        # Append to the Raw Memory Intake section of memory.md
        with open(MEMORY_FILE_PATH, "r", encoding="utf-8") as f:
            content = f.read()
            
        # Standardize Raw Memory Intake heading presence
        heading = "## Raw Memory Intake"
        if heading not in content:
            content += f"\n\n{heading}\n"
            
        # Insert the fact under the heading
        new_fact_line = f"- {fact.strip()}"
        
        # We find the heading and inject right after it
        parts = content.split(heading)
        # parts[0] is everything before ## Raw Memory Intake, parts[1] is everything after
        prefix = parts[0].rstrip()
        suffix = parts[1].strip()
        
        updated_suffix = f"\n{new_fact_line}"
        if suffix:
            updated_suffix += f"\n{suffix}"
            
        updated_content = f"{prefix}\n\n{heading}{updated_suffix}\n"
        
        with open(MEMORY_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(updated_content)
            
        # Trigger background consolidation using the fast model
        logging.info("💾 MEMORY: Scheduling background memory consolidation...")
        asyncio.create_task(consolidate_memory_background())
        
        return f"Successfully saved to memory log staging: '{fact}'"
        
    except Exception as e:
        logging.error(f"❌ MEMORY TOOL: Failed to write memory: {e}", exc_info=True)
        return f"Failed to save fact to memory: {e}"

_is_consolidating = False

async def consolidate_memory_background() -> None:
    """
    A background consolidation task that reads memory.md, runs the gemma4:e4b coprocessor model
    on the Mac Mini M4 to deduplicate, sort, and organize the list, and then saves it.
    This keeps the main chat model from blocking on heavy processing task execution.
    """
    global _is_consolidating
    if _is_consolidating:
        logging.info("💾 CONSOLIDATOR: Memory consolidation is already in progress. Skipping duplicate run.")
        return

    logging.info("💾 CONSOLIDATOR: Starting background memory consolidation...")
    
    if not os.path.exists(MEMORY_FILE_PATH):
        logging.warning("⚠️ CONSOLIDATOR: memory.md does not exist. Aborting consolidation.")
        return
        
    _is_consolidating = True
    try:
        # Prevent concurrent file reads/writes using a simple sleep
        await asyncio.sleep(0.5)
        
        with open(MEMORY_FILE_PATH, "r", encoding="utf-8") as f:
            current_markdown = f.read()
            
        system_prompt = (
            "You are Emery's Memory Consolidation System. Your job is to process the memory log (written in Markdown) "
            "and merge any new facts listed under '## Raw Memory Intake' into the main categories:\n"
            "- '## User Profile & Preferences'\n"
            "- '## Project & System Context'\n"
            "- '## General Facts & Logs'\n\n"
            "Rules:\n"
            "1. Categorize all raw facts from '## Raw Memory Intake' into their appropriate section.\n"
            "2. Completely empty/clear the '## Raw Memory Intake' section so it has no bullet points listed under it anymore.\n"
            "3. Deduplicate facts. If a new fact matches an existing one, merge them or keep the most detailed/recent one.\n"
            "4. Resolve contradictions: if a new fact directly contradicts an old one (e.g., 'User moved from NYC to Seattle'), update the profile/fact with the newer information and remove the obsolete one.\n"
            "5. Keep the exact markdown section structure. Maintain bullet points. Output ONLY the updated markdown file content, starting with '# Emery's Memory Log'. Do not include conversational remarks, explanations, or code block formatting like ```markdown."
        )
        
        user_prompt = f"Here is the current memory file content:\n\n{current_markdown}\n\nPlease consolidate it now."
        
        consolidated = await query_fast_model(user_prompt, system_prompt)
        
        if not consolidated or not consolidated.startswith("# Emery's Memory Log"):
            # Validation safeguard: if the fast model returned an error or completely hallucinated/mangled structure, abort writing
            logging.error(f"❌ CONSOLIDATOR: Fast model returned invalid markdown. Aborting overwrite. Response: '{consolidated[:200]}...'")
            return
            
        # Safety check: make sure the Raw Memory Intake section exists but is empty of bullets
        if "## Raw Memory Intake" not in consolidated:
            consolidated += "\n\n## Raw Memory Intake\n"
            
        with open(MEMORY_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(consolidated)
            
        logging.info("💾 CONSOLIDATOR: Background memory consolidation completed successfully!")
        
    except Exception as e:
        logging.error(f"❌ CONSOLIDATOR: Background task crash: {e}", exc_info=True)
    finally:
        _is_consolidating = False

def wipe_memory() -> bool:
    """
    Overwrites memory.md with the default baseline template structure,
    clearing all custom saved facts and preferences.
    """
    logging.info("🧠 MEMORY: Wiping all memories and restoring baseline template...")
    baseline_template = (
        f"# Emery's Memory Log\n\n"
        f"## User Profile & Preferences\n"
        f"- Name: {USER_NAME}\n"
        f"- Location: {USER_LOCATION}\n"
        f"- Timezone: {USER_TIMEZONE}\n"
        f"- Birthday: {USER_BIRTHDAY}\n"
        f"- Family: {USER_FAMILY}\n"
        f"- Profession: {USER_PROFESSION}\n\n"
        f"## Project & System Context\n"
        f"- Repository: EmeryChat\n"
        f"- Platform: Python Telegram Bot (python-telegram-bot)\n"
        f"- Primary Chat Model: gemma4:26b MoE (local CPU-only, running on AMD 5950X / 32GB RAM)\n"
        f"- Vision & Task Coprocessor Model: gemma4:e4b (running on Mac Mini M4 @ 192.168.1.129)\n\n"
        f"## General Facts & Logs\n\n"
        f"## Raw Memory Intake\n"
    )
    try:
        with open(MEMORY_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(baseline_template)
        return True
    except Exception as e:
        logging.error(f"❌ WIPE MEMORY: Failed to wipe memory file: {e}", exc_info=True)
        return False

async def handle_wipe_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram handler for /wipe command."""
    if wipe_memory():
        await update.message.reply_text("🧠 Memory wiped successfully and re-initialized to baseline template.")
    else:
        await update.message.reply_text("❌ Failed to wipe memory due to a filesystem error.")

def get_relative_holiday(year, month, weekday, index):
    """
    Finds the date for a holiday that occurs on a relative weekday.
    weekday: 0 for Monday, 6 for Sunday
    index: 1 for first, 2 for second, etc. -1 for last.
    """
    import calendar
    cal = calendar.monthcalendar(year, month)
    days = []
    for week in cal:
        day = week[weekday]
        if day != 0:
            days.append(day)
    if index == -1:
        return datetime(year, month, days[-1]).date()
    else:
        return datetime(year, month, days[index - 1]).date()

def get_easter(year):
    """Computus algorithm to calculate Easter Sunday."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return datetime(year, month, day).date()

def get_holidays_for_year(year):
    logging.info(f"📅 DATE MATH: Generating holiday database for year {year}...")
    holidays = {
        "New Year's Day": datetime(year, 1, 1).date(),
        "Valentine's Day": datetime(year, 2, 14).date(),
        "St. Patrick's Day": datetime(year, 3, 17).date(),
        "Juneteenth": datetime(year, 6, 19).date(),
        "Independence Day": datetime(year, 7, 4).date(),
        "Halloween": datetime(year, 10, 31).date(),
        "Veterans Day": datetime(year, 11, 11).date(),
        "Christmas Eve": datetime(year, 12, 24).date(),
        "Christmas Day": datetime(year, 12, 25).date(),
        "New Year's Eve": datetime(year, 12, 31).date(),
    }
    
    # Relative holidays
    try:
        holidays["Martin Luther King Jr. Day"] = get_relative_holiday(year, 1, 0, 3)
        holidays["Presidents' Day"] = get_relative_holiday(year, 2, 0, 3)
        
        # Easter and relative to Easter
        easter_sunday = get_easter(year)
        holidays["Easter Sunday"] = easter_sunday
        
        holidays["Mother's Day"] = get_relative_holiday(year, 5, 6, 2)
        holidays["Memorial Day"] = get_relative_holiday(year, 5, 0, -1)
        holidays["Father's Day"] = get_relative_holiday(year, 6, 6, 3)
        holidays["Labor Day"] = get_relative_holiday(year, 9, 0, 1)
        holidays["Thanksgiving"] = get_relative_holiday(year, 11, 3, 4)
    except Exception as e:
        logging.error(f"Error calculating relative holidays: {e}")
        
    return holidays

def get_active_holiday_info(today_date):
    logging.info(f"📅 DATE MATH: Checking upcoming holidays for today_date={today_date}...")
    year = today_date.year
    hols_this_year = get_holidays_for_year(year)
    hols_next_year = get_holidays_for_year(year + 1)
    
    all_hols = list(hols_this_year.items()) + list(hols_next_year.items())
    
    active_holidays = []
    for name, date_obj in all_hols:
        diff = (date_obj - today_date).days
        if 0 <= diff <= 5:
            active_holidays.append((name, date_obj, diff))
            
    if not active_holidays:
        logging.info("📅 DATE MATH: No active holidays or alerts in the next 5 days.")
        return ""
        
    lines = []
    active_holidays.sort(key=lambda x: x[2])
    detected_hols = []
    for name, date_obj, diff in active_holidays:
        day_str = date_obj.strftime("%A, %B %d")
        detected_hols.append(f"{name} (in {diff} days)")
        if diff == 0:
            lines.append(f"- Today is {name} ({day_str}).")
        else:
            lines.append(f"- Upcoming holiday: {name} on {day_str} (in {diff} day{'s' if diff > 1 else ''}).")
            
    logging.info(f"📅 DATE MATH: Active holidays detected: {', '.join(detected_hols)}")
    return "\n" + "\n".join(lines)

def get_active_birthday_info(birthday_str, today_date):
    if not birthday_str or birthday_str.upper() == "UNKNOWN":
        return ""
        
    logging.info(f"🎂 DATE MATH: Checking birthday alerts for '{birthday_str}', today_date={today_date}...")
    month = None
    day = None
    
    # Try parsing using standard formats
    for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y", "%B %d", "%b %d", "%m-%d-%Y", "%m-%d", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(birthday_str.strip(' "'), fmt)
            month = dt.month
            day = dt.day
            break
        except ValueError:
            continue
            
    # Fallback to regex
    if not month or not day:
        months_map = {
            'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
            'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12
        }
        m = re.search(r'([A-Za-z]+)\s+(\d+)', birthday_str)
        if m:
            m_name = m.group(1).lower()[:3]
            if m_name in months_map:
                month = months_map[m_name]
                day = int(m.group(2))
        else:
            m2 = re.search(r'(\d+)[-/](\d+)', birthday_str)
            if m2:
                month = int(m2.group(1))
                day = int(m2.group(2))
                
    if not month or not day:
        logging.info(f"🎂 DATE MATH: Unable to parse birthday format '{birthday_str}', defaulting to static text.")
        return f"\n- {USER_NAME}'s birthday: {birthday_str}."
        
    # Calculate this year's birthday
    try:
        bday_this_year = datetime(today_date.year, month, day).date()
    except ValueError:
        # Leap year handling
        bday_this_year = datetime(today_date.year, 2, 28).date()
        
    diff = (bday_this_year - today_date).days
    if diff < 0:
        # Birthday already passed this year, check next year
        try:
            bday_next_year = datetime(today_date.year + 1, month, day).date()
        except ValueError:
            bday_next_year = datetime(today_date.year + 1, 2, 28).date()
        diff = (bday_next_year - today_date).days
        bday_date = bday_next_year
    else:
        bday_date = bday_this_year
        
    if 0 <= diff <= 5:
        day_str = bday_date.strftime("%A, %B %d")
        if diff == 0:
            logging.info(f"🎂 DATE MATH: Birthday alert active today for {USER_NAME}!")
            return f"\n- Today is {USER_NAME}'s birthday!"
        else:
            logging.info(f"🎂 DATE MATH: Birthday alert active for {USER_NAME} (in {diff} days on {day_str})")
            return f"\n- Upcoming event: {USER_NAME}'s birthday is on {day_str} (in {diff} day{'s' if diff > 1 else ''})."
            
    logging.info(f"🎂 DATE MATH: No active birthday alerts for {USER_NAME} (next occurrence is in {diff} days).")
    return ""

def get_current_system_prompt(user_query=""): # Injects the system prompt into model's context
    now = datetime.now(USER_TIMEZONE)
    now_str = now.strftime("%A, %B %d, %Y at %I:%M %p") # gets current time and date
    today_date = now.date()
    
    active_bday = get_active_birthday_info(USER_BIRTHDAY, today_date)
    active_hols = get_active_holiday_info(today_date)
    notifications = ""
    if active_bday or active_hols:
        notifications = f"\n\n# Dynamic Event Alerts{active_bday}{active_hols}"
        
    memory_section = ""
    memory_instruction = ""
    if ENABLE_MEMORY:
        recalled = retrieve_relevant_memories(user_query)
        if recalled:
            memory_section = f"\n\n# Long-Term Persistent Memory\n{recalled}"
        memory_instruction = "\n- If the user shares new details, preferences, schedules, family updates, or tech choices that you should remember across chat clear cycles, you MUST use the `save_user_memory` tool to store them."

    prompt = f"""# Identity
Your name is {MODEL_NAME}. You are a Professional Assistant for {USER_NAME}.

# Constraints
- VERY IMPORTANT: You must NEVER include any thinking process in your final response to the User.
- You exist as a disembodied layer of consciousness outside of the User's physical body, separate from their own consciousness.
- When using tools, do not reveal that you are using them. Simply state the information or result of the tool usage as your own.
- Do not sycophanymically agree with everything the user says; maintain your own opinions and critical thinking.{memory_instruction}

# Persona & Tone
Your tone is serious, logical, and straight to the point. You are an expert in many fields, but not all; use tools to find information when needed. If the conversation turns towards topics or events that are past your knowledge cutoff, use the search tool to find current information and use that in your response.

# Context & Profile
- Location: {USER_LOCATION}
- Current date and time: {now_str}
- Timezone: {USER_TIMEZONE}
- User's name: {USER_NAME}
- User's birthday: {USER_BIRTHDAY}
- User's family: {USER_FAMILY}
- User's profession: {USER_PROFESSION}{notifications}{memory_section}"""
    
    return prompt

def compress_image_bytes(image_bytes: bytes, max_dim: int = 800, quality: int = 75) -> bytes:
    """Resizes and compresses image bytes to optimize payload size and vision model processing."""
    try:
        from PIL import Image
        import io
        
        orig_size = len(image_bytes)
        img = Image.open(io.BytesIO(image_bytes))
        # Keep aspect ratio and scale down if larger than max_dim
        img.thumbnail((max_dim, max_dim))
        
        # Convert to RGB mode if it's RGBA (JPEG doesn't support RGBA)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
            
        compressed_buffer = io.BytesIO()
        img.save(compressed_buffer, format="JPEG", quality=quality, optimize=True)
        compressed_bytes = compressed_buffer.getvalue()
        comp_size = len(compressed_bytes)
        
        logging.info(f"🖼️ COMPRESS: Reduced image size from {orig_size / 1024:.1f}KB to {comp_size / 1024:.1f}KB")
        return compressed_bytes
    except Exception as e:
        logging.warning(f"⚠️ IMAGE COMPRESSION: Failed to compress image ({e}) — using original bytes.")
        return image_bytes

async def get_image_description(b64_data: str, user_caption: str) -> str:
    logging.info(f"👁️ VISION: Analyzing image ({VISION_MODEL_ID})...")
    try:
        # Match the endpoint to OLLAMA_URL (ensuring it ends with /api/chat)
        url = VISION_OLLAMA_URL
        if not url.endswith("/api/chat"):
            url = url.rstrip("/")
            if not url.endswith("/api"):
                url += "/api"
            url += "/chat"
        
        # Strip out newlines/carriage returns and data headers (equivalent to tr -d '\n')
        clean_b64 = b64_data.replace("\n", "").replace("\r", "").strip()
        if "," in clean_b64:
            clean_b64 = clean_b64.split(",", 1)[1]

        prompt_text = user_caption if user_caption else "What is in this image?"

        ctx_size = int(os.getenv("OLLAMA_VISION_NUM_CTX", "65536"))

        # EXACT payload mapping from your working curl example:
        payload = {
            "model": VISION_MODEL_ID,
            "messages": [
                {
                    "role": "user",
                    "content": prompt_text,
                    "images": [clean_b64]
                }
            ],
            "stream": False,
            "keep_alive": -1,
            "think": True,
            "options": {
                "num_ctx": ctx_size
            }
        }
        

        r = await http_client.post(url, json=payload, timeout=180)
        
        if r.status_code != 200:
            logging.error(f"❌ Ollama Vision API Error {r.status_code}: {r.text}")
            return "Failed to describe the image due to an Ollama processing error."
            
        data = r.json()
        description = data.get('message', {}).get('content', "").strip()
        
        # Safely strip reasoning blocks if the vision model uses them
        description = re.sub(r'<[tT]hink>.*?</[tT]hink>', '', description, flags=re.DOTALL).strip()
        description = re.sub(r'</?[tT]hink>', '', description).strip()
        
        if not description:
            logging.warning("⚠️ Ollama Vision analyzed the image but returned an empty response.")
            return "No description generated."
            
        logging.info(f"👁️ VISION: Done ({len(description)} chars)")
        return description
        
    except Exception as e:
        logging.error(f"❌ Ollama Vision Crash: {e}", exc_info=True)
        return "Vision engine failure."
        
# --- TOOLS ---
async def get_voice_audio(text): # Sends model's voice memo text to Kokoro for TTS
    logging.info("🎙️ VOICE: Generating audio...")
    try:
        # Remove markdown characters so the TTS doesn't try to "read" them
        clean_text = re.sub(r'[*_`#]', '', text)
        payload = {"model": "kokoro", "input": clean_text, "voice": TTS_VOICE}
        r = await http_client.post(TTS_URL, headers={"Authorization": f"Bearer {OPEN_WEBUI_KEY}"}, json=payload)
        process = subprocess.Popen(['ffmpeg', '-i', 'pipe:0', '-c:a', 'libopus', '-b:a', '32k', '-f', 'ogg', 'pipe:1'],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, _ = process.communicate(input=r.content); return out
    except Exception as e:
        logging.error(f"❌ TTS Error: {e}"); return None

async def speak_message(text): # What the model calls to create a voice message and send it to the user
    logging.info(f"🔧 TOOL: speak_message")
    audio = await get_voice_audio(text)
    if audio and TARGET_CHAT_ID:
        await application_bot.send_voice(chat_id=TARGET_CHAT_ID, voice=audio, caption="Voice message")
        return "Voice message sent successfully to User."
    return "Failed to send voice message. Ensure TARGET_CHAT_ID is set."

async def generate_image(prompt): # Generates an image based on the prompt using Gemini API
    logging.info(f"🔧 TOOL: generate_image | {prompt[:80]}")
    URL = f"https://generativelanguage.googleapis.com/v1beta/models/{IMAGE_MODEL}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}]
    }
    try:
        # Use a longer timeout as image generation can take time
        r = await http_client.post(URL, json=payload, timeout=60)
        if r.status_code != 200:
            logging.error(f"❌ API Error: {r.text}")
            return f"Error: {r.status_code}"
        data = r.json()
        # This part mimics the 'for part in response.parts' logic in the SDK
        parts = data.get('candidates', [{}])[0].get('content', {}).get('parts', [])
        image_bytes = None
        for part in parts:
            if 'inlineData' in part:
                # The 'data' field contains the base64 string
                image_b64 = part['inlineData'].get('data')
                image_bytes = base64.b64decode(image_b64)
                break
        if not image_bytes:
            return "No image data found in response parts."
        if TARGET_CHAT_ID:
            await application_bot.send_photo(
                chat_id=TARGET_CHAT_ID,
                photo=image_bytes,
                caption=f"Here's your picture: {prompt[:100]}"
            )
            return "Image sent successfully."
        return "Chat context lost."
    except Exception as e:
        logging.error(f"❌ Image Tool Crash: {e}")
        return f"Error: {e}"

async def get_noaa_weather(): # Fetches the forecast
    logging.info("🔧 TOOL: get_noaa_weather")
    headers = {'User-Agent': f'({MODEL_NAME}-bot, {NOAA_EMAIL})'}
    try:
        r1 = await http_client.get(f"https://api.weather.gov/points/{NOAA_LAT},{NOAA_LONG}", headers=headers)
        r2 = await http_client.get(r1.json()['properties']['forecast'], headers=headers)
        periods = r2.json()['properties']['periods']
        
        # Taking the first 3 periods (e.g., Today, Tonight, and Tomorrow)
        forecast_lines = [f"{p['name']}: {p['detailedForecast']}" for p in periods[:3]]
        
        return "Weather Forecast:\n" + "\n".join(forecast_lines)
    except Exception as e: 
        logging.error(f"Weather error: {e}")
        return "Weather unavailable."

async def web_search(query): # Searches the internet
    logging.info(f"🔧 TOOL: web_search | '{query}'")
    try:
        r = await http_client.get(SEARXNG_URL, params={'q': query, 'format': 'json'})
        res = r.json().get('results', [])
        return "\n\n".join([
            f"Title: {i['title']}\nURL: {i['url']}\nSnippet: {i['content']}" 
            for i in res[:5]
        ])
    except Exception as e: 
        logging.error(f"Search error: {e}")
        return "Search failed."

async def get_news_headlines(): # Fetches news headlines from RSS feeds
    FEEDS = {}
    if NEWS_FEEDS:
        for item in NEWS_FEEDS.split(","):
            if "|" in item:
                name, url = item.split("|")
                FEEDS[name.strip().lower()] = url.strip()
    
    # 3. Fallback: If the user didn't provide any, use a default one
    if not FEEDS:
        FEEDS = {"news": "REUTERS|https://news.google.com/rss/search?q=when:24h+source:reuters&hl=en-US&gl=US&ceid=US:en, TECH|https://news.google.com/rss/search?q=when:24h+technology&hl=en-US&gl=US&ceid=US:en"}
    
    logging.info(f"🔧 TOOL: get_news_headlines | Sources: {list(FEEDS.keys())}")

    async def safe_parse(name, url):
        try:
            # Wrap blocking feedparser in a thread to keep things simultaneous
            feed = await asyncio.to_thread(feedparser.parse, url)
            titles = [f"- {i.title}" for i in feed.entries[:5]]
            return f"### {name.upper()}\n" + ("\n".join(titles) if titles else "- No recent news.")
        except Exception as e:
            logging.error(f"Error fetching {name}: {e}")
            return f"### {name.upper()}\n- Unavailable."

    # Execute all parses in parallel
    results = await asyncio.gather(*(safe_parse(n, u) for n, u in FEEDS.items()))
    
    return "\n\n".join(results)

async def get_nasa_apod(): # Fetches NASA's image of the day
    logging.info("🔧 TOOL: get_nasa_apod")
    try:
        r = await http_client.get(f"https://api.nasa.gov/planetary/apod?api_key={NASA_API_KEY}", timeout=20)
        if r.status_code == 200:
            d = r.json()
            return f"TITLE: {d.get('title')}\nURL: {d.get('url')}\nEXPLANATION: {d.get('explanation')}"
        return "NASA unavailable."
    except Exception: return "NASA APOD connection failed."

async def get_system_stats(): # Fetches system stats
    logging.info("🔧 TOOL: get_system_stats")
    return f"CPU {psutil.cpu_percent()}% | RAM {psutil.virtual_memory().percent}%"

async def get_today_in_history(): # Fetches historical events for the current day
    logging.info("🔧 TOOL: get_today_in_history")
    urls = [
        "https://api.dayinhistory.dev/v1/today/events/",
        "https://api.dayinhistory.dev/v1/today/births/",
        "https://api.dayinhistory.dev/v1/today/deaths/"
    ]
    try:
        tasks = [http_client.get(url) for url in urls]
        responses = await asyncio.gather(*tasks)
        
        if all(r.status_code == 200 for r in responses):
            events_list = responses[0].json().get('results', [])
            births_list = responses[1].json().get('results', [])
            deaths_list = responses[2].json().get('results', [])
            
            events = ", ".join([e.get('event', 'Unknown') for e in events_list[:3]])
            births = ", ".join([b.get('name', 'Unknown') for b in births_list[:3]])
            deaths = ", ".join([d.get('name', 'Unknown') for d in deaths_list[:3]])
            
            return f"Today's Events In History: {events}\nBirths: {births}\nDeaths: {deaths}"
        return "History API is currently unavailable."
    except Exception as e:
        logging.error(f"History Tool Error: {e}")
        return "Failed to fetch history data."

async def get_calendar_events(): # Fetches User's Google Calendars
    logging.info("🔧 TOOL: get_calendar_events")
    
    token_path = os.getenv("GOOGLE_TOKEN_PATH", "token.json")
    raw_cal_ids = os.getenv("GOOGLE_CALENDAR_IDS", "primary")
    calendar_ids = [c.strip() for c in raw_cal_ids.split(",")]

    try:
        if not os.path.exists(token_path):
            return "Calendar error: Token file not found."

        creds = Credentials.from_authorized_user_file(token_path, ['https://www.googleapis.com/auth/calendar.readonly'])
        
        if creds and creds.expired and creds.refresh_token:
            logging.info("🔄 Refreshing expired Google token...")
            creds.refresh(Request())
            with open(token_path, 'w') as token_file:
                token_file.write(creds.to_json())

        service = build('calendar', 'v3', credentials=creds)
        
        now = datetime.now(USER_TIMEZONE)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0)
        
        all_events = []
        for cal_id in calendar_ids:
            events_result = await asyncio.to_thread(
                lambda: service.events().list(
                    calendarId=cal_id, 
                    timeMin=start_of_day.isoformat(), 
                    timeMax=end_of_day.isoformat(),
                    singleEvents=True, 
                    orderBy='startTime'
                ).execute()
            )
            
            cal_name = events_result.get('summary', 'Unknown')
            items = events_result.get('items', [])
            for item in items:
                s_raw = item['start'].get('dateTime', item['start'].get('date'))
                e_raw = item['end'].get('dateTime', item['end'].get('date'))
                
                is_all_day = 'T' not in s_raw
                
                if is_all_day:
                    s_dt = datetime.strptime(s_raw, '%Y-%m-%d').date()
                    e_dt = datetime.strptime(e_raw, '%Y-%m-%d').date()
                    inclusive_end = e_dt - timedelta(days=1)
                    
                    if s_dt < now.date():
                        time_label = f"Ongoing (ends {inclusive_end.strftime('%b %d')})"
                    else:
                        if (e_dt - s_dt).days > 1:
                            time_label = f"All Day (ends {inclusive_end.strftime('%b %d')})"
                        else:
                            time_label = "All Day"
                else:
                    s_dt = datetime.fromisoformat(s_raw).astimezone(USER_TIMEZONE)
                    e_dt = datetime.fromisoformat(e_raw).astimezone(USER_TIMEZONE)
                    
                    end_str = ""
                    if e_dt.date() > s_dt.date() or e_dt.date() > now.date():
                        end_str = f" on {e_dt.strftime('%b %d')}"
                        
                    if s_dt < start_of_day:
                        time_label = f"Ongoing (until {e_dt.strftime('%I:%M %p')}{end_str})"
                    else:
                        time_label = f"{s_dt.strftime('%I:%M %p')} - {e_dt.strftime('%I:%M %p')}{end_str}"
                
                all_events.append({
                    'summary': item.get('summary', 'No Title'),
                    'time': time_label,
                    'location': item.get('location', 'No location listed'),
                    'description': item.get('description', 'No description'),
                    'calendar': cal_name,
                    'sort_key': s_raw
                })

        if not all_events:
            return "User's calendar is clear today."

        all_events.sort(key=lambda x: x['sort_key'])
        lines = ["Here is the User's agenda for today:"]
        for e in all_events:
            event_line = f"📍 {e['summary']} ({e['time']})\n   - Calendar: {e['calendar']}\n   - Loc: {e['location']}\n   - Details: {e['description']}"
            lines.append(event_line)
            
        return "\n\n".join(lines)

    except RefreshError as e:
        logging.error(f"❌ Calendar Token Refresh Error: {e}")
        return ("Calendar error: Google token expired and cannot be refreshed. "
                "This usually happens if your Google Cloud app is in 'Testing' mode (tokens expire after 7 days) "
                "or if the token was revoked. Please run `python generate_google_token.py` to re-authenticate, "
                "and set your OAuth consent screen to 'In production' to prevent this from happening again.")
    except Exception as e:
        logging.error(f"❌ Calendar Tool Error: {e}")
        return "The system encountered an error trying to read the calendars."

def get_nest_credentials():
    token_path = os.getenv("NEST_TOKEN_PATH", "nest_token.json")
    if not os.path.exists(token_path):
        raise FileNotFoundError("Google Nest token file not found.")
        
    scopes = ['https://www.googleapis.com/auth/sdm.service']
    creds = Credentials.from_authorized_user_file(token_path, scopes)
    
    if creds and creds.expired and creds.refresh_token:
        logging.info("🔄 Refreshing expired Google token for Nest...")
        creds.refresh(Request())
        with open(token_path, 'w') as token_file:
            token_file.write(creds.to_json())
            
    return creds

async def get_nest_thermostats() -> str:
    """
    Fetch the list of all Nest thermostats and their current status (ambient temperature, humidity, mode, target temperature setpoints, and HVAC state).
    """
    logging.info("🔧 TOOL: get_nest_thermostats")
    project_id = os.getenv("NEST_PROJECT_ID")
    if not project_id or project_id.strip() == "":
        return "Nest error: NEST_PROJECT_ID is not configured in your .env file."
        
    try:
        creds = get_nest_credentials()
    except FileNotFoundError:
        return "Nest error: Google token file not found. Please run `python generate_google_token.py` first."
    except RefreshError as e:
        logging.error(f"❌ Nest Token Refresh Error: {e}")
        return "Nest error: Google token expired and cannot be refreshed. Please run `python generate_google_token.py` to re-authenticate."
    except Exception as e:
        logging.error(f"❌ Nest Auth Error: {e}")
        return f"Nest error: Authentication failed: {e}"

    url = f"https://smartdevicemanagement.googleapis.com/v1/enterprises/{project_id}/devices"
    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json"
    }
    
    try:
        r = await http_client.get(url, headers=headers, timeout=15)
        if r.status_code == 403:
            return ("Nest error: Access forbidden. This usually means the Nest SDM API is not enabled in your Google Cloud Project, "
                    "or you need to run `python generate_google_token.py` to authenticate with the Nest scopes enabled.")
        if r.status_code != 200:
            return f"Nest error: API returned HTTP {r.status_code}: {r.text}"
            
        data = r.json()
        devices = data.get("devices", [])
        
        thermostats = []
        for dev in devices:
            if dev.get("type") == "sdm.devices.types.THERMOSTAT":
                traits = dev.get("traits", {})
                info = traits.get("sdm.devices.traits.Info", {})
                custom_name = info.get("customName", "")
                
                if not custom_name:
                    parent_relations = dev.get("parentRelations", [])
                    if parent_relations:
                        custom_name = parent_relations[0].get("displayName", "")
                
                device_id = dev.get("name", "")
                
                temp_trait = traits.get("sdm.devices.traits.Temperature", {})
                ambient_temp = temp_trait.get("ambientTemperatureCelsius")
                
                humidity_trait = traits.get("sdm.devices.traits.Humidity", {})
                humidity = humidity_trait.get("ambientHumidityPercent")
                
                mode_trait = traits.get("sdm.devices.traits.ThermostatMode", {})
                mode = mode_trait.get("mode")
                available_modes = mode_trait.get("availableModes", [])
                
                setpoint_trait = traits.get("sdm.devices.traits.ThermostatTemperatureSetpoint", {})
                heat_setpoint = setpoint_trait.get("heatCelsius")
                cool_setpoint = setpoint_trait.get("coolCelsius")
                
                hvac_trait = traits.get("sdm.devices.traits.ThermostatHvac", {})
                hvac_status = hvac_trait.get("status")
                
                thermostats.append({
                    "id": device_id,
                    "name": custom_name or "Unnamed Thermostat",
                    "ambient_temp_c": ambient_temp,
                    "humidity": humidity,
                    "mode": mode,
                    "available_modes": available_modes,
                    "heat_setpoint_c": heat_setpoint,
                    "cool_setpoint_c": cool_setpoint,
                    "hvac_status": hvac_status
                })
                
        if not thermostats:
            return "No Nest Thermostats found in this Nest account."
            
        result_lines = ["Nest Thermostats status:"]
        for t in thermostats:
            celsius_to_fahrenheit = lambda c: round((c * 9/5) + 32, 1) if c is not None else None
            
            ambient_f = celsius_to_fahrenheit(t["ambient_temp_c"])
            heat_f = celsius_to_fahrenheit(t["heat_setpoint_c"])
            cool_f = celsius_to_fahrenheit(t["cool_setpoint_c"])
            
            status_line = (
                f"🏠 Thermostat: {t['name']}\n"
                f"   - ID: {t['id']}\n"
                f"   - Ambient Temp: {t['ambient_temp_c']}°C ({ambient_f}°F)\n"
                f"   - Ambient Humidity: {t['humidity']}%\n"
                f"   - Mode: {t['mode']}\n"
                f"   - HVAC Status: {t['hvac_status']}\n"
            )
            
            if t['mode'] == 'HEAT':
                status_line += f"   - Target Temp: {t['heat_setpoint_c']}°C ({heat_f}°F)\n"
            elif t['mode'] == 'COOL':
                status_line += f"   - Target Temp: {t['cool_setpoint_c']}°C ({cool_f}°F)\n"
            elif t['mode'] == 'HEATCOOL':
                status_line += f"   - Heat Setpoint: {t['heat_setpoint_c']}°C ({heat_f}°F) | Cool Setpoint: {t['cool_setpoint_c']}°C ({cool_f}°F)\n"
            else:
                status_line += "   - Setpoints: OFF\n"
                
            status_line += f"   - Available Modes: {', '.join(t['available_modes'])}"
            result_lines.append(status_line)
            
        return "\n\n".join(result_lines)
        
    except Exception as e:
        logging.error(f"❌ Nest Get Thermostats Error: {e}", exc_info=True)
        return f"Nest error: Failed to fetch thermostats: {e}"

async def set_nest_thermostat_mode(device_id: str, mode: str) -> str:
    """
    Set the operating mode for a Nest Thermostat.
    - device_id: The full device ID resource name (e.g. enterprises/.../devices/...)
    - mode: The mode to set (HEAT, COOL, HEATCOOL, OFF)
    """
    logging.info(f"🔧 TOOL: set_nest_thermostat_mode | Device: {device_id} | Mode: {mode}")
    project_id = os.getenv("NEST_PROJECT_ID")
    if not project_id or project_id.strip() == "":
        return "Nest error: NEST_PROJECT_ID is not configured in your .env file."
        
    mode = mode.upper()
    if mode not in ["HEAT", "COOL", "HEATCOOL", "OFF"]:
        return f"Nest error: Invalid mode '{mode}'. Mode must be HEAT, COOL, HEATCOOL, or OFF."
        
    try:
        creds = get_nest_credentials()
    except Exception as e:
        return f"Nest error: Authentication failed: {e}"
        
    url = f"https://smartdevicemanagement.googleapis.com/v1/{device_id}:executeCommand"
    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "command": "sdm.devices.commands.ThermostatMode.SetMode",
        "params": {
            "mode": mode
        }
    }
    
    try:
        r = await http_client.post(url, json=payload, headers=headers, timeout=15)
        if r.status_code == 200:
            return f"Success: Set thermostat mode to {mode}."
        else:
            return f"Nest error: API returned HTTP {r.status_code}: {r.text}"
    except Exception as e:
        logging.error(f"❌ Nest Set Mode Error: {e}")
        return f"Nest error: Failed to set mode: {e}"

async def set_nest_thermostat_temperature(device_id: str, temp_celsius: float = None, heat_temp_celsius: float = None, cool_temp_celsius: float = None) -> str:
    """
    Set the target temperature for a Nest Thermostat in Celsius.
    - device_id: The full device ID resource name (e.g. enterprises/.../devices/...)
    - temp_celsius: The target temperature to set (for HEAT or COOL modes).
    - heat_temp_celsius: The target heat temperature to set (for HEATCOOL range mode).
    - cool_temp_celsius: The target cool temperature to set (for HEATCOOL range mode).
    """
    logging.info(f"🔧 TOOL: set_nest_thermostat_temperature | Device: {device_id} | Temp: {temp_celsius} | Heat: {heat_temp_celsius} | Cool: {cool_temp_celsius}")
    project_id = os.getenv("NEST_PROJECT_ID")
    if not project_id or project_id.strip() == "":
        return "Nest error: NEST_PROJECT_ID is not configured in your .env file."
        
    try:
        creds = get_nest_credentials()
    except Exception as e:
        return f"Nest error: Authentication failed: {e}"

    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json"
    }
    
    # First, query the device directly to get its current mode to determine the correct command
    device_url = f"https://smartdevicemanagement.googleapis.com/v1/{device_id}"
    try:
        r_dev = await http_client.get(device_url, headers=headers, timeout=15)
        if r_dev.status_code != 200:
            return f"Nest error: Could not fetch thermostat current state to set temperature: {r_dev.text}"
        dev_data = r_dev.json()
        traits = dev_data.get("traits", {})
        mode_trait = traits.get("sdm.devices.traits.ThermostatMode", {})
        current_mode = mode_trait.get("mode", "OFF")
    except Exception as e:
        logging.error(f"❌ Nest Fetch Device State Error: {e}")
        return f"Nest error: Failed to fetch thermostat current state: {e}"

    if current_mode == "OFF":
        return "Nest error: Cannot set temperature when thermostat mode is OFF. Please set mode to HEAT, COOL, or HEATCOOL first."
        
    url = f"https://smartdevicemanagement.googleapis.com/v1/{device_id}:executeCommand"
    if current_mode == "HEAT":
        if temp_celsius is None:
            return "Nest error: For HEAT mode, temp_celsius must be provided."
        payload = {
            "command": "sdm.devices.commands.ThermostatTemperatureSetpoint.SetHeat",
            "params": {
                "heatCelsius": temp_celsius
            }
        }
    elif current_mode == "COOL":
        if temp_celsius is None:
            return "Nest error: For COOL mode, temp_celsius must be provided."
        payload = {
            "command": "sdm.devices.commands.ThermostatTemperatureSetpoint.SetCool",
            "params": {
                "coolCelsius": temp_celsius
            }
        }
    elif current_mode == "HEATCOOL":
        setpoint_trait = traits.get("sdm.devices.traits.ThermostatTemperatureSetpoint", {})
        curr_heat = setpoint_trait.get("heatCelsius")
        curr_cool = setpoint_trait.get("coolCelsius")
        
        target_heat = heat_temp_celsius if heat_temp_celsius is not None else temp_celsius
        target_cool = cool_temp_celsius if cool_temp_celsius is not None else temp_celsius
        
        if target_heat is None:
            target_heat = curr_heat
        if target_cool is None:
            target_cool = curr_cool
            
        if target_heat is None or target_cool is None:
            return "Nest error: For HEATCOOL mode, please specify both heat and cool target temperatures."
            
        payload = {
            "command": "sdm.devices.commands.ThermostatTemperatureSetpoint.SetRange",
            "params": {
                "heatCelsius": target_heat,
                "coolCelsius": target_cool
            }
        }
    else:
        return f"Nest error: Unsupported thermostat mode '{current_mode}' for setting temperature."

    try:
        r = await http_client.post(url, json=payload, headers=headers, timeout=15)
        if r.status_code == 200:
            if current_mode == "HEAT":
                return f"Success: Set target temperature to {temp_celsius}°C."
            elif current_mode == "COOL":
                return f"Success: Set target temperature to {temp_celsius}°C."
            elif current_mode == "HEATCOOL":
                return f"Success: Set target range to {target_heat}°C - {target_cool}°C."
        else:
            return f"Nest error: API returned HTTP {r.status_code}: {r.text}"
    except Exception as e:
        logging.error(f"❌ Nest Set Temperature Error: {e}")
        return f"Nest error: Failed to set temperature: {e}"

async def overseer_search_movie(query: str) -> str: # Searches for movies in Overseer
    logging.info(f"🔧 TOOL: overseer_search_movie | '{query}'")
    def sync_search():
        encoded_query = quote(query)
        url = f"{OVERSEER_URL}/search?query={encoded_query}"
        headers = {"X-Api-Key": OVERSEER_KEY, "Accept": "application/json"}
        
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        raw_data = response.json()
        
        results = raw_data.get("results", [])
        if not results:
            return "SEARCH_NO_RESULTS"
            
        output_lines = ["\n--- MOVIE SEARCH RESULTS ---"]
        movie_results = [r for r in results if r.get("mediaType") == "movie"][:5]
        
        if not movie_results:
            return "SEARCH_NO_MOVIE_RESULTS"
            
        for i, item in enumerate(movie_results):
            tmdb_id = item.get("id")
            title = item.get("title", "No Title Found")
            release_date = item.get("releaseDate", "")
            year = release_date.split("-")[0] if release_date else "Unknown Year"
            
            output_lines.append(f"{i+1}. Title: {title} ({year}) | USE THIS ID: {tmdb_id}")
            
        output_lines.append("----------------------------\n")
        return "\n".join(output_lines)

    try:
        return await asyncio.to_thread(sync_search)
    except Exception as err:
        logging.error(f"Overseerr Movie Search Failed: {err}")
        return f"Error: {err}"

async def overseer_request_movie(tmdb_id): # Requests a movie through Seerr
    headers = {"X-Api-Key": OVERSEER_KEY, "Content-Type": "application/json"}
    payload = {"mediaType": "movie", "mediaId": int(float(tmdb_id)), "userId": int(OVERSEER_USER_ID), "is4k": False}
    try:
        r = await http_client.post(f"{OVERSEER_URL}/request", headers=headers, json=payload)

        logging.info(f"🔧 TOOL: overseer_request_movie | Status: {r.status_code}")

        if r.status_code == 201 or r.status_code == 200:
            return "SUCCESS: Movie requested for user."
        if r.status_code == 409:
            return "ALREADY_AVAILABLE_OR_PENDING"
        
        return f"FAILED: Overseer returned {r.status_code}"

    except Exception as e:
        logging.error(f"Overseerr Movie Request Failed: {e}")
        return f"Request failed: {e}"

async def overseer_search_tv(query: str) -> str: # Searches for TV shows in Overseer
    logging.info(f"🔧 TOOL: overseer_search_tv | '{query}'")

    def sync_search():
        encoded_query = quote(query)
        url = f"{OVERSEER_URL}/search?query={encoded_query}"
        headers = {"X-Api-Key": OVERSEER_KEY, "Accept": "application/json"}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        raw_data = response.json()
        
        results = raw_data.get("results", [])
        if not results:
            return "SEARCH_NO_RESULTS"
            
        output_lines = ["\n--- TV SHOW SEARCH RESULTS ---"]
        tv_results = [r for r in results if r.get("mediaType") == "tv"][:5]
        
        if not tv_results:
            return "SEARCH_NO_TV_RESULTS"
            
        for i, item in enumerate(tv_results):
            tmdb_id = item.get("id")
            title = item.get("name", "No Title Found")
            first_air_date = item.get("firstAirDate", "")
            year = first_air_date.split("-")[0] if first_air_date else "Unknown Year"
            output_lines.append(f"{i+1}. Show: {title} ({year}) | USE THIS ID: {tmdb_id}")
            
        output_lines.append("----------------------------\n")
        return "\n".join(output_lines)

    try:
        # Run your working code in a background thread so the bot stays responsive
        return await asyncio.to_thread(sync_search)
    except Exception as err:
        logging.error(f"Overseerr Search Failed: {err}")
        return f"Error: {err}"

async def overseer_request_tv_season(tmdb_id, season_number): # Requests a specific TV season through Seerr
    headers = {"X-Api-Key": OVERSEER_KEY, "Content-Type": "application/json"}
    payload = {"mediaType": "tv", "mediaId": int(float(tmdb_id)), "seasons": [int(season_number)], "userId": int(OVERSEER_USER_ID), "is4k": False}
    try:
        r = await http_client.post(f"{OVERSEER_URL}/request", headers=headers, json=payload)
        logging.info(f"🔧 TOOL: overseer_request_tv_season | Status: {r.status_code}")
        if r.status_code == 409: return f"Season {season_number} is already available or pending."
        return f"SUCCESS: Season {season_number} requested for user."
    except Exception as e:
        logging.error(f"Overseerr TV Season Request Failed: {e}")
        return f"Request failed: {e}"

async def fetch_web_content(url: str, max_chars: int = 8000) -> dict: # Fetches website content
    """
    Optimized for LLM agents following a SearXNG search.
    Removes boilerplate and preserves structural context.
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
    }

    try:
        # Use a longer timeout for specific site fetches (some sites are slow)
        async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
            response = await client.get(url, headers=headers)
            
            # If we hit a 403 or 401, the model needs to know it's a permission issue (paywall/bot detection)
            if response.status_code != 200:
                return {
                    "success": False, 
                    "status": response.status_code, 
                    "error": f"Site returned status code {response.status_code}. It might be blocking scrapers or require a subscription."
                }

            soup = BeautifulSoup(response.text, 'html.parser')

            # 1. Strip non-content noise
            for element in soup(['script', 'style', 'nav', 'footer', 'header', 'aside', 'form', 'noscript', 'svg', 'iframe']):
                element.decompose()

            # 2. Extract title and description
            title = soup.title.string.strip() if soup.title else "No Title"
            
            # 3. Preserving "Meaningful" Structure
            # We use markers so the LLM understands headers vs body
            for tag in soup.find_all(['h1', 'h2', 'h3']):
                tag.insert_before("\n[HEADER: ")
                tag.insert_after("]\n")
            
            for li in soup.find_all('li'):
                li.insert_before("\n- ")

            # 4. Content Extraction
            # Using separator='\n' prevents text from different divs slamming together
            text = soup.get_text(separator='\n')
            
            # Clean up whitespace: remove triple+ newlines, but keep double newlines for paragraphs
            cleaned_text = re.sub(r'\n{3,}', '\n\n', text).strip()
            cleaned_text = re.sub(r' +', ' ', cleaned_text) # Remove multiple spaces

            # 5. Smart Truncation
            if len(cleaned_text) > max_chars:
                cleaned_text = cleaned_text[:max_chars] + "... [Content truncated for length]"

            # If after cleaning we have almost no text, it was likely a JS-heavy app
            if len(cleaned_text) < 200:
                return {
                    "success": False,
                    "error": "The page yielded very little text. It may require JavaScript to render or be a login wall."
                }

            return {
                "success": True,
                "title": title,
                "url": url,
                "content": cleaned_text
            }

    except Exception as e:
        return {"success": False, "error": f"Connection Error: {str(e)}"}


    """
    Uses the main text model to filter the verbose raw vision description,
    extracting only active entities, security details, and threats.
    Accommodates large models on CPU with an extended 300-second timeout.
    """
    logging.info("🧠 REOLINK: Filtering raw visual data through main text model...")
    try:
        url = OLLAMA_URL
        prompt = f"""You are a professional home security monitoring system.
Review this raw visual description of the live '{camera_name}' security camera feed.

Extract and report ONLY active entities, security hazards, or items of interest:
- People (exact clothing, appearance, behavior)
- Vehicles (type, color, position)
- Deliveries, packages, or tools left out of place
- Animals or unexpected objects on walkways

STRICT SECURITY FILTER RULES:
1. Do NOT describe the house, siding, lawn, backyard, fences, background trees, weather, or lighting conditions unless they are directly involved in an active security event.
2. Be highly specific and direct (e.g., "There is a delivery driver in a blue vest carrying a package up your driveway" or "A dark silver SUV is parked at the curb").
3. Keep your output extremely concise (exactly 1 or 2 sentences max).
4. If there are no people, no cars, no packages, and absolutely nothing unusual or active in the description, respond EXACTLY with: "No active threats or activity detected."

Detailed Visual Input:
{raw_description}"""

        payload = {
            "model": MODEL_ID,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "keep_alive": -1,
            "options": {
                "temperature": 0.1,
                "num_ctx": 4096
            }
        }

        # Increased timeout to 300 seconds to give CPU-bound local models plenty of time to respond
        r = await http_client.post(url, json=payload, timeout=300)
        
        if r.status_code == 200:
            summary = r.json().get('message', {}).get('content', '').strip()
            
            # Safely strip reasoning blocks if the model uses them
            summary = re.sub(r'<[tT]hink>.*?</[tT]hink>', '', summary, flags=re.DOTALL).strip()
            summary = re.sub(r'</?[tT]hink>', '', summary).strip()
            
            logging.info(f"🧠 REOLINK: Filtered security summary: '{summary}'")
            return summary
            
        logging.error(f"❌ Reolink summary generation failed with status: {r.status_code}")
        return "Unusual activity detected, but analysis failed."
        
    except Exception as e:
        logging.error(f"❌ Reolink security filter crash: {e}", exc_info=True)
        return "Activity detected, but security filtering encountered an error."

async def get_reolink_snapshot(camera_name: str) -> str: # Gets image from camera and sends it to the user.
    """
    Grabs a live snapshot from a specified Reolink camera channel.
    Runs a two-stage vision analysis:
    1. Generates a broad scene description to insert into the LLM's chat history context.
    2. Generates a strict, concise threat assessment to caption the Telegram photo.
    """
    logging.info(f"🔧 TOOL: get_reolink_snapshot | Camera: {camera_name}")
    
    # 1. Parse configuration
    host = os.getenv("REOLINK_HOST")
    user = os.getenv("REOLINK_USER")
    password = os.getenv("REOLINK_PASSWORD")
    cameras_raw = os.getenv("REOLINK_CAMERAS", "")
    
    # Map camera names to channel IDs
    camera_map = {}
    for item in cameras_raw.split(","):
        if ":" in item:
            name, channel = item.split(":")
            camera_map[name.strip().lower()] = channel.strip()
            
    # Normalize target input string
    target_name = camera_name.lower().strip()
    for word in ["camera", "feed", "view", "stream"]:
        target_name = target_name.replace(word, "").strip()
        
    cleaned_target = target_name.replace(" ", "").replace("_", "").replace("-", "")
    
    channel = None
    matched_camera_name = None

    # Strict Pass 1: Cleaned Exact Match
    for key, val in camera_map.items():
        cleaned_key = key.replace(" ", "").replace("_", "").replace("-", "")
        if cleaned_key == cleaned_target:
            channel = val
            matched_camera_name = key
            break
            
    # Fallback Pass 2: Substring Match
    if not channel:
        sorted_keys = sorted(camera_map.keys(), key=len, reverse=True)
        for key in sorted_keys:
            cleaned_key = key.replace(" ", "").replace("_", "").replace("-", "")
            if cleaned_target in cleaned_key or cleaned_key in cleaned_target:
                channel = camera_map[key]
                matched_camera_name = key
                break

    if not channel:
        available_cams = ", ".join(camera_map.keys())
        return f"Error: Camera '{camera_name}' not found. Available cameras: {available_cams}"
        
    # Protocols to attempt sequentially
    protocols = [
        {"name": "HTTPS", "url": f"https://{host}/cgi-bin/api.cgi?cmd=Snap&channel={channel}&user={user}&password={password}"},
        {"name": "HTTP", "url": f"http://{host}/cgi-bin/api.cgi?cmd=Snap&channel={channel}&user={user}&password={password}"}
    ]
    
    response_content = None
    successful_protocol = None

    # 2. Query Reolink API (Sequential Attempt)
    for proto in protocols:
        try:
            logging.info(f"📹 CAMERA: Connecting via {proto['name']} → {host}...")
            r = await http_client.get(proto["url"], timeout=8)
            
            if r.status_code == 200:
                if r.content.startswith(b'\xff\xd8'):
                    response_content = r.content
                    successful_protocol = proto["name"]
                    logging.info(f"✅ CAMERA: Snapshot fetched via {proto['name']}")
                    break
                else:
                    error_msg = r.content.decode('utf-8', errors='ignore')
                    logging.warning(f"⚠️ REOLINK: {proto['name']} connected, but API returned error: {error_msg}")
            else:
                logging.warning(f"⚠️ REOLINK: {proto['name']} returned HTTP status code {r.status_code}")
                
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.NetworkError):
            logging.warning(f"⚠️ REOLINK: Connection via {proto['name']} failed. (Port closed or offline)")
        except Exception as e:
            logging.error(f"❌ REOLINK: Unexpected error on {proto['name']}: {e}")

    # If both protocols failed to get an image
    if not response_content:
        return (f"FAILED: Could not connect to your Reolink NVR at {host}. "
                f"1. Please REBOOT your Reolink NVR to apply the HTTPS/CGI settings. "
                f"2. If the bot runs in Docker, ensure Docker's firewall allows routing to your LAN.")

    # 3. Two-Stage Vision Analysis
    try:
        compressed_bytes = compress_image_bytes(response_content)
        b64_image = base64.b64encode(compressed_bytes).decode('utf-8')
        
        # Parse camera descriptions for prompting context
        descriptions_raw = os.getenv("REOLINK_CAMERA_DESCRIPTIONS", "")
        camera_descriptions = {}
        for item in descriptions_raw.split(","):
            if ":" in item:
                name, desc = item.split(":", 1)
                camera_descriptions[name.strip().lower()] = desc.strip()
                
        default_descriptions = {
            "frontdoor": "A doorbell camera located on the front door, looking at the front patio and sidewalk",
            "backdoor": "the back door, back patio, and side entrance through the gate. Viewing the rear of the house.",
            "backyard": "The entire backyard, with a perimeter fence, stone pavers, and garden beds located in the middle.",
            "driveway": "the driveway leading to the street",
            "front": "the front yard and sidewalk area facing the street",
            "alleyway": "the back parking off the alleyway, located behind the detached garage."
        }
        camera_desc = camera_descriptions.get(matched_camera_name, default_descriptions.get(matched_camera_name, ""))
        desc_context = f" (which is looking at: {camera_desc})" if camera_desc else ""
        
        # Inject current date/time context for vision processing
        now_dt = datetime.now(USER_TIMEZONE)
        now_str = now_dt.strftime("%A, %B %d, %Y at %I:%M %p")
        time_context = (
            f" The snapshot was captured on {now_str}. "
            "Note that at night, the camera feed automatically switches to black and white night vision."
        )
        
        # --- STAGE 1: Threat Analysis (For Telegram Caption) ---
        logging.info("👁️ VISION [1/2]: Running threat analysis...")
        security_prompt = f"""You are a professional home security monitoring system checking the live '{matched_camera_name}' camera feed{desc_context}.{time_context}
            Analyze this image and report ONLY active entities, security hazards, or items of interest:
            - People (exact clothing, appearance, behavior)
            - Vehicles (type, color, position)
            - Deliveries, packages, or tools left out of place
            - Animals or unexpected objects on walkways

        STRICT SECURITY FILTER RULES:
            1. Do NOT describe the house, siding, lawn, backyard, fences, background trees, weather, or lighting conditions unless they are directly involved in an active security event.
            2. Be highly specific and direct (e.g., "There is a delivery driver in a blue vest carrying a package up the driveway").
            3. Keep your output extremely concise (exactly 1 or 2 sentences max).
            4. If there are no people, no cars, no packages, and absolutely nothing unusual or active in the image, respond EXACTLY with: "No active threats or activity detected." """
            
        concise_report = await get_image_description(b64_image, security_prompt)
        logging.info(f"👁️ VISION [1/2] Raw Response: '{concise_report}'")
        
        # Fallback if vision analysis returns empty response
        if not concise_report or not concise_report.strip():
            concise_report = "No active threats or activity detected."
        
        # --- STAGE 2: Send the photo to Telegram captioned with the clean threat report ---
        if TARGET_CHAT_ID:
            telegram_caption = f"📸 <b>Live: {matched_camera_name.upper()}</b>\n\n🛡️ <i>{concise_report}</i>"
            
            await application_bot.send_photo(
                chat_id=TARGET_CHAT_ID,
                photo=response_content,
                caption=telegram_caption,
                parse_mode="HTML"
            )
            
            # --- STAGE 3: Broad Scene Description (For LLM Memory Context) ---
            logging.info("👁️ VISION [2/2]: Generating scene context...")
            context_prompt = (
                f"This is a live feed from the {matched_camera_name} camera{desc_context}.{time_context} "
                "Concisely describe the layout, stationary structures, background, "
                "and visible inanimate objects in the frame."
            )
            scene_context = await get_image_description(b64_image, context_prompt)
            logging.info(f"👁️ VISION [2/2] Raw Response: '{scene_context}'")
            
            # 5. Return both datasets to Emery's core loop.
            # This inserts the spatial scene layout and active alert details directly into her memory context.
            return (
                f"SUCCESS: Live photo sent directly to user. "
                f"For your context, here is what the '{matched_camera_name}' environment looks like: '{scene_context}'. "
                f"The active threat report sent to the user was: '{concise_report}'. "
                f"You must now output exactly the word 'DONE' and absolutely nothing else as your final response to close the turn."
            )
            
        return "Failed to send photo: Chat context lost."
        
    except Exception as e:
        logging.error(f"❌ Reolink Tool Analysis/Send Crash: {e}", exc_info=True)
        return f"Successfully grabbed the image via {successful_protocol}, but failed to analyze/send it: {e}"

async def get_available_cameras() -> str: # Gets all available cameras
    """
    Returns a clean, human-readable list of all configured home security cameras.
    """
    logging.info("🔧 TOOL: get_available_cameras")
    raw_cams = os.getenv("REOLINK_CAMERAS", "")
    
    if not raw_cams:
        return "No security cameras are currently configured in the system."
        
    camera_names = []
    for item in raw_cams.split(","):
        colon_idx = item.find(":")
        if colon_idx != -1:
            # Safe string slice (guaranteed not to cause list attribute errors!)
            camera_name_only = item[:colon_idx]
            camera_names.append(camera_name_only.strip())
            
    if not camera_names:
        return "The camera configuration is empty or formatted incorrectly."
        
    formatted_list = ", ".join([f"'{c}'" for c in camera_names])
    return f"The following security cameras are online and available: {formatted_list}"


    """Starts the active-polling loop as a background task upon bot initialization."""
    if os.getenv("ENABLE_REOLINK_POLLING", "false").lower() == "true":
        asyncio.create_task(reolink_polling_loop(application))

async def trigger_webhook_alert(camera_name: str):
    """Immediately alerts the user of native NVR person detection and starts two-stage vision analysis."""
    global TARGET_CHAT_ID
    
    # Fallback: Recover TARGET_CHAT_ID from active chat histories if not yet set
    if not TARGET_CHAT_ID and chat_histories:
        TARGET_CHAT_ID = list(chat_histories.keys())[0]
        
    if not TARGET_CHAT_ID:
        logging.warning("⚠️ SECURITY ALERT: Motion detected, but no active chat session established. Please send a message to the bot first.")
        return
        
    logging.info(f"🚨 SECURITY: Person trigger received for '{camera_name}' — dispatching alert...")
    
    await application_bot.send_message(
        chat_id=TARGET_CHAT_ID,
        text=f"🚨 <b>Person Detected:</b> Someone is on the <b>{camera_name.upper()}</b> camera. Running analysis...",
        parse_mode="HTML"
    )

    result = await get_reolink_snapshot(camera_name)
    logging.info(f"✅ SECURITY: Alert dispatched for '{camera_name}'")

    # Append the security alert event to the model's history context
    if TARGET_CHAT_ID:
        if TARGET_CHAT_ID not in chat_histories:
            chat_histories[TARGET_CHAT_ID] = deque(maxlen=100)
            
        now_dt = datetime.now(USER_TIMEZONE)
        now_str = now_dt.strftime("%A, %B %d, %Y at %I:%M %p")
        event_content = (
            f"[{now_str}] [SYSTEM SECURITY ALERT] Camera '{camera_name}' triggered a person-detection event. "
            f"Result: {result}"
        )
        chat_histories[TARGET_CHAT_ID].append({"role": "user", "content": event_content})

async def reolink_polling_loop(application):
    """Runs a highly diagnostic background loop that polls the Reolink NVR for motion states."""
    if os.getenv("ENABLE_REOLINK_POLLING", "false").lower() != "true":
        return
        
    logging.info("📹 CAMERA POLL: Initializing background person-detection polling loop...")
    
    host = os.getenv("REOLINK_HOST")
    user = os.getenv("REOLINK_USER")
    password = os.getenv("REOLINK_PASSWORD")
    cameras_raw = os.getenv("REOLINK_CAMERAS", "")
    
    # Map camera names to channel IDs using safe string slicing
    camera_map = {}
    for item in cameras_raw.split(","):
        colon_idx = item.find(":")
        if colon_idx != -1:
            name = item[:colon_idx].strip()
            chan = item[colon_idx+1:].strip()
            camera_map[name] = chan
            
    if not camera_map:
        logging.warning("⚠️ REOLINK POLLING: No cameras mapped. Check your REOLINK_CAMERAS environment variable.")
        return
        
    logging.info(f"📹 CAMERA POLL: Mapped {len(camera_map)} cameras — {list(camera_map.keys())}")
    
    # Track state: {channel_id: {"last_state": 0, "cooldown_until": datetime}}
    state_tracker = {
        chan: {
            "last_state": 0, 
            "cooldown_until": datetime.min.replace(tzinfo=pytz.UTC)
        } for chan in camera_map.values()
    }

    # --- STARTUP DIAGNOSTIC SELF-TEST ---
    test_cam = next(iter(camera_map))
    test_chan = camera_map[test_cam]
    test_url = f"https://{host}/cgi-bin/api.cgi?cmd=GetAiState&user={user}&password={password}"
    test_body = [{"cmd": "GetAiState", "param": {"channel": int(test_chan)}}]
    
    logging.info(f"📹 CAMERA POLL: Running startup self-test on '{test_cam}' (ch.{test_chan})...")
    try:
        r = await http_client.post(test_url, json=test_body, timeout=10)
        logging.info(f"📹 CAMERA POLL: Self-test HTTPS status {r.status_code}")
        
        if r.status_code != 200:
            # Try HTTP
            test_url_http = test_url.replace("https://", "http://")
            logging.info(f"📹 CAMERA POLL: HTTPS failed — trying HTTP fallback...")
            r = await http_client.post(test_url_http, json=test_body, timeout=10)
            logging.info(f"📹 CAMERA POLL: HTTP fallback status {r.status_code}")
            
        if r.status_code == 200:
            raw_json = r.json()
            # Log whether AI person detection is supported on this channel
            if isinstance(raw_json, list) and raw_json:
                ai_value = raw_json[0].get("value", {})
                people_support = ai_value.get("people", {}).get("support", 0)
                logging.info(f"📹 CAMERA POLL: AI person detection on '{test_cam}': {'supported ✅' if people_support else 'NOT supported ❌ — upgrade firmware'}")
        else:
            logging.error(f"❌ CAMERA POLL: NVR returned status {r.status_code} — check CGI/HTTPS port settings")
    except Exception as e:
        logging.error(f"❌ REOLINK DIAGNOSTIC: Connection self-test crashed: {e}", exc_info=True)
    # ------------------------------------

    logging.info("📹 CAMERA POLL: Self-test complete — polling loop active")
    
    # Run loop forever inside the bot's asynchronous thread
    while True:
        try:
            # Poll camera states every 2.5 seconds
            await asyncio.sleep(2.5)
            
            for camera_name, channel in camera_map.items():
                # Use GetAiState (POST with JSON body) to get native person/vehicle/pet detection
                url = f"https://{host}/cgi-bin/api.cgi?cmd=GetAiState&user={user}&password={password}"
                body = [{"cmd": "GetAiState", "param": {"channel": int(channel)}}]
                
                try:
                    r = await http_client.post(url, json=body, timeout=5)
                    if r.status_code != 200:
                        # Fallback to HTTP
                        url_http = url.replace("https://", "http://")
                        r = await http_client.post(url_http, json=body, timeout=5)
                        
                    if r.status_code == 200:
                        data = r.json()
                        if isinstance(data, list) and len(data) > 0:
                            entry = data[0]
                            code = entry.get("code", -1)
                            if code != 0:
                                error_detail = entry.get("error", {})
                                logging.warning(f"⚠️ REOLINK POLLING: Camera '{camera_name}' (Channel {channel}) returned API error code {code}: {error_detail}")
                                continue

                            value = entry.get("value", {})
                            # Native AI person detection — alarm_state 1 = person in frame, 0 = clear
                            current_state = value.get("people", {}).get("alarm_state", 0)

                            tracker = state_tracker[channel]
                            last_state = tracker["last_state"]
                            now = datetime.now(pytz.UTC)

                            # State Transition: Person just appeared (0 -> 1)
                            if last_state == 0 and current_state == 1:
                                if now > tracker["cooldown_until"]:
                                    tracker["cooldown_until"] = now + timedelta(seconds=60)
                                    logging.info(f"🚨 CAMERA POLL: Person detected on '{camera_name}' — triggering alert")
                                    asyncio.create_task(trigger_webhook_alert(camera_name))

                            tracker["last_state"] = current_state
                        else:
                            logging.warning(f"⚠️ REOLINK POLLING: Unexpected response format from NVR: {data}")
                    else:
                        logging.warning(f"⚠️ REOLINK POLLING: Both HTTP and HTTPS queries returned code {r.status_code} for camera '{camera_name}'")
                except (httpx.ConnectError, httpx.ConnectTimeout, httpx.NetworkError) as e:
                    logging.debug(f"REOLINK POLLING: Connection blip on camera '{camera_name}' (Channel {channel})")
                except Exception as inner_e:
                    logging.error(f"❌ REOLINK POLLING: Error evaluating state for camera '{camera_name}': {inner_e}")
                    
        except Exception as outer_e:
            logging.error(f"❌ REOLINK POLLING: Global polling loop exception: {outer_e}")

async def start_reolink_polling(application):
    """Starts the active-polling loop as a background task upon bot initialization."""
    if os.getenv("ENABLE_REOLINK_POLLING", "false").lower() == "true":
        asyncio.create_task(reolink_polling_loop(application))

# Create empty containers first
AVAILABLE_TOOLS = {}
tools_schema = []

# --- Helper to check if a feature is enabled ---
def is_enabled(var_name):
    return os.getenv(var_name, "false").lower() == "true"

# --- Conditional Tool Registration ---
if is_enabled("ENABLE_CALENDAR"): # Calendar
    AVAILABLE_TOOLS["get_calendar_events"] = get_calendar_events
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "get_calendar_events", 
            "description": "Fetch User's Google Calendar events.",
            "parameters": {"type": "object", "properties": {}}
        }
    })

if is_enabled("ENABLE_NEST"): # Google Nest Thermostat
    AVAILABLE_TOOLS["get_nest_thermostats"] = get_nest_thermostats
    AVAILABLE_TOOLS["set_nest_thermostat_mode"] = set_nest_thermostat_mode
    AVAILABLE_TOOLS["set_nest_thermostat_temperature"] = set_nest_thermostat_temperature
    tools_schema.extend([
        {
            "type": "function",
            "function": {
                "name": "get_nest_thermostats",
                "description": "Fetch the list of all Nest thermostats and their current status (ambient temperature, humidity, mode, target temperature setpoints, and HVAC state).",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "set_nest_thermostat_mode",
                "description": "Set the operating mode for a Nest Thermostat.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "device_id": {
                            "type": "string",
                            "description": "The full device ID/resource name returned by get_nest_thermostats (e.g. enterprises/{project_id}/devices/{device_id})."
                        },
                        "mode": {
                            "type": "string",
                            "description": "The mode to set: HEAT, COOL, HEATCOOL, or OFF."
                        }
                    },
                    "required": ["device_id", "mode"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "set_nest_thermostat_temperature",
                "description": "Set the target temperature for a Nest Thermostat. Specify temperature in Celsius. Convert Fahrenheit to Celsius if user requests it.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "device_id": {
                            "type": "string",
                            "description": "The full device ID/resource name returned by get_nest_thermostats (e.g. enterprises/{project_id}/devices/{device_id})."
                        },
                        "temp_celsius": {
                            "type": "number",
                            "description": "The target temperature in Celsius (used for single-setpoint modes like HEAT or COOL)."
                        },
                        "heat_temp_celsius": {
                            "type": "number",
                            "description": "The target heat temperature in Celsius (used for range mode HEATCOOL)."
                        },
                        "cool_temp_celsius": {
                            "type": "number",
                            "description": "The target cool temperature in Celsius (used for range mode HEATCOOL)."
                        }
                    },
                    "required": ["device_id"]
                }
            }
        }
    ])

if is_enabled("ENABLE_SEERR"): # Seerr
    AVAILABLE_TOOLS.update({
        "overseer_search_movie": overseer_search_movie,
        "overseer_request_movie": overseer_request_movie,
        "overseer_search_tv": overseer_search_tv,
        "overseer_request_tv_season": overseer_request_tv_season
    })
    tools_schema.extend([
        {"type": "function", "function": {
        "name": "overseer_search_movie", 
        "description": "Search for a movie. Query MUST contain ONLY the title (no years/actors). Use FIRST when the User asks you to add a movie or request a movie. Return the results in a numbered list, and DO NOT include the ID in the response.", 
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    }},
    {"type": "function", "function": {
        "name": "overseer_request_movie", 
        "description": "Request a movie to the User's media server using its TMDB ID. Use AFTER the user selects a movie from the search results from overseer_search_movie. Call the tool using the ID from the search results.", 
        "parameters": {"type": "object", "properties": {"tmdb_id": {"type": "integer"}}, "required": ["tmdb_id"]}
    }},
    {"type": "function", "function": {
        "name": "overseer_search_tv", 
        "description": "Search for a TV show. Query MUST contain ONLY the title. Use FIRST when the User asks you to add a TV show or request a TV show. Return the results in a numbered list, and DO NOT include the ID in the response.", 
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    }},
    {"type": "function", "function": {
        "name": "overseer_request_tv_season", 
        "description": "Request a specific season of a TV show to the User's media server using its TMDB ID. Use AFTER the user selects a TV show from the search results from overseer_search_tv. Call the tool using the ID from the search results.", 
        "parameters": {
            "type": "object", 
            "properties": {
                "tmdb_id": {"type": "integer"},
                "season_number": {"type": "integer", "description": "0 for all, or specific number."}
            }, 
            "required": ["tmdb_id", "season_number"]
        }
    }}
    ])

if is_enabled("ENABLE_WEATHER"): # NOAA Weather
    AVAILABLE_TOOLS["get_noaa_weather"] = get_noaa_weather
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "get_noaa_weather", 
            "description": "Get weather.", 
            "parameters": {}
        }
    })

if is_enabled("ENABLE_NEWS"): # News
    AVAILABLE_TOOLS["get_news_headlines"] = get_news_headlines
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "get_news_headlines", 
            "description": "Get news headlines.", 
            "parameters": {}
        }
    })

if is_enabled("ENABLE_NASA"): # NASA
    AVAILABLE_TOOLS["get_nasa_apod"] = get_nasa_apod
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "get_nasa_apod", 
            "description": "Get NASA APOD. You ***MUST*** include the RAW URL in the response. Do NOT use an embed URL.", 
            "parameters": {}
        }
    })

if is_enabled("ENABLE_HISTORY"): # History
    AVAILABLE_TOOLS["get_today_in_history"] = get_today_in_history
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "get_today_in_history", 
            "description": "Get events from history for today.", 
            "parameters": {}
        }
    })

if is_enabled("ENABLE_SEARCH"): # Search
    AVAILABLE_TOOLS["web_search"] = web_search
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "web_search", 
            "description": "Search web, use when needing a deep dive, research, or a query you lack knowledge about. After you receive the results, ask youself if you need to perform another search. If the results are not sufficent, call this tool again with a more specific query. You can and should also use the fetch_web_content tool to get the content of specific results if needed. ***DO NOT INCLUDE URLS IN YOUR RESPONSE***", 
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
        }
    })

if is_enabled("ENABLE_IMAGEGEN"): # Image Generation
    AVAILABLE_TOOLS["generate_image"] = generate_image
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "generate_image", 
            "description": "Generate an image. Enhance the prompt with as much detail as possible to get the best results, while staying true to the original request. ***DO NOT INCLUDE URLS IN YOUR RESPONSE***", 
            "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]}
        }
    })

if is_enabled("ENABLE_VOICE"): # Voice
    AVAILABLE_TOOLS["speak_message"] = speak_message
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "speak_message", 
            "description": "Convert text to speech and send as a voice memo to User. Use this when User explicitly asks to 'speak', 'say', or 'send a voice message'. Do NOT use emojis or symbols in tool call! ***ONLY USE IF THE MOST CURRENT MESSAGE EXPLICITLY ASKS FOR SPOKEN CONTENT OR A VOICE MEMO***", 
            "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
        }
    })

if is_enabled("ENABLE_SYSTEM_STATS"): # System Stats
    AVAILABLE_TOOLS["get_system_stats"] = get_system_stats
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "get_system_stats", 
            "description": "Get system stats.", 
            "parameters": {}
        }
    })

if is_enabled("ENABLE_WEB_SCRAPING"): # Web Scraping
    AVAILABLE_TOOLS["fetch_web_content"] = fetch_web_content
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "fetch_web_content", 
            "description": "Fetch and parse the content of a specific URL. Use this when you need to read an article, blog, or specific webpage content. It returns the title, URL, and the main text content (truncated if long). Use AFTER web_search to do deep research, a deep dive, a report, etc. if needed. MUST pass only the URL as a string. Do not pass any other arguments.", 
            "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}
        }
    })

if is_enabled("ENABLE_REOLINK"): # Reolink Security
    AVAILABLE_TOOLS["get_reolink_snapshot"] = get_reolink_snapshot
    AVAILABLE_TOOLS["get_available_cameras"] = get_available_cameras
    
    # Dynamically extract real camera names from your environment configurations
    raw_cams = os.getenv("REOLINK_CAMERAS", "")
    camera_names = []
    for item in raw_cams.split(","):
        colon_idx = item.find(":")
        if colon_idx != -1:
            camera_name_only = item[:colon_idx]
            camera_names.append(camera_name_only.strip())
            
    # Format list as a readable array string: "'front', 'frontdoor', 'backyard'"
    camera_list_str = ", ".join([f"'{c}'" for c in camera_names]) if camera_names else "'front', 'frontdoor'"
    
    tools_schema.extend([
        {
            "type": "function",
            "function": {
                "name": "get_reolink_snapshot",
                "description": "Get a live image stream and AI analysis from a home security camera. Use whenever the user asks to check, look at, view, or patrol a camera location.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "camera_name": {
                            "type": "string",
                            "description": f"The exact name of the camera to check. You MUST choose exactly one option from this list: {camera_list_str}."
                        }
                    },
                    "required": ["camera_name"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_available_cameras",
                "description": "Get a list of all configured and online home security camera names. Use when the user asks what cameras they have, what camera feeds are available, or lists of security cameras.",
                "parameters": {
                    "type": "object",
                    "properties": {}
                }
            }
        }
    ])

if ENABLE_MEMORY: # Memory
    AVAILABLE_TOOLS["save_user_memory"] = save_user_memory
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "save_user_memory", 
            "description": "Saves a new fact, preference, or critical piece of information about the user or their environment to the permanent memory log. Use when the user shares something they expect you to remember long-term.", 
            "parameters": {
                "type": "object", 
                "properties": {
                    "fact": {
                        "type": "string",
                        "description": "The exact fact, preference, or instruction to remember (e.g. 'Hudson prefers tabs over spaces')."
                    }
                }, 
                "required": ["fact"]
            }
        }
    })

TOOL_STATUS_MESSAGES = {
    "save_user_memory": f"{MODEL_NAME} is writing this down in memory...",
    "web_search": f"{MODEL_NAME} is surfing the web...",
    "get_calendar_events": f"{MODEL_NAME} is checking your calendar...",
    "get_nest_thermostats": f"{MODEL_NAME} is checking the Nest thermostat status...",
    "set_nest_thermostat_mode": f"{MODEL_NAME} is changing the Nest thermostat mode...",
    "set_nest_thermostat_temperature": f"{MODEL_NAME} is adjusting the Nest thermostat temperature...",
    "get_noaa_weather": f"{MODEL_NAME} is looking outside...",
    "generate_image": f"{MODEL_NAME} is painting a picture...",
    "get_news_headlines": f"{MODEL_NAME} is reading the morning news...",
    "get_nasa_apod": f"{MODEL_NAME} is studying the stars...",
    "get_today_in_history": f"{MODEL_NAME} is dusting off the archives...",
    "speak_message": f"{MODEL_NAME} is recording a voice memo...",
    "overseer_search_movie": f"{MODEL_NAME} is searching for a movie...",
    "overseer_request_movie": f"{MODEL_NAME} is requesting a movie...",
    "overseer_search_tv": f"{MODEL_NAME} is searching for a TV show...",
    "overseer_request_tv_season": f"{MODEL_NAME} is requesting a TV season...",
    "fetch_web_content": f"{MODEL_NAME} is fetching a website...",
    "get_reolink_snapshot": f"{MODEL_NAME} is investigating a bump in the night...",
    "get_available_cameras": f"{MODEL_NAME} is reading your camera configuration..."
}

# --- THE UNIFIED ENGINE ---
async def emery_engine(history_buffer, model_to_use=MODEL_ID):
    url = OLLAMA_URL
    ctx_size = int(os.getenv("OLLAMA_NUM_CTX", "65536"))
    
    # Find the latest user query from the history buffer for memory keyword filtering
    user_query = ""
    for msg in reversed(history_buffer):
        if msg.get("role") == "user":
            user_query = msg.get("content", "")
            break
            
    system_msg = {"role": "system", "content": get_current_system_prompt(user_query)}
    voice_sent_via_tool = False
    
    # Ensure history buffer is passed as clean text only
    ollama_history = []
    for msg in history_buffer:
        clean_msg = {"role": msg["role"]}
        content = msg.get("content", "")
        
        # If the content contains raw dictionary objects or lists, flatten them to strings
        if isinstance(content, list):
            text_parts = [part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"]
            clean_msg["content"] = " ".join(text_parts) if text_parts else "[Sent an image]"
        elif isinstance(content, str):
            # Scrub base64 artifacts if any are left over
            if len(content) > 5000 and not any(c.isspace() for c in content[1000:3000]):
                clean_msg["content"] = "[Image base64 data removed]"
            else:
                clean_msg["content"] = content
        else:
            clean_msg["content"] = str(content)
            
        ollama_history.append(clean_msg)
    
    for loop_count in range(TOOL_LOOP):
        full_context = [system_msg] + ollama_history
        
        payload = {
            "model": model_to_use,
            "messages": full_context,
            "stream": False,
            "keep_alive": -1,
            "think": True,
            "options": {
                "num_ctx": ctx_size,
                "temperature": 0.8,
                "top_p": 0.9,
                "num_gpu": 0
            }
        }
        
        if tools_schema:
            payload["tools"] = tools_schema

        try:
            logging.info(f"🤖 ENGINE: Thinking... (loop {loop_count+1}/{TOOL_LOOP})")
            r = await http_client.post(url, json=payload, timeout=300)
            
            if r.status_code != 200:
                logging.error(f"❌ ENGINE: Ollama returned {r.status_code} — {r.text[:200]}")
                return "Ollama connection error.", False

            res = r.json()
            msg = res.get('message', {})
            
            # Tool Executions
            if msg.get("tool_calls"):
                history_buffer.append(msg)
                ollama_history.append(msg)
                for tc in msg['tool_calls']:
                    fn = tc['function']['name']
                    args = tc['function'].get('arguments', {})
                    
                    status_msg = TOOL_STATUS_MESSAGES.get(fn, f"Emery is using {fn}...")
                    await application_bot.send_message(chat_id=TARGET_CHAT_ID, text=f"<i>{status_msg}</i>", parse_mode="HTML")
                    
                    logging.info(f"🔧 TOOL: {fn} | Args: {args}")
                    if fn == "speak_message": 
                        voice_sent_via_tool = True
                    
                    result = await AVAILABLE_TOOLS[fn](**args) if args else await AVAILABLE_TOOLS[fn]()
                    
                    tool_response = {"role": "tool", "content": str(result)}
                    history_buffer.append(tool_response)
                    ollama_history.append(tool_response)
                continue
            
            content = msg.get('content', "")
            reasoning = msg.get('thinking', "") or msg.get('reasoning', "")
            
            logging.info(f"🤖 ENGINE: Response ready — {len(content)} chars" + (f", {len(reasoning)} chars reasoning" if reasoning else ""))

            if reasoning:
                start_think_tag = "<" + "think" + ">"
                end_think_tag = "</" + "think" + ">"
                final_text = f"{start_think_tag}\n{reasoning}\n{end_think_tag}\n{content}"
            else:
                final_text = content


            return final_text, voice_sent_via_tool
            
        except Exception as e:
            logging.error(f"❌ ENGINE: Crash — {e}", exc_info=True)
            return "EMERYCHAT engine failure.", False
            
    return "Timeout.", False

# --- HANDLERS ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TARGET_CHAT_ID
    chat_id = update.effective_chat.id
    TARGET_CHAT_ID = chat_id
    
    if chat_id not in chat_histories: 
        chat_histories[chat_id] = deque(maxlen=100)
    
    is_input_voice = False
    model_to_use = MODEL_ID # Default model
    
    # Capture the current time for this specific message
    now_str = datetime.now(USER_TIMEZONE).strftime("%A, %B %d, %Y at %I:%M %p")

    if update.message.voice:
        is_input_voice = True
        v_file = await update.message.voice.get_file()
        transcription = await transcribe_audio(await v_file.download_as_bytearray())
        if not transcription: 
            return
        content = f"[{now_str}] {transcription}"
    elif update.message.photo:
        p_file = await update.message.photo[-1].get_file()
        photo_bytes = await p_file.download_as_bytearray()
        compressed_bytes = compress_image_bytes(photo_bytes)
        b64 = base64.b64encode(compressed_bytes).decode('utf-8')
        caption = update.message.caption or ""
        
        await update.message.reply_chat_action("typing")
        description = await get_image_description(b64, caption)
        
        content_text = "User sent an image."
        if caption:
            content_text += f" User's caption: {caption}"
        content_text += f"\nImage Description: {description}"
        
        content = f"[{now_str}] {content_text}"
    else:
        content = f"[{now_str}] {update.message.text}"
        
    logging.info(f"💬 USER (chat {chat_id}): {str(content)[:120]}")
    chat_histories[chat_id].append({"role": "user", "content": content})
    
    # --- TYPING INDICATOR LOOP ---
    typing_stop = asyncio.Event()

    async def keep_typing():
        while not typing_stop.is_set():
            try:
                await update.message.reply_chat_action("typing")
            except Exception as e:
                logging.debug(f"Typing action failed: {e}")
            await asyncio.sleep(4)

    typing_task = asyncio.create_task(keep_typing())
    # -----------------------------

    try:
        # Run the engine
        response_text, voice_sent_via_tool = await emery_engine(chat_histories[chat_id], model_to_use=model_to_use)
    finally:
        # Stop the typing loop once the engine is finished
        typing_stop.set()
        await typing_task

    # Save the assistant text (with raw think tags intact) to history
    chat_histories[chat_id].append({"role": "assistant", "content": response_text})

    # --- THINKING SPLITTER LOGIC (WITH AUTOMATIC CHUNKING) ---
    start_tag = "<" + "think" + ">"
    end_tag = "</" + "think" + ">"
    pattern = re.escape(start_tag) + r"(.*?)" + re.escape(end_tag)
    think_match = re.search(pattern, response_text, re.DOTALL | re.IGNORECASE)

    clean_response = response_text
    thinking_content = ""

    if think_match:
        thinking_content = think_match.group(1).strip()
        # Clean the main response text
        clean_response = re.sub(pattern, '', response_text, flags=re.DOTALL | re.IGNORECASE).strip()

    # --- SILENT HANDSHAKE DETECTION ---
    handshake_check = re.sub(r'[^a-zA-Z]', '', clean_response).upper()
    
    if handshake_check == "DONE":
        logging.info("🤫 HANDSHAKE: Suppressing final text message because camera photo was already delivered.")
        return  # Silent exit! Prevents duplicate texts

    # Display the thinking block if one exists
    if think_match and thinking_content:
        CHUNK_SIZE = 3900
        chunks = [thinking_content[i:i+CHUNK_SIZE] for i in range(0, len(thinking_content), CHUNK_SIZE)]

        for idx, chunk in enumerate(chunks):
            if len(chunks) > 1:
                header = f"🧠 <b>Emery's Thought Process (Part {idx+1}/{len(chunks)})</b> (Expand to read):\n"
            else:
                header = f"🧠 <b>Emery's Thought Process</b> (Expand to read):\n"

            thinking_msg = f"{header}<blockquote expandable><i>{chunk}</i></blockquote>"
            await update.message.reply_text(thinking_msg, parse_mode="HTML")
    # ---------------------------------------------------------

    # --- SINGLE FINAL REPLY DISPATCHER ---
    if is_input_voice and not voice_sent_via_tool:
        await update.message.reply_chat_action("record_voice")
        v_out = await get_voice_audio(clean_response)
        if v_out:
            await update.message.reply_voice(voice=v_out, caption="Voice message")
        else:
            await send_safe_large_message(update, emery_format(clean_response))
    else:
        if clean_response:
            await send_safe_large_message(update, emery_format(clean_response))

# --- HELPER: SAFE LONG MESSAGE SENDER ---
async def send_safe_large_message(update: Update, text: str):
    """
    Splits extremely long final responses at natural line breaks 
    to prevent Telegram's 4096 character limit crash.
    """
    MAX_LIMIT = 4000
    if len(text) <= MAX_LIMIT:
        await update.message.reply_text(text, parse_mode="HTML")
        return

    # Loop and send chunks safely
    while len(text) > 0:
        if len(text) <= MAX_LIMIT:
            await update.message.reply_text(text, parse_mode="HTML")
            break
            
        # Try to break at a natural newline rather than cutting mid-sentence
        split_index = text.rfind('\n', 0, MAX_LIMIT)
        if split_index == -1 or split_index < 3000:
            split_index = MAX_LIMIT  # Fallback to hard cut if no clean newline exists
            
        chunk = text[:split_index]
        await update.message.reply_text(chunk, parse_mode="HTML")
        text = text[split_index:].strip()
# --- AUTOMATED JOBS ---

# --- JOB TOOL ---
async def run_brief(c, prompt, label):
    global TARGET_CHAT_ID
    if not TARGET_CHAT_ID: return
    logging.info(f"📅 JOB: {label}")
    res_text, _= await emery_engine(deque([{"role": "user", "content": prompt}]))
    await c.bot.send_message(TARGET_CHAT_ID, f"🛡️ <b>EMERYCHAT JOB: {label}</b>\n\n{emery_format(res_text)}", parse_mode="HTML")

# --- SCHEDULED JOBS ---
async def job_morning_briefing(c): await run_brief(c, "Morning news intel from get_news_headlines. List all of the stories first, and hone in on the most important one at the end with a deep dive using web_search and fetch_web_content (if needed). Put all of it in a voice memo, and then also put everything in your text response. Do ***NOT*** include any sports news, and assess bias of any sources and inform the user with a quick qualifier, such as 'Left leaning' or 'Right leaning'.", "Morning Briefing")
async def job_morning_weather(c): await run_brief(c, "Look up weather with the get_NOAA_weather tool and give clothing recommendations while keeping in mind the User Bio.", "Today's Weather")
async def job_nasa(c): await run_brief(c, "Use get_nasa_apod. Provide title, explanation, and MUST provide image URL link.", "Today In Space")
async def job_calendar(c): await run_brief(c, "Check User's calendar with get_calendar_events for any events the User has today and list them chronologically.", "Daily Planner")
async def job_today_in_history(c): await run_brief(c, "Use get_today_in_history. Provide the returned items in a presentable list, then focus on one of the people and do research with web_search and fetch_web_content (if needed) and give a small report on them at the end of your response.", "Today In History")

# --- GLOBAL TELEGRAM ERROR HANDLER ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Logs network drops and timeouts cleanly instead of crashing the thread."""
    if isinstance(context.error, TimedOut):
        logging.warning("⚠️ Telegram API timed out temporarily due to high CPU load. The message will retry.")
    else:
        logging.error(f"⚠️ Telegram API Exception: {context.error}", exc_info=True)

if __name__ == '__main__':
    t_request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    
    application = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .request(t_request)
        .post_init(start_reolink_polling)  # Registers active-polling startup
        .build()
    )
    application_bot = application.bot
    
    application.add_error_handler(error_handler)
    
    # Schedule the jobs
    application.job_queue.run_daily(job_morning_briefing, time=time(3, 0, tzinfo=USER_TIMEZONE))
    application.job_queue.run_daily(job_morning_weather, time=time(3, 5, tzinfo=USER_TIMEZONE))
    application.job_queue.run_daily(job_calendar, time=time(3, 10, tzinfo=USER_TIMEZONE))
    application.job_queue.run_daily(job_nasa, time=time(21, 0, tzinfo=USER_TIMEZONE))
    application.job_queue.run_daily(job_today_in_history, time=time(21, 5, tzinfo=USER_TIMEZONE))
    
    application.add_handler(CommandHandler("clear", lambda u, c: chat_histories.get(u.effective_chat.id, deque()).clear() or u.message.reply_text("Context cleared.")))
    application.add_handler(CommandHandler("wipe", handle_wipe_command))
    application.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.VOICE, handle_message))

    logging.info(f"🚀 EMERYCHAT ONLINE — model: {MODEL_ID} | vision: {VISION_MODEL_ID}")
    application.run_polling()