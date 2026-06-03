import os
import re
import logging
import asyncio
import base64
import json
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
    IMAGE_MODEL, NOAA_LAT, NOAA_LONG, NOAA_EMAIL, WEATHER_LOCATIONS_FILE_PATH,
    calendar_ids, TELEGRAM_TOKEN, OVERSEER_URL, OVERSEER_KEY,
    OVERSEER_USER_ID, TTS_URL, TTS_VOICE, NEWS_FEEDS, USER_TIMEZONE, USER_NAME,
    ENABLE_PORTAINER, PORTAINER_URL, PORTAINER_API_KEY, PORTAINER_SSL_VERIFY,
    FRED_API_KEY, ALPHA_VANTAGE_API_KEY, GOOGLE_TOKEN_PATH, NEST_TOKEN_PATH,
    NEST_PROJECT_ID, REOLINK_SILENT_ALERTS, SECURITY_TOPIC_ID, REOLINK_HOST,
    REOLINK_USER, REOLINK_PASSWORD, REOLINK_CAMERAS, REOLINK_CAMERA_DESCRIPTIONS,
    TELEGRAM_GROUP_CHAT_ID, ENABLE_REOLINK_THREADING, REOLINK_THREAD_WINDOW_MINUTES,
    ENABLE_REOLINK_POLLING, GIPHY_API_KEY, TENOR_API_KEY
)
import emery.globals as globals
from emery.helpers import compress_image_bytes, get_image_description, query_fast_model
from emery.logging_utils import safe_preview


def _format_large_number(value):
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "N/A"

    abs_num = abs(num)
    suffixes = [
        (1_000_000_000_000, "T"),
        (1_000_000_000, "B"),
        (1_000_000, "M"),
        (1_000, "K"),
    ]
    for threshold, suffix in suffixes:
        if abs_num >= threshold:
            return f"{num / threshold:.2f}{suffix}"
    if num.is_integer():
        return str(int(num))
    return f"{num:.2f}"


def _format_decimal(value, digits=2):
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "N/A"
    return f"{num:.{digits}f}"


def _fred_error_prefix():
    if not FRED_API_KEY:
        return "FRED error: FRED_API_KEY is not configured in your .env file."
    return None


def _alpha_vantage_error_prefix():
    if not ALPHA_VANTAGE_API_KEY:
        return "Stock data error: ALPHA_VANTAGE_API_KEY is not configured in your .env file."
    return None


def _build_imf_periods(start_year=None, end_year=None):
    if start_year is None and end_year is None:
        return None

    try:
        start = int(start_year) if start_year is not None else int(end_year)
        end = int(end_year) if end_year is not None else int(start_year)
    except (TypeError, ValueError):
        return None

    if start > end:
        start, end = end, start

    return ",".join(str(year) for year in range(start, end + 1))


async def _get_fred_series_metadata(series_id: str):
    response = await globals.http_client.get(
        "https://api.stlouisfed.org/fred/series",
        params={
            "api_key": FRED_API_KEY,
            "file_type": "json",
            "series_id": series_id,
        },
        timeout=20,
    )
    if response.status_code != 200:
        return None, f"FRED error: API returned HTTP {response.status_code} for series '{series_id}'."

    series_list = response.json().get("seriess", [])
    if not series_list:
        return None, f"FRED error: Series '{series_id}' was not found."
    return series_list[0], None


async def _get_fred_series_points(series_id: str, limit: int = 6, frequency: str = None, units: str = "lin"):
    params = {
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "series_id": series_id,
        "sort_order": "desc",
        "limit": max(2, min(limit, 24)),
        "units": units,
    }
    if frequency:
        params["frequency"] = frequency

    response = await globals.http_client.get(
        "https://api.stlouisfed.org/fred/series/observations",
        params=params,
        timeout=20,
    )
    if response.status_code != 200:
        return None, f"FRED error: API returned HTTP {response.status_code} for observations of '{series_id}'."

    observations = response.json().get("observations", [])
    clean = [obs for obs in observations if obs.get("value") not in (None, "", ".")]
    if not clean:
        return None, f"FRED series '{series_id}' returned no usable observations."
    return clean, None


def _compute_series_change(points):
    if not points or len(points) < 2:
        return None
    try:
        latest = float(points[0]["value"])
        prior = float(points[1]["value"])
    except (TypeError, ValueError, KeyError):
        return None
    return latest - prior


def _format_series_change(change, digits=2):
    if change is None:
        return "N/A"
    sign = "+" if change >= 0 else ""
    return f"{sign}{change:.{digits}f}"


async def _build_fred_dashboard_entry(
    label: str,
    series_id: str,
    summary_hint: str = "",
    limit: int = 6,
    frequency: str = None,
    units: str = "lin",
):
    metadata, meta_error = await _get_fred_series_metadata(series_id)
    if meta_error:
        return {"label": label, "series_id": series_id, "error": meta_error}

    points, points_error = await _get_fred_series_points(series_id, limit=limit, frequency=frequency, units=units)
    if points_error:
        return {"label": label, "series_id": series_id, "error": points_error}

    latest = points[0]
    change = _compute_series_change(points)
    return {
        "label": label,
        "series_id": series_id,
        "title": metadata.get("title", label),
        "units": metadata.get("units_short", metadata.get("units", "N/A")),
        "frequency": metadata.get("frequency_short", metadata.get("frequency", "N/A")),
        "latest_date": latest.get("date"),
        "latest_value": latest.get("value"),
        "change": change,
        "summary_hint": summary_hint,
        "recent_points": points[:limit],
    }


def _format_dashboard_entry(entry, include_recent_count=3):
    if entry.get("error"):
        return f"- {entry['label']} ({entry['series_id']}): {entry['error']}"

    change_text = _format_series_change(entry.get("change"))
    header = (
        f"- {entry['label']} [{entry['series_id']}] ({entry.get('frequency', 'N/A')}, {entry.get('units', 'N/A')}): "
        f"{entry.get('latest_value', 'N/A')} on {entry.get('latest_date', 'N/A')} | Change vs prior: {change_text}"
    )
    if entry.get("summary_hint"):
        header += f" | Why it matters: {entry['summary_hint']}"

    recent_points = entry.get("recent_points", [])[: max(1, include_recent_count)]
    if not recent_points:
        return header

    compact_points = ", ".join(f"{point.get('date')}: {point.get('value')}" for point in recent_points)
    return f"{header}\n  Recent: {compact_points}"


def _build_imf_dashboard_rows(indicator_code, payload, allowed_countries=None, max_countries=6, max_years=4):
    values = payload.get("values", {}).get(indicator_code, {})
    countries_meta = payload.get("countries", {})
    allowed = {code.strip().upper() for code in allowed_countries or [] if code and str(code).strip()}
    rows = []
    for code, yearly_values in values.items():
        if code is None:
            continue
        if allowed and str(code).upper() not in allowed:
            continue
        valid_points = [(year, value) for year, value in yearly_values.items() if value not in (None, "", ".")]
        if not valid_points:
            continue
        valid_points.sort(key=lambda item: item[0], reverse=True)
        latest_year, latest_value = valid_points[0]
        recent = ", ".join(f"{year}: {value}" for year, value in valid_points[:max_years])
        label = countries_meta.get(code, {}).get("label", code)
        rows.append(f"- {label} ({code}) latest: {latest_year} = {latest_value} | Recent: {recent}")
    return rows[:max_countries]


async def _alpha_vantage_query(params: dict, retries: int = 2, delay: float = 1.25):
    last_error = None
    for attempt in range(max(1, retries)):
        response = await globals.http_client.get("https://www.alphavantage.co/query", params=params, timeout=25)
        if response.status_code != 200:
            return None, f"Stock data error: API returned HTTP {response.status_code}."

        payload = response.json()
        if not isinstance(payload, dict):
            return None, "Stock data error: unexpected Alpha Vantage response format."

        note = payload.get("Note") or payload.get("Information") or payload.get("Error Message")
        if note:
            last_error = note
            if attempt < retries - 1:
                await asyncio.sleep(delay * (attempt + 1))
                continue
            return None, note

        return payload, None

    return None, last_error or "Stock data error: Alpha Vantage request failed."

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
    audio = await get_voice_audio(text)
    if audio and globals.TARGET_CHAT_ID.get():
        await globals.application_bot.send_voice(chat_id=globals.TARGET_CHAT_ID.get(), voice=audio, caption="Voice message", message_thread_id=globals.CURRENT_THREAD_ID.get())
        return "Voice message sent successfully."
    return "Failed to send voice message. Ensure TARGET_CHAT_ID is set."

# --- IMAGE GENERATION ---
async def generate_image(prompt): # Generates an image based on the prompt using Gemini API
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
        if globals.TARGET_CHAT_ID.get():
            await globals.application_bot.send_photo(
                chat_id=globals.TARGET_CHAT_ID.get(),
                photo=image_bytes,
                caption=f"Here's your picture: {prompt[:1000]}",
                message_thread_id=globals.CURRENT_THREAD_ID.get()
            )
            return "Image sent successfully."
        return "Chat context lost."
    except Exception as e:
        logging.error(f"❌ Image Tool Crash: {e}")
        return f"Error: {e}"

# --- NOAA WEATHER ---
WEATHER_GEOCODER_URL = "https://nominatim.openstreetmap.org/search"
WEATHER_SUPPORTED_COUNTRY_CODES = "us,pr,vi,gu,mp,as"


def _weather_headers():
    contact = NOAA_EMAIL or "no-contact-provided"
    return {
        "User-Agent": f"{MODEL_NAME}-weather/1.0 ({contact})",
        "Accept": "application/geo+json, application/json",
    }


