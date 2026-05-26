import os
import re
import logging
import asyncio
import base64
import subprocess
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, quote
from datetime import datetime, time, timedelta
import pytz
import feedparser
import psutil

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build

from emery.config import (
    MODEL_NAME, OLLAMA_URL, OPEN_WEBUI_KEY, MODEL_ID, VISION_MODEL_ID,
    VISION_OLLAMA_URL, SEARXNG_URL, NASA_API_KEY, GEMINI_API_KEY,
    IMAGE_MODEL, NOAA_LAT, NOAA_LONG, NOAA_EMAIL, raw_cal_string,
    calendar_ids, TELEGRAM_TOKEN, OVERSEER_URL, OVERSEER_KEY,
    OVERSEER_USER_ID, TTS_URL, TTS_VOICE, NEWS_FEEDS, USER_TIMEZONE, USER_NAME
)
import emery.globals as globals
from emery.helpers import compress_image_bytes, get_image_description

# --- VOICE / TTS TOOLS ---
async def get_voice_audio(text): # Sends model's voice memo text to Kokoro for TTS
    logging.info("🎙️ VOICE: Generating audio...")
    try:
        # Remove markdown characters so the TTS doesn't try to "read" them
        clean_text = re.sub(r'[*_`#]', '', text)
        payload = {"model": "kokoro", "input": clean_text, "voice": TTS_VOICE}
        r = await globals.http_client.post(TTS_URL, headers={"Authorization": f"Bearer {OPEN_WEBUI_KEY}"}, json=payload)
        process = subprocess.Popen(['ffmpeg', '-i', 'pipe:0', '-c:a', 'libopus', '-b:a', '32k', '-f', 'ogg', 'pipe:1'],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, _ = process.communicate(input=r.content)
        return out
    except Exception as e:
        logging.error(f"❌ TTS Error: {e}"); return None

async def speak_message(text): # What the model calls to create a voice message and send it to the user
    logging.info(f"🔧 TOOL: speak_message")
    audio = await get_voice_audio(text)
    if audio and globals.TARGET_CHAT_ID:
        await globals.application_bot.send_voice(chat_id=globals.TARGET_CHAT_ID, voice=audio, caption="Voice message")
        return "Voice message sent successfully to User."
    return "Failed to send voice message. Ensure TARGET_CHAT_ID is set."

# --- IMAGE GENERATION ---
async def generate_image(prompt): # Generates an image based on the prompt using Gemini API
    logging.info(f"🔧 TOOL: generate_image | {prompt[:80]}")
    URL = f"https://generativelanguage.googleapis.com/v1beta/models/{IMAGE_MODEL}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}]
    }
    try:
        r = await globals.http_client.post(URL, json=payload, timeout=60)
        if r.status_code != 200:
            logging.error(f"❌ API Error: {r.text}")
            return f"Error: {r.status_code}"
        data = r.json()
        parts = data.get('candidates', [{}])[0].get('content', {}).get('parts', [])
        image_bytes = None
        for part in parts:
            if 'inlineData' in part:
                image_b64 = part['inlineData'].get('data')
                image_bytes = base64.b64decode(image_b64)
                break
        if not image_bytes:
            return "No image data found in response parts."
        if globals.TARGET_CHAT_ID:
            await globals.application_bot.send_photo(
                chat_id=globals.TARGET_CHAT_ID,
                photo=image_bytes,
                caption=f"Here's your picture: {prompt[:100]}"
            )
            return "Image sent successfully."
        return "Chat context lost."
    except Exception as e:
        logging.error(f"❌ Image Tool Crash: {e}")
        return f"Error: {e}"

# --- NOAA WEATHER ---
async def get_noaa_weather(): # Fetches the forecast
    logging.info("🔧 TOOL: get_noaa_weather")
    headers = {'User-Agent': f'({MODEL_NAME}-bot, {NOAA_EMAIL})'}
    try:
        r1 = await globals.http_client.get(f"https://api.weather.gov/points/{NOAA_LAT},{NOAA_LONG}", headers=headers)
        r2 = await globals.http_client.get(r1.json()['properties']['forecast'], headers=headers)
        periods = r2.json()['properties']['periods']
        
        forecast_lines = [f"{p['name']}: {p['detailedForecast']}" for p in periods[:3]]
        return "Weather Forecast:\n" + "\n".join(forecast_lines)
    except Exception as e: 
        logging.error(f"Weather error: {e}")
        return "Weather unavailable."

# --- WEB SEARCH & SCRAPING ---
async def web_search(query): # Searches the internet
    logging.info(f"🔧 TOOL: web_search | '{query}'")
    try:
        r = await globals.http_client.get(SEARXNG_URL, params={'q': query, 'format': 'json'})
        res = r.json().get('results', [])
        return "\n\n".join([
            f"Title: {i['title']}\nURL: {i['url']}\nSnippet: {i['content']}" 
            for i in res[:5]
        ])
    except Exception as e: 
        logging.error(f"Search error: {e}")
        return "Search failed."

