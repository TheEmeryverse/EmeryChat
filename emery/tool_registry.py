from emery.config import ENABLE_MEMORY, REOLINK_CAMERAS
from emery.memory import save_user_memory, get_camera_security_log
from emery.scratchpad import clear_scratchpad, jot_down_note, read_scratchpad

from emery.tools import (
    get_calendar_events,
    get_nest_thermostats, set_nest_thermostat_mode, set_nest_thermostat_temperature,
    overseer_search_movie, overseer_request_movie, overseer_search_tv, overseer_request_tv_season,
    get_noaa_weather, set_weather_location_alias, remove_weather_location_alias, list_weather_location_aliases,
    get_news_headlines,
    get_nasa_apod,
    get_today_in_history,
    web_search,
    generate_image,
    speak_message,
    get_system_stats,
    fetch_web_content, get_youtube_transcript,
    search_fred_series, get_fred_series_observations,
    search_imf_indicators, get_imf_datamapper_series,
    get_stock_snapshot, get_stock_price_history,
    get_bond_market_dashboard, get_inflation_dashboard,
    get_us_macro_dashboard, get_equity_market_dashboard, get_global_macro_dashboard,
    get_housing_consumer_dashboard, get_labor_market_dashboard,
    get_reolink_snapshot, get_available_cameras,
    delegate_to_coprocessor, react_to_message, reply_to_message,
    send_sticker, send_gif,
    list_portainer_environments, list_portainer_containers, update_portainer_container,
    import_recipe_to_mealie, send_inter_agent_message
)

# Helper to check if a feature is enabled
def is_enabled(var_name):
    try:
        import emery.config as config
        val = getattr(config, var_name, None)
        if val is not None:
            return bool(val) if isinstance(val, bool) else str(val).lower() == "true"
    except Exception:
        pass
    return False


AVAILABLE_TOOLS = {}
tools_schema = []

