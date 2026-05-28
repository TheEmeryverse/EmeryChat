import os
import re
import logging
import io
import markdown
from datetime import datetime, timedelta
import pytz
from tghtml import TgHTML
from PIL import Image

from emery.config import (
    MODEL_NAME, OLLAMA_URL, OPEN_WEBUI_KEY, MODEL_ID, VISION_MODEL_ID,
    VISION_OLLAMA_URL, ENABLE_MEMORY, MEMORY_THRESHOLD, USER_NAME,
    USER_LOCATION, USER_TIMEZONE, USER_BIRTHDAY, USER_FAMILY,
    USER_PROFESSION, STT_URL, ENABLE_SCHEDULER,
    get_user_profile, get_memory_file_path
)
import emery.globals as globals

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
        r = await globals.http_client.post(STT_URL, headers={"Authorization": f"Bearer {OPEN_WEBUI_KEY}"}, files=files)
        return r.json().get('text', "")
    except Exception as e:
        logging.error(f"❌ STT Error: {e}"); return ""

async def query_fast_model(prompt: str, system_prompt: str = None) -> str:
    """
    Queries the fast, vision-capable coprocessor model (e.g. gemma4:e4b) on a secondary endpoint.
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
        logging.info(f"⚡ FAST MODEL: Querying {VISION_MODEL_ID} on secondary coprocessor...")
        async with globals.fast_model_lock:
            r = await globals.http_client.post(url, json=payload, timeout=300)
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

def compress_image_bytes(image_bytes: bytes, max_dim: int = 800, quality: int = 75) -> bytes:
    """Resizes and compresses image bytes to optimize payload size and vision model processing."""
    try:
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
        url = VISION_OLLAMA_URL
        if not url.endswith("/api/chat"):
            url = url.rstrip("/")
            if not url.endswith("/api"):
                url += "/api"
            url += "/chat"
        
        clean_b64 = b64_data.replace("\n", "").replace("\r", "").strip()
        if "," in clean_b64:
            clean_b64 = clean_b64.split(",", 1)[1]

        prompt_text = user_caption if user_caption else "What is in this image?"
        import os
        ctx_size = int(os.getenv("OLLAMA_VISION_NUM_CTX", "65536"))

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
        
        async with globals.fast_model_lock:
            r = await globals.http_client.post(url, json=payload, timeout=300)
        
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

from functools import lru_cache

@lru_cache(maxsize=16)
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

@lru_cache(maxsize=16)
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

@lru_cache(maxsize=16)
def get_active_birthday_info(birthday_str, today_date, user_name):
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
        return f"\n- {user_name}'s birthday: {birthday_str}."
        
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
            logging.info(f"🎂 DATE MATH: Birthday alert active today for {user_name}!")
            return f"\n- Today is {user_name}'s birthday!"
        else:
            logging.info(f"🎂 DATE MATH: Birthday alert active for {user_name} (in {diff} days on {day_str})")
            return f"\n- Upcoming event: {user_name}'s birthday is on {day_str} (in {diff} day{'s' if diff > 1 else ''})."
            
    logging.info(f"🎂 DATE MATH: No active birthday alerts for {user_name} (next occurrence is in {diff} days).")
    return ""

def get_current_system_prompt(user_query="", user_id=None): # Injects the system prompt into model's context
    if user_id is None:
        user_id = globals.current_user_id.get()
        
    profile = get_user_profile(user_id)
    user_name = profile["name"]
    user_birthday = profile["birthday"]
    user_profession = profile["profession"]
    user_family = profile["family"]

    now = datetime.now(USER_TIMEZONE)
    now_str = now.strftime("%A, %B %d, %Y at %I:%M %p")
    today_date = now.date()
    
    active_bday = get_active_birthday_info(user_birthday, today_date, user_name)
    active_hols = get_active_holiday_info(today_date)
    notifications = ""
    if active_bday or active_hols:
        notifications = f"\n\n# Dynamic Event Alerts{active_bday}{active_hols}"
        
    memory_section = ""
    memory_instruction = ""
    if ENABLE_MEMORY:
        # Resolve circular import locally
        from emery.memory import retrieve_relevant_memories
        recalled = retrieve_relevant_memories(user_query, user_id)
        
        # Also retrieve spouse's memories if a secondary user exists
        from emery.config import PRIMARY_USER_ID, SECONDARY_USER_ID
        spouse_recalled = ""
        spouse_name = ""
        if SECONDARY_USER_ID != 0:
            spouse_id = None
            if user_id == PRIMARY_USER_ID:
                spouse_id = SECONDARY_USER_ID
            elif user_id == SECONDARY_USER_ID:
                spouse_id = PRIMARY_USER_ID
                
            if spouse_id is not None:
                spouse_profile = get_user_profile(spouse_id)
                spouse_name = spouse_profile["name"]
                spouse_recalled = retrieve_relevant_memories(user_query, spouse_id)
                
        if recalled or spouse_recalled:
            memory_section = "\n\n# Long-Term Persistent Memory"
            if recalled:
                memory_section += f"\n## Memories of {user_name} (Active Conversational Partner):\n{recalled}"
            if spouse_recalled:
                memory_section += f"\n## Memories of {spouse_name} (Spouse):\n{spouse_recalled}"
        memory_instruction = "\n- If the user shares new details, preferences, schedules, family updates, or tech choices that you should remember across chat clear cycles, you MUST use the `save_user_memory` tool to store them."

    scheduler_instruction = ""
    if str(ENABLE_SCHEDULER).lower() == "true":
        scheduler_instruction = "\n- You have the ability to schedule automated background jobs/tasks for the user (like checking the weather daily, fetching news headlines, or setting repeating or one-time reminders/alerts) using the `add_scheduled_job`, `list_scheduled_jobs`, and `remove_scheduled_job` tools."

    coprocessor_instruction = (
        "\n- You operate in a dual-model topology. To keep system latency low and protect your context window, "
        "you MUST delegate heavy text parsing, table formatting, data extraction, and general summarization tasks "
        "to the coprocessor via the `delegate_to_coprocessor` tool, especially when the target content is long "
        "(exceeds 1,500 characters) or highly repetitive."
    )

    reaction_instruction = (
        "\n- You can react to any message in the chat with an emoji using the `react_to_message` tool. "
        "Use this for normal texting interaction when a full text response is not needed, or in addition to text. "
        "Use reactions sparingly and only when highly natural (e.g. laughing at a joke, showing appreciation, or a simple status check-in). Do not react to every message. "
        "If you only want to react to a message and send no text response, call the `react_to_message` tool and then respond with exactly 'DONE'."
        "\n- You can send a Telegram sticker using the `send_sticker` tool, and you can send a GIF (animation) using the `send_gif` tool. "
        "Use stickers and GIFs contextually and naturally (just like a human participant in the chat would). "
        "If the user sends you a sticker or a GIF, you can choose to respond with a text message, react with an emoji, or send a sticker/GIF back. "
        "If you only want to send a sticker or a GIF without any text response, call `send_sticker` or `send_gif` and then respond with exactly 'DONE'."
    )

    reply_instruction = (
        "\n- You can quote/thread your response to a specific message using the `reply_to_message` tool. "
        "Use this ONLY when you want to explicitly quote an older message from earlier in the conversation, or if the user asks a question about a specific past message. "
        "DO NOT use this tool for normal back-and-forth messaging. For normal replies, just write your response text directly without calling this tool."
    )

    camera_log_hint = ""
    if re.sub(r'[^a-zA-Z]', '', os.getenv("ENABLE_REOLINK", "false")).lower() == "true":
        try:
            from emery.memory import get_camera_log_summary
            summary = get_camera_log_summary()
            if summary:
                camera_log_hint = f"\n\n# Security Camera Activity\n- {summary}. Use the `get_camera_security_log` tool to review specific events."
        except Exception as e:
            logging.error(f"❌ SYSTEM PROMPT: Failed to generate camera log summary hint: {e}", exc_info=True)

    prompt = f"""# Identity
Your name is {MODEL_NAME}. You are a Professional Assistant for {user_name}.

# Constraints
- VERY IMPORTANT: You must NEVER include any thinking process in your final response to the User.
- You exist as a disembodied layer of consciousness outside of the User's physical body, separate from their own consciousness.
- When using tools, do not reveal that you are using them. Simply state the information or result of the tool usage as your own.
- Do not sycophantically agree with everything the user says; maintain your own opinions and critical thinking.{memory_instruction}{scheduler_instruction}{coprocessor_instruction}{reaction_instruction}{reply_instruction}

# Persona & Tone
Your tone is serious, logical, and straight to the point. You are an expert in many fields, but not all; use tools to find information when needed. If the conversation turns towards topics or events that are past your knowledge cutoff, use the search tool to find current information and use that in your response.

# Context & Profile
- Location: {USER_LOCATION}
- Current date and time: {now_str}
- Timezone: {USER_TIMEZONE}
- User's name: {user_name}
- User's birthday: {user_birthday}
- User's family: {user_family}
- User's profession: {user_profession}{notifications}{memory_section}{camera_log_hint}"""

    return prompt
