import os
import logging
import re

from emery.config import (
    OLLAMA_URL, MODEL_ID, TOOL_LOOP, MODEL_NAME, ENABLE_MEMORY,
    ENABLE_CALENDAR, ENABLE_SEERR, ENABLE_WEATHER, ENABLE_NEWS, ENABLE_NASA,
    ENABLE_HISTORY, ENABLE_SEARCH, ENABLE_IMAGEGEN, ENABLE_VOICE,
    ENABLE_WEB_SCRAPING
)
# Re-import ENABLE_SYSTEM_STATS & ENABLE_NEST which are checked in main.py but also here
# ENABLE_SYSTEM_STATS might not have been defined as a direct flag, but is checked: is_enabled("ENABLE_SYSTEM_STATS")
import emery.globals as globals
from emery.helpers import get_current_system_prompt
from emery.memory import save_user_memory, get_camera_security_log

# Import all tools from tools.py
from emery.tools import (
    get_calendar_events,
    get_nest_thermostats, set_nest_thermostat_mode, set_nest_thermostat_temperature,
    overseer_search_movie, overseer_request_movie, overseer_search_tv, overseer_request_tv_season,
    get_noaa_weather,
    get_news_headlines,
    get_nasa_apod,
    get_today_in_history,
    web_search,
    generate_image,
    speak_message,
    get_system_stats,
    fetch_web_content,
    get_reolink_snapshot, get_available_cameras
)

# Helper to check if a feature is enabled
def is_enabled(var_name):
    try:
        import emery.config as config
        val = getattr(config, var_name, None)
        if val is not None:
            return str(val).lower() == "true"
    except Exception:
        pass
    return os.getenv(var_name, "false").lower() == "true"


AVAILABLE_TOOLS = {}
tools_schema = []

# --- Conditional Tool Registration ---
if is_enabled("ENABLE_CALENDAR"):
    AVAILABLE_TOOLS["get_calendar_events"] = get_calendar_events
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "get_calendar_events", 
            "description": "Fetch User's Google Calendar events.",
            "parameters": {"type": "object", "properties": {}}
        }
    })

if is_enabled("ENABLE_NEST"):
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

if is_enabled("ENABLE_SEERR"):
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

if is_enabled("ENABLE_WEATHER"):
    AVAILABLE_TOOLS["get_noaa_weather"] = get_noaa_weather
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "get_noaa_weather", 
            "description": "Get weather.", 
            "parameters": {}
        }
    })

if is_enabled("ENABLE_NEWS"):
    AVAILABLE_TOOLS["get_news_headlines"] = get_news_headlines
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "get_news_headlines", 
            "description": "Get news headlines.", 
            "parameters": {}
        }
    })

if is_enabled("ENABLE_NASA"):
    AVAILABLE_TOOLS["get_nasa_apod"] = get_nasa_apod
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "get_nasa_apod", 
            "description": "Get NASA APOD. You ***MUST*** include the RAW URL in the response. Do NOT use an embed URL.", 
            "parameters": {}
        }
    })

if is_enabled("ENABLE_HISTORY"):
    AVAILABLE_TOOLS["get_today_in_history"] = get_today_in_history
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "get_today_in_history", 
            "description": "Get events from history for today.", 
            "parameters": {}
        }
    })

if is_enabled("ENABLE_SEARCH"):
    AVAILABLE_TOOLS["web_search"] = web_search
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "web_search", 
            "description": "Search web, use when needing a deep dive, research, or a query you lack knowledge about. After you receive the results, ask youself if you need to perform another search. If the results are not sufficent, call this tool again with a more specific query. You can and should also use the fetch_web_content tool to get the content of specific results if needed. ***DO NOT INCLUDE URLS IN YOUR RESPONSE***", 
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
        }
    })

if is_enabled("ENABLE_IMAGEGEN"):
    AVAILABLE_TOOLS["generate_image"] = generate_image
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "generate_image", 
            "description": "Generate an image. Enhance the prompt with as much detail as possible to get the best results, while staying true to the original request. ***DO NOT INCLUDE URLS IN YOUR RESPONSE***", 
            "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]}
        }
    })