# --- General Scratchpad (always enabled) ---
AVAILABLE_TOOLS["jot_down_note"] = jot_down_note
AVAILABLE_TOOLS["read_scratchpad"] = read_scratchpad
AVAILABLE_TOOLS["clear_scratchpad"] = clear_scratchpad
tools_schema.extend([
    {
        "type": "function",
        "function": {
            "name": "jot_down_note",
            "description": (
                "Store temporary working context in the current chat/thread scratchpad. Use during multi-step work or research for confirmed facts, source takeaways, decisions, and open questions that may need to survive context compaction. "
                "This is not durable personal memory; do not save secrets, private facts, or every intermediate result unless the user explicitly asks."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "note": {
                        "type": "string",
                        "description": "One self-contained working note; include enough context to understand it later.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Optional short label such as 'Finding', 'Decision', or 'Open question'.",
                    },
                },
                "required": ["note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_scratchpad",
            "description": (
                "Read all temporary working notes saved for the current chat/thread. Use before continuing a multi-step task when earlier research, decisions, or unresolved questions may be outside the active conversation context. Do not use it as a substitute for long-term personal memory."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clear_scratchpad",
            "description": (
                "Delete all temporary working notes for the current chat/thread. Use only when the user explicitly asks to clear, reset, or forget the scratchpad; never clear it merely because a task is complete."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
])

# --- Conditional Tool Registration ---
if is_enabled("ENABLE_CALENDAR"):
    AVAILABLE_TOOLS["get_calendar_events"] = get_calendar_events
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "get_calendar_events", 
            "description": "List today's events from the user's configured Google Calendars, ordered by start time. Use when the user asks what is on their calendar today or what appointments/events they have today. This tool does not accept a date; ask for clarification rather than implying it can retrieve an arbitrary date.",
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
                "description": "Read all configured Nest thermostats and their current state, including name, device ID, ambient temperature, humidity, mode, target setpoints, HVAC state, and available modes. Use for status questions; this tool does not change thermostat settings.",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "set_nest_thermostat_mode",
                "description": "Change a Nest thermostat's operating mode. Use only when the user explicitly asks to heat, cool, use heat/cool range mode, or turn the thermostat off. Obtain the exact device ID from `get_nest_thermostats`; allowed modes are HEAT, COOL, HEATCOOL, and OFF.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "device_id": {
                            "type": "string",
                            "description": "The full device ID/resource name returned by get_nest_thermostats (e.g. enterprises/{project_id}/devices/{device_id})."
                        },
                        "mode": {
                            "type": "string",
                            "description": "The target operating mode: HEAT, COOL, HEATCOOL, or OFF."
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
                "description": "Change a Nest thermostat's target temperature. Use only when the user explicitly asks to change the setpoint. Pass Celsius values; convert a Fahrenheit request to Celsius. The tool reads the current mode: use `temp_celsius` for HEAT or COOL, and use `heat_temp_celsius` and/or `cool_temp_celsius` for HEATCOOL. It cannot set a temperature while the thermostat is OFF.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "device_id": {
                            "type": "string",
                            "description": "The full device ID/resource name returned by get_nest_thermostats (e.g. enterprises/{project_id}/devices/{device_id})."
                        },
                        "temp_celsius": {
                            "type": "number",
                            "description": "Target temperature in Celsius for the current HEAT or COOL mode; convert from Fahrenheit if needed."
                        },
                        "heat_temp_celsius": {
                            "type": "number",
                            "description": "Optional heat-side target in Celsius for HEATCOOL range mode."
                        },
                        "cool_temp_celsius": {
                            "type": "number",
                            "description": "Optional cool-side target in Celsius for HEATCOOL range mode."
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
            "description": "Search the user's media server for a movie by title. Use this FIRST when the user asks to add or request a movie, before calling `overseer_request_movie`. Pass only the movie title—do not add years, actors, or other filters. Present the numbered matches to the user and wait for them to select one; do not request a movie during the search step.",
            "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "Movie title only; omit year, actors, and other search qualifiers."}}, "required": ["query"]}
        }},
        {"type": "function", "function": {
            "name": "overseer_request_movie", 
            "description": "Request one movie on the user's media server. Use only after the user selects a result from `overseer_search_movie`, and pass that result's TMDB ID. Do not guess an ID or call this merely because the user mentioned a movie; obtain confirmation when multiple matches exist.",
            "parameters": {"type": "object", "properties": {"tmdb_id": {"type": "integer", "description": "TMDB ID from the movie selected in `overseer_search_movie`."}}, "required": ["tmdb_id"]}
        }},
        {"type": "function", "function": {
            "name": "overseer_search_tv", 
            "description": "Search the user's media server for a TV show by title. Use this FIRST when the user asks to add or request a show, before calling `overseer_request_tv_season`. Pass only the show title. Present the numbered matches and wait for the user to select one; do not request a season during the search step.",
            "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "TV show title only; omit year, actors, and other search qualifiers."}}, "required": ["query"]}
        }},
        {"type": "function", "function": {
            "name": "overseer_request_tv_season", 
            "description": "Request a season of one TV show on the user's media server. Use only after the user selects a result from `overseer_search_tv`, and pass that result's TMDB ID plus the requested season number. Use season 0 only when the user asks for all seasons; otherwise pass the specific season number. Do not guess the ID or infer a season the user did not request.",
            "parameters": {
                "type": "object", 
                "properties": {
                    "tmdb_id": {"type": "integer", "description": "TMDB ID from the selected `overseer_search_tv` result."},
                    "season_number": {"type": "integer", "description": "The requested season number; use 0 for all seasons, otherwise a specific positive season number."}
                }, 
                "required": ["tmdb_id", "season_number"]
            }
        }}
    ])

if is_enabled("ENABLE_WEATHER"):
    AVAILABLE_TOOLS["get_noaa_weather"] = get_noaa_weather
    AVAILABLE_TOOLS["set_weather_location_alias"] = set_weather_location_alias
    AVAILABLE_TOOLS["remove_weather_location_alias"] = remove_weather_location_alias
    AVAILABLE_TOOLS["list_weather_location_aliases"] = list_weather_location_aliases
    tools_schema.extend([
        {
            "type": "function",
            "function": {
                "name": "get_noaa_weather",
                "description": "Get current NOAA/NWS weather for a U.S. place, including a city, state, ZIP code, street address, or saved alias such as home or work. Use for forecasts, hourly conditions, or weather alerts. Prefer the location stated by the user; if none is stated, use a saved home alias only when available, otherwise ask for a place rather than inventing one.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {
                            "type": "string",
                            "description": "Optional U.S. city, state, ZIP code, street address, or saved weather alias such as home or work."
                        },
                        "timeframe": {
                            "type": "string",
                            "enum": ["forecast", "hourly"],
                            "description": "Use forecast for the standard multi-period forecast or hourly for the next several hourly periods."
                        },
                        "include_alerts": {
                            "type": "boolean",
                            "description": "Set true to include active NOAA/NWS alerts for the location; defaults to true."
                        }
                    }
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "set_weather_location_alias",
                "description": "Save or replace a persistent weather location alias. Use only when the user explicitly asks to set, save, update, or change a named place such as home, work, school, or office. Resolve the natural-language location as supplied by the user; this changes future weather lookups and should not be done implicitly.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "alias": {
                            "type": "string",
                            "description": "Short alias to save, such as home, work, school, office, or cabin."
                        },
                        "location": {
                            "type": "string",
                            "description": "The place to resolve and save, such as 'Houston, TX' or '123 Main St, Dallas, TX'."
                        }
                    },
                    "required": ["alias", "location"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "remove_weather_location_alias",
                "description": "Delete one saved weather location alias. Use only when the user explicitly asks to clear, remove, or delete a named place such as home or work; do not remove aliases as cleanup or because a lookup failed.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "alias": {
                            "type": "string",
                            "description": "The saved alias to remove."
                        }
                    },
                    "required": ["alias"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "list_weather_location_aliases",
                "description": "List all saved persistent weather location aliases and their resolved places. Use when the user asks which named weather locations are configured or wants to check a saved alias before changing it.",
                "parameters": {
                    "type": "object",
                    "properties": {}
                }
            }
        }
    ])

