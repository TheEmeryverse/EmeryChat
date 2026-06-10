import hashlib
import json
import logging
import re

from telegram.error import BadRequest

from emery.config import MAIN_MODEL_URL, MODEL_ID, TOOL_LOOP, MODEL_NAME, THINK
from emery import tool_registry
import emery.globals as globals
from emery.helpers import get_stable_system_prompt, normalize_gemma_thinking, clean_thinking_tags
from emery.logging_utils import format_logging_payload
from emery.telegram_utils import normalize_message_thread_id

AVAILABLE_TOOLS = tool_registry.AVAILABLE_TOOLS
tools_schema = tool_registry.tools_schema

def _strip_id_prefix(text: str) -> str:
    return re.sub(r'^\s*\[ID:\s*\d+[^\]]*\]\s*', '', text or '', flags=re.IGNORECASE)


def _extract_thinking_blocks(content: str) -> tuple[list[str], str]:
    if not content:
        return [], ""

    normalized = normalize_gemma_thinking(content)
    pattern = re.compile(r'<[tT]hink>(.*?)</[tT]hink>', re.DOTALL)
    thoughts = [match.strip() for match in pattern.findall(normalized) if match.strip()]
    cleaned = pattern.sub('', normalized).strip()
    return thoughts, cleaned


def _format_thinking_turn(loop_count: int, phase: str, thought: str) -> str:
    thought = (thought or "").strip()
    if not thought:
        return ""
    return f"Turn {loop_count + 1}\n{phase}\n\n{thought}"


def _format_tool_timeline_entry(fn: str) -> str:
    return f"{MODEL_NAME} used {fn}"


def _extract_response_message(response_json: dict) -> dict:
    choices = response_json.get("choices")
    if isinstance(choices, list) and choices:
        return choices[0].get("message") or {}
    return response_json.get("message", {})


def _normalize_tool_arguments(arguments):
    if isinstance(arguments, str):
        try:
            return json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError:
            logging.warning("⚠️ ENGINE: Tool arguments were not valid JSON: %r", arguments[:200])
            return {}
    return arguments or {}


def _stable_json(data) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_prompt_part(messages: list[dict], chat_template_kwargs: dict | None) -> str:
    hash_payload = {
        "messages": messages,
        "chat_template_kwargs": chat_template_kwargs or {},
    }
    return hashlib.sha256(_stable_json(hash_payload).encode("utf-8")).hexdigest()[:16]


def _hash_stable_data(data) -> str:
    return hashlib.sha256(_stable_json(data).encode("utf-8")).hexdigest()[:16]


def _approx_prompt_chars(messages: list[dict]) -> int:
    return len(_stable_json(messages))


def _truncate_tool_content(content: str, max_len: int = 500) -> str:
    if len(content) <= max_len:
        return content
    return content[:max_len] + f"\n\n[... Tool output truncated. {len(content) - max_len} characters omitted ...]"


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