async def fetch_web_content(url: str, max_chars: int = 8000) -> dict: # Fetches website content
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
    }
    try:
        import httpx
        async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
            response = await client.get(url, headers=headers)
            if response.status_code != 200:
                return {
                    "success": False, 
                    "status": response.status_code, 
                    "error": f"Site returned status code {response.status_code}. It might be blocking scrapers or require a subscription."
                }

            soup = BeautifulSoup(response.text, 'html.parser')
            for element in soup(['script', 'style', 'nav', 'footer', 'header', 'aside', 'form', 'noscript', 'svg', 'iframe']):
                element.decompose()

            title = soup.title.string.strip() if soup.title else "No Title"
            for tag in soup.find_all(['h1', 'h2', 'h3']):
                tag.insert_before("\n[HEADER: ")
                tag.insert_after("]\n")
            for li in soup.find_all('li'):
                li.insert_before("\n- ")

            text = soup.get_text(separator='\n')
            cleaned_text = re.sub(r'\n{3,}', '\n\n', text).strip()
            cleaned_text = re.sub(r' +', ' ', cleaned_text)

            if len(cleaned_text) > max_chars:
                cleaned_text = cleaned_text[:max_chars] + "... [Content truncated for length]"

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

# --- UTILITIES ---
async def get_news_headlines(): # Fetches news headlines from RSS feeds
    FEEDS = {}
    if NEWS_FEEDS:
        for item in NEWS_FEEDS.split(","):
            if "|" in item:
                name, url = item.split("|")
                FEEDS[name.strip().lower()] = url.strip()
    
    if not FEEDS:
        FEEDS = {"news": "REUTERS|https://news.google.com/rss/search?q=when:24h+source:reuters&hl=en-US&gl=US&ceid=US:en, TECH|https://news.google.com/rss/search?q=when:24h+technology&hl=en-US&gl=US&ceid=US:en"}
    
    logging.info(f"🔧 TOOL: get_news_headlines | Sources: {list(FEEDS.keys())}")

    async def safe_parse(name, url):
        try:
            feed = await asyncio.to_thread(feedparser.parse, url)
            titles = [f"- {i.title}" for i in feed.entries[:5]]
            return f"### {name.upper()}\n" + ("\n".join(titles) if titles else "- No recent news.")
        except Exception as e:
            logging.error(f"Error fetching {name}: {e}")
            return f"### {name.upper()}\n- Unavailable."

    results = await asyncio.gather(*(safe_parse(n, u) for n, u in FEEDS.items()))
    return "\n\n".join(results)

async def get_nasa_apod(): # Fetches NASA's image of the day
    logging.info("🔧 TOOL: get_nasa_apod")
    try:
        r = await globals.http_client.get(f"https://api.nasa.gov/planetary/apod?api_key={NASA_API_KEY}", timeout=20)
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
        tasks = [globals.http_client.get(url) for url in urls]
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

# --- GOOGLE NEST ---
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
        r = await globals.http_client.get(url, headers=headers, timeout=15)
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
        r = await globals.http_client.post(url, json=payload, headers=headers, timeout=15)
        if r.status_code == 200:
            return f"Success: Set thermostat mode to {mode}."
        else:
            return f"Nest error: API returned HTTP {r.status_code}: {r.text}"
    except Exception as e:
        logging.error(f"❌ Nest Set Mode Error: {e}")
        return f"Nest error: Failed to set mode: {e}"

async def set_nest_thermostat_temperature(device_id: str, temp_celsius: float = None, heat_temp_celsius: float = None, cool_temp_celsius: float = None) -> str:
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
    
    device_url = f"https://smartdevicemanagement.googleapis.com/v1/{device_id}"
    try:
        r_dev = await globals.http_client.get(device_url, headers=headers, timeout=15)
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
        r = await globals.http_client.post(url, json=payload, headers=headers, timeout=15)
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

# --- OVERSEERR MOVIE/TV REQ SYSTEM ---
async def overseer_search_movie(query: str) -> str:
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

async def overseer_request_movie(tmdb_id):
    headers = {"X-Api-Key": OVERSEER_KEY, "Content-Type": "application/json"}
    payload = {"mediaType": "movie", "mediaId": int(float(tmdb_id)), "userId": int(OVERSEER_USER_ID), "is4k": False}
    try:
        r = await globals.http_client.post(f"{OVERSEER_URL}/request", headers=headers, json=payload)
        logging.info(f"🔧 TOOL: overseer_request_movie | Status: {r.status_code}")
        if r.status_code == 201 or r.status_code == 200:
            return "SUCCESS: Movie requested for user."
        if r.status_code == 409:
            return "ALREADY_AVAILABLE_OR_PENDING"
        return f"FAILED: Overseer returned {r.status_code}"
    except Exception as e:
        logging.error(f"Overseerr Movie Request Failed: {e}")
        return f"Request failed: {e}"