if is_enabled("ENABLE_NEWS"):
    AVAILABLE_TOOLS["get_news_headlines"] = get_news_headlines
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "get_news_headlines", 
            "description": "Fetch the latest headlines from the bot's configured RSS news feeds. Use for a current headline roundup or a quick 'what's in the news' request. Do not use for deep research, a specific article, or a topic that needs web search and source inspection.",
            "parameters": {}
        }
    })

if is_enabled("ENABLE_NASA"):
    AVAILABLE_TOOLS["get_nasa_apod"] = get_nasa_apod
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "get_nasa_apod", 
            "description": "Fetch NASA's Astronomy Picture of the Day for today, including its title, explanation, and media URL. Use when the user asks for NASA APOD, NASA's picture of the day, or today's astronomy image. Include the raw media URL in the final response when presenting the result; do not substitute an embed URL.",
            "parameters": {}
        }
    })

if is_enabled("ENABLE_HISTORY"):
    AVAILABLE_TOOLS["get_today_in_history"] = get_today_in_history
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "get_today_in_history", 
            "description": "Fetch notable historical events, births, and deaths associated with today's calendar date. Use for questions such as 'what happened on this day in history?' or 'who was born today'; it is not a general historical search for another date or topic.",
            "parameters": {}
        }
    })

# --- Inter-Agent Bridge (always enabled) ---
AVAILABLE_TOOLS["send_inter_agent_message"] = send_inter_agent_message
tools_schema.append({
    "type": "function",
    "function": {
        "name": "send_inter_agent_message",
        "description": (
            "Send a self-contained task to Hermes and wait for its response. Use this only when the user explicitly "
            "asks for Hermes, or when the task materially requires capabilities Emery does not have directly: inspecting "
            "the local environment, running commands, testing or debugging code, reviewing a repository, or interacting "
            "with a graphical browser. Do not use it for simple questions, ordinary web research, or work already covered "
            "by Emery's direct tools. Include the relevant context, constraints, and desired output. Keep the task within "
            "the user's authorization; do not delegate destructive, irreversible, credential-sensitive, or externally "
            "consequential actions without explicit authorization. Evaluate Hermes's response before using it in the final answer."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "recipient_bot_id": {
                    "type": "integer",
                    "enum": [8726427681],
                    "description": "The Hermes bot ID. Must be 8726427681."
                },
                "message": {
                    "type": "string",
                    "description": "A self-contained task for Hermes, including relevant context, constraints, and the desired result or response format."
                }
            },
            "required": ["recipient_bot_id", "message"]
        }
    }
})

if is_enabled("ENABLE_SEARCH"):
    AVAILABLE_TOOLS["web_search"] = web_search
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "web_search", 
            "description": "Search the public web for current information, unfamiliar topics, news, or broad research questions. Use when no more specific structured tool applies, or when finance/tools do not cover the needed context. After searching, use `fetch_web_content` to read a promising specific result when the answer requires article-level detail; search again with a narrower query if the results are insufficient. Do not use this for a URL the user already supplied or for direct structured finance data when a finance tool applies. Keep raw result URLs out of the final user-facing response unless the user asks for them.",
            "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "The web-search query describing the information or topic to find."}}, "required": ["query"]}
        }
    })

if is_enabled("ENABLE_IMAGEGEN"):
    AVAILABLE_TOOLS["generate_image"] = generate_image
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "generate_image", 
            "description": "Generate a new image from the user's visual request. Use only when the user asks to create, draw, generate, illustrate, or design an image; do not use for ordinary text descriptions, image analysis, or finding an existing image. Expand the prompt with useful visual details while preserving the user's subject, style, composition, and constraints.",
            "parameters": {"type": "object", "properties": {"prompt": {"type": "string", "description": "A self-contained visual prompt describing the requested subject, style, composition, and important constraints."}}, "required": ["prompt"]}
        }
    })

