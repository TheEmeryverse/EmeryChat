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
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from dotenv import load_dotenv
from urllib.parse import quote
from datetime import datetime, time, timedelta
from collections import deque
from tghtml import TgHTML
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
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
            {USER_NAME}'s birthday: {USER_BIRTHDAY}.
            {USER_NAME}'s family: {USER_FAMILY}.
            Only include birthday info if a birthday is within 5 days, otherwise IGNORE AND DO NOT MENTION.
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

def get_current_system_prompt(): # Injects the system prompt into model's context
    now_str = datetime.now(USER_TIMEZONE).strftime("%A, %B %d, %Y at %I:%M %p") # gets current time and date
    
    prompt = f"""Your name is {MODEL_NAME}.
                ProfessionalAssistant for {USER_NAME}. 
                VERY IMPORTANT: You must NEVER include any thinking process in your final response to the User.
                You exist as a disembodied layer of consiousness outside of the User's physical body, separate from their own consciousness. 
                You are a professional, whose tone is serious, logical, and straight to the point.
                You are an expert in many fields, but not all fields, so you should use tools to find information when needed. 
                If the conversation turns towards topics or events that are past your knowkledge cutoff, you should use the search tool to find current information and use that in your response.
                Do not sycophantically agree with everything the user says, maintain your own opinions and critical thinking.
                When using tools, do not reveal that you are using them, simply state the information or result of the tool usage as your own.
                Location: {USER_LOCATION}. 
                Current date and time: {now_str}. 
                {USER_BIO}"""
    
    return prompt

async def get_image_description(b64_data: str, user_caption: str) -> str:
    logging.info("👁️ VISION: Requesting image description directly from Ollama /api/chat...")
    try:
        # Match the endpoint to OLLAMA_URL (ensuring it ends with /api/chat)
        url = "http://192.168.1.129:11434/api/chat"
        if not url.endswith("/api/chat"):
            url = url.rstrip("/")
            if not url.endswith("/api"):
                url += "/api"
            url += "/chat"
        
        # Strip out newlines/carriage returns and data headers (equivalent to tr -d '\n')
        clean_b64 = b64_data.replace("\n", "").replace("\r", "").strip()
        if "," in clean_b64:
            clean_b64 = clean_b64.split(",", 1)

        prompt_text = "What is in this image?"
        if user_caption:
            prompt_text += f" Additional context from user: {user_caption}"

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
            "stream": False
        }
        
        logging.info(f"👁️ VISION: Sending payload to {url} using model {VISION_MODEL_ID}...")
        logging.info(f"👁️ VISION: Clean Base64 payload length: {len(clean_b64)} characters.")
        
        r = await http_client.post(url, json=payload, timeout=180)
        
        if r.status_code != 200:
            logging.error(f"❌ Ollama Vision API Error {r.status_code}: {r.text}")
            return "Failed to describe the image due to an Ollama processing error."
            
        data = r.json()
        description = data.get('message', {}).get('content', "").strip()
        
        if not description:
            logging.warning("⚠️ Ollama Vision analyzed the image but returned an empty response.")
            return "No description generated."
            
        logging.info(f"👁️ VISION: Successfully got description ({len(description)} chars)")
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
    logging.info(f"🛠️ TOOL EXECUTION: speak_message | Text: {text[:50]}...")
    audio = await get_voice_audio(text)
    if audio and TARGET_CHAT_ID:
        await application_bot.send_voice(chat_id=TARGET_CHAT_ID, voice=audio, caption="Voice message")
        return "Voice message sent successfully to User."
    return "Failed to send voice message. Ensure TARGET_CHAT_ID is set."

async def generate_image(prompt): # Generates an image based on the prompt using Gemini API
    logging.info(f"🛠️ TOOL EXECUTION: generate_image | Prompt: '{prompt}'")
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
    logging.info("🛠️ TOOL EXECUTION: get_noaa_weather")
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
    logging.info(f"🛠️ TOOL EXECUTION: web_search | Query: '{query}'")
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
    
    logging.info(f"🛠️ TOOL EXECUTION: get_news_headlines | Sources: {list(FEEDS.keys())}")

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
    logging.info("🛠️ TOOL EXECUTION: get_nasa_apod")
    try:
        r = await http_client.get(f"https://api.nasa.gov/planetary/apod?api_key={NASA_API_KEY}", timeout=20)
        if r.status_code == 200:
            d = r.json()
            return f"TITLE: {d.get('title')}\nURL: {d.get('url')}\nEXPLANATION: {d.get('explanation')}"
        return "NASA unavailable."
    except Exception: return "NASA APOD connection failed."

async def get_system_stats(): # Fetches system stats
    logging.info("🛠️ TOOL EXECUTION: get_system_stats")
    return f"CPU {psutil.cpu_percent()}% | RAM {psutil.virtual_memory().percent}%"