def _normalize_weather_alias(alias: str) -> str:
    alias = (alias or "").strip().lower()
    alias = re.sub(r"[^a-z0-9_-]+", "_", alias)
    alias = re.sub(r"_+", "_", alias).strip("_")
    return alias


def _load_weather_locations() -> dict:
    if not WEATHER_LOCATIONS_FILE_PATH or not os.path.exists(WEATHER_LOCATIONS_FILE_PATH):
        return {}
    try:
        with open(WEATHER_LOCATIONS_FILE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logging.error(f"❌ WEATHER: Failed to load locations file: {e}", exc_info=True)
        return {}


def _save_weather_locations(locations: dict) -> bool:
    try:
        with open(WEATHER_LOCATIONS_FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(locations, f, indent=2, sort_keys=True)
        return True
    except Exception as e:
        logging.error(f"❌ WEATHER: Failed to save locations file: {e}", exc_info=True)
        return False


def _get_saved_weather_alias(alias: str):
    normalized = _normalize_weather_alias(alias)
    if not normalized:
        return None
    return _load_weather_locations().get(normalized)


def _saved_weather_record_label(record, fallback_label: str):
    if isinstance(record, dict):
        label = str(record.get("label", fallback_label)).strip()
        return label or fallback_label
    label = str(record or "").strip()
    return label or fallback_label


def _saved_weather_record_has_coordinates(record):
    if not isinstance(record, dict):
        return False
    try:
        float(record.get("lat"))
        float(record.get("lon"))
        return True
    except (TypeError, ValueError):
        return False


def _env_default_weather_location():
    try:
        if NOAA_LAT and NOAA_LONG:
            return {
                "label": "default configured location",
                "lat": float(NOAA_LAT),
                "lon": float(NOAA_LONG),
                "source": "env",
            }
    except ValueError:
        logging.warning("⚠️ WEATHER: NOAA_LAT/NOAA_LONG are set but invalid.")
    return None


def _default_weather_location():
    saved_home = _get_saved_weather_alias("home")
    if _saved_weather_record_has_coordinates(saved_home):
        return {
            "label": _saved_weather_record_label(saved_home, "home"),
            "lat": saved_home.get("lat"),
            "lon": saved_home.get("lon"),
            "source": "alias:home",
        }
    return _env_default_weather_location()


async def _geocode_weather_location(location: str):
    params = {
        "q": location,
        "format": "jsonv2",
        "limit": 5,
        "addressdetails": 1,
        "countrycodes": WEATHER_SUPPORTED_COUNTRY_CODES,
    }
    response = await globals.http_client.get(
        WEATHER_GEOCODER_URL,
        params=params,
        headers=_weather_headers(),
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload:
        return None, f"I couldn't resolve '{location}' to a U.S. weather location."

    best = payload[0]
    try:
        lat = float(best["lat"])
        lon = float(best["lon"])
    except (KeyError, TypeError, ValueError):
        return None, f"I found a match for '{location}', but the coordinates were invalid."

    display_name = best.get("display_name", location)
    return {
        "label": display_name,
        "lat": lat,
        "lon": lon,
        "source": "geocoder",
    }, None


async def _resolve_weather_location(location: str = None):
    if not location:
        saved_home = _get_saved_weather_alias("home")
        if _saved_weather_record_has_coordinates(saved_home):
            return _default_weather_location(), None
        if saved_home:
            return await _geocode_weather_location(_saved_weather_record_label(saved_home, "home"))
        env_default = _env_default_weather_location()
        if env_default:
            return env_default, None
        return None, (
            "No default weather location is set yet. "
            "Ask for a place directly like 'What is the weather in Houston?' "
            "or save one with 'Set my home to Houston, TX.'"
        )

    alias_record = _get_saved_weather_alias(location)
    if alias_record:
        if _saved_weather_record_has_coordinates(alias_record):
            return {
                "label": _saved_weather_record_label(alias_record, location),
                "lat": alias_record.get("lat"),
                "lon": alias_record.get("lon"),
                "source": f"alias:{_normalize_weather_alias(location)}",
            }, None
        return await _geocode_weather_location(_saved_weather_record_label(alias_record, location))

    return await _geocode_weather_location(location)


async def _get_noaa_point_metadata(lat: float, lon: float):
    response = await globals.http_client.get(
        f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}",
        headers=_weather_headers(),
        timeout=20,
    )
    response.raise_for_status()
    data = response.json().get("properties", {})
    forecast_url = data.get("forecast")
    hourly_url = data.get("forecastHourly")
    forecast_zone = data.get("forecastZone", "")
    if not forecast_url or not hourly_url:
        raise ValueError("NOAA point lookup did not return forecast URLs.")
    return {
        "forecast_url": forecast_url,
        "hourly_url": hourly_url,
        "forecast_zone": forecast_zone.rsplit("/", 1)[-1] if forecast_zone else "",
    }


async def _get_noaa_alert_summary(forecast_zone: str):
    if not forecast_zone:
        return []
    response = await globals.http_client.get(
        "https://api.weather.gov/alerts/active",
        params={"zone": forecast_zone},
        headers=_weather_headers(),
        timeout=20,
    )
    response.raise_for_status()
    features = response.json().get("features", [])
    alert_lines = []
    for feature in features[:3]:
        properties = feature.get("properties", {})
        event = properties.get("event", "Weather Alert")
        headline = properties.get("headline") or properties.get("description", "")
        headline = re.sub(r"\s+", " ", headline).strip()
        if headline:
            alert_lines.append(f"- {event}: {headline[:180]}")
        else:
            alert_lines.append(f"- {event}")
    return alert_lines


def _format_noaa_forecast(location_label: str, periods: list, timeframe: str, alerts: list):
    title = f"Weather Forecast for {location_label}:"
    if timeframe == "hourly":
        lines = []
        for period in periods[:6]:
            temp = period.get("temperature")
            temp_unit = period.get("temperatureUnit", "F")
            wind = f"{period.get('windSpeed', 'N/A')} {period.get('windDirection', '')}".strip()
            short_forecast = period.get("shortForecast", "No forecast text")
            lines.append(
                f"{period.get('startTime', '')[:16]}: {temp}°{temp_unit}, {short_forecast}, Wind {wind}"
            )
    else:
        lines = []
        for period in periods[:3]:
            lines.append(f"{period.get('name', 'Period')}: {period.get('detailedForecast', 'No forecast available.')}")

    if alerts:
        lines.append("")
        lines.append("Active alerts:")
        lines.extend(alerts)

    return title + "\n" + "\n".join(lines)


async def get_noaa_weather(location: str = None, timeframe: str = "forecast", include_alerts: bool = True):
    timeframe = (timeframe or "forecast").strip().lower()
    if timeframe not in {"forecast", "hourly"}:
        return "Weather error: timeframe must be either 'forecast' or 'hourly'."

    try:
        resolved_location, error = await _resolve_weather_location(location)
        if error:
            return error

        lat = float(resolved_location["lat"])
        lon = float(resolved_location["lon"])
        point_meta = await _get_noaa_point_metadata(lat, lon)
        forecast_url = point_meta["hourly_url"] if timeframe == "hourly" else point_meta["forecast_url"]

        forecast_response = await globals.http_client.get(
            forecast_url,
            headers=_weather_headers(),
            timeout=20,
        )
        forecast_response.raise_for_status()
        periods = forecast_response.json().get("properties", {}).get("periods", [])
        if not periods:
            return f"Weather unavailable for {resolved_location['label']}."

        alerts = []
        if include_alerts:
            alerts = await _get_noaa_alert_summary(point_meta.get("forecast_zone", ""))

        return _format_noaa_forecast(resolved_location["label"], periods, timeframe, alerts)
    except Exception as e:
        logging.error(f"Weather error: {e}", exc_info=True)
        return "Weather unavailable."


async def set_weather_location_alias(alias: str, location: str):
    normalized_alias = _normalize_weather_alias(alias)
    if not normalized_alias:
        return "Weather alias error: alias must contain letters or numbers."

    try:
        resolved_location, error = await _geocode_weather_location(location)
        if error:
            return error

        locations = _load_weather_locations()
        locations[normalized_alias] = {
            "label": resolved_location["label"],
            "lat": round(float(resolved_location["lat"]), 6),
            "lon": round(float(resolved_location["lon"]), 6),
        }
        if not _save_weather_locations(locations):
            return "Weather alias error: failed to save the location."

        return (
            f"Saved weather alias '{normalized_alias}' as {resolved_location['label']} "
            f"({locations[normalized_alias]['lat']}, {locations[normalized_alias]['lon']})."
        )
    except Exception as e:
        logging.error(f"Weather alias save error: {e}", exc_info=True)
        return "Weather alias error: unable to save that location right now."


async def remove_weather_location_alias(alias: str):
    normalized_alias = _normalize_weather_alias(alias)
    if not normalized_alias:
        return "Weather alias error: alias must contain letters or numbers."

    locations = _load_weather_locations()
    if normalized_alias not in locations:
        return f"No saved weather alias named '{normalized_alias}' exists."

    del locations[normalized_alias]
    if not _save_weather_locations(locations):
        return "Weather alias error: failed to update the saved locations."
    return f"Removed weather alias '{normalized_alias}'."


async def list_weather_location_aliases():
    locations = _load_weather_locations()
    if not locations:
        return "No saved weather aliases yet."

    lines = ["Saved weather aliases:"]
    for alias in sorted(locations):
        location = locations[alias]
        lines.append(f"- {alias}: {_saved_weather_record_label(location, 'Unknown location')}")
    return "\n".join(lines)

# --- WEB SEARCH & SCRAPING ---
async def web_search(query): # Searches the internet
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

            if len(cleaned_text) < 200:
                return {
                    "success": False,
                    "error": "The page yielded very little text. It may require JavaScript to render or be a login wall."
                }

            if len(cleaned_text) > 1500:
                logging.debug(f"⚡ FAST MODEL: Summarizing web content of {len(cleaned_text)} chars from {url}...")
                summary_prompt = (
                    f"Summarize this web page content. Extract key details, facts, numbers, dates, or relevant info. "
                    f"Keep it objective, concise, and structured under 600 words.\n\n"
                    f"Title: {title}\n"
                    f"URL: {url}\n\n"
                    f"Content:\n{cleaned_text}"
                )
                try:
                    summary = await query_fast_model(summary_prompt)
                    if summary:
                        cleaned_text = f"[Summarized by Coprocessor]:\n{summary}"
                except Exception as sum_err:
                    logging.warning(f"⚠️ FAST MODEL: Failed to summarize content, using original text. Error: {sum_err}")

            if len(cleaned_text) > max_chars:
                cleaned_text = cleaned_text[:max_chars] + "... [Content truncated for length]"

            return {
                "success": True,
                "title": title,
                "url": url,
                "content": cleaned_text
            }
    except Exception as e:
        return {"success": False, "error": f"Connection Error: {str(e)}"}

async def delegate_to_coprocessor(task_prompt: str, content_to_process: str) -> str:
    """
    Delegate a lightweight sub-task, summarization, formatting, or extraction query 
    to the fast secondary model (coprocessor).
    """
    try:
        prompt = f"Task: {task_prompt}\n\nContent to process:\n{content_to_process}"
        system_prompt = (
            "You are Emery's Coprocessor System. Your job is to process the content provided "
            "according to the specific task prompt. Be extremely concise, accurate, and direct. "
            "Provide only the processed result, with no introductory or conversational remarks."
        )
        result = await query_fast_model(prompt, system_prompt)
        if not result:
            return "Coprocessor returned an empty response."
        return result
    except Exception as e:
        logging.error(f"❌ COPROCESSOR DELEGATION: Tool execution failed: {e}", exc_info=True)
        return f"Delegation failed: {e}"

# --- FINANCE & ECONOMIC DATA ---
async def search_fred_series(query: str, limit: int = 8) -> str:
    config_error = _fred_error_prefix()
    if config_error:
        return config_error

    try:
        response = await globals.http_client.get(
            "https://api.stlouisfed.org/fred/series/search",
            params={
                "api_key": FRED_API_KEY,
                "file_type": "json",
                "search_text": query,
                "limit": max(1, min(limit, 12)),
                "order_by": "search_rank",
                "sort_order": "desc",
            },
            timeout=20,
        )
        if response.status_code != 200:
            return f"FRED error: API returned HTTP {response.status_code}."

        series = response.json().get("seriess", [])
        if not series:
            return f"No FRED series found for '{query}'."

        lines = [f"Top FRED series for '{query}':"]
        for item in series:
            lines.append(
                f"- {item.get('id', 'N/A')}: {item.get('title', 'Untitled')} | "
                f"Freq: {item.get('frequency_short', item.get('frequency', 'N/A'))} | "
                f"Units: {item.get('units_short', item.get('units', 'N/A'))}"
            )
        return "\n".join(lines)
    except Exception as e:
        logging.error(f"❌ FRED Search Error: {e}", exc_info=True)
        return "FRED search failed."


async def get_fred_series_observations(
    series_id: str,
    observation_start: str = None,
    observation_end: str = None,
    units: str = "lin",
    frequency: str = None,
    limit: int = 12,
) -> str:
    config_error = _fred_error_prefix()
    if config_error:
        return config_error

    try:
        meta_response = await globals.http_client.get(
            "https://api.stlouisfed.org/fred/series",
            params={
                "api_key": FRED_API_KEY,
                "file_type": "json",
                "series_id": series_id,
            },
            timeout=20,
        )
        obs_params = {
            "api_key": FRED_API_KEY,
            "file_type": "json",
            "series_id": series_id,
            "units": units,
            "sort_order": "desc",
            "limit": max(1, min(limit, 24)),
        }
        if observation_start:
            obs_params["observation_start"] = observation_start
        if observation_end:
            obs_params["observation_end"] = observation_end
        if frequency:
            obs_params["frequency"] = frequency

        obs_response = await globals.http_client.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params=obs_params,
            timeout=20,
        )

        if meta_response.status_code != 200 or obs_response.status_code != 200:
            status = meta_response.status_code if meta_response.status_code != 200 else obs_response.status_code
            return f"FRED error: API returned HTTP {status}."

        meta_series = meta_response.json().get("seriess", [])
        observations = obs_response.json().get("observations", [])
        if not meta_series:
            return f"FRED error: Series '{series_id}' was not found."

        clean_observations = [obs for obs in observations if obs.get("value") not in (None, ".", "")]
        if not clean_observations:
            return f"FRED series '{series_id}' returned no observations for the requested range."

        meta = meta_series[0]
        latest = clean_observations[0]
        oldest = clean_observations[-1]

        lines = [
            f"FRED Series {series_id}: {meta.get('title', 'Untitled')}",
            f"Frequency: {meta.get('frequency', 'N/A')} | Units: {meta.get('units', 'N/A')} | Seasonal Adjustment: {meta.get('seasonal_adjustment_short', meta.get('seasonal_adjustment', 'N/A'))}",
            f"Latest observation: {latest.get('date')} = {latest.get('value')}",
        ]

        if len(clean_observations) > 1:
            lines.append(f"Oldest returned observation: {oldest.get('date')} = {oldest.get('value')}")

        lines.append("Recent observations:")
        for obs in clean_observations:
            lines.append(f"- {obs.get('date')}: {obs.get('value')}")

        notes = meta.get("notes", "")
        if notes:
            lines.append(f"Notes: {notes[:500]}" + ("..." if len(notes) > 500 else ""))

        return "\n".join(lines)
    except Exception as e:
        logging.error(f"❌ FRED Observations Error: {e}", exc_info=True)
        return "FRED series lookup failed."


async def search_imf_indicators(query: str, limit: int = 8) -> str:
    try:
        response = await globals.http_client.get(
            "https://www.imf.org/external/datamapper/api/v1/indicators",
            timeout=20,
        )
        if response.status_code != 200:
            return f"IMF error: API returned HTTP {response.status_code}."

        raw = response.json()
        indicators = raw.get("indicators", raw)
        query_lower = query.lower()
        matches = []
        for code, details in indicators.items():
            label = (details or {}).get("label", "")
            description = (details or {}).get("description", "")
            haystack = f"{code} {label} {description}".lower()
            if query_lower in haystack:
                matches.append((code, label, description))

        if not matches:
            return f"No IMF indicators found for '{query}'."

        lines = [f"Top IMF indicators for '{query}':"]
        for code, label, description in matches[: max(1, min(limit, 12))]:
            snippet = description[:180] + ("..." if len(description) > 180 else "")
            lines.append(f"- {code}: {label}" + (f" | {snippet}" if snippet else ""))
        return "\n".join(lines)
    except Exception as e:
        logging.error(f"❌ IMF Indicator Search Error: {e}", exc_info=True)
        return "IMF indicator search failed."


async def get_imf_datamapper_series(
    indicator: str,
    countries: str = "USA",
    start_year: int = None,
    end_year: int = None,
) -> str:
    try:
        country_codes = [part.strip().upper() for part in countries.split(",") if part.strip()]
        path_parts = [indicator.strip().upper()] + country_codes
        endpoint = "/".join(path_parts)
        params = {}
        periods = _build_imf_periods(start_year, end_year)
        if periods:
            params["periods"] = periods

        response = await globals.http_client.get(
            f"https://www.imf.org/external/datamapper/api/v1/{endpoint}",
            params=params,
            timeout=20,
        )
        if response.status_code != 200:
            return f"IMF error: API returned HTTP {response.status_code}."

        payload = response.json()
        values = payload.get("values", {})
        indicator_values = values.get(indicator.strip().upper(), {})
        if not indicator_values:
            return f"IMF error: No data found for indicator '{indicator}'."

        countries_meta = payload.get("countries", {})
        indicators_meta = payload.get("indicators", {})
        indicator_meta = indicators_meta.get(indicator.strip().upper(), {})
        indicator_label = indicator_meta.get("label", indicator.strip().upper())

        lines = [f"IMF DataMapper {indicator.strip().upper()}: {indicator_label}"]
        for code, yearly_values in indicator_values.items():
            if not yearly_values:
                continue
            label = countries_meta.get(code, {}).get("label", code)
            valid_points = [(year, value) for year, value in yearly_values.items() if value not in (None, "", ".")]
            if not valid_points:
                continue

            valid_points.sort(key=lambda item: item[0], reverse=True)
            latest_year, latest_value = valid_points[0]
            lines.append(f"{label} ({code}) latest: {latest_year} = {latest_value}")
            lines.append("Recent years:")
            for year, value in valid_points[:8]:
                lines.append(f"- {year}: {value}")

        if len(lines) == 1:
            return f"IMF error: No usable observations found for indicator '{indicator}'."

        return "\n".join(lines)
    except Exception as e:
        logging.error(f"❌ IMF Data Error: {e}", exc_info=True)
        return "IMF data lookup failed."


async def get_stock_snapshot(symbol: str) -> str:
    config_error = _alpha_vantage_error_prefix()
    if config_error:
        return config_error

    symbol = symbol.strip().upper()
    try:
        quote_payload, quote_error = await _alpha_vantage_query({
            "function": "GLOBAL_QUOTE",
            "symbol": symbol,
            "apikey": ALPHA_VANTAGE_API_KEY,
        }, retries=3)
        overview_payload, overview_error = await _alpha_vantage_query({
            "function": "OVERVIEW",
            "symbol": symbol,
            "apikey": ALPHA_VANTAGE_API_KEY,
        }, retries=2)
        earnings_payload, earnings_error = await _alpha_vantage_query({
            "function": "EARNINGS",
            "symbol": symbol,
            "apikey": ALPHA_VANTAGE_API_KEY,
        }, retries=2)

        quote_data = (quote_payload or {}).get("Global Quote", {})
        overview_data = overview_payload or {}
        earnings_data = earnings_payload or {}

        if not quote_data and not overview_data and not earnings_data:
            return f"Stock data error: No results returned for '{symbol}'."

        quarterly_earnings = earnings_data.get("quarterlyEarnings", []) if isinstance(earnings_data, dict) else []
        latest_earnings = quarterly_earnings[0] if quarterly_earnings else {}

        display_name = overview_data.get("Name") or quote_data.get("01. symbol") or symbol
        lines = [
            f"Stock snapshot for {symbol}: {display_name}",
        ]

        price = quote_data.get('05. price') or quote_data.get('04. close') or quote_data.get('08. previous close')
        day_low = quote_data.get('04. low')
        day_high = quote_data.get('03. high')
        prev_close = quote_data.get('08. previous close')
        lines.append(
            f"Price: {price or 'N/A'} | Day range: {day_low or 'N/A'} - {day_high or 'N/A'} | Previous close: {prev_close or 'N/A'}"
        )

        market_cap = overview_data.get('MarketCapitalization')
        ebitda = overview_data.get('EBITDA')
        pe_ratio = overview_data.get('PERatio')
        lines.append(
            f"Market cap: {_format_large_number(market_cap)} | EBITDA: {_format_large_number(ebitda)} | P/E: {pe_ratio or 'N/A'}"
        )
        lines.append(
            f"52-week range: {overview_data.get('52WeekLow', 'N/A')} - {overview_data.get('52WeekHigh', 'N/A')} | EPS: {overview_data.get('EPS', 'N/A')} | Beta: {overview_data.get('Beta', 'N/A')}"
        )

        if latest_earnings:
            lines.append(
                f"Latest quarterly earnings: fiscal date {latest_earnings.get('fiscalDateEnding', 'N/A')} | "
                f"reported date {latest_earnings.get('reportedDate', 'N/A')} | "
                f"reported EPS {latest_earnings.get('reportedEPS', 'N/A')} | "
                f"estimated EPS {latest_earnings.get('estimatedEPS', 'N/A')} | "
                f"surprise {latest_earnings.get('surprise', 'N/A')}"
            )

        description = overview_data.get("Description", "")
        if description:
            lines.append(f"Business summary: {description[:500]}" + ("..." if len(description) > 500 else ""))

        unavailable_notes = []
        if quote_error and not quote_data:
            unavailable_notes.append("quote data unavailable")
        if overview_error and not overview_data:
            unavailable_notes.append("overview data unavailable")
        if earnings_error and not quarterly_earnings:
            unavailable_notes.append("earnings data unavailable")
        if unavailable_notes:
            lines.append(f"Note: {', '.join(unavailable_notes)}.")

        return "\n".join(lines)
    except Exception as e:
        logging.error(f"❌ Stock Snapshot Error: {e}", exc_info=True)
        return "Stock snapshot lookup failed."


async def get_stock_price_history(
    symbol: str,
    outputsize: str = "compact",
    limit: int = 10,
) -> str:
    config_error = _alpha_vantage_error_prefix()
    if config_error:
        return config_error

    symbol = symbol.strip().upper()
    try:
        response = await globals.http_client.get(
            "https://www.alphavantage.co/query",
            params={
                "function": "TIME_SERIES_DAILY",
                "symbol": symbol,
                "outputsize": "full" if str(outputsize).lower() == "full" else "compact",
                "apikey": ALPHA_VANTAGE_API_KEY,
            },
            timeout=25,
        )
        if response.status_code != 200:
            return f"Stock history error: API returned HTTP {response.status_code}."

        payload = response.json()
        series = payload.get("Time Series (Daily)", {})
        if not series:
            note = payload.get("Note") or payload.get("Information") or payload.get("Error Message")
            if note:
                return f"Stock history error: {note}"
            return f"Stock history error: No historical data returned for '{symbol}'."

        entries = sorted(series.items(), key=lambda item: item[0], reverse=True)[: max(1, min(limit, 30))]
        lines = [f"Daily price history for {symbol}:"]
        for date_str, values in entries:
            lines.append(
                f"- {date_str}: open {values.get('1. open')} | high {values.get('2. high')} | "
                f"low {values.get('3. low')} | close {values.get('4. close')} | volume {values.get('5. volume')}"
            )
        return "\n".join(lines)
    except Exception as e:
        logging.error(f"❌ Stock History Error: {e}", exc_info=True)
        return "Stock price history lookup failed."


async def get_bond_market_dashboard() -> str:
    config_error = _fred_error_prefix()
    if config_error:
        return config_error

    series_specs = [
        ("Fed Funds Rate", "FEDFUNDS", "Policy rate anchor for overall financial conditions.", 4, "m", "lin"),
        ("2-Year Treasury Yield", "DGS2", "Short-end Treasury yield that tracks policy expectations.", 5, "d", "lin"),
        ("10-Year Treasury Yield", "DGS10", "Benchmark long-end yield for growth and inflation expectations.", 5, "d", "lin"),
        ("30-Year Treasury Yield", "DGS30", "Long-duration rate used for term-premium and long-horizon rate context.", 5, "d", "lin"),
        ("10Y-2Y Treasury Spread", "T10Y2Y", "Curve slope signal that helps frame recession risk and market expectations.", 5, "d", "lin"),
        ("30-Year Mortgage Rate", "MORTGAGE30US", "Housing and consumer-finance transmission channel for long rates.", 5, "w", "lin"),
        ("5-Year Breakeven Inflation", "T5YIE", "Market-based inflation expectation that helps explain nominal yields.", 5, "d", "lin"),
        ("5-Year Forward Inflation Expectation Rate", "T5YIFR", "Longer-run inflation expectation gauge for term-structure context.", 5, "d", "lin"),
        ("BBB Corporate Bond Spread", "BAA10Y", "Credit spread proxy for corporate borrowing stress relative to Treasuries.", 5, "d", "lin"),
        ("High Yield Bond Spread", "BAMLH0A0HYM2", "Riskier credit-spread gauge that helps frame stress appetite beyond Treasuries.", 5, "d", "lin"),
        ("S&P 500 Index", "SP500", "Risk-asset cross-check to compare bond moves with equities.", 5, "d", "lin"),
        ("Unemployment Rate", "UNRATE", "Labor-market context for whether rates look restrictive or supportive.", 4, "m", "lin"),
    ]

    entries = await asyncio.gather(*[
        _build_fred_dashboard_entry(label, series_id, summary_hint, limit=limit, frequency=frequency, units=units)
        for label, series_id, summary_hint, limit, frequency, units in series_specs
    ])

    lines = [
        "Bond Market Dashboard:",
        "Use this bundle for broad bond-market questions. It includes policy, Treasury curve, mortgage, inflation-expectation, credit-spread, equity, and labor context.",
    ]
    lines.extend(_format_dashboard_entry(entry) for entry in entries)
    return "\n".join(lines)


async def get_inflation_dashboard() -> str:
    config_error = _fred_error_prefix()
    if config_error:
        return config_error

    series_specs = [
        ("Headline CPI", "CPIAUCSL", "Broad consumer inflation level.", 4, "m", "pc1"),
        ("Core CPI", "CPILFESL", "Underlying CPI excluding food and energy.", 4, "m", "pc1"),
        ("Headline PCE Price Index", "PCEPI", "Fed-preferred broad inflation gauge.", 4, "m", "pc1"),
        ("Core PCE Price Index", "PCEPILFE", "Fed-preferred core inflation gauge.", 4, "m", "pc1"),
        ("5-Year Breakeven Inflation", "T5YIE", "Market-based inflation expectations proxy.", 5, "d", "lin"),
        ("5-Year Forward Inflation Expectation Rate", "T5YIFR", "Longer-term inflation expectation gauge.", 5, "d", "lin"),
    ]

    entries = await asyncio.gather(*[
        _build_fred_dashboard_entry(label, series_id, summary_hint, limit=limit, frequency=frequency, units=units)
        for label, series_id, summary_hint, limit, frequency, units in series_specs
    ])

    lines = [
        "Inflation Dashboard:",
        "Use this bundle for inflation questions. It includes headline/core CPI and PCE plus market-based inflation expectations.",
    ]
    lines.extend(_format_dashboard_entry(entry) for entry in entries)
    return "\n".join(lines)


async def get_us_macro_dashboard() -> str:
    config_error = _fred_error_prefix()
    if config_error:
        return config_error

    series_specs = [
        ("Real GDP", "GDPC1", "Top-line real output measure for U.S. growth.", 4, "q", "pch"),
        ("Unemployment Rate", "UNRATE", "Headline labor-market slack measure.", 4, "m", "lin"),
        ("Nonfarm Payrolls", "PAYEMS", "Employment growth and labor demand trend.", 4, "m", "chg"),
        ("Retail Sales", "RSAFS", "Consumer demand proxy.", 4, "m", "pch"),
        ("Industrial Production", "INDPRO", "Production-side activity gauge.", 4, "m", "pch"),
        ("Fed Funds Rate", "FEDFUNDS", "Policy stance anchor.", 4, "m", "lin"),
        ("10-Year Treasury Yield", "DGS10", "Long-term rate context.", 5, "d", "lin"),
    ]

    entries = await asyncio.gather(*[
        _build_fred_dashboard_entry(label, series_id, summary_hint, limit=limit, frequency=frequency, units=units)
        for label, series_id, summary_hint, limit, frequency, units in series_specs
    ])

    lines = [
        "U.S. Macro Dashboard:",
        "Use this bundle for broad U.S. economy questions. It includes growth, labor, demand, production, and policy context.",
    ]
    lines.extend(_format_dashboard_entry(entry) for entry in entries)
    return "\n".join(lines)


async def get_equity_market_dashboard() -> str:
    config_error = _fred_error_prefix()
    if config_error:
        return config_error

    series_specs = [
        ("S&P 500 Index", "SP500", "Core U.S. large-cap equity benchmark.", 5, "d", "lin"),
        ("Nasdaq Composite", "NASDAQCOM", "Growth and tech-heavy equity benchmark.", 5, "d", "lin"),
        ("CBOE VIX", "VIXCLS", "Equity implied-volatility and risk-sentiment gauge.", 5, "d", "lin"),
        ("10-Year Treasury Yield", "DGS10", "Rates backdrop for equity valuation pressure.", 5, "d", "lin"),
        ("High Yield Bond Spread", "BAMLH0A0HYM2", "Credit-risk backdrop for equities.", 5, "d", "lin"),
        ("Dollar Index Broad", "DTWEXBGS", "Dollar backdrop for earnings and global risk appetite.", 5, "d", "lin"),
    ]

    entries = await asyncio.gather(*[
        _build_fred_dashboard_entry(label, series_id, summary_hint, limit=limit, frequency=frequency, units=units)
        for label, series_id, summary_hint, limit, frequency, units in series_specs
    ])

    lines = [
        "Equity Market Dashboard:",
        "Use this bundle for broad equity-market questions. It includes index performance, volatility, rates, credit, and dollar context.",
    ]
    lines.extend(_format_dashboard_entry(entry) for entry in entries)
    return "\n".join(lines)


async def get_global_macro_dashboard(
    countries: str = "USA,CHN,EAQ,JPN,GBR,IND",
    start_year: int = 2022,
    end_year: int = None,
) -> str:
    if end_year is None:
        end_year = datetime.now().year

    country_codes = [part.strip().upper() for part in countries.split(",") if part.strip()]
    if not country_codes:
        country_codes = ["USA", "CHN", "EAQ", "JPN", "GBR", "IND"]

    indicator_specs = [
        ("NGDP_RPCH", "Real GDP Growth", "Top-line growth comparison across major economies."),
        ("PCPIPCH", "Inflation", "Headline inflation comparison across major economies."),
        ("LUR", "Unemployment Rate", "Labor-market slack comparison where available."),
        ("GGXWDG_NGDP", "General Government Gross Debt (% GDP)", "Public debt burden comparison."),
        ("BCA_NGDPD", "Current Account Balance (% GDP)", "External-balance comparison."),
    ]

    periods = _build_imf_periods(start_year, end_year)
    try:
        responses = await asyncio.gather(*[
            globals.http_client.get(
                f"https://www.imf.org/external/datamapper/api/v1/{indicator_code}/{'/'.join(country_codes)}",
                params={"periods": periods} if periods else {},
                timeout=20,
            )
            for indicator_code, _, _ in indicator_specs
        ])

        if any(response.status_code != 200 for response in responses):
            failed = next(response for response in responses if response.status_code != 200)
            return f"IMF error: API returned HTTP {failed.status_code} while building the global macro dashboard."

        lines = [
            "Global Macro Dashboard:",
            f"Use this bundle for broad cross-country macro questions. Countries included: {', '.join(country_codes)}.",
        ]

        for (indicator_code, label, summary_hint), response in zip(indicator_specs, responses):
            payload = response.json()
            indicators_meta = payload.get("indicators", {})
            resolved_label = indicators_meta.get(indicator_code, {}).get("label", label)
            lines.append(f"{resolved_label} [{indicator_code}]")
            lines.append(f"Why it matters: {summary_hint}")
            rows = _build_imf_dashboard_rows(
                indicator_code,
                payload,
                allowed_countries=country_codes,
                max_countries=len(country_codes),
                max_years=4,
            )
            if rows:
                lines.extend(rows)
            else:
                lines.append("- No usable observations returned.")

        return "\n".join(lines)
    except Exception as e:
        logging.error(f"❌ Global Macro Dashboard Error: {e}", exc_info=True)
        return "Global macro dashboard lookup failed."


async def get_housing_consumer_dashboard() -> str:
    config_error = _fred_error_prefix()
    if config_error:
        return config_error

    series_specs = [
        ("30-Year Mortgage Rate", "MORTGAGE30US", "Primary housing-finance rate for affordability and demand.", 5, "w", "lin"),
        ("Home Price Index", "CSUSHPINSA", "National home-price trend and shelter-wealth backdrop.", 4, "m", "pch"),
        ("Housing Starts", "HOUST", "New residential construction and housing-cycle activity.", 4, "m", "pch"),
        ("Building Permits", "PERMIT", "Forward-looking housing construction pipeline.", 4, "m", "pch"),
        ("Retail Sales", "RSAFS", "Top-line consumer spending proxy.", 4, "m", "pch"),
        ("Real Personal Consumption Expenditures", "DPCERC1Q027SBEA", "Inflation-adjusted household consumption trend.", 4, "q", "pch"),
        ("Consumer Credit", "TOTALSL", "Household credit growth and borrowing backdrop.", 4, "m", "chg"),
        ("Delinquency Rate on Consumer Loans", "DRCLACBS", "Household credit stress and repayment quality signal.", 4, "q", "lin"),
    ]

    entries = await asyncio.gather(*[
        _build_fred_dashboard_entry(label, series_id, summary_hint, limit=limit, frequency=frequency, units=units)
        for label, series_id, summary_hint, limit, frequency, units in series_specs
    ])

    lines = [
        "Housing & Consumer Dashboard:",
        "Use this bundle for broad housing, consumer, and household-balance-sheet questions. It includes affordability, construction, spending, and consumer-credit stress context.",
    ]
    lines.extend(_format_dashboard_entry(entry) for entry in entries)
    return "\n".join(lines)


async def get_labor_market_dashboard() -> str:
    config_error = _fred_error_prefix()
    if config_error:
        return config_error

    series_specs = [
        ("Unemployment Rate", "UNRATE", "Headline labor-market slack measure.", 4, "m", "lin"),
        ("Nonfarm Payrolls", "PAYEMS", "Employment growth and labor demand trend.", 4, "m", "chg"),
        ("Initial Jobless Claims", "ICSA", "High-frequency layoff and labor-softness signal.", 5, "w", "lin"),
        ("Continuing Jobless Claims", "CCSA", "Persistence of unemployment and rehiring difficulty signal.", 5, "w", "lin"),
        ("Job Openings", "JTSJOL", "Labor demand and hiring appetite proxy.", 4, "m", "chg"),
        ("Quits Rate", "JTSQUR", "Worker confidence and labor-market tightness signal.", 4, "m", "lin"),
        ("Labor Force Participation Rate", "CIVPART", "Labor supply participation backdrop.", 4, "m", "lin"),
        ("Employment-Population Ratio", "EMRATIO", "Broad employment utilization measure.", 4, "m", "lin"),
        ("Average Hourly Earnings", "CES0500000003", "Nominal wage-growth and labor-income signal.", 4, "m", "pch"),
    ]

    entries = await asyncio.gather(*[
        _build_fred_dashboard_entry(label, series_id, summary_hint, limit=limit, frequency=frequency, units=units)
        for label, series_id, summary_hint, limit, frequency, units in series_specs
    ])

    lines = [
        "Labor Market Dashboard:",
        "Use this bundle for broad labor-market questions. It includes employment, unemployment, claims, openings, quits, participation, and wage-growth context.",
    ]
    lines.extend(_format_dashboard_entry(entry) for entry in entries)
    return "\n".join(lines)

# --- UTILITIES ---
async def get_news_headlines(): # Fetches news headlines from RSS feeds
    FEEDS = {}
    if NEWS_FEEDS:
        for item in NEWS_FEEDS:
            name = str(item.get("name", "")).strip()
            url = str(item.get("url", "")).strip()
            if name and url:
                FEEDS[name.lower()] = url
    
    if not FEEDS:
        FEEDS = {"news": "REUTERS|https://news.google.com/rss/search?q=when:24h+source:reuters&hl=en-US&gl=US&ceid=US:en, TECH|https://news.google.com/rss/search?q=when:24h+technology&hl=en-US&gl=US&ceid=US:en"}
    
    async def safe_parse(name, url):
        try:
            feed = await asyncio.to_thread(feedparser.parse, url)
            titles = [f"- {i.title}" for i in feed.entries[:5]]
            return f"### {name.upper()}\n" + ("\n".join(titles) if titles else "- No recent news.")
        except Exception as e:
            logging.warning(f"⚠️ NEWS: Failed to fetch feed '{name}': {e}")
            return f"### {name.upper()}\n- Unavailable."

    results = await asyncio.gather(*(safe_parse(n, u) for n, u in FEEDS.items()))
    return "\n\n".join(results)

async def get_nasa_apod(): # Fetches NASA's image of the day
    try:
        r = await globals.http_client.get(f"https://api.nasa.gov/planetary/apod?api_key={NASA_API_KEY}", timeout=20)
        if r.status_code == 200:
            d = r.json()
            return f"TITLE: {d.get('title')}\nURL: {d.get('url')}\nEXPLANATION: {d.get('explanation')}"
        return "NASA unavailable."
    except Exception: return "NASA APOD connection failed."

async def get_system_stats(): # Fetches system stats
    return f"CPU {psutil.cpu_percent()}% | RAM {psutil.virtual_memory().percent}%"

async def get_today_in_history(): # Fetches historical events for the current day
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
    token_path = GOOGLE_TOKEN_PATH

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
    token_path = NEST_TOKEN_PATH
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
    project_id = NEST_PROJECT_ID
    if not project_id or project_id.strip() == "":
        return "Nest error: Nest project ID is not configured in config/integrations.json."
        
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
    project_id = NEST_PROJECT_ID
    if not project_id or project_id.strip() == "":
        return "Nest error: Nest project ID is not configured in config/integrations.json."
        
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
    project_id = NEST_PROJECT_ID
    if not project_id or project_id.strip() == "":
        return "Nest error: Nest project ID is not configured in config/integrations.json."
        
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
        logging.error(f"❌ OVERSEERR: Movie search failed: {err}")
        return f"Error: {err}"

async def overseer_request_movie(tmdb_id):
    headers = {"X-Api-Key": OVERSEER_KEY, "Content-Type": "application/json"}
    payload = {"mediaType": "movie", "mediaId": int(float(tmdb_id)), "userId": int(OVERSEER_USER_ID), "is4k": False}
    try:
        r = await globals.http_client.post(f"{OVERSEER_URL}/request", headers=headers, json=payload)
        if r.status_code == 201 or r.status_code == 200:
            return "SUCCESS: Movie requested for user."
        if r.status_code == 409:
            return "ALREADY_AVAILABLE_OR_PENDING"
        return f"FAILED: Overseer returned {r.status_code}"
    except Exception as e:
        logging.error(f"❌ OVERSEERR: Movie request failed: {e}")
        return f"Request failed: {e}"

async def overseer_search_tv(query: str) -> str:
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
        logging.error(f"❌ OVERSEERR: TV search failed: {err}")
        return f"Error: {err}"

async def overseer_request_tv_season(tmdb_id, season_number):
    headers = {"X-Api-Key": OVERSEER_KEY, "Content-Type": "application/json"}
    payload = {"mediaType": "tv", "mediaId": int(float(tmdb_id)), "seasons": [int(season_number)], "userId": int(OVERSEER_USER_ID), "is4k": False}
    try:
        r = await globals.http_client.post(f"{OVERSEER_URL}/request", headers=headers, json=payload)
        if r.status_code == 409: return f"Season {season_number} is already available or pending."
        return f"SUCCESS: Season {season_number} requested for user."
    except Exception as e:
        logging.error(f"❌ OVERSEERR: TV season request failed: {e}")
        return f"Request failed: {e}"

# --- REOLINK CAMERA NVR INTEGRATIONS ---
async def get_reolink_snapshot(
    camera_name: str, 
    reply_to_message_id: int = None,
    target_chat_id: int = None,
    message_thread_id: int = None,
    update_thread_tracker: bool = False
) -> str:
    # Determine target chat ID and options
    if target_chat_id is None:
        target_chat_id = globals.TARGET_CHAT_ID.get()
        actual_thread_id = globals.CURRENT_THREAD_ID.get()
        use_alert_configs = False
    else:
        actual_thread_id = message_thread_id
        use_alert_configs = True

    # Resolve topics and silent settings
    silent_alerts = False
    if use_alert_configs:
        silent_alerts = REOLINK_SILENT_ALERTS
        if actual_thread_id is None and SECURITY_TOPIC_ID is not None:
            actual_thread_id = SECURITY_TOPIC_ID

    host = REOLINK_HOST
    user = REOLINK_USER
    password = REOLINK_PASSWORD
    camera_map = {name.strip().lower(): str(channel).strip() for name, channel in REOLINK_CAMERAS.items()}
            
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
            logging.debug(f"📹 CAMERA: Connecting via {proto['name']} → {host}...")
            r = await globals.http_client.get(proto["url"], timeout=8)
            
            if r.status_code == 200:
                if r.content.startswith(b'\xff\xd8'):
                    response_content = r.content
                    successful_protocol = proto["name"]
                    logging.debug(f"✅ CAMERA: Snapshot fetched via {proto['name']}")
                    break
                else:
                    error_msg = r.content.decode('utf-8', errors='ignore')
                    logging.warning(
                        f"⚠️ REOLINK: {proto['name']} connected, but API returned error: {safe_preview(error_msg, max_len=200)}"
                    )
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
        
        camera_descriptions = {
            name.strip().lower(): str(desc).strip()
            for name, desc in REOLINK_CAMERA_DESCRIPTIONS.items()
        }
                
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
        logging.debug("👁️ VISION [1/2]: Running threat analysis...")
        security_prompt = f"""You are a professional home security monitoring system checking the live '{matched_camera_name}' camera feed{desc_context}.{time_context}
            Analyze this image and report ONLY the following active elements if present:
            - People (assess sex, race/ethnicity, hair color, exact clothing details, and if they are holding or carrying any objects)
            - Vehicles (type, color, position)
            - Packages, deliveries, or parcels (especially near entryways like the front door)

        STRICT SECURITY FILTER RULES:
            1. Do NOT describe static background objects, stationary items, or daily environmental features (e.g., grills, bicycles, stairs, chairs, tables, lawn furniture, toys, structures, siding, or fences).
            2. Do NOT describe domestic pets or local animals unless they represent an active safety/security issue.
            3. Be highly descriptive when analyzing people: detail their physical characteristics, apparel, and actions.
            4. If any people are detected in the image, you MUST append a confidence score and a brief explanation of the visual conditions affecting that confidence on a new line (e.g., "Confidence in assessment: 67%. Poor lighting and face partially obscured from view.").
            5. Keep the description very concise (1 or 2 sentences of description + 1 sentence on a new line for the confidence score and rationale).
            6. If there are no people, vehicles, or packages in the image, respond EXACTLY with: "No active activity detected." """
            
        concise_report = await get_image_description(b64_image, security_prompt)
        logging.debug(f"👁️ VISION [1/2]: Completed ({len(concise_report or '')} chars)")
        
        if not concise_report or not concise_report.strip():
            concise_report = "No active activity detected."
        
        if target_chat_id:
            telegram_caption = f"📸 <b>Live: {matched_camera_name.upper()}</b>\n\n🛡️ <i>{concise_report}</i>"
            sent_photo_msg = await globals.application_bot.send_photo(
                chat_id=target_chat_id,
                photo=compressed_bytes,
                caption=telegram_caption,
                parse_mode="HTML",
                reply_to_message_id=reply_to_message_id,
                message_thread_id=actual_thread_id,
                disable_notification=silent_alerts
            )
            
            if update_thread_tracker and sent_photo_msg:
                cam_key = matched_camera_name.lower().strip()
                if reply_to_message_id is None:
                    globals.reolink_thread_trackers[cam_key] = {
                        "message_id": sent_photo_msg.message_id,
                        "timestamp": datetime.now(USER_TIMEZONE)
                    }
            
            # --- STAGE 3: Broad Scene Description (For LLM Memory Context) ---
            logging.debug("👁️ VISION [2/2]: Generating scene context...")
            context_prompt = (
                f"This is a live feed from the {matched_camera_name} camera{desc_context}.{time_context} "
                "Concisely describe the layout, stationary structures, background, "
                "and visible inanimate objects in the frame."
            )
            scene_context = await get_image_description(b64_image, context_prompt)
            logging.debug(f"👁️ VISION [2/2]: Completed ({len(scene_context or '')} chars)")
            
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
    if not REOLINK_CAMERAS:
        return "No security cameras are currently configured in the system."
    camera_names = list(REOLINK_CAMERAS.keys())
            
    if not camera_names:
        return "The camera configuration is empty or formatted incorrectly."
        
    formatted_list = ", ".join([f"'{c}'" for c in camera_names])
    return f"The following security cameras are online and available: {formatted_list}"

async def trigger_webhook_alert(camera_name: str):
    logging.info(f"🚨 SECURITY: Person trigger received for '{camera_name}' — dispatching alert...")
    
    # Resolve the target chat ID for alerts (check TELEGRAM_GROUP_CHAT_ID first, fall back to TARGET_CHAT_ID)
    alert_chat_id = TELEGRAM_GROUP_CHAT_ID

    if not alert_chat_id:
        if not globals.TARGET_CHAT_ID.get() and globals.chat_histories:
            globals.TARGET_CHAT_ID.set(list(globals.chat_histories.keys())[0])
        alert_chat_id = globals.TARGET_CHAT_ID.get()
        
    if not alert_chat_id:
        logging.warning("⚠️ SECURITY ALERT: Motion detected, but no target chat ID is available. Set telegram.group_chat_id in config/integrations.json or message the bot first.")
        return

    if TELEGRAM_GROUP_CHAT_ID is None:
        logging.warning(
            "⚠️ SECURITY ALERT: telegram.group_chat_id is not configured. "
            "Falling back to the last in-memory chat target (%s), which may be stale.",
            alert_chat_id,
        )
        
    # Check if threading is enabled and configure parameters
    enable_threading = ENABLE_REOLINK_THREADING
    thread_window_minutes = REOLINK_THREAD_WINDOW_MINUTES
        
    reply_to_message_id = None
    cam_key = camera_name.lower().strip()
    now_dt = datetime.now(USER_TIMEZONE)
    
    if enable_threading:
        tracker = globals.reolink_thread_trackers.get(cam_key)
        if tracker:
            first_alert_time = tracker["timestamp"]
            elapsed = (now_dt - first_alert_time).total_seconds()
            if elapsed < thread_window_minutes * 60:
                reply_to_message_id = tracker["message_id"]
                logging.debug(f"🧵 REOLINK THREAD: Successive alert within window for camera '{camera_name}'. Replying to message ID {reply_to_message_id} (elapsed: {elapsed:.1f}s)")
            else:
                logging.debug(f"🧵 REOLINK THREAD: Thread window ({thread_window_minutes}m) expired for camera '{camera_name}'. Starting a new thread.")
                globals.reolink_thread_trackers.pop(cam_key, None)
        else:
            logging.debug(f"🧵 REOLINK THREAD: No active thread for camera '{camera_name}'. Starting a new thread.")
            
    # Resolve the SECURITY_TOPIC_ID
    security_topic_id = SECURITY_TOPIC_ID

    # Call get_reolink_snapshot directly (skipping the status message)
    # We pass update_thread_tracker=True so the photo message ID gets tracked if it starts a new thread.
    result = await get_reolink_snapshot(
        camera_name,
        reply_to_message_id=reply_to_message_id,
        target_chat_id=alert_chat_id,
        message_thread_id=security_topic_id,
        update_thread_tracker=True
    )
    if not isinstance(result, str) or not result.startswith("SUCCESS:"):
        logging.warning(
            "⚠️ SECURITY: Alert delivery failed for '%s' (chat_id=%s thread_id=%s): %s",
            camera_name,
            alert_chat_id,
            security_topic_id,
            result,
        )
        return

    logging.info(
        "✅ SECURITY: Alert dispatched for '%s' to chat_id=%s thread_id=%s",
        camera_name,
        alert_chat_id,
        security_topic_id,
    )

    # Get the sent photo's message ID from the thread tracker
    photo_msg_id = reply_to_message_id
    if not photo_msg_id:
        tracker = globals.reolink_thread_trackers.get(cam_key)
        if tracker:
            photo_msg_id = tracker["message_id"]

    # Append alert info to history using alert_chat_id
    from emery.globals import chat_histories
    if alert_chat_id not in chat_histories:
        from collections import deque
        from emery.config import MAX_HISTORY_LEN
        chat_histories[alert_chat_id] = deque(maxlen=MAX_HISTORY_LEN)
        
    now_dt = datetime.now(USER_TIMEZONE)
    now_str = now_dt.strftime("%A, %B %d, %Y at %I:%M %p")
    event_content = (
        f"[{now_str}] [SYSTEM SECURITY ALERT] Camera '{camera_name}' triggered a person-detection event. "
        f"Photo sent. Security log updated ({camera_name}, {now_str})."
    )
    chat_histories[alert_chat_id].append({
        "role": "user",
        "content": event_content,
        "timestamp": now_dt,
        "message_id": photo_msg_id,
        "message_thread_id": security_topic_id
    })

async def reolink_polling_loop(application):
    if not ENABLE_REOLINK_POLLING:
        return
        
    logging.info("📹 CAMERA POLL: Initializing background person-detection polling loop...")
    host = REOLINK_HOST
    user = REOLINK_USER
    password = REOLINK_PASSWORD
    camera_map = {}
    for name, channel in REOLINK_CAMERAS.items():
        clean_name = str(name).strip()
        clean_channel = str(channel).strip()
        if not clean_name or not clean_channel:
            continue
        try:
            camera_map[clean_name] = str(int(clean_channel))
        except (TypeError, ValueError):
            logging.warning(
                f"⚠️ REOLINK POLLING: Skipping camera '{clean_name}' because channel is invalid: {channel!r}"
            )
            
    if not camera_map:
        logging.warning("⚠️ REOLINK POLLING: No cameras mapped. Check config/integrations.json.")
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
        logging.debug(f"📹 CAMERA POLL: Self-test HTTPS status {r.status_code}")
        
        if r.status_code != 200:
            test_url_http = test_url.replace("https://", "http://")
            logging.debug(f"📹 CAMERA POLL: HTTPS failed — trying HTTP fallback...")
            r = await globals.http_client.post(test_url_http, json=test_body, timeout=10)
            logging.debug(f"📹 CAMERA POLL: HTTP fallback status {r.status_code}")
            
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
                                logging.warning(
                                    f"⚠️ REOLINK POLLING: Camera '{camera_name}' (Channel {channel}) returned API error code {code}: {safe_preview(error_detail, max_len=160)}"
                                )
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
                            logging.warning(
                                f"⚠️ REOLINK POLLING: Unexpected response format from NVR: {safe_preview(data, max_len=180)}"
                            )
                    else:
                        logging.warning(f"⚠️ REOLINK POLLING: Both HTTP and HTTPS queries returned code {r.status_code} for camera '{camera_name}'")
                except (httpx.ConnectError, httpx.ConnectTimeout, httpx.NetworkError) as e:
                    logging.debug(f"📹 REOLINK POLLING: Connection blip on camera '{camera_name}' (Channel {channel})")
                except Exception as inner_e:
                    logging.error(f"❌ REOLINK POLLING: Error evaluating state for camera '{camera_name}': {inner_e}", exc_info=True)
                    
        except Exception as outer_e:
            logging.error(f"❌ REOLINK POLLING: Global polling loop exception: {outer_e}", exc_info=True)

async def start_reolink_polling(application):
    if ENABLE_REOLINK_POLLING:
        asyncio.create_task(reolink_polling_loop(application))

# --- EMOJI REACTIONS AND THREADING TOOLS ---
async def react_to_message(emoji: str, message_id: int = None) -> str:
    """
    Reacts to a specific message in the chat with an emoji.
    If message_id is not specified, it defaults to the latest user message in history.
    Available standard emojis: '👍', '👎', '❤️', '🔥', '👏', '😂', '😮', '😢', '🎉', '🤔', '👀'
    """
    chat_id = globals.TARGET_CHAT_ID.get()
    if not chat_id:
        return "Error: No active chat session to react to."
        
    if not message_id:
        # Try to find the latest user message in history
        history = globals.chat_histories.get(chat_id)
        if history:
            for msg in reversed(history):
                if msg.get("role") == "user" and msg.get("message_id"):
                    message_id = msg.get("message_id")
                    break
                    
    if not message_id:
        return "Error: Could not determine message ID to react to. Please specify a message_id."
        
    try:
        await globals.application_bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=emoji
        )
        return f"Successfully reacted to message {message_id} with {emoji}."
    except Exception as e:
        logging.error(f"❌ TOOLS: Failed to react to message {message_id}: {e}")
        return f"Error setting reaction: {e}"