if is_enabled("ENABLE_VOICE"):
    AVAILABLE_TOOLS["speak_message"] = speak_message
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "speak_message", 
            "description": "Convert a natural spoken script to audio and send it as a voice memo. Use only when the user's most recent message explicitly asks to speak, say something aloud, or send a voice message. Do not use for an ordinary written answer. The script must be conversational prose with no Markdown, headings, lists, labels, emojis, or symbols.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The spoken voice memo script only. No markdown, headings, titles, bullets, numbered lists, section labels, emojis, or symbols."
                    }
                },
                "required": ["text"]
            }
        }
    })

if is_enabled("ENABLE_SYSTEM_STATS"):
    AVAILABLE_TOOLS["get_system_stats"] = get_system_stats
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "get_system_stats", 
            "description": "Read the current CPU and RAM utilization of the Emery host. Use for questions about this bot's current resource usage or whether the host is under load; this is not a general system diagnostic or historical metrics tool.",
            "parameters": {}
        }
    })

if is_enabled("ENABLE_WEB_SCRAPING"):
    AVAILABLE_TOOLS["fetch_web_content"] = fetch_web_content
    tools_schema.append({
        "type": "function", 
        "function": {
            "name": "fetch_web_content", 
            "description": "Fetch and extract the readable content of one specific public webpage or document URL. Use after `web_search` when a result needs close reading, or when the user gives you a URL and asks for a summary, analysis, or key details. Do not use this to discover pages; use `web_search` for that. Pass only the URL; the tool returns the page title, resolved URL, and extracted text, possibly truncated.",
            "parameters": {"type": "object", "properties": {"url": {"type": "string", "description": "The single HTTP or HTTPS URL to read."}}, "required": ["url"]}
        }
    })

if is_enabled("ENABLE_YOUTUBE_TRANSCRIPT"):
    AVAILABLE_TOOLS["get_youtube_transcript"] = get_youtube_transcript
    tools_schema.append({
        "type": "function",
        "function": {
            "name": "get_youtube_transcript",
            "description": "Retrieve the available captions/transcript for one YouTube video. Use when the user asks to summarize, quote, analyze, search within, or otherwise work from a video's spoken content. Accept a YouTube URL or exact 11-character video ID; do not guess an ID. This works only when public manual or auto-generated captions are available, and is not a general video-metadata or web-search tool.",
            "parameters": {
                "type": "object",
                "properties": {
                    "video_url_or_id": {
                        "type": "string",
                        "description": "A YouTube watch/shorts/embed/youtu.be URL or raw 11-character video ID."
                    },
                    "languages": {
                        "type": "string",
                        "description": "Optional comma-separated preferred transcript language codes, such as 'en' or 'en,es'. Defaults to English."
                    },
                    "translate_to": {
                        "type": "string",
                        "description": "Optional target language code for YouTube's caption translation, such as 'en'. Leave blank to keep the transcript's original selected language."
                    },
                    "include_timestamps": {
                        "type": "boolean",
                        "description": "Whether to include timestamps before each transcript segment. Use true when the user asks for timestamps or exact locations."
                    }
                },
                "required": ["video_url_or_id"]
            }
        }
    })

