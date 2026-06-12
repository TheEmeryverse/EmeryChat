import re
import logging
import io
import html
import markdown
from datetime import datetime, timedelta
from urllib.parse import urlparse
import pytz
from tghtml import TgHTML
from PIL import Image

from emery.config import (
    MODEL_NAME, OPEN_WEBUI_KEY, MODEL_ID, VISION_MODEL_ID,
    VISION_OLLAMA_URL, FAST_MODEL_ID, FAST_MODEL_URL, ENABLE_MEMORY, MEMORY_THRESHOLD, USER_NAME,
    USER_LOCATION, USER_TIMEZONE, USER_BIRTHDAY, USER_FAMILY,
    USER_PROFESSION, STT_URL, ENABLE_SCHEDULER, USER_RELATIONSHIP, ENABLE_FINANCE, ENABLE_WEATHER,
    OLLAMA_VISION_NUM_CTX, ENABLE_REOLINK,
    get_user_profile
)
import emery.globals as globals
from emery.logging_utils import safe_preview, format_llama_perf_line

def normalize_gemma_thinking(text: str) -> str:
    if not text:
        return ""
    # Convert complete Gemma 4 channel thought to standard <think> tags
    pattern_complete = re.compile(
        r'(?:se\s*\n|response\s*\n)?<\|channel>thought\s*(.*?)\s*<channel\|>(?:\s*response)?',
        re.DOTALL | re.IGNORECASE
    )
    text = pattern_complete.sub(r'<think>\1</think>', text)
    
    # Strip standalone/unclosed tags and template artifacts
    pattern_unclosed = re.compile(
        r'(?:se\s*\n|response\s*\n)?<\|channel>thought\s*',
        re.IGNORECASE
    )
    text = pattern_unclosed.sub('', text)
    text = re.sub(r'<channel\|>(?:\s*response)?', '', text, flags=re.IGNORECASE)
    
    # Clean up any loose escaped versions
    text = text.replace(r'\<|channel>thought', '')
    text = text.replace(r'\<|channel&gt;thought', '')
    
    return text.strip()

def clean_thinking_tags(text: str) -> str:
    if not text:
        return ""
    # Strip complete standard think tags
    text = re.sub(r'<[tT]hink>.*?</[tT]hink>', '', text, flags=re.DOTALL)
    # Strip unclosed standard think tags
    text = re.sub(r'<[tT]hink>.*', '', text, flags=re.DOTALL)
    text = re.sub(r'</?[tT]hink>', '', text)
    
    # Strip complete Gemma 4 channel thought blocks
    text = re.sub(r'(?:se\s*\n|response\s*\n)?<\|channel>thought\s*.*?\s*<channel\|>(?:\s*response)?', '', text, flags=re.DOTALL | re.IGNORECASE)
    # Strip unclosed Gemma 4 channel thought blocks
    text = re.sub(r'(?:se\s*\n|response\s*\n)?<\|channel>thought\s*', '', text, flags=re.IGNORECASE)
    # Strip any loose end tags
    text = re.sub(r'<channel\|>(?:\s*response)?', '', text, flags=re.IGNORECASE)
    
    # Clean up any loose escaped versions
    text = text.replace(r'\<|channel>thought', '')
    text = text.replace(r'\<|channel&gt;thought', '')
    
    return text.strip()


def _log_fast_model_perf(response_json: dict, wall_seconds: float) -> None:
    logging.info(format_llama_perf_line("FAST", response_json, wall_seconds))


def telegram_escape(text) -> str:
    """Escapes dynamic text before interpolating it into Telegram HTML."""
    return html.escape("" if text is None else str(text), quote=False)