async def reply_to_message(message_id: int) -> str:
    """
    Sets the target message for the bot's final response in this turn to reply directly to a specific previous message.
    """
    chat_id = globals.TARGET_CHAT_ID.get()
    if not chat_id:
        return "Error: No active chat session."
    globals.chat_reply_targets[chat_id] = message_id
    return f"Success: The bot's final message will reply to message ID {message_id}."

async def send_sticker(sticker_id_or_emoji: str) -> str:
    """
    Sends a sticker to the chat.
    You can specify a direct Telegram file ID, or a standard emoji (e.g. '👍', '❤️', '🔥') 
    to look up a sticker in your learned library.
    """
    from telegram import ReplyParameters
    chat_id = globals.TARGET_CHAT_ID.get()
    if not chat_id:
        return "Error: No active chat session."
        
    file_id = None
    if sticker_id_or_emoji in globals.learned_stickers:
        file_id = globals.learned_stickers[sticker_id_or_emoji]
    else:
        file_id = sticker_id_or_emoji

    if not file_id:
        return f"Error: No sticker found in library for emoji/lookup '{sticker_id_or_emoji}'."

    try:
        reply_to_id = globals.chat_reply_targets.pop(chat_id, None)
        reply_params = ReplyParameters(message_id=reply_to_id, allow_sending_without_reply=True) if reply_to_id else None
        
        await globals.application_bot.send_sticker(
            chat_id=chat_id,
            sticker=file_id,
            reply_parameters=reply_params,
            message_thread_id=globals.CURRENT_THREAD_ID.get()
        )
        return f"Successfully sent sticker: {sticker_id_or_emoji}."
    except Exception as e:
        logging.error(f"❌ TOOLS: Failed to send sticker: {e}")
        return f"Error sending sticker: {e}"