# --- THE UNIFIED ENGINE ---
async def emery_engine(history_buffer, model_to_use=MODEL_ID, allow_tools=True):
    url = MAIN_MODEL_URL
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
        
    chat_template_kwargs = {"enable_thinking": bool(THINK)}
    stable_prefix = [{"role": "system", "content": get_stable_system_prompt()}]
    voice_sent_via_tool = False
    thinking_timeline = []
    
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
        full_context = stable_prefix + ollama_history
        stable_prefix_hash = _hash_prompt_part(stable_prefix, chat_template_kwargs)
        tool_schema_hash = _hash_stable_data(tools_schema if allow_tools and tools_schema else [])
        request_static_hash = _hash_stable_data({
            "model": model_to_use,
            "url": url,
            "chat_template_kwargs": chat_template_kwargs,
            "tools": tools_schema if allow_tools and tools_schema else [],
        })
        prompt_chars = _approx_prompt_chars(full_context)
        first_roles = ",".join(msg.get("role", "?") for msg in full_context[:5])
        last_roles = ",".join(msg.get("role", "?") for msg in full_context[-5:])
        logging.info(
            "🧩 PROMPT CACHE: stable_prefix_hash=%s tool_schema_hash=%s request_static_hash=%s tools_count=%d stable_messages=%d dynamic_messages=%d history_messages=%d total_messages=%d approx_chars=%d first_roles=%s last_roles=%s history_trimmed=%s model=%s url=%s thinking=%s",
            stable_prefix_hash,
            tool_schema_hash,
            request_static_hash,
            len(tools_schema) if allow_tools and tools_schema else 0,
            len(stable_prefix),
            0,
            len(ollama_history),
            len(full_context),
            prompt_chars,
            first_roles,
            last_roles,
            False,
            model_to_use,
            url,
            THINK,
        )
        
        payload = {
            "model": model_to_use,
            "messages": full_context,
            "stream": False,
            "max_tokens": 8192,
            "temperature": 0.8,
            "top_p": 0.95,
            "top_k": 20,
            "chat_template_kwargs": chat_template_kwargs,
        }
        
        if allow_tools and tools_schema:
            payload["tools"] = tools_schema
 
        try:
            logging.info(f"🤖 ENGINE: Thinking... (loop {loop_count+1}/{TOOL_LOOP})")
            async with globals.main_model_lock:
                r = await globals.http_client.post(url, json=payload, timeout=900)
            
            if r.status_code != 200:
                logging.error(f"❌ ENGINE: Main model returned {r.status_code} — {r.text[:200]}")
                return "Main model connection error.", False
 
            res = r.json()
            msg = _extract_response_message(res)
            raw_content = msg.get('content') or ""
            content_thoughts, cleaned_msg_content = _extract_thinking_blocks(raw_content)
            reasoning = msg.get('reasoning_content', "") or msg.get('thinking', "") or msg.get('reasoning', "")
            if reasoning:
                reasoning = _strip_id_prefix(reasoning)
                thinking_timeline.append(_format_thinking_turn(loop_count, "Reasoning", reasoning))
            for thought in content_thoughts:
                thinking_timeline.append(_format_thinking_turn(loop_count, "Inline thought", thought))

            if allow_tools and msg.get("tool_calls"):
                assistant_tool_msg = {
                    "role": msg.get("role", "assistant"),
                    "content": cleaned_msg_content if cleaned_msg_content != raw_content else msg.get("content"),
                    "tool_calls": msg["tool_calls"],
                }
                if cleaned_msg_content != raw_content:
                    assistant_tool_msg["content"] = cleaned_msg_content
                history_buffer.append(assistant_tool_msg)
                ollama_history.append(assistant_tool_msg)
                for tc in assistant_tool_msg['tool_calls']:
                    fn = tc['function']['name']
                    args = _normalize_tool_arguments(tc['function'].get('arguments', {}))
                    
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
                    thinking_timeline.append(_format_tool_timeline_entry(fn))
                    if fn == "speak_message": 
                        voice_sent_via_tool = True
                    
                    result = await AVAILABLE_TOOLS[fn](**args) if args else await AVAILABLE_TOOLS[fn]()
                    
                    tool_response = {
                        "role": "tool",
                        "content": _truncate_tool_content(str(result)),
                        "name": fn
                    }
                    if "id" in tc:
                        tool_response["tool_call_id"] = tc["id"]
                        
                    history_buffer.append(tool_response)
                    ollama_history.append(tool_response)
                continue
            
            content = cleaned_msg_content
            # Strip hallucinated [ID: ...] prefixes that the model imitated from history formatting
            content = re.sub(r'(</think>\s*)\[ID:\s*\d+[^\]]*\]\s*', r'\1', content, flags=re.IGNORECASE)
            content = re.sub(r'^\s*\[ID:\s*\d+[^\]]*\]\s*', '', content, flags=re.IGNORECASE)

            thinking_char_count = sum(len(entry) for entry in thinking_timeline if entry)
            logging.info(f"🤖 ENGINE: Response ready — {len(content)} chars" + (f", {thinking_char_count} chars reasoning" if thinking_char_count else ""))

            thinking_payload = "\n\n".join(entry for entry in thinking_timeline if entry)
            if thinking_payload:
                start_think_tag = "<" + "think" + ">"
                end_think_tag = "</" + "think" + ">"
                final_text = f"{start_think_tag}\n{thinking_payload}\n{end_think_tag}\n{content}"
            else:
                final_text = content

            return final_text, voice_sent_via_tool
            
        except Exception as e:
            logging.error(f"❌ ENGINE: Crash — {e}", exc_info=True)
            return "EMERYCHAT engine failure.", False
            
    return "Timeout.", False