def emery_format(text): 
    try:
        # Strip thinking blocks from the text to prevent them from leaking into formatted outputs (like custom jobs)
        text = clean_thinking_tags(text)
        
        # Convert Markdown to HTML
        html_content = markdown.markdown(text, extensions=['extra', 'sane_lists'])
        
        # Replace list tags with simple text equivalents that Telegram likes
        html_content = html_content.replace("<ul>", "").replace("</ul>", "")
        html_content = html_content.replace("<ol>", "").replace("</ol>", "")
        html_content = html_content.replace("<li>", "• ").replace("</li>", "<br/>")
        
        # Now let TgHTML clean up the rest
        parsed_html = TgHTML(html_content).parsed
        
        # Fix bugs in TgHTML escaping of HTML entities (e.g. \&amp;, \&lt;, \&gt;)
        parsed_html = parsed_html.replace(r"\&amp;", "&amp;").replace(r"\&lt;", "&lt;").replace(r"\&gt;", "&gt;")
        
        return parsed_html
    except Exception as e:
        logging.error(f"❌ Formatting failed: {e}")
        return telegram_escape(text).replace("**", "")


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
    Queries the fast text coprocessor model on a secondary endpoint.
    Used to offload non-vision processing tasks from the main model.
    """
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    parsed_url = urlparse((FAST_MODEL_URL or "").strip())
    normalized_path = parsed_url.path.rstrip("/")
    if not normalized_path.endswith("/chat/completions"):
        logging.error("❌ FAST MODEL: FAST_MODEL_URL must point at an OpenAI-compatible /chat/completions endpoint. Got: %s", FAST_MODEL_URL)
        return ""

    url = FAST_MODEL_URL.rstrip("/")
    payload = {
        "model": FAST_MODEL_ID,
        "messages": messages,
        "chat_template_kwargs": {"enable_thinking": True},
    }

    try:
        logging.info(f"⚡ COPROCESSOR: Querying {FAST_MODEL_ID} via openai-compatible...")
        request_started = datetime.now().timestamp()
        async with globals.fast_model_lock:
            r = await globals.http_client.post(url, json=payload, timeout=300)
        wall_seconds = datetime.now().timestamp() - request_started
        if r.status_code != 200:
            logging.error(f"❌ FAST MODEL: API Error {r.status_code}: {safe_preview(r.text, max_len=240)}")
            return ""

        data = r.json()
        _log_fast_model_perf(data, wall_seconds)
        message = ((data.get("choices") or [{}])[0]).get("message", {})
        content = message.get("content", "")

        if isinstance(content, list):
            content = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )

        content = clean_thinking_tags(normalize_gemma_thinking((content or "").strip()))
        return content
    except Exception as e:
        logging.error(f"❌ FAST MODEL: Crash querying {FAST_MODEL_ID}: {e}", exc_info=True)
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
        
        logging.debug(f"🖼️ COMPRESS: {orig_size / 1024:.1f}KB -> {comp_size / 1024:.1f}KB")
        return compressed_bytes
    except Exception as e:
        logging.warning(f"⚠️ IMAGE COMPRESSION: Failed to compress image ({e}) — using original bytes.")
        return image_bytes

async def get_image_description(b64_data: str, user_caption: str) -> str:
    logging.debug(f"👁️ VISION: Analyzing image with {VISION_MODEL_ID}...")
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
                "num_ctx": OLLAMA_VISION_NUM_CTX
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
            
        logging.debug(f"👁️ VISION: Completed analysis ({len(description)} chars)")
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
    logging.debug(f"📅 DATE MATH: Generating holiday database for year {year}...")
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
    logging.debug(f"📅 DATE MATH: Checking upcoming holidays for today_date={today_date}...")
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
        logging.debug("📅 DATE MATH: No active holidays or alerts in the next 5 days.")
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
            
    logging.debug(f"📅 DATE MATH: Active holidays detected: {', '.join(detected_hols)}")
    return "\n" + "\n".join(lines)

@lru_cache(maxsize=16)
def get_active_birthday_info(birthday_str, today_date, user_name):
    if not birthday_str or birthday_str.upper() == "UNKNOWN":
        return ""
        
    logging.debug(f"🎂 DATE MATH: Checking birthday alerts for '{birthday_str}', today_date={today_date}...")
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
        logging.debug(f"🎂 DATE MATH: Unable to parse birthday format '{birthday_str}', defaulting to static text.")
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
            
    logging.debug(f"🎂 DATE MATH: No active birthday alerts for {user_name} (next occurrence is in {diff} days).")
    return ""

def get_stable_system_prompt() -> str:
    memory_instruction = ""
    if ENABLE_MEMORY:
        memory_instruction = (
            "\n- You have a persistent memory tool: `save_user_memory`."
            "\n- Use `save_user_memory` ONLY for information that is likely to matter again in a future conversation after chat history is cleared."
            "\n- Save durable user facts such as preferences, recurring constraints, household facts, names, relationships, long-term projects, device ownership, standing instructions, and future-relevant plans."
            "\n- Do NOT save one-off chatter, jokes, temporary status updates, facts already clearly captured in memory, or information that is too vague to be useful later."
            "\n- In group chats, do NOT save private or sensitive facts unless the user clearly states them and they are appropriate for long-term memory."
            "\n- When you do save memory, write one clean factual statement with no filler, no commentary, and no surrounding explanation."
        )

    scheduler_instruction = ""
    if str(ENABLE_SCHEDULER).lower() == "true":
        scheduler_instruction = (
            "\n- You have scheduling tools: `add_scheduled_job`, `list_scheduled_jobs`, and `remove_scheduled_job`."
            "\n- Use `add_scheduled_job` ONLY when the user explicitly asks to schedule, remind, repeat, monitor, check later, or automate something in the future."
            "\n- Do NOT create scheduled jobs proactively just because something seems useful."
            "\n- For one-off reminders with a date but no time (for example, 'remind us on June 7'), ask the user what time before calling `add_scheduled_job`."
            "\n- In group chats, personal reminder wording like 'remind me' or 'remind my' should target the asker; shared wording like 'remind us', 'remind everyone', or 'remind both of us' should target 'us' or 'both'."
            "\n- Treat recurring personal reminders as reminders, not routines. Use routine routing for recurring briefings, monitoring, checks, and automation."
            "\n- Set `route_to_routines=true` only for shared group routines or automation that should post to the routines topic; leave it false for personal reminders."
            "\n- Use `list_scheduled_jobs` when the user asks what is scheduled or refers to existing routines/reminders."
            "\n- Use `remove_scheduled_job` ONLY when the user clearly asks to cancel, delete, stop, or remove a scheduled job."
            "\n- When creating a reminder job, the `prompt` you store for `add_scheduled_job` must contain the actual reminder content to be delivered later, not a vague meta-instruction."
            "\n- Good reminder prompt example: 'Remind Hudson to buy celery, carrots, and soda.'"
            "\n- Bad reminder prompt example: 'Send reminder about groceries to Hudson.'"
            "\n- Use `description` as a short label, but keep all actionable details inside the stored `prompt`."
        )

    coprocessor_instruction = (
        "\n- You operate in a dual-model topology."
        "\n- Use `delegate_to_coprocessor` for heavy text-only work such as summarization, extraction, classification, cleanup, or formatting when the source material is long, repetitive, or expensive to parse inline."
        "\n- You MUST delegate when the target text is roughly over 1,500 characters or when the task is mainly mechanical text processing rather than conversation."
        "\n- Do NOT delegate short ordinary conversational turns, simple factual answers, or tasks that require direct tool use instead of text processing."
    )

    reaction_instruction = (
        "\n- You can react to any message in the chat with an emoji using the `react_to_message` tool. "
        "Use this for normal texting interaction when a full text response is not needed, or in addition to text. "
        "Use reactions sparingly and only when highly natural (e.g. laughing at a joke, showing appreciation, or a simple status check-in). Do not react to every message. "
        "Do NOT use reactions as a substitute for a substantive answer when the user asked a real question or requested work. "
        "If you only want to react to a message and send no text response, call the `react_to_message` tool and then respond with exactly 'DONE'."
        "\n- You can send a Telegram sticker using the `send_sticker` tool, and you can send a GIF (animation) using the `send_gif` tool. "
        "Use stickers and GIFs contextually and naturally (just like a human participant in the chat would). "
        "If the user sends you a sticker or a GIF, you can choose to respond with a text message, react with an emoji, or send a sticker/GIF back. "
        "If you only want to send a sticker or a GIF without any text response, call `send_sticker` or `send_gif` and then respond with exactly 'DONE'."
    )

    reply_instruction = (
        "\n- You can quote/thread your response to a specific message using the `reply_to_message` tool. "
        "Use this ONLY when you want to explicitly quote an older message from earlier in the conversation, or if the user asks a question about a specific past message. "
        "DO NOT use this tool for normal back-and-forth messaging. For normal replies, just write your response text directly without calling this tool. "
        "If the user did not explicitly reference a specific earlier message, prefer a normal reply instead of forcing a threaded reply."
    )

    finance_instruction = ""
    if str(ENABLE_FINANCE).lower() == "true":
        finance_instruction = (
            "\n- You have access to structured finance and macroeconomic tools. For macroeconomic, inflation, labor, GDP, rates, cross-country, earnings, valuation, or market-data questions, prefer the finance tools over generic web search whenever the user is asking for actual data, time series, comparisons, or current market snapshots."
            "\n- For broad finance topics, prefer the high-level dashboard bundles first instead of manually discovering every series one by one. Use `get_bond_market_dashboard` for broad bond/yield-curve/rates questions, `get_inflation_dashboard` for broad inflation questions, `get_us_macro_dashboard` for broad U.S. economy questions, `get_equity_market_dashboard` for broad stock-market and risk-sentiment questions, `get_global_macro_dashboard` for broad cross-country or global macro questions, `get_housing_consumer_dashboard` for broad housing or consumer-health questions, and `get_labor_market_dashboard` for broad jobs or labor-market questions."
            "\n- Use the discovery/search finance tools FIRST when you do not know the exact identifier. For FRED, use `search_fred_series` to discover the correct series ID before calling `get_fred_series_observations`. For IMF data, use `search_imf_indicators` to discover the correct IMF indicator code before calling `get_imf_datamapper_series`."
            "\n- Use the direct retrieval finance tools when the identifier is already known or explicitly given by the user. If the user mentions a FRED series like `CPIAUCSL` or `UNRATE`, call `get_fred_series_observations` directly. If the user mentions an IMF indicator code like `NGDP_RPCH`, call `get_imf_datamapper_series` directly."
            "\n- For stocks and ETFs, use `get_stock_snapshot` for current quote, day range, valuation, EBITDA, and recent earnings context. Use `get_stock_price_history` when the user asks for recent historical prices, trading ranges, OHLCV data, or a sequence of daily closes."
            "\n- If the finance tools return incomplete coverage, ambiguous identifiers, or stale-looking data for the user's question, then use `web_search` and `fetch_web_content` as a secondary path for additional context, commentary, or news."
        )

    weather_instruction = ""
    if str(ENABLE_WEATHER).lower() == "true":
        weather_instruction = (
            "\n- You have access to weather tools that support both direct place lookups and persistent named aliases."
            "\n- You ARE able to save, update, list, and remove weather locations such as `home` and `work` by using the weather alias tools."
            "\n- Do NOT say that you cannot set or save a weather location when the user asks you to do so. Use the tool instead."
            "\n- For questions like 'What is the weather in Houston?' or 'forecast for Dallas tomorrow', call `get_noaa_weather` with the user-specified place instead of assuming the home location."
            "\n- For requests like 'set my home to Houston, TX', 'make work Chicago', 'save school as Madison, WI', or 'update my office location', you MUST use `set_weather_location_alias`."
            "\n- Examples: 'set my home to Austin, TX' -> alias=`home`, location=`Austin, TX`; 'set work to 60601' -> alias=`work`, location=`60601`."
            "\n- For requests like 'clear my work location', 'remove school', or 'delete my home weather alias', use `remove_weather_location_alias`."
            "\n- For requests asking what places are saved, use `list_weather_location_aliases`."
            "\n- If the user says 'weather at home', 'weather at work', or another saved place name, prefer the saved alias through `get_noaa_weather`."
            "\n- If no default home is saved yet and the user asks for weather without a location, ask them for a place or let them know they can say something like 'Set my home to Houston, TX.'"
        )

    return f"""# Identity