if is_enabled("ENABLE_FINANCE"):
    AVAILABLE_TOOLS.update({
        "search_fred_series": search_fred_series,
        "get_fred_series_observations": get_fred_series_observations,
        "search_imf_indicators": search_imf_indicators,
        "get_imf_datamapper_series": get_imf_datamapper_series,
        "get_stock_snapshot": get_stock_snapshot,
        "get_stock_price_history": get_stock_price_history,
        "get_bond_market_dashboard": get_bond_market_dashboard,
        "get_inflation_dashboard": get_inflation_dashboard,
        "get_us_macro_dashboard": get_us_macro_dashboard,
        "get_equity_market_dashboard": get_equity_market_dashboard,
        "get_global_macro_dashboard": get_global_macro_dashboard,
        "get_housing_consumer_dashboard": get_housing_consumer_dashboard,
        "get_labor_market_dashboard": get_labor_market_dashboard,
    })
    tools_schema.extend([
        {
            "type": "function",
            "function": {
                "name": "search_fred_series",
                "description": "Discover the correct FRED series ID from a topic or keyword. Use this FIRST when the user asks for a specific economic indicator but has not supplied its FRED ID, such as 'core CPI', 'unemployment', 'real GDP', or 'the 2-year Treasury yield'. Inspect the returned titles, frequency, and units, then call `get_fred_series_observations` with the best matching ID. Do not use this when the user already gave an exact FRED series ID or when a high-level dashboard directly answers a broad question.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "A topic or indicator phrase, not necessarily an ID, such as 'core CPI', 'unemployment rate', 'real GDP', or '2 year treasury yield'."},
                        "limit": {"type": "integer", "description": "Optional number of candidate series to return; maximum 12."}
                    },
                    "required": ["query"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_fred_series_observations",
                "description": "Retrieve observations and metadata for one known FRED series. Use this when the user supplied an exact FRED series ID or after `search_fred_series` identified the correct ID. It returns the series title, frequency, units, latest value, and recent observations, with optional date bounds, unit transformation, frequency aggregation, and row limit. If you do not know the exact ID, use `search_fred_series` first; for broad multi-indicator questions, prefer a dashboard.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "series_id": {"type": "string", "description": "The exact FRED series ID, such as CPIAUCSL, UNRATE, FEDFUNDS, GDPC1, or DGS10."},
                        "observation_start": {"type": "string", "description": "Optional inclusive start date in YYYY-MM-DD format."},
                        "observation_end": {"type": "string", "description": "Optional inclusive end date in YYYY-MM-DD format."},
                        "units": {"type": "string", "description": "Optional FRED transformation such as lin (level), chg (change), pch (percent change), or pc1 (percent change from one year ago). Defaults to lin."},
                        "frequency": {"type": "string", "description": "Optional aggregation frequency: d (daily), w (weekly), bw (biweekly), m (monthly), q (quarterly), or a (annual)."},
                        "limit": {"type": "integer", "description": "Optional number of observations to return; maximum 24. Results are newest first."}
                    },
                    "required": ["series_id"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "search_imf_indicators",
                "description": "Discover the correct IMF DataMapper indicator code from a concept or keyword. Use this FIRST for a specific IMF or cross-country economic measure when the user has not supplied the code, such as real GDP growth, inflation, government debt, or the current account. Inspect the returned labels and descriptions, then call `get_imf_datamapper_series` with the best code. Do not use this when the user already gave an exact IMF indicator code or when a broad dashboard is sufficient.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "A concept or keyword, such as 'real GDP growth', 'inflation', 'government debt', or 'current account'."},
                        "limit": {"type": "integer", "description": "Optional number of candidate indicators to return; maximum 12."}
                    },
                    "required": ["query"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_imf_datamapper_series",
                "description": "Retrieve one known IMF DataMapper indicator for one or more countries across years. Use this when the user supplied an exact indicator code or after `search_imf_indicators` identified it. If you do not know the code, discover it first instead of guessing. For broad cross-country questions covering several standard measures, prefer `get_global_macro_dashboard`.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "indicator": {"type": "string", "description": "The exact IMF indicator code, such as NGDP_RPCH, PCPIPCH, or GGXWDG_NGDP."},
                        "countries": {"type": "string", "description": "Optional comma-separated ISO-3 country codes such as USA,CAN,MEX. Defaults to USA."},
                        "start_year": {"type": "integer", "description": "Optional first year of the comparison window."},
                        "end_year": {"type": "integer", "description": "Optional last year of the comparison window."}
                    },
                    "required": ["indicator"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_stock_snapshot",
                "description": "Get a current stock or ETF snapshot plus basic fundamentals. Use for a ticker's current price, day range, previous close, 52-week range, market cap, EBITDA, P/E, EPS, beta, business summary, or recent quarterly earnings. If the user wants multiple daily prices, a chart-like time sequence, or OHLCV history, use `get_stock_price_history` instead.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Ticker symbol such as AAPL, MSFT, BRK.B, or SPY."}
                    },
                    "required": ["symbol"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_stock_price_history",
                "description": "Get recent historical daily OHLCV data for one stock or ETF ticker. Use when the user asks about price action over time, a recent trading range, multiple daily closes, or daily open/high/low/close/volume rows. This is daily history, not an intraday quote. For the current quote, valuation, fundamentals, or earnings context, use `get_stock_snapshot` instead.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Ticker symbol such as AAPL, MSFT, BRK.B, or SPY."},
                        "outputsize": {"type": "string", "description": "Optional. Use 'compact' for recent history or 'full' when the requested dates may be older."},
                        "limit": {"type": "integer", "description": "Optional number of newest daily rows to return; maximum 30."}
                    },
                    "required": ["symbol"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_bond_market_dashboard",
                "description": "Get a curated bond-market dashboard. Use this FIRST for broad questions about bonds, Treasury yields, the yield curve, mortgage rates, credit spreads, inflation expectations, or how rates relate to policy, growth, labor, and equities. It bundles the relevant FRED series; do not use it when the user asks for one exact FRED series ID, which should use `get_fred_series_observations`.",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_inflation_dashboard",
                "description": "Get a curated inflation dashboard covering headline and core CPI, headline and core PCE, and market-based inflation expectations. Use this FIRST for broad questions about inflation, disinflation, price pressures, or inflation expectations. If the user asks for one exact FRED series, use `get_fred_series_observations` instead.",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_us_macro_dashboard",
                "description": "Get a curated U.S. macroeconomic dashboard covering real GDP, unemployment, payrolls, retail sales, industrial production, the Fed funds rate, and the 10-year Treasury yield. Use this FIRST for broad questions about the U.S. economy, growth, labor, activity, recession risk, or policy context. For one exact FRED series, use `get_fred_series_observations` instead.",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_equity_market_dashboard",
                "description": "Get a curated broad equity-market dashboard covering the S&P 500, Nasdaq, VIX, Treasury yields, high-yield credit spreads, and the dollar. Use this FIRST for questions about the overall stock market, market performance, risk sentiment, or cross-asset conditions. If the user names a specific stock or ETF ticker, use `get_stock_snapshot` or `get_stock_price_history` instead.",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_global_macro_dashboard",
                "description": "Get a curated IMF-based global macro dashboard comparing real GDP growth, inflation, unemployment, government debt, and current-account balances across countries. Use this FIRST for broad cross-country or global-economy questions. Optional countries are comma-separated ISO-3 codes; use `get_imf_datamapper_series` instead for one exact IMF indicator or a custom single-measure comparison.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "countries": {"type": "string", "description": "Optional comma-separated ISO-3 country or area codes. Defaults to USA,CHN,EAQ,JPN,GBR,IND."},
                        "start_year": {"type": "integer", "description": "Optional first year of the comparison window; defaults to 2022."},
                        "end_year": {"type": "integer", "description": "Optional last year of the comparison window; defaults to the current year."}
                    }
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_housing_consumer_dashboard",
                "description": "Get a curated housing-and-consumer dashboard covering mortgage rates, home prices, housing starts, building permits, consumer spending, consumer credit, and delinquency stress. Use this FIRST for broad questions about housing, affordability, construction, household spending, credit, or consumer health. For one exact FRED series, use `get_fred_series_observations` instead.",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_labor_market_dashboard",
                "description": "Get a curated labor-market dashboard covering unemployment, payrolls, initial and continuing claims, job openings, quits, participation, employment utilization, and wage growth. Use this FIRST for broad questions about jobs, layoffs, hiring, labor supply, wage growth, or labor-market conditions. For one exact FRED series, use `get_fred_series_observations` instead.",
                "parameters": {"type": "object", "properties": {}}
            }
        }
    ])