if is_enabled("ENABLE_VOICE"):
    AVAILABLE_TOOLS["speak_message"] = speak_message
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "speak_message", 
            "description": "Convert text to speech and send as a voice memo to User. Use this when User explicitly asks to 'speak', 'say', or 'send a voice message'. Do NOT use emojis or symbols in tool call! ***ONLY USE IF THE MOST CURRENT MESSAGE EXPLICITLY ASKS FOR SPOKEN CONTENT OR A VOICE MEMO***", 
            "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
        }
    })

if is_enabled("ENABLE_SYSTEM_STATS"):
    AVAILABLE_TOOLS["get_system_stats"] = get_system_stats
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "get_system_stats", 
            "description": "Get system stats.", 
            "parameters": {}
        }
    })

if is_enabled("ENABLE_WEB_SCRAPING"):
    AVAILABLE_TOOLS["fetch_web_content"] = fetch_web_content
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "fetch_web_content", 
            "description": "Fetch and parse the content of a specific URL. Use this when you need to read an article, blog, or specific webpage content. It returns the title, URL, and the main text content (truncated if long). Use AFTER web_search to do deep research, a deep dive, a report, etc. if needed. MUST pass only the URL as a string. Do not pass any other arguments.", 
            "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}
        }
    })

if is_enabled("ENABLE_REOLINK"):
    AVAILABLE_TOOLS["get_reolink_snapshot"] = get_reolink_snapshot
    AVAILABLE_TOOLS["get_available_cameras"] = get_available_cameras
    AVAILABLE_TOOLS["get_camera_security_log"] = get_camera_security_log
    
    # Extract camera names from configuration
    raw_cams = os.getenv("REOLINK_CAMERAS", "")
    camera_names = []
    for item in raw_cams.split(","):
        colon_idx = item.find(":")
        if colon_idx != -1:
            camera_name_only = item[:colon_idx]
            camera_names.append(camera_name_only.strip())
            
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
        },
        {
            "type": "function",
            "function": {
                "name": "get_camera_security_log",
                "description": "Retrieve recent security camera activity logs including AI threat reports and scene descriptions. Use when the user asks what happened on a camera, what activity was detected, or wants a security summary.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "camera_name": {
                            "type": "string",
                            "description": "Optional. Filter by a specific camera name (e.g. 'frontdoor'). Omit to get all cameras."
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max number of recent log entries to return. Default 10."
                        }
                    }
                }
            }
        }
    ])

if ENABLE_MEMORY:
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

if is_enabled("ENABLE_SCHEDULER"):
    from emery.scheduler import add_scheduled_job, list_scheduled_jobs, remove_scheduled_job
    AVAILABLE_TOOLS.update({
        "add_scheduled_job": add_scheduled_job,
        "list_scheduled_jobs": list_scheduled_jobs,
        "remove_scheduled_job": remove_scheduled_job
    })
    tools_schema.extend([
        {
            "type": "function",
            "function": {
                "name": "add_scheduled_job",
                "description": "Schedule a new automated job/task (like checking the weather daily, fetching the news, or setting a repeating or one-time reminder/alert).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "schedule_type": {
                            "type": "string",
                            "enum": ["daily", "interval", "once", "weekly", "monthly", "yearly"],
                            "description": "The schedule trigger type: 'daily' (HH:MM time format), 'interval' (repeating delay), 'once' (one-off localized date-time or relative delay), 'weekly' (e.g. Monday 08:30), 'monthly' (e.g. 1 12:00), or 'yearly' (e.g. 12-19 08:30)."
                        },
                        "schedule_value": {
                            "type": "string",
                            "description": "Trigger specification. 'daily' requires 'HH:MM' (24-hour format, e.g. '08:30'). 'interval' requires a duration (e.g. '30m', '1h', or seconds like '3600'). 'once' requires a localized datetime string 'YYYY-MM-DD HH:MM:SS' or relative delay (e.g. '15m'). 'weekly' requires '<day_name> <HH:MM>' (e.g. 'Monday 08:30'). 'monthly' requires '<day_of_month> <HH:MM>' (e.g. '1 12:00'). 'yearly' requires '<MM-DD> <HH:MM>' (e.g. '12-19 08:30')."
                        },
                        "prompt": {
                            "type": "string",
                            "description": "The exact instruction/query the bot will run when triggered (e.g. 'Check the NOAA weather using get_noaa_weather and send weather summary with clothing recommendations')."
                        },
                        "description": {
                            "type": "string",
                            "description": "A short, user-friendly label/description of the job (e.g. 'Daily Weather Briefing')."
                        }
                    },
                    "required": ["schedule_type", "schedule_value", "prompt", "description"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "list_scheduled_jobs",
                "description": "Retrieve a list of all currently configured custom scheduled jobs, including their IDs, schedules, and prompts.",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "remove_scheduled_job",
                "description": "Cancel and delete a scheduled job using its unique ID.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "job_id": {
                            "type": "string",
                            "description": "The unique ID of the scheduled job to remove."
                        }
                    },
                    "required": ["job_id"]
                }
            }
        }
    ])

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
    "get_available_cameras": f"{MODEL_NAME} is reading your camera configuration...",
    "get_camera_security_log": f"{MODEL_NAME} is reviewing the security log...",
    "add_scheduled_job": f"{MODEL_NAME} is scheduling a job...",
    "list_scheduled_jobs": f"{MODEL_NAME} is retrieving scheduled jobs...",
    "remove_scheduled_job": f"{MODEL_NAME} is removing a scheduled job..."
}


