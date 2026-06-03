import logging
import re

from telegram.error import BadRequest

from emery.config import (
    OLLAMA_URL, MODEL_ID, TOOL_LOOP, MODEL_NAME, ENABLE_MEMORY,
    ENABLE_CALENDAR, ENABLE_SEERR, ENABLE_WEATHER, ENABLE_NEWS, ENABLE_NASA,
    ENABLE_HISTORY, ENABLE_SEARCH, ENABLE_IMAGEGEN, ENABLE_VOICE,
    ENABLE_WEB_SCRAPING, ENABLE_FINANCE, ENABLE_NEST, ENABLE_SYSTEM_STATS,
    ENABLE_REOLINK, ENABLE_SCHEDULER, REOLINK_CAMERAS, OLLAMA_NUM_CTX
)
import emery.globals as globals
from emery.helpers import get_current_system_prompt, normalize_gemma_thinking, clean_thinking_tags
from emery.logging_utils import format_logging_payload
from emery.memory import save_user_memory, get_camera_security_log
from emery.telegram_utils import normalize_message_thread_id

# Import all tools from tools.py
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
    fetch_web_content,
    search_fred_series, get_fred_series_observations,
    search_imf_indicators, get_imf_datamapper_series,
    get_stock_snapshot, get_stock_price_history,
    get_bond_market_dashboard, get_inflation_dashboard,
    get_us_macro_dashboard, get_equity_market_dashboard, get_global_macro_dashboard,
    get_housing_consumer_dashboard, get_labor_market_dashboard,
    get_reolink_snapshot, get_available_cameras,
    delegate_to_coprocessor, react_to_message, reply_to_message,
    send_sticker, send_gif,
    list_portainer_environments, list_portainer_containers, update_portainer_container
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
    AVAILABLE_TOOLS["set_weather_location_alias"] = set_weather_location_alias
    AVAILABLE_TOOLS["remove_weather_location_alias"] = remove_weather_location_alias
    AVAILABLE_TOOLS["list_weather_location_aliases"] = list_weather_location_aliases
    tools_schema.extend([
        {
            "type": "function",
            "function": {
                "name": "get_noaa_weather",
                "description": "Get weather for a user-specified U.S. place like 'Houston', 'Houston, TX', a ZIP code, street address, or a saved alias like 'home', 'work', or 'school'. If no location is provided, use the saved 'home' alias first, then the optional env fallback if configured.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {
                            "type": "string",
                            "description": "Optional place name, ZIP, address, or saved alias such as home/work/school."
                        },
                        "timeframe": {
                            "type": "string",
                            "enum": ["forecast", "hourly"],
                            "description": "Use 'forecast' for the standard period forecast or 'hourly' for the next several hourly slices."
                        },
                        "include_alerts": {
                            "type": "boolean",
                            "description": "Whether to include active NOAA/NWS weather alerts for that area."
                        }
                    }
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "set_weather_location_alias",
                "description": "Save or update a persistent weather alias like 'home', 'work', or 'school' from a natural-language location. Use this when the user explicitly asks to set, save, update, or change one of their named places.",
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
                "description": "Delete a saved weather alias like 'home', 'work', or 'school'. Use this when the user explicitly asks to clear, remove, or delete a saved location alias.",
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
                "description": "List the saved persistent weather aliases like home, work, school, or office.",
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
                "description": "Discovery tool for FRED. Use this FIRST when the user wants macroeconomic data but you do not know the exact FRED series ID yet. Search by topic or keyword, then inspect the returned candidate IDs and choose the best one. After this, call `get_fred_series_observations` with the chosen series ID. Do NOT use this tool when the user already gave you a specific FRED series ID.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Keywords such as 'core CPI', 'unemployment rate', 'real GDP', or '2 year treasury yield'."},
                        "limit": {"type": "integer", "description": "Optional. Number of results to return, up to 12."}
                    },
                    "required": ["query"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_fred_series_observations",
                "description": "Data retrieval tool for FRED. Use this when you already know the exact FRED series ID, either because the user gave it to you directly or because you just discovered it with `search_fred_series`. Use it to pull recent or historical observations, metadata, units, and frequency. If you do not know the correct FRED ID yet, call `search_fred_series` first instead of guessing.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "series_id": {"type": "string", "description": "A FRED series ID such as CPIAUCSL, UNRATE, FEDFUNDS, GDPC1, or DGS10."},
                        "observation_start": {"type": "string", "description": "Optional start date in YYYY-MM-DD format."},
                        "observation_end": {"type": "string", "description": "Optional end date in YYYY-MM-DD format."},
                        "units": {"type": "string", "description": "Optional FRED units transform such as lin, chg, pch, or pc1."},
                        "frequency": {"type": "string", "description": "Optional FRED frequency aggregation like d, w, bw, m, q, or a."},
                        "limit": {"type": "integer", "description": "Optional. Number of returned observations, up to 24."}
                    },
                    "required": ["series_id"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "search_imf_indicators",
                "description": "Discovery tool for IMF DataMapper. Use this FIRST when the user wants IMF or cross-country macro data but you do not know the exact IMF indicator code yet. Search by concept, then choose the best returned code. After this, call `get_imf_datamapper_series` with the chosen code. Do NOT use this tool when the user already provided a specific IMF indicator code.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Keywords such as 'real gdp growth', 'inflation', 'government debt', or 'current account'."},
                        "limit": {"type": "integer", "description": "Optional. Number of results to return, up to 12."}
                    },
                    "required": ["query"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_imf_datamapper_series",
                "description": "Data retrieval tool for IMF DataMapper. Use this when you already know the exact IMF indicator code, either because the user supplied it or because you discovered it with `search_imf_indicators`. Use it to compare one or more countries across time. If you do not know the correct IMF indicator code yet, call `search_imf_indicators` first instead of guessing.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "indicator": {"type": "string", "description": "An IMF indicator code such as NGDP_RPCH, PCPIPCH, or GGXWDG_NGDP."},
                        "countries": {"type": "string", "description": "Comma-separated ISO-3 country codes such as USA,CAN,MEX. Defaults to USA."},
                        "start_year": {"type": "integer", "description": "Optional start year such as 2015."},
                        "end_year": {"type": "integer", "description": "Optional end year such as 2026."}
                    },
                    "required": ["indicator"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_stock_snapshot",
                "description": "Use this for current market snapshots and basic fundamentals for a stock or ETF ticker. It is the correct tool when the user asks for current price, intraday high or low, 52-week range, market cap, EBITDA, valuation context, or recent earnings details. If the user instead wants a sequence of recent historical daily prices or OHLCV rows, use `get_stock_price_history`.",
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
                "description": "Use this for recent historical daily price data for a stock or ETF ticker. It returns daily open, high, low, close, and volume rows. Use this when the user wants recent price action, a trading range over time, OHLCV history, or multiple daily closes. If the user instead wants a current quote or fundamentals like EBITDA or market cap, use `get_stock_snapshot`.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Ticker symbol such as AAPL, MSFT, BRK.B, or SPY."},
                        "outputsize": {"type": "string", "description": "Optional. Use 'compact' for recent history or 'full' for full daily history."},
                        "limit": {"type": "integer", "description": "Optional. Number of daily rows to return, up to 30."}
                    },
                    "required": ["symbol"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_bond_market_dashboard",
                "description": "High-level bond-market bundle. Use this FIRST for broad questions about the bond market, yields, the yield curve, credit spreads, or how bonds relate to the economy. This tool returns a curated pack of relevant series so you do not have to discover each FRED ID one by one. After reading it, explain the current bond-market regime and how it relates to policy, growth, labor, and equities.",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_inflation_dashboard",
                "description": "High-level inflation bundle. Use this FIRST for broad inflation questions when you need headline and core inflation context plus market-based inflation expectations. This tool is preferred over manually searching multiple inflation series one by one unless the user explicitly requests a particular FRED series ID.",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_us_macro_dashboard",
                "description": "High-level U.S. macro bundle. Use this FIRST for broad questions about the overall U.S. economy, growth, labor, activity, and policy context. This tool returns a curated macro dashboard so you can ground your answer in multiple datasets before explaining what they imply.",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_equity_market_dashboard",
                "description": "High-level equity-market bundle. Use this FIRST for broad questions about the stock market, market performance, risk sentiment, and cross-asset context. This tool returns a curated pack of equity, volatility, rates, credit, and dollar indicators. If the user asks about a specific stock ticker instead of the broad market, use `get_stock_snapshot` or `get_stock_price_history` instead.",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_global_macro_dashboard",
                "description": "High-level global macro bundle. Use this FIRST for broad questions about the global economy, cross-country growth, inflation, labor conditions, public debt, or external balances. This tool returns a curated IMF-based cross-country dashboard so you can ground global-macro answers in structured international data before explaining what it implies.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "countries": {"type": "string", "description": "Optional comma-separated ISO-3 country codes. Defaults to USA,CHN,EAQ,JPN,GBR,IND."},
                        "start_year": {"type": "integer", "description": "Optional start year for the comparison window. Defaults to 2022."},
                        "end_year": {"type": "integer", "description": "Optional end year for the comparison window. Defaults to the current year."}
                    }
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_housing_consumer_dashboard",
                "description": "High-level housing-and-consumer bundle. Use this FIRST for broad questions about housing, affordability, construction, household spending, consumer credit, or the health of the consumer. This tool returns a curated dashboard covering mortgage rates, home prices, housing activity, consumer spending, and credit stress so you can explain the household side of the economy with data.",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_labor_market_dashboard",
                "description": "High-level labor-market bundle. Use this FIRST for broad questions about jobs, unemployment, layoffs, hiring, participation, quits, or wage growth. This tool returns a curated labor dashboard so you can ground labor-market answers in multiple datasets before explaining what they imply.",
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
                        "description": "The exact fact, preference, or instruction to remember (e.g. 'User prefers tabs over spaces')."
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
                "description": "List all active environments configured in Portainer. Use this to find the target environment names and IDs.",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "list_portainer_containers",
                "description": "List all containers (running and stopped) in a specific Portainer environment.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "environment_name": {
                            "type": "string",
                            "description": "The exact name of the Portainer environment (e.g., 'emeryverse', 'thegrand')."
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
                "description": "Update, recreate, and upgrade a specific container in a Portainer environment. This stops, pulls the latest image, deletes, and recreates the container, preserving its original configuration. WARNING: This is a powerful administrative action. DO NOT invoke this tool unless the user has explicitly asked you to update, restart, or recreate a container.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "environment_name": {
                            "type": "string",
                            "description": "The exact name of the Portainer environment (e.g., 'emeryverse', 'thegrand')."
                        },
                        "container_name": {
                            "type": "string",
                            "description": "The name of the container to update (e.g., 'seerr', 'plex')."
                        }
                    },
                    "required": ["environment_name", "container_name"]
                }
            }
        }
    ])


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
                        },
                        "target_user": {
                            "type": "string",
                            "description": "Optional name or alias of the family member this job/reminder is targeted at (e.g. 'Alice', 'Bob', or 'both'). If specified, the bot will run the job under that user's context and explicitly tag them in the chat notification."
                        },
                        "route_to_routines": {
                            "type": "boolean",
                            "description": "Optional. If true (default for group chats), the scheduled repeating routine is routed to the designated routines topic. If false (default for DMs), it is sent to the original chat/topic where it was scheduled."
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

AVAILABLE_TOOLS["delegate_to_coprocessor"] = delegate_to_coprocessor
tools_schema.append({
    "type": "function",
    "function": {
        "name": "delegate_to_coprocessor",
        "description": "Send a lightweight sub-task, summarization, formatting, or text extraction to the fast secondary model (coprocessor). MANDATORY to use this to offload processing, clean up large text blocks, or analyze documents when input exceeds 1,500 characters, as your main context is limited and expensive.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_prompt": {
                    "type": "string",
                    "description": "The specific instruction for the coprocessor (e.g., 'Extract all dates and times', 'Summarize this page')."
                },
                "content_to_process": {
                    "type": "string",
                    "description": "The target text block, CSV data, email, or webpage content to process."
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
        "description": "Reacts to a specific message in the chat with an emoji. Use this to express reactions (e.g. thumbs up, heart, laugh) when a full text response is not needed, or in addition to text. Use reactions sparingly and only when highly natural.",
        "parameters": {
            "type": "object",
            "properties": {
                "emoji": {
                    "type": "string",
                    "description": "The emoji to react with. Must be one of standard Telegram reaction emojis: '👍', '👎', '❤️', '🔥', '👏', '😂', '😮', '😢', '🎉', '🤔', '👀'."
                },
                "message_id": {
                    "type": "integer",
                    "description": "Optional. The ID of the message to react to. If omitted, defaults to the latest user message in history."
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
        "description": "Directs the bot's final response in this turn to reply directly to a specific previous message ID in the thread, instead of replying to the latest user message.",
        "parameters": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "integer",
                    "description": "The message ID to reply to."
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
        "description": "Sends a Telegram sticker to the chat. You can specify a standard emoji (e.g. '👍', '❤️', '🔥') to look up a sticker in your library, or pass a direct sticker file ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "sticker_id_or_emoji": {
                    "type": "string",
                    "description": "The emoji (e.g. '👍') or sticker file ID to send."
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
        "description": "Sends a GIF (animation) to the chat. You can pass a direct URL to a .gif / .mp4 file, or a search query (e.g. 'happy dance', 'confused') to automatically search and send a matching GIF.",
        "parameters": {
            "type": "object",
            "properties": {
                "query_or_url": {
                    "type": "string",
                    "description": "The GIF search query or a direct GIF URL to send."
                }
            },
            "required": ["query_or_url"]
        }
    }
})

TOOL_STATUS_MESSAGES = {
    "delegate_to_coprocessor": f"{MODEL_NAME} is delegating a task to the coprocessor...",
    "save_user_memory": f"{MODEL_NAME} is writing this down in memory...",
    "web_search": f"{MODEL_NAME} is surfing the web...",
    "get_calendar_events": f"{MODEL_NAME} is checking your calendar...",
    "get_nest_thermostats": f"{MODEL_NAME} is checking the Nest thermostat status...",
    "set_nest_thermostat_mode": f"{MODEL_NAME} is changing the Nest thermostat mode...",
    "set_nest_thermostat_temperature": f"{MODEL_NAME} is adjusting the Nest thermostat temperature...",
    "get_noaa_weather": f"{MODEL_NAME} is looking outside...",
    "set_weather_location_alias": f"{MODEL_NAME} is saving a weather location...",
    "remove_weather_location_alias": f"{MODEL_NAME} is clearing a weather location...",
    "list_weather_location_aliases": f"{MODEL_NAME} is checking saved weather locations...",
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
    "search_fred_series": f"{MODEL_NAME} is searching the FRED database...",
    "get_fred_series_observations": f"{MODEL_NAME} is pulling FRED economic data...",
    "search_imf_indicators": f"{MODEL_NAME} is searching IMF indicators...",
    "get_imf_datamapper_series": f"{MODEL_NAME} is pulling IMF economic data...",
    "get_stock_snapshot": f"{MODEL_NAME} is checking the market...",
    "get_stock_price_history": f"{MODEL_NAME} is pulling stock price history...",
    "get_bond_market_dashboard": f"{MODEL_NAME} is assembling a bond market dashboard...",
    "get_inflation_dashboard": f"{MODEL_NAME} is assembling an inflation dashboard...",
    "get_us_macro_dashboard": f"{MODEL_NAME} is assembling a U.S. macro dashboard...",
    "get_equity_market_dashboard": f"{MODEL_NAME} is assembling an equity market dashboard...",
    "get_global_macro_dashboard": f"{MODEL_NAME} is assembling a global macro dashboard...",
    "get_housing_consumer_dashboard": f"{MODEL_NAME} is assembling a housing and consumer dashboard...",
    "get_labor_market_dashboard": f"{MODEL_NAME} is assembling a labor market dashboard...",
    "get_reolink_snapshot": f"{MODEL_NAME} is investigating a bump in the night...",
    "get_available_cameras": f"{MODEL_NAME} is reading your camera configuration...",
    "get_camera_security_log": f"{MODEL_NAME} is reviewing the security log...",
    "add_scheduled_job": f"{MODEL_NAME} is scheduling a job...",
    "list_scheduled_jobs": f"{MODEL_NAME} is retrieving scheduled jobs...",
    "remove_scheduled_job": f"{MODEL_NAME} is removing a scheduled job...",
    "list_portainer_environments": f"{MODEL_NAME} is retrieving Portainer environments...",
    "list_portainer_containers": f"{MODEL_NAME} is listing Portainer containers...",
    "update_portainer_container": f"{MODEL_NAME} is updating a container in Portainer..."
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
    ctx_size = OLLAMA_NUM_CTX

    
    # Find the latest user query and sender info from the history buffer
    user_query = ""
    sender_user_id = None
    for msg in reversed(history_buffer):
        if msg.get("role") == "user" and not msg.get("is_heartbeat_trigger") and not msg.get("is_reaction_trigger"):
            user_query = msg.get("content", "")
            sender_user_id = msg.get("user_id")
            break
            
    if sender_user_id is not None:
        globals.current_user_id.set(sender_user_id)
    else:
        sender_user_id = globals.current_user_id.get()
        
    system_msg = {"role": "system", "content": get_current_system_prompt(user_query, sender_user_id)}
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
                content_str = " ".join(text_parts) if text_parts else "[Sent an image]"
            elif isinstance(content, str):
                if len(content) > 5000 and not any(c.isspace() for c in content[1000:3000]):
                    content_str = "[Image base64 data removed]"
                else:
                    if msg.get("role") == "assistant":
                        content_str = clean_thinking_tags(content)
                    else:
                        content_str = content
            else:
                content_str = str(content)
                
            # Append message details (ID, Replies, Reactions) to the content for LLM awareness
            msg_details = []
            if msg.get("message_id"):
                msg_details.append(f"ID: {msg['message_id']}")
            if msg.get("reply_to_message_id"):
                msg_details.append(f"Replying to: {msg['reply_to_message_id']}")
                
            reactions = msg.get("reactions", {})
            reaction_parts = []
            if reactions.get("user"):
                reaction_parts.append(f"User: {', '.join(reactions['user'])}")
            if reactions.get("assistant"):
                reaction_parts.append(f"Emery: {', '.join(reactions['assistant'])}")
            if reaction_parts:
                msg_details.append(f"Reactions: {', '.join(reaction_parts)}")
                
            if msg_details:
                prefix = f"[{' | '.join(msg_details)}] "
                clean_msg["content"] = prefix + content_str
            else:
                clean_msg["content"] = content_str
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
                "num_gpu": 0,
                "num_thread": 16
            }
        }
        
        if tools_schema:
            payload["tools"] = tools_schema
 
        try:
            logging.info(f"🤖 ENGINE: Thinking... (loop {loop_count+1}/{TOOL_LOOP})")
            async with globals.main_model_lock:
                r = await globals.http_client.post(url, json=payload, timeout=900)
            
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
                    
                    if fn not in ("react_to_message", "reply_to_message", "send_sticker", "send_gif"):
                        status_msg = TOOL_STATUS_MESSAGES.get(fn, f"Emery is using {fn}...")
                        chat_id = globals.TARGET_CHAT_ID.get()
                        thread_id = normalize_message_thread_id(chat_id, globals.CURRENT_THREAD_ID.get())
                        if chat_id is not None:
                            try:
                                await globals.application_bot.send_message(
                                    chat_id=chat_id,
                                    text=f"<i>{status_msg}</i>",
                                    parse_mode="HTML",
                                    message_thread_id=thread_id,
                                )
                            except BadRequest as e:
                                logging.warning(
                                    "⚠️ ENGINE: Skipping tool status message for chat_id=%s thread_id=%s: %s",
                                    chat_id,
                                    thread_id,
                                    e,
                                )
                            except Exception as e:
                                logging.error(
                                    "❌ ENGINE: Unexpected error sending tool status message to chat_id=%s thread_id=%s: %s",
                                    chat_id,
                                    thread_id,
                                    e,
                                    exc_info=True,
                                )
                    
                    logging.info(f"🔧 TOOL: {fn} | Args: {format_logging_payload(args)}")
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
            # Strip hallucinated [ID: ...] prefixes that the model imitated from history formatting
            content = re.sub(r'(</think>\s*)\[ID:\s*\d+[^\]]*\]\s*', r'\1', content, flags=re.IGNORECASE)
            content = re.sub(r'^\s*\[ID:\s*\d+[^\]]*\]\s*', '', content, flags=re.IGNORECASE)
            
            # Normalize Gemma 4 reasoning tags into standard <think> tags
            content = normalize_gemma_thinking(content)

            reasoning = msg.get('thinking', "") or msg.get('reasoning', "")
            if reasoning:
                reasoning = re.sub(r'^\s*\[ID:\s*\d+[^\]]*\]\s*', '', reasoning, flags=re.IGNORECASE)
            
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