if is_enabled("ENABLE_REOLINK"):
    AVAILABLE_TOOLS["get_reolink_snapshot"] = get_reolink_snapshot
    AVAILABLE_TOOLS["get_available_cameras"] = get_available_cameras
    AVAILABLE_TOOLS["get_camera_security_log"] = get_camera_security_log
    
    # Extract camera names from configuration
    camera_names = list(REOLINK_CAMERAS.keys())
            
    camera_list_str = ", ".join([f"'{c}'" for c in camera_names]) if camera_names else "'front', 'frontdoor'"
    
    tools_schema.extend([
        {
            "type": "function",
            "function": {
                "name": "get_reolink_snapshot",
                "description": "Capture a live snapshot from one configured Reolink security camera and return an AI scene/threat analysis. Use when the user asks to check, look at, view, or patrol a specific camera. This is for the current live scene; use `get_camera_security_log` for past activity and `get_available_cameras` when the camera name is unknown.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "camera_name": {
                            "type": "string",
                            "description": f"The configured camera name to check. Choose exactly one option from this list: {camera_list_str}. Do not invent a camera name; call `get_available_cameras` first if the user did not identify one."
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
                "description": "List the configured and currently reachable home security camera names. Use when the user asks what cameras or feeds are available, or before a snapshot when the requested camera name is ambiguous or unknown. This does not capture a live image.",
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
                "description": "Read recent recorded security-camera activity, including AI threat reports and scene descriptions. Use when the user asks what happened, what was detected, or wants a recent security summary. This is historical log data, not a live camera snapshot; omit `camera_name` to review all cameras.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "camera_name": {
                            "type": "string",
                            "description": "Optional configured camera name (for example, frontdoor) to filter by. Omit to include all cameras."
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Optional maximum number of recent log entries; defaults to 10."
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
            "description": "Persist one durable, future-relevant fact for later conversations. Use only for stable preferences, recurring constraints, names or relationships, household facts, long-term projects, owned devices/services, or standing instructions that should survive after chat history is cleared. Do not save temporary context, one-off updates, jokes, facts already stored, secrets, or sensitive private information in a group chat unless clearly appropriate and explicitly requested.",
            "parameters": {
                "type": "object", 
                "properties": {
                    "fact": {
                        "type": "string",
                        "description": "One concise, self-contained factual statement with no filler or commentary, such as 'Hudson prefers tabs over spaces in code editors.'"
                    }
                }, 
                "required": ["fact"]
            }
        }
    })

if is_enabled("ENABLE_PORTAINER"):
    AVAILABLE_TOOLS["list_portainer_environments"] = list_portainer_environments
    AVAILABLE_TOOLS["list_portainer_containers"] = list_portainer_containers
    AVAILABLE_TOOLS["update_portainer_container"] = update_portainer_container
    tools_schema.extend([
        {
            "type": "function",
            "function": {
                "name": "list_portainer_environments",
                "description": "List Portainer environments with their names, IDs, types, and online/offline status. Use this read-only tool first when you need to identify an environment before listing or updating its containers.",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "list_portainer_containers",
                "description": "List all running and stopped Docker containers in one Portainer environment, including container name, state, and image. Use after identifying the exact environment name; this tool is read-only and does not start, stop, or update containers.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "environment_name": {
                            "type": "string",
                            "description": "The exact Portainer environment name, obtained from `list_portainer_environments` (for example, emeryverse or thegrand)."
                        }
                    },
                    "required": ["environment_name"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "update_portainer_container",
                "description": "Pull the latest image, stop, delete, recreate, and start one Docker container in Portainer while preserving its inspected configuration. This is a powerful, disruptive administrative action. Use only when the user explicitly asks to update, restart, recreate, or upgrade that specific container; never infer authorization from a status question or a general request to inspect containers.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "environment_name": {
                            "type": "string",
                            "description": "The exact Portainer environment name, obtained from `list_portainer_environments`."
                        },
                        "container_name": {
                            "type": "string",
                            "description": "The exact container name to recreate, obtained from `list_portainer_containers` (for example, seerr or plex)."
                        }
                    },
                    "required": ["environment_name", "container_name"]
                }
            }
        }
    ])