def prune_past_tool_responses(history_buffer, max_len=500):
    """
    Truncates tool responses from past turns to save context window tokens and reduce
    prefill response latency for local LLMs, while keeping the API structure intact.
    """
    for msg in history_buffer:
        if msg.get("role") == "tool" and msg.get("content"):
            content = msg["content"]
            if len(content) > max_len:
                msg["content"] = content[:max_len] + f"\n\n[... Tool output truncated. {len(content) - max_len} characters omitted ...]"


# --- THE UNIFIED ENGINE ---
async def emery_engine(history_buffer, model_to_use=MODEL_ID):
    # Prune past tool responses to prevent context bloat and speed up local inference prefill
    prune_past_tool_responses(history_buffer)

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
    
    ollama_history = []
    for msg in history_buffer:
        clean_msg = {"role": msg["role"]}
        
        # Preserve tool calling fields if present in history
        if "tool_calls" in msg:
            clean_msg["tool_calls"] = msg["tool_calls"]
        if "tool_call_id" in msg:
            clean_msg["tool_call_id"] = msg["tool_call_id"]
        if "name" in msg:
            clean_msg["name"] = msg["name"]
            
        content = msg.get("content")
        if content is not None:
            if isinstance(content, list):
                text_parts = [part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"]
                clean_msg["content"] = " ".join(text_parts) if text_parts else "[Sent an image]"
            elif isinstance(content, str):
                if len(content) > 5000 and not any(c.isspace() for c in content[1000:3000]):
                    clean_msg["content"] = "[Image base64 data removed]"
                else:
                    clean_msg["content"] = content
            else:
                clean_msg["content"] = str(content)
        else:
            clean_msg["content"] = None if "tool_calls" in msg else ""
            
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
            r = await globals.http_client.post(url, json=payload, timeout=300)
            
            if r.status_code != 200:
                logging.error(f"❌ ENGINE: Ollama returned {r.status_code} — {r.text[:200]}")
                return "Ollama connection error.", False

            res = r.json()
            msg = res.get('message', {})
            
            if msg.get("tool_calls"):
                history_buffer.append(msg)
                ollama_history.append(msg)
                for tc in msg['tool_calls']:
                    fn = tc['function']['name']
                    args = tc['function'].get('arguments', {})
                    
                    status_msg = TOOL_STATUS_MESSAGES.get(fn, f"Emery is using {fn}...")
                    await globals.application_bot.send_message(chat_id=globals.TARGET_CHAT_ID, text=f"<i>{status_msg}</i>", parse_mode="HTML")
                    
                    logging.info(f"🔧 TOOL: {fn} | Args: {args}")
                    if fn == "speak_message": 
                        voice_sent_via_tool = True
                    
                    result = await AVAILABLE_TOOLS[fn](**args) if args else await AVAILABLE_TOOLS[fn]()
                    
                    tool_response = {
                        "role": "tool",
                        "content": str(result),
                        "name": fn
                    }
                    if "id" in tc:
                        tool_response["tool_call_id"] = tc["id"]
                        
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