async def overseer_search_tv(query: str) -> str:
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
        return await asyncio.to_thread(sync_search)
    except Exception as err:
        logging.error(f"Overseerr Search Failed: {err}")
        return f"Error: {err}"

async def overseer_request_tv_season(tmdb_id, season_number):
    headers = {"X-Api-Key": OVERSEER_KEY, "Content-Type": "application/json"}
    payload = {"mediaType": "tv", "mediaId": int(float(tmdb_id)), "seasons": [int(season_number)], "userId": int(OVERSEER_USER_ID), "is4k": False}
    try:
        r = await globals.http_client.post(f"{OVERSEER_URL}/request", headers=headers, json=payload)
        logging.info(f"🔧 TOOL: overseer_request_tv_season | Status: {r.status_code}")
        if r.status_code == 409: return f"Season {season_number} is already available or pending."
        return f"SUCCESS: Season {season_number} requested for user."
    except Exception as e:
        logging.error(f"Overseerr TV Season Request Failed: {e}")
        return f"Request failed: {e}"

# --- REOLINK CAMERA NVR INTEGRATIONS ---
async def get_reolink_snapshot(camera_name: str) -> str:
    logging.info(f"🔧 TOOL: get_reolink_snapshot | Camera: {camera_name}")
    host = os.getenv("REOLINK_HOST")
    user = os.getenv("REOLINK_USER")
    password = os.getenv("REOLINK_PASSWORD")
    cameras_raw = os.getenv("REOLINK_CAMERAS", "")
    
    camera_map = {}
    for item in cameras_raw.split(","):
        if ":" in item:
            name, channel = item.split(":")
            camera_map[name.strip().lower()] = channel.strip()
            
    target_name = camera_name.lower().strip()
    for word in ["camera", "feed", "view", "stream"]:
        target_name = target_name.replace(word, "").strip()
        
    cleaned_target = target_name.replace(" ", "").replace("_", "").replace("-", "")
    channel = None
    matched_camera_name = None

    for key, val in camera_map.items():
        cleaned_key = key.replace(" ", "").replace("_", "").replace("-", "")
        if cleaned_key == cleaned_target:
            channel = val
            matched_camera_name = key
            break
            
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
        
    protocols = [
        {"name": "HTTPS", "url": f"https://{host}/cgi-bin/api.cgi?cmd=Snap&channel={channel}&user={user}&password={password}"},
        {"name": "HTTP", "url": f"http://{host}/cgi-bin/api.cgi?cmd=Snap&channel={channel}&user={user}&password={password}"}
    ]
    
    response_content = None
    successful_protocol = None

    for proto in protocols:
        try:
            logging.info(f"📹 CAMERA: Connecting via {proto['name']} → {host}...")
            r = await globals.http_client.get(proto["url"], timeout=8)
            
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

    import httpx
    if not response_content:
        return (f"FAILED: Could not connect to your Reolink NVR at {host}. "
                f"1. Please REBOOT your Reolink NVR to apply the HTTPS/CGI settings. "
                f"2. If the bot runs in Docker, ensure Docker's firewall allows routing to your LAN.")

    try:
        compressed_bytes = compress_image_bytes(response_content)
        b64_image = base64.b64encode(compressed_bytes).decode('utf-8')
        
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
        
        if not concise_report or not concise_report.strip():
            concise_report = "No active threats or activity detected."
        
        if globals.TARGET_CHAT_ID:
            telegram_caption = f"📸 <b>Live: {matched_camera_name.upper()}</b>\n\n🛡️ <i>{concise_report}</i>"
            await globals.application_bot.send_photo(
                chat_id=globals.TARGET_CHAT_ID,
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
            
            # Write to out-of-context log
            from emery.memory import append_camera_log
            await append_camera_log(matched_camera_name, concise_report, scene_context)
            
            return (
                f"SUCCESS: Photo sent. Security log updated ({matched_camera_name}, {now_str}). "
                f"You must now output exactly the word 'DONE' and absolutely nothing else as your final response to close the turn."
            )
            
        return "Failed to send photo: Chat context lost."
    except Exception as e:
        logging.error(f"❌ Reolink Tool Analysis/Send Crash: {e}", exc_info=True)
        return f"Successfully grabbed the image, but failed to analyze/send it: {e}"

async def get_available_cameras() -> str:
    logging.info("🔧 TOOL: get_available_cameras")
    raw_cams = os.getenv("REOLINK_CAMERAS", "")
    if not raw_cams:
        return "No security cameras are currently configured in the system."
        
    camera_names = []
    for item in raw_cams.split(","):
        colon_idx = item.find(":")
        if colon_idx != -1:
            camera_name_only = item[:colon_idx]
            camera_names.append(camera_name_only.strip())
            
    if not camera_names:
        return "The camera configuration is empty or formatted incorrectly."
        
    formatted_list = ", ".join([f"'{c}'" for c in camera_names])
    return f"The following security cameras are online and available: {formatted_list}"

async def trigger_webhook_alert(camera_name: str):
    logging.info(f"🚨 SECURITY: Person trigger received for '{camera_name}' — dispatching alert...")
    
    # Recover TARGET_CHAT_ID from active chat histories if not yet set
    if not globals.TARGET_CHAT_ID and globals.chat_histories:
        globals.TARGET_CHAT_ID = list(globals.chat_histories.keys())[0]
        
    if not globals.TARGET_CHAT_ID:
        logging.warning("⚠️ SECURITY ALERT: Motion detected, but no active chat session established. Please send a message to the bot first.")
        return
        
    await globals.application_bot.send_message(
        chat_id=globals.TARGET_CHAT_ID,
        text=f"🚨 <b>Person Detected:</b> Someone is on the <b>{camera_name.upper()}</b> camera. Running analysis...",
        parse_mode="HTML"
    )

    result = await get_reolink_snapshot(camera_name)
    logging.info(f"✅ SECURITY: Alert dispatched for '{camera_name}'")

    if globals.TARGET_CHAT_ID:
        from emery.globals import chat_histories
        if globals.TARGET_CHAT_ID not in chat_histories:
            from collections import deque
            chat_histories[globals.TARGET_CHAT_ID] = deque(maxlen=100)
            
        now_dt = datetime.now(USER_TIMEZONE)
        now_str = now_dt.strftime("%A, %B %d, %Y at %I:%M %p")
        event_content = (
            f"[{now_str}] [SYSTEM SECURITY ALERT] Camera '{camera_name}' triggered a person-detection event. "
            f"Photo sent. Security log updated ({camera_name}, {now_str})."
        )
        chat_histories[globals.TARGET_CHAT_ID].append({"role": "user", "content": event_content})

async def reolink_polling_loop(application):
    if os.getenv("ENABLE_REOLINK_POLLING", "false").lower() != "true":
        return
        
    logging.info("📹 CAMERA POLL: Initializing background person-detection polling loop...")
    host = os.getenv("REOLINK_HOST")
    user = os.getenv("REOLINK_USER")
    password = os.getenv("REOLINK_PASSWORD")
    cameras_raw = os.getenv("REOLINK_CAMERAS", "")
    
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
        r = await globals.http_client.post(test_url, json=test_body, timeout=10)
        logging.info(f"📹 CAMERA POLL: Self-test HTTPS status {r.status_code}")
        
        if r.status_code != 200:
            test_url_http = test_url.replace("https://", "http://")
            logging.info(f"📹 CAMERA POLL: HTTPS failed — trying HTTP fallback...")
            r = await globals.http_client.post(test_url_http, json=test_body, timeout=10)
            logging.info(f"📹 CAMERA POLL: HTTP fallback status {r.status_code}")
            
        if r.status_code == 200:
            raw_json = r.json()
            if isinstance(raw_json, list) and raw_json:
                ai_value = raw_json[0].get("value", {})
                people_support = ai_value.get("people", {}).get("support", 0)
                logging.info(f"📹 CAMERA POLL: AI person detection on '{test_cam}': {'supported ✅' if people_support else 'NOT supported ❌ — upgrade firmware'}")
        else:
            logging.error(f"❌ CAMERA POLL: NVR returned status {r.status_code} — check CGI/HTTPS port settings")
    except Exception as e:
        logging.error(f"❌ REOLINK DIAGNOSTIC: Connection self-test crashed: {e}", exc_info=True)

    logging.info("📹 CAMERA POLL: Self-test complete — polling loop active")
    
    import httpx
    while True:
        try:
            await asyncio.sleep(2.5)
            for camera_name, channel in camera_map.items():
                url = f"https://{host}/cgi-bin/api.cgi?cmd=GetAiState&user={user}&password={password}"
                body = [{"cmd": "GetAiState", "param": {"channel": int(channel)}}]
                
                try:
                    r = await globals.http_client.post(url, json=body, timeout=5)
                    if r.status_code != 200:
                        url_http = url.replace("https://", "http://")
                        r = await globals.http_client.post(url_http, json=body, timeout=5)
                        
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
                            current_state = value.get("people", {}).get("alarm_state", 0)

                            tracker = state_tracker[channel]
                            last_state = tracker["last_state"]
                            now = datetime.now(pytz.UTC)

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
    if os.getenv("ENABLE_REOLINK_POLLING", "false").lower() == "true":
        asyncio.create_task(reolink_polling_loop(application))