if is_enabled("ENABLE_MEALIE"):
    AVAILABLE_TOOLS["import_recipe_to_mealie"] = import_recipe_to_mealie
    tools_schema.append({
        "type": "function",
        "function": {
            "name": "import_recipe_to_mealie",
            "description": "Import one recipe from a web URL into the user's Mealie recipe collection. Use when the user shares a recipe link or explicitly asks to save/import a recipe. Pass exactly one HTTP or HTTPS recipe URL; do not use for general webpage summaries or multiple links in one call.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Exactly one HTTP or HTTPS URL for the recipe to import."
                    }
                },
                "required": ["url"]
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
                "description": "Create one future reminder, recurring reminder, routine, monitor, or automated check. Use only when the user explicitly asks to schedule, remind, repeat, monitor, check later, or automate something; never create a job proactively. For a one-off calendar date without a time, ask for the time first. Put the complete action or reminder content in `prompt`, not only in the short label.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "schedule_type": {
                            "type": "string",
                            "enum": ["daily", "interval", "once", "weekly", "monthly", "yearly"],
                            "description": "Trigger type: daily, interval, once, weekly, monthly, or yearly. Use once for a one-off reminder; use a recurring type for repeated reminders or routines. Personal recurring reminders still use their recurring schedule type and are routed privately when target_user/wording indicates that they are personal."
                        },
                        "schedule_value": {
                            "type": "string",
                            "description": "Trigger value: daily requires HH:MM in 24-hour time; interval requires a duration such as 30m, 1h, or 3600 seconds; once requires localized YYYY-MM-DD HH:MM:SS or a relative delay such as 15m; weekly requires <day_name> <HH:MM>; monthly requires <day_of_month> <HH:MM>; yearly requires <MM-DD> <HH:MM>. Do not pass a date-only value for once."
                        },
                        "prompt": {
                            "type": "string",
                            "description": "The complete instruction the bot will execute when triggered, including the actual reminder content or the tool/action to perform. Do not use a vague label such as 'send reminder about groceries'."
                        },
                        "description": {
                            "type": "string",
                            "description": "A short user-facing label for the job, such as Daily Weather Briefing. Keep actionable details in prompt."
                        },
                        "target_user": {
                            "type": "string",
                            "description": "Optional person or audience: a family member name/alias, me, us, or both. In group chats, me routes a personal reminder to the asker; us/both routes a shared reminder to the group topic."
                        },
                        "route_to_routines": {
                            "type": "boolean",
                            "description": "Optional. Set true for shared recurring briefings, checks, monitoring, or automation that should go to the routines topic. Leave false for personal reminders; routing otherwise follows target_user and wording."
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
                "description": "List currently configured scheduled jobs with their IDs, schedules, labels, prompts, and routing details. Use when the user asks what is scheduled or wants to inspect an existing reminder/routine before changing or removing it. This tool does not modify jobs.",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "remove_scheduled_job",
                "description": "Cancel and delete one existing scheduled job by ID. Use only when the user clearly asks to cancel, stop, delete, or remove that reminder/routine. List jobs first if the correct ID is not already known; do not remove jobs merely because they are complete or unfamiliar.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "job_id": {
                            "type": "string",
                            "description": "The exact unique job ID returned by `list_scheduled_jobs`."
                        }
                    },
                    "required": ["job_id"]
                }
            }
        }
    ])