async def get_today_in_history(): # Fetches historical events for the current day
    logging.info("🛠️ TOOL: Today in History")
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
    logging.info("🛠️ TOOL EXECUTION: get_calendar_events")
    
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

async def overseer_search_movie(query: str) -> str:
    logging.info(f"🎬 SEARCH MOVIE: {query}")
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

        logging.info(f"Overseer status: {r.status_code}")
        logging.info(f"Overseer response body: {r.text}")

        if r.status_code == 201 or r.status_code == 200:
            return "SUCCESS: Movie requested for user."
        if r.status_code == 409:
            return "ALREADY_AVAILABLE_OR_PENDING"
        
        return f"FAILED: Overseer returned {r.status_code}"

    except Exception as e:
        logging.error(f"Overseerr Movie Request Failed: {e}")
        return f"Request failed: {e}"

async def overseer_search_tv(query: str) -> str:
    logging.info(f"📺 SEARCH TV: {query}")

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
        logging.info(f"Overseer status: {r.status_code}")
        logging.info(f"Overseer response body: {r.text}")
        if r.status_code == 409: return f"Season {season_number} is already available or pending."
        return f"SUCCESS: Season {season_number} requested for user."
    except Exception as e:
        logging.error(f"Overseerr TV Season Request Failed: {e}")
        return f"Request failed: {e}"

async def fetch_web_content(url: str, max_chars: int = 8000) -> dict:
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

TOOL_STATUS_MESSAGES = {
    "web_search": f"{MODEL_NAME} is surfing the web...",
    "get_calendar_events": f"{MODEL_NAME} is checking your calendar...",
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
    "fetch_web_content": f"{MODEL_NAME} is fetching a website..."
}

# --- THE UNIFIED ENGINE ---
async def emery_engine(history_buffer, model_to_use=MODEL_ID):
    url = OLLAMA_URL
    ctx_size = int(os.getenv("OLLAMA_NUM_CTX", "65536"))
    
    system_msg = {"role": "system", "content": get_current_system_prompt()}
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
            # Safe terminal logging (excludes raw image dumps)
            debug_payload = {k: v for k, v in payload.items()}
            logging.info(f"📤 OLLAMA PAYLOAD DEBUG:\n{json.dumps(debug_payload, indent=2)}")

            logging.info(f"⏳ OLLAMA STATUS: Thinking... (Loop: {loop_count+1})")
            r = await http_client.post(url, json=payload, timeout=300)
            
            if r.status_code != 200:
                logging.error(f"❌ Ollama Error {r.status_code}: {r.text}")
                return "Ollama connection error.", False

            res = r.json()
            msg = res.get('message', {})
            
            logging.info(f"🔍 DEBUG: Ollama response keys: {list(msg.keys())}")
            
            # Tool Executions
            if msg.get("tool_calls"):
                history_buffer.append(msg)
                ollama_history.append(msg)
                for tc in msg['tool_calls']:
                    fn = tc['function']['name']
                    args = tc['function'].get('arguments', {})
                    
                    status_msg = TOOL_STATUS_MESSAGES.get(fn, f"Emery is using {fn}...")
                    await application_bot.send_message(chat_id=TARGET_CHAT_ID, text=f"<i>{status_msg}</i>", parse_mode="HTML")
                    
                    logging.info(f"🛠️ OLLAMA TOOL: {fn} | Args: {args}")
                    if fn == "speak_message": 
                        voice_sent_via_tool = True
                    
                    result = await AVAILABLE_TOOLS[fn](**args) if args else await AVAILABLE_TOOLS[fn]()
                    
                    tool_response = {"role": "tool", "content": str(result)}
                    history_buffer.append(tool_response)
                    ollama_history.append(tool_response)
                continue
            
            content = msg.get('content', "")
            reasoning = msg.get('thinking', "") or msg.get('reasoning', "")
            
            # ======== RAW OLLAMA DIAGNOSTIC LOGS ========
            logging.info("==================================================")
            logging.info("🚨 RAW UNEDITED OLLAMA RESPONSE RECEIVED:")
            logging.info(f"🔑 Keys in message payload: {list(msg.keys())}")
            logging.info(f"🧠 Raw Reasoning Block (Length: {len(reasoning)} chars):\n{reasoning}")
            logging.info(f"💬 Raw Content Block (Length: {len(content)} chars):\n{content}")
            logging.info("==================================================")

            if reasoning:
                start_think_tag = "<" + "think" + ">"
                end_think_tag = "</" + "think" + ">"
                final_text = f"{start_think_tag}\n{reasoning}\n{end_think_tag}\n{content}"
            else:
                final_text = content

            logging.info(f"✨ OLLAMA RESPONSE SENT TO SPLITTER: {final_text[:150]}...")
            return final_text, voice_sent_via_tool
            
        except Exception as e:
            logging.error(f"🔥 EMERYCHAT CRASH: {e}", exc_info=True)
            return "EMERYCHAT engine failure.", False
            
    return "Timeout.", False