async def search_gif(query: str) -> str:
    """Helper function to search Giphy and Tenor APIs for a GIF matching the query."""
    # 1. Try Giphy search first (using custom key if provided, fallback to Giphy public beta key)
    try:
        giphy_key = GIPHY_API_KEY
        url = "https://api.giphy.com/v1/gifs/search"
        params = {"api_key": giphy_key, "q": query, "limit": 1}
        r = await globals.http_client.get(url, params=params, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data.get("data"):
                return data["data"][0]["images"]["original"]["url"]
    except Exception as e:
        logging.error(f"❌ TOOLS: Giphy search failed: {e}")

    # 2. Try Tenor search (using custom key if provided, fallback to Tenor default key)
    try:
        tenor_key = TENOR_API_KEY
        url = "https://tenor.googleapis.com/v2/posts"
        params = {"key": tenor_key, "q": query, "limit": 1, "client_key": "emerychat"}
        r = await globals.http_client.get(url, params=params, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data.get("results"):
                return data["results"][0]["media_formats"]["gif"]["url"]
    except Exception as e:
        logging.error(f"❌ TOOLS: Tenor search failed: {e}")

    return None

async def send_gif(query_or_url: str) -> str:
    """
    Sends a GIF (animation) to the chat.
    Specify a direct URL to a .gif / .mp4 file, or a search query (e.g. 'happy dance', 'confused') 
    to automatically search and send a matching GIF.
    """
    from telegram import ReplyParameters
    chat_id = globals.TARGET_CHAT_ID.get()
    if not chat_id:
        return "Error: No active chat session."

    gif_url = None
    if query_or_url.startswith("http://") or query_or_url.startswith("https://"):
        gif_url = query_or_url
    else:
        gif_url = await search_gif(query_or_url)

    if not gif_url:
        return f"Error: Could not find or resolve any GIF for query '{query_or_url}'."

    try:
        reply_to_id = globals.chat_reply_targets.pop(chat_id, None)
        reply_params = ReplyParameters(message_id=reply_to_id, allow_sending_without_reply=True) if reply_to_id else None
        
        await globals.application_bot.send_animation(
            chat_id=chat_id,
            animation=gif_url,
            reply_parameters=reply_params,
            message_thread_id=globals.CURRENT_THREAD_ID.get()
        )
        return f"Successfully sent GIF for query/url: '{query_or_url}'."
    except Exception as e:
        logging.error(f"❌ TOOLS: Failed to send GIF: {e}")
        return f"Error sending GIF: {e}"

# --- PORTAINER INTEGRATION TOOLS ---
def get_portainer_headers():
    return {
        "X-API-Key": PORTAINER_API_KEY,
        "Accept": "application/json"
    }

async def list_portainer_environments() -> str:
    """
    Fetches the list of all environments (endpoints) configured in Portainer.
    Returns names, IDs, types, and status.
    """
    if str(ENABLE_PORTAINER).lower() != "true" or not PORTAINER_URL:
        return "Portainer integration is not enabled or configured."

    import httpx
    url = f"{PORTAINER_URL}/api/endpoints"
    try:
        verify_ssl = PORTAINER_SSL_VERIFY
        async with httpx.AsyncClient(verify=verify_ssl, timeout=15.0) as client:
            r = await client.get(url, headers=get_portainer_headers())
            if r.status_code != 200:
                return f"Failed to fetch Portainer environments: HTTP {r.status_code}"
            
            envs = r.json()
            if not envs:
                return "No environments found in Portainer."

            lines = ["Available Portainer Environments:"]
            for env in envs:
                env_type = "Docker" if env.get("Type") in [1, 2, 4] else "Kubernetes/Other"
                status = "Online" if env.get("Status") == 1 else "Offline"
                lines.append(f"- '{env.get('Name')}' (ID: {env.get('Id')}, Type: {env_type}, Status: {status})")
            return "\n".join(lines)
    except Exception as e:
        return f"Error listing Portainer environments: {str(e)}"

async def list_portainer_containers(environment_name: str) -> str:
    """
    Lists all containers (running and stopped) in a specific Portainer environment.
    """
    if str(ENABLE_PORTAINER).lower() != "true" or not PORTAINER_URL:
        return "Portainer integration is not enabled or configured."

    import httpx
    verify_ssl = PORTAINER_SSL_VERIFY

    try:
        async with httpx.AsyncClient(verify=verify_ssl, timeout=15.0) as client:
            headers = get_portainer_headers()

            # 1. Resolve environment_name -> env_id
            r_envs = await client.get(f"{PORTAINER_URL}/api/endpoints", headers=headers)
            if r_envs.status_code != 200:
                return f"Failed to list environments: HTTP {r_envs.status_code}"
            
            env_id = None
            for env in r_envs.json():
                if env.get("Name").lower().strip() == environment_name.lower().strip():
                    env_id = env.get("Id")
                    break
            
            if env_id is None:
                return f"Environment '{environment_name}' not found."

            # 2. List containers
            url = f"{PORTAINER_URL}/api/endpoints/{env_id}/docker/containers/json?all=true"
            r_containers = await client.get(url, headers=headers)
            if r_containers.status_code != 200:
                return f"Failed to fetch containers: HTTP {r_containers.status_code}"
            
            containers = r_containers.json()
            if not containers:
                return f"No containers found in environment '{environment_name}'."

            lines = [f"Containers in Environment '{environment_name}':"]
            for c in containers:
                names = ", ".join(c.get("Names", [])).lstrip("/")
                state = c.get("State")
                image = c.get("Image")
                lines.append(f"- {names} ({state}) - Image: {image}")
            return "\n".join(lines)
    except Exception as e:
        return f"Error listing containers: {str(e)}"

async def update_portainer_container(environment_name: str, container_name: str) -> str:
    """
    Recreates and restarts a container in the specified Portainer environment.
    Always pulls the latest image before recreation.
    """
    if str(ENABLE_PORTAINER).lower() != "true" or not PORTAINER_URL:
        return "Portainer integration is not enabled or configured."

    import httpx
    # Clean the container name (strip leading slashes if the LLM includes it)
    clean_target_name = container_name.lstrip("/").strip()
    verify_ssl = PORTAINER_SSL_VERIFY
    always_pull = True

    try:
        async with httpx.AsyncClient(verify=verify_ssl, timeout=30.0) as client:
            headers = get_portainer_headers()

            # 1. Resolve environment_name -> env_id
            r_envs = await client.get(f"{PORTAINER_URL}/api/endpoints", headers=headers)
            if r_envs.status_code != 200:
                return f"Failed to list environments: HTTP {r_envs.status_code}"
            
            env_id = None
            for env in r_envs.json():
                if env.get("Name").lower().strip() == environment_name.lower().strip():
                    env_id = env.get("Id")
                    break
            
            if env_id is None:
                return f"Environment '{environment_name}' not found."

            # 2. Find target container -> container_id and full name
            r_containers = await client.get(f"{PORTAINER_URL}/api/endpoints/{env_id}/docker/containers/json?all=true", headers=headers)
            if r_containers.status_code != 200:
                return f"Failed to list containers in environment '{environment_name}': HTTP {r_containers.status_code}"
            
            container_id = None
            image_name = None
            for c in r_containers.json():
                names = [n.lstrip("/") for n in c.get("Names", [])]
                if clean_target_name in names:
                    container_id = c.get("Id")
                    image_name = c.get("Image")
                    break

            if not container_id:
                return f"Container '{container_name}' not found in environment '{environment_name}'."

            # 3. Retrieve container details for recreation settings
            r_inspect = await client.get(f"{PORTAINER_URL}/api/endpoints/{env_id}/docker/containers/{container_id}/json", headers=headers)
            if r_inspect.status_code != 200:
                return f"Failed to inspect container: HTTP {r_inspect.status_code}"
            
            container_config = r_inspect.json()
            config = container_config.get("Config", {})
            host_config = container_config.get("HostConfig", {})
            networking_config = container_config.get("NetworkSettings", {}).get("Networks", {})
            
            endpoints_config = {}
            for net_name, net_detail in networking_config.items():
                endpoints_config[net_name] = {
                    "IPAMConfig": net_detail.get("IPAMConfig"),
                    "Links": net_detail.get("Links"),
                    "Aliases": net_detail.get("Aliases")
                }

            creation_payload = {
                "Hostname": config.get("Hostname"),
                "Domainname": config.get("Domainname"),
                "User": config.get("User"),
                "AttachStdin": config.get("AttachStdin", False),
                "AttachStdout": config.get("AttachStdout", True),
                "AttachStderr": config.get("AttachStderr", True),
                "Tty": config.get("Tty", False),
                "OpenStdin": config.get("OpenStdin", False),
                "StdinOnce": config.get("StdinOnce", False),
                "Env": config.get("Env"),
                "Cmd": config.get("Cmd"),
                "Image": image_name,
                "Volumes": config.get("Volumes"),
                "WorkingDir": config.get("WorkingDir"),
                "Entrypoint": config.get("Entrypoint"),
                "OnBuild": config.get("OnBuild"),
                "Labels": config.get("Labels"),
                "HostConfig": host_config,
                "NetworkingConfig": {
                    "EndpointsConfig": endpoints_config
                }
            }

            # 4. Pull new image
            if always_pull:
                pull_url = f"{PORTAINER_URL}/api/endpoints/{env_id}/docker/images/create?fromImage={image_name}"
                r_pull = await client.post(pull_url, headers=headers, timeout=120.0)
                if r_pull.status_code != 200:
                    return f"Failed to pull image '{image_name}': HTTP {r_pull.status_code}. Aborting recreation."

            # 5. Stop old container
            r_stop = await client.post(f"{PORTAINER_URL}/api/endpoints/{env_id}/docker/containers/{container_id}/stop", headers=headers)
            if r_stop.status_code not in [204, 304, 200]:
                return f"Failed to stop container: HTTP {r_stop.status_code}"

            # 6. Delete old container
            r_del = await client.delete(f"{PORTAINER_URL}/api/endpoints/{env_id}/docker/containers/{container_id}", headers=headers)
            if r_del.status_code not in [204, 200]:
                return f"Failed to delete old container: HTTP {r_del.status_code}"

            # 7. Create new container
            create_url = f"{PORTAINER_URL}/api/endpoints/{env_id}/docker/containers/create?name={clean_target_name}"
            r_create = await client.post(create_url, headers=headers, json=creation_payload)
            if r_create.status_code not in [200, 201]:
                return f"Failed to create new container: HTTP {r_create.status_code}. Payload: {r_create.text}"
            
            new_container_id = r_create.json().get("Id")

            # 8. Start new container
            r_start = await client.post(f"{PORTAINER_URL}/api/endpoints/{env_id}/docker/containers/{new_container_id}/start", headers=headers)
            if r_start.status_code not in [204, 200]:
                return f"Recreated container (ID: {new_container_id}) but failed to start it: HTTP {r_start.status_code}"

            return f"Success: Container '{clean_target_name}' in environment '{environment_name}' has been successfully recreated and started with the latest image."
            
    except Exception as e:
        return f"Error updating container: {str(e)}"