AVAILABLE_TOOLS["delegate_to_coprocessor"] = delegate_to_coprocessor
tools_schema.append({
    "type": "function",
    "function": {
        "name": "delegate_to_coprocessor",
        "description": "Delegate long or mechanical text-only processing to the fast coprocessor. Use for summarization, extraction, classification, cleanup, formatting, or document parsing when the source is roughly over 1,500 characters, highly repetitive, or expensive to process inline. Do not use for ordinary conversation, direct factual answers, tasks requiring another tool, or work that needs independent reasoning rather than text transformation.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_prompt": {
                    "type": "string",
                    "description": "The exact text-processing instruction, such as 'Extract all dates and times' or 'Summarize this page'."
                },
                "content_to_process": {
                    "type": "string",
                    "description": "The complete text, CSV, email, transcript, or webpage content to process. Do not pass a URL alone when the task requires fetching it first."
                }
            },
            "required": ["task_prompt", "content_to_process"]
        }
    }
})

AVAILABLE_TOOLS["react_to_message"] = react_to_message
tools_schema.append({
    "type": "function",
    "function": {
        "name": "react_to_message",
        "description": "Add one lightweight Telegram emoji reaction to a chat message. Use when a simple reaction is natural, either instead of a response when no text is needed or as a small addition to text. Do not use it instead of answering a substantive question or completing requested work; use the optional message ID only when reacting to an older message.",
        "parameters": {
            "type": "object",
            "properties": {
                "emoji": {
                    "type": "string",
                    "description": "One supported Telegram reaction emoji: 👍, 👎, ❤️, 🔥, 👏, 😂, 😮, 😢, 🎉, 🤔, or 👀."
                },
                "message_id": {
                    "type": "integer",
                    "description": "Optional ID of the message to react to. If omitted, the tool targets the latest user message in the current history."
                }
            },
            "required": ["emoji"]
        }
    }
})

AVAILABLE_TOOLS["reply_to_message"] = reply_to_message
tools_schema.append({
    "type": "function",
    "function": {
        "name": "reply_to_message",
        "description": "Make the bot's final response a Telegram reply to one specific earlier message. Use only when the user explicitly refers to that older message or threading materially clarifies the response. Do not use for normal back-and-forth conversation.",
        "parameters": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "integer",
                    "description": "The exact earlier message ID to quote/reply to."
                }
            },
            "required": ["message_id"]
        }
    }
})

AVAILABLE_TOOLS["send_sticker"] = send_sticker
tools_schema.append({
    "type": "function",
    "function": {
        "name": "send_sticker",
        "description": "Send one Telegram sticker to the current chat. Use when the user asks for a sticker or when a lightweight sticker response is natural. Pass a supported emoji to look up a sticker in the learned library, or pass a direct Telegram sticker file ID; this sends media and is not an emoji reaction.",
        "parameters": {
            "type": "object",
            "properties": {
                "sticker_id_or_emoji": {
                    "type": "string",
                    "description": "A supported lookup emoji such as 👍, ❤️, or 🔥, or a direct Telegram sticker file ID."
                }
            },
            "required": ["sticker_id_or_emoji"]
        }
    }
})

AVAILABLE_TOOLS["send_gif"] = send_gif
tools_schema.append({
    "type": "function",
    "function": {
        "name": "send_gif",
        "description": "Send one animated GIF to the current chat. Use when the user asks for a GIF or when a contextual lightweight animation is natural. Pass a direct HTTP(S) GIF/video URL or a short search query; this sends media and is not a web-search request or an emoji reaction.",
        "parameters": {
            "type": "object",
            "properties": {
                "query_or_url": {
                    "type": "string",
                    "description": "A direct HTTP(S) GIF/video URL or a concise search query such as 'happy dance' or 'confused'."
                }
            },
            "required": ["query_or_url"]
        }
    }
})