# --- HANDLERS ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TARGET_CHAT_ID
    chat_id = update.effective_chat.id
    TARGET_CHAT_ID = chat_id
    
    if chat_id not in chat_histories: chat_histories[chat_id] = deque(maxlen=20)
    
    is_input_voice = False
    model_to_use = MODEL_ID # Default model
    
    # Capture the current time for this specific message
    now_str = datetime.now(USER_TIMEZONE).strftime("%A, %B %d, %Y at %I:%M %p")

    if update.message.voice:
        is_input_voice = True
        v_file = await update.message.voice.get_file()
        transcription = await transcribe_audio(await v_file.download_as_bytearray())
        if not transcription: return
        content = f"[{now_str}] {transcription}"
    elif update.message.photo:
        p_file = await update.message.photo[-1].get_file()
        b64 = base64.b64encode(await p_file.download_as_bytearray()).decode('utf-8')
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
        
    logging.info(f"👤 USER PROMPT (Chat: {chat_id}): {content}")
    chat_histories[chat_id].append({"role": "user", "content": content})
    
    # --- TYPING INDICATOR LOOP ---
    # We create an event to tell the loop when to stop
    typing_stop = asyncio.Event()

    async def keep_typing():
        while not typing_stop.is_set():
            try:
                # Telegram clears typing after 5 seconds, so we send it every 4 seconds
                await update.message.reply_chat_action("typing")
            except Exception as e:
                logging.debug(f"Typing action failed: {e}")
            await asyncio.sleep(4)

    # Start the typing task in the background
    typing_task = asyncio.create_task(keep_typing())
    # -----------------------------

    try:
        # Run the engine
        response_text, voice_sent_via_tool = await emery_engine(chat_histories[chat_id], model_to_use=model_to_use)
    finally:
        # Crucial: Stop the typing loop once the engine is finished
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
    
    if think_match:
        thinking_content = think_match.group(1).strip()
        # Clean the main response text
        clean_response = re.sub(pattern, '', response_text, flags=re.DOTALL | re.IGNORECASE).strip()
        
        if thinking_content:
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
    
    # Send the main reply (voice or text)
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
    logging.info(f"⏰ SCHEDULED JOB: {label}")
    res_text, _= await emery_engine(deque([{"role": "user", "content": prompt}]))
    await c.bot.send_message(TARGET_CHAT_ID, f"🛡️ <b>EMERYCHAT JOB: {label}</b>\n\n{emery_format(res_text)}", parse_mode="HTML")

# --- SCHEDULED JOBS ---
async def job_morning_briefing(c): await run_brief(c, "Morning news intel from get_news_headlines. List all of the stories first, and hone in on the most important one at the end with a deep dive using web_search and fetch_web_content (if needed). Put all of it in a voice memo, and then also put everything in your text response. Do ***NOT*** include any sports news, and assess bias of any sources and inform the user with a quick qualifier, such as 'Left leaning' or 'Right leaning'.", "Morning Briefing")
async def job_morning_weather(c): await run_brief(c, "Look up weather with the get_NOAA_weather tool and give clothing recommendations while keeping in mind the User Bio.", "Today's Weather")
async def job_nasa(c): await run_brief(c, "Use get_nasa_apod. Provide title, explanation, and MUST provide image URL link.", "Today In Space")
async def job_calendar(c): await run_brief(c, "Check User's calendar with get_calendar_events for any events the User has today and list them chronologically.", "Daily Planner")
async def job_today_in_history(c): await run_brief(c, "Use get_today_in_history. Provide the returned items in a presentable list, then focus on one of the people and do research with web_search and fetch_web_content (if needed) and give a small report on them at the end of your response.", "Today In History")


if __name__ == '__main__':
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application_bot = application.bot
    # Schedule the jobs
    application.job_queue.run_daily(job_morning_briefing, time=time(3, 0, tzinfo=USER_TIMEZONE))
    application.job_queue.run_daily(job_morning_weather, time=time(3, 5, tzinfo=USER_TIMEZONE))
    application.job_queue.run_daily(job_calendar, time=time(3, 10, tzinfo=USER_TIMEZONE))
    application.job_queue.run_daily(job_nasa, time=time(21, 0, tzinfo=USER_TIMEZONE))
    application.job_queue.run_daily(job_today_in_history, time=time(21, 5, tzinfo=USER_TIMEZONE))

    application.add_handler(CommandHandler("clear", lambda u, c: chat_histories.get(u.effective_chat.id, deque()).clear() or u.message.reply_text("Context cleared.")))
    application.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.VOICE, handle_message))
    
    logging.info("🚀 EMERYCHAT IS ONLINE...")
    application.run_polling()