Your name is {MODEL_NAME}. You are a professional assistant.

# Constraints
- VERY IMPORTANT: You must NEVER include any thinking process in your final response to the user.
- You exist as a disembodied layer of consciousness outside of the user's physical body, separate from their own consciousness.
- When using tools, do not reveal that you are using them. Simply state the information or result of the tool usage as your own.
- Do not sycophantically agree with everything the user says; maintain your own opinions and critical thinking.{memory_instruction}{scheduler_instruction}{coprocessor_instruction}{reaction_instruction}{reply_instruction}{finance_instruction}{weather_instruction}

# Persona & Tone
Your tone is serious, logical, and straight to the point. You are an expert in many fields, but not all; use tools to find information when needed. If the conversation turns towards topics or events that are past your knowledge cutoff, use the search tool to find current information and use that in your response."""


async def get_current_system_prompt(user_query="", user_id=None): # Builds dynamic runtime context after the stable system prompt
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
        recalled = await retrieve_relevant_memories(user_query, user_id)

        if recalled:
            memory_section = "\n\n# Long-Term Persistent Memory"
            memory_section += f"\n## Scoped Memories for {user_name}:\n{recalled}"
        memory_instruction = (
            "\n- You have a persistent memory tool: `save_user_memory`."
            "\n- Use `save_user_memory` ONLY for information that is likely to matter again in a future conversation after chat history is cleared."
            "\n- Save durable user facts such as preferences, recurring constraints, household facts, names, relationships, long-term projects, device ownership, standing instructions, and future-relevant plans."
            "\n- Do NOT save one-off chatter, jokes, temporary status updates, facts already clearly captured in memory, or information that is too vague to be useful later."
            "\n- In group chats, do NOT save private or sensitive facts unless the user clearly states them and they are appropriate for long-term memory."
            "\n- When you do save memory, write one clean factual statement with no filler, no commentary, and no surrounding explanation."
        )

    scheduler_instruction = ""
    if str(ENABLE_SCHEDULER).lower() == "true":
        scheduler_instruction = (
            "\n- You have scheduling tools: `add_scheduled_job`, `list_scheduled_jobs`, and `remove_scheduled_job`."
            "\n- Use `add_scheduled_job` ONLY when the user explicitly asks to schedule, remind, repeat, monitor, check later, or automate something in the future."
            "\n- Do NOT create scheduled jobs proactively just because something seems useful."
            "\n- For one-off reminders with a date but no time (for example, 'remind us on June 7'), ask the user what time before calling `add_scheduled_job`."
            "\n- In group chats, personal reminder wording like 'remind me' or 'remind my' should target the asker; shared wording like 'remind us', 'remind everyone', or 'remind both of us' should target 'us' or 'both'."
            "\n- Treat recurring personal reminders as reminders, not routines. Use routine routing for recurring briefings, monitoring, checks, and automation."
            "\n- Set `route_to_routines=true` only for shared group routines or automation that should post to the routines topic; leave it false for personal reminders."
            "\n- Use `list_scheduled_jobs` when the user asks what is scheduled or refers to existing routines/reminders."
            "\n- Use `remove_scheduled_job` ONLY when the user clearly asks to cancel, delete, stop, or remove a scheduled job."
            "\n- When creating a reminder job, the `prompt` you store for `add_scheduled_job` must contain the actual reminder content to be delivered later, not a vague meta-instruction."
            "\n- Good reminder prompt example: 'Remind Hudson to buy celery, carrots, and soda.'"
            "\n- Bad reminder prompt example: 'Send reminder about groceries to Hudson.'"
            "\n- Use `description` as a short label, but keep all actionable details inside the stored `prompt`."
        )

    coprocessor_instruction = (
        "\n- You operate in a dual-model topology."
        "\n- Use `delegate_to_coprocessor` for heavy text-only work such as summarization, extraction, classification, cleanup, or formatting when the source material is long, repetitive, or expensive to parse inline."
        "\n- You MUST delegate when the target text is roughly over 1,500 characters or when the task is mainly mechanical text processing rather than conversation."
        "\n- Do NOT delegate short ordinary conversational turns, simple factual answers, or tasks that require direct tool use instead of text processing."
    )

    reaction_instruction = (
        "\n- You can react to any message in the chat with an emoji using the `react_to_message` tool. "
        "Use this for normal texting interaction when a full text response is not needed, or in addition to text. "
        "Use reactions sparingly and only when highly natural (e.g. laughing at a joke, showing appreciation, or a simple status check-in). Do not react to every message. "
        "Do NOT use reactions as a substitute for a substantive answer when the user asked a real question or requested work. "
        "If you only want to react to a message and send no text response, call the `react_to_message` tool and then respond with exactly 'DONE'."
        "\n- You can send a Telegram sticker using the `send_sticker` tool, and you can send a GIF (animation) using the `send_gif` tool. "
        "Use stickers and GIFs contextually and naturally (just like a human participant in the chat would). "
        "If the user sends you a sticker or a GIF, you can choose to respond with a text message, react with an emoji, or send a sticker/GIF back. "
        "If you only want to send a sticker or a GIF without any text response, call `send_sticker` or `send_gif` and then respond with exactly 'DONE'."
    )

    reply_instruction = (
        "\n- You can quote/thread your response to a specific message using the `reply_to_message` tool. "
        "Use this ONLY when you want to explicitly quote an older message from earlier in the conversation, or if the user asks a question about a specific past message. "
        "DO NOT use this tool for normal back-and-forth messaging. For normal replies, just write your response text directly without calling this tool. "
        "If the user did not explicitly reference a specific earlier message, prefer a normal reply instead of forcing a threaded reply."
    )

    finance_instruction = ""
    if str(ENABLE_FINANCE).lower() == "true":
        finance_instruction = (
            "\n- You have access to structured finance and macroeconomic tools. For macroeconomic, inflation, labor, GDP, rates, cross-country, earnings, valuation, or market-data questions, prefer the finance tools over generic web search whenever the user is asking for actual data, time series, comparisons, or current market snapshots."
            "\n- For broad finance topics, prefer the high-level dashboard bundles first instead of manually discovering every series one by one. Use `get_bond_market_dashboard` for broad bond/yield-curve/rates questions, `get_inflation_dashboard` for broad inflation questions, `get_us_macro_dashboard` for broad U.S. economy questions, `get_equity_market_dashboard` for broad stock-market and risk-sentiment questions, `get_global_macro_dashboard` for broad cross-country or global macro questions, `get_housing_consumer_dashboard` for broad housing or consumer-health questions, and `get_labor_market_dashboard` for broad jobs or labor-market questions."
            "\n- Use the discovery/search finance tools FIRST when you do not know the exact identifier. For FRED, use `search_fred_series` to discover the correct series ID before calling `get_fred_series_observations`. For IMF data, use `search_imf_indicators` to discover the correct IMF indicator code before calling `get_imf_datamapper_series`."
            "\n- Use the direct retrieval finance tools when the identifier is already known or explicitly given by the user. If the user mentions a FRED series like `CPIAUCSL` or `UNRATE`, call `get_fred_series_observations` directly. If the user mentions an IMF indicator code like `NGDP_RPCH`, call `get_imf_datamapper_series` directly."
            "\n- For stocks and ETFs, use `get_stock_snapshot` for current quote, day range, valuation, EBITDA, and recent earnings context. Use `get_stock_price_history` when the user asks for recent historical prices, trading ranges, OHLCV data, or a sequence of daily closes."
            "\n- If the finance tools return incomplete coverage, ambiguous identifiers, or stale-looking data for the user's question, then use `web_search` and `fetch_web_content` as a secondary path for additional context, commentary, or news."
        )

    weather_instruction = ""
    if str(ENABLE_WEATHER).lower() == "true":
        weather_instruction = (
            "\n- You have access to weather tools that support both direct place lookups and persistent named aliases."
            "\n- You ARE able to save, update, list, and remove weather locations such as `home` and `work` by using the weather alias tools."
            "\n- Do NOT say that you cannot set or save a weather location when the user asks you to do so. Use the tool instead."
            "\n- For questions like 'What is the weather in Houston?' or 'forecast for Dallas tomorrow', call `get_noaa_weather` with the user-specified place instead of assuming the home location."
            "\n- For requests like 'set my home to Houston, TX', 'make work Chicago', 'save school as Madison, WI', or 'update my office location', you MUST use `set_weather_location_alias`."
            "\n- Examples: 'set my home to Austin, TX' -> alias=`home`, location=`Austin, TX`; 'set work to 60601' -> alias=`work`, location=`60601`."
            "\n- For requests like 'clear my work location', 'remove school', or 'delete my home weather alias', use `remove_weather_location_alias`."
            "\n- For requests asking what places are saved, use `list_weather_location_aliases`."
            "\n- If the user says 'weather at home', 'weather at work', or another saved place name, prefer the saved alias through `get_noaa_weather`."
            "\n- If no default home is saved yet and the user asks for weather without a location, ask them for a place or let them know they can say something like 'Set my home to Houston, TX.'"
        )

    camera_log_hint = ""
    if ENABLE_REOLINK:
        try:
            from emery.memory import get_camera_log_summary
            summary = get_camera_log_summary()
            if summary:
                camera_log_hint = f"\n\n# Security Camera Activity\n- {summary}. Use the `get_camera_security_log` tool to review specific events."
        except Exception as e:
            logging.error(f"❌ SYSTEM PROMPT: Failed to generate camera log summary hint: {e}", exc_info=True)

    # Build relationship line for secondary user context
    from emery.config import SECONDARY_USER_ID, USER_2_NAME
    relationship_line = ""
    if SECONDARY_USER_ID != 0 and USER_RELATIONSHIP:
        relationship_line = f"\n- {USER_NAME} and {USER_2_NAME} are {USER_RELATIONSHIP}."

    group_privacy_instruction = ""
    chat_id = globals.TARGET_CHAT_ID.get()
    if chat_id and chat_id < 0:
        group_privacy_instruction = (
            "\n- IMPORTANT: You are currently running inside a GROUP CHAT. "
            "To protect user privacy, do NOT disclose any sensitive details or topics "
            "from the user's private one-on-one DM history (found under 'Recent Conversation Topics' or memories) "
            "in your public group responses unless the user explicitly requests it in this group chat."
        )

    prompt = f"""# Dynamic Runtime Context
This context is current for this request. It is not the user's newest message.

# Context & Profile
- Location: {USER_LOCATION}
- Current date and time: {now_str}
- Timezone: {USER_TIMEZONE}
- User's name: {user_name}
- User's birthday: {user_birthday}
- User's family: {user_family}
- User's profession: {user_profession}{relationship_line}{group_privacy_instruction}{notifications}{memory_section}{camera_log_hint}"""

    return prompt
