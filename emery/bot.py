import re
import logging
import asyncio
import base64
from datetime import datetime, time
from collections import deque

from telegram import Update, ReplyParameters
from telegram.ext import ContextTypes
from telegram.error import TimedOut

from emery.config import (
    MODEL_ID, MODEL_NAME, USER_TIMEZONE, VISION_MODEL_ID, USER_BIRTHDAY,
    ENABLE_HEARTBEAT, HEARTBEAT_INTERVAL_SECONDS, HEARTBEAT_SILENCE_THRESHOLD_SECONDS,
    HEARTBEAT_SILENT_RETRY_SECONDS, HEARTBEAT_PROACTIVE_COOLDOWN_SECONDS,
    HEARTBEAT_DAILY_PROACTIVE_LIMIT, HEARTBEAT_SLEEP_START, HEARTBEAT_SLEEP_END,
    ALLOWED_USER_IDS, ENABLE_WEATHER,
    TELEGRAM_GROUP_CHAT_ID, CHAT_TOPIC_ID, TELEGRAM_STICKER_SET,
    ALLOW_UNRESTRICTED_TELEGRAM_ACCESS
)
import emery.globals as globals
from emery.helpers import (
    emery_format, transcribe_audio, compress_image_bytes,
    get_image_description, clean_thinking_tags, telegram_escape, get_current_system_prompt
)
from emery.logging_utils import safe_preview
from emery.memory import retrieve_relevant_memories, wipe_memory
from emery.engine import emery_engine
from emery.telegram_delivery import send_split_html_message
from emery.tools import get_noaa_weather_alerts, get_voice_audio
from emery.telegram_utils import normalize_message_thread_id


_heartbeat_last_evaluation = {}
_heartbeat_last_proactive = {}
_heartbeat_daily_proactive_counts = {}

_HEARTBEAT_HOOK_RE = re.compile(
    r"\b("
    r"follow up|circle back|later|tomorrow|next week|next month|remind|remember|"
    r"need to|should|decide|decision|plan|project|stuck|worried|stress|"
    r"frustrated|excited|appointment|meeting|deadline|waiting|promised|you said"
    r")\b|[?？]",
    re.IGNORECASE,
)

_HEARTBEAT_EXCLUDED_CONTEXT_RE = re.compile(
    r"\b(security|camera|reolink|motion alert|snapshot|news|headline|reuters|fox news)\b",
    re.IGNORECASE,
)

def is_user_allowed(update: Update) -> bool:
    """Checks if the user interacting with the bot is whitelisted in TELEGRAM_ALLOWED_USERS."""
    user = update.effective_user
    if not user:
        return False
        
    if not ALLOWED_USER_IDS:
        return bool(ALLOW_UNRESTRICTED_TELEGRAM_ACCESS)
    return user.id in ALLOWED_USER_IDS


def validate_telegram_access_policy() -> None:
    """Logs the effective Telegram access posture at startup."""
    if ALLOWED_USER_IDS:
        logging.info("🔐 TELEGRAM ACCESS: allowlist enabled for %s user(s).", len(ALLOWED_USER_IDS))
        return

    if ALLOW_UNRESTRICTED_TELEGRAM_ACCESS:
        logging.warning(
            "⚠️ TELEGRAM ACCESS: unrestricted Telegram access is explicitly enabled. "
            "Anyone who can message this bot can use enabled tools."
        )
        return

    logging.critical(
        "🚫 TELEGRAM ACCESS: no allowed_user_ids are configured and unrestricted access is disabled. "
        "The bot will start but ignore all Telegram users until config/users.json includes allowed user IDs "
        "or ALLOW_UNRESTRICTED_TELEGRAM_ACCESS=true is set."
    )


async def validate_telegram_routing(application) -> bool:
    """Checks the configured Telegram group/topic routing and logs actionable warnings."""
    from emery.config import SECURITY_TOPIC_ID, ROUTINES_TOPIC_ID, CHAT_TOPIC_ID

    group_chat_id = TELEGRAM_GROUP_CHAT_ID
    topic_ids = {
        "security_topic_id": SECURITY_TOPIC_ID,
        "routines_topic_id": ROUTINES_TOPIC_ID,
        "chat_topic_id": CHAT_TOPIC_ID,
    }

    configured_topics = {
        name: topic_id
        for name, topic_id in topic_ids.items()
        if topic_id is not None
    }

    if group_chat_id is None:
        if configured_topics:
            logging.warning(
                "⚠️ TELEGRAM ROUTING: topic IDs are configured (%s), but telegram.group_chat_id is missing. "
                "Security alerts and scheduled routines will fall back to in-memory chat state until the bot is restarted in a chat.",
                ", ".join(f"{name}={topic_id}" for name, topic_id in configured_topics.items()),
            )
        else:
            logging.warning(
                "⚠️ TELEGRAM ROUTING: telegram.group_chat_id is not configured. "
                "Security alerts, routines, and heartbeat routing may be unstable until a chat is established."
            )
        return False

    try:
        chat = await application.bot.get_chat(group_chat_id)
        chat_title = getattr(chat, "title", None) or getattr(chat, "full_name", None) or str(group_chat_id)
        chat_type = getattr(chat, "type", "unknown")
        is_forum = bool(getattr(chat, "is_forum", False))

        logging.info(
            "📡 TELEGRAM ROUTING: resolved group chat %s (type=%s, forum=%s)",
            chat_title,
            chat_type,
            is_forum,
        )

        if configured_topics and not is_forum:
            logging.warning(
                "⚠️ TELEGRAM ROUTING: topic IDs are configured for chat_id=%s, but Telegram reports the chat is not a forum. "
                "Topic sends may fail until the group is converted to a forum or the topic IDs are removed.",
                group_chat_id,
            )

        if len(configured_topics) > 1:
            duplicate_ids = {}
            for name, topic_id in configured_topics.items():
                duplicate_ids.setdefault(topic_id, []).append(name)
            duplicate_ids = {topic_id: names for topic_id, names in duplicate_ids.items() if len(names) > 1}
            if duplicate_ids:
                logging.warning(
                    "⚠️ TELEGRAM ROUTING: some topic IDs are reused across multiple roles: %s",
                    ", ".join(f"{topic_id}={names}" for topic_id, names in duplicate_ids.items()),
                )

        if not configured_topics:
            logging.warning(
                "⚠️ TELEGRAM ROUTING: telegram.group_chat_id is configured, but no topic IDs are set. "
                "That's fine for a non-forum group, but forum-specific delivery will use the chat root."
            )

        return True
    except Exception as e:
        logging.warning(
            "⚠️ TELEGRAM ROUTING: unable to verify configured group chat %s via Telegram API: %s",
            group_chat_id,
            e,
        )
        return False

# --- TELEGRAM HANDLERS ---
async def handle_clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram handler for /clear command."""
    if not is_user_allowed(update):
        return
    chat_id = update.effective_chat.id
    if chat_id in globals.chat_histories:
        globals.chat_histories[chat_id].clear()
    await update.message.reply_text("Context cleared.")

async def handle_wipe_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram handler for /wipe command."""
    if not is_user_allowed(update):
        return
        
    globals.current_user_id.set(update.effective_user.id)
    if wipe_memory(update.effective_user.id):
        await update.message.reply_text("🧠 Memory wiped successfully and re-initialized to baseline template.")
    else:
        await update.message.reply_text("❌ Failed to wipe memory due to a filesystem error.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
        
    if not is_user_allowed(update):
        return
        
    globals.current_user_id.set(update.effective_user.id)
    chat_id = update.effective_chat.id
    globals.TARGET_CHAT_ID.set(chat_id)
    globals.CURRENT_THREAD_ID.set(
        normalize_message_thread_id(
            chat_id,
            update.message.message_thread_id if update.message else None,
        )
    )
    
    # Dynamically associate user chat ID with any pending jobs (like default briefings)
    from emery.scheduler import update_jobs_with_chat_id
    update_jobs_with_chat_id(chat_id)
    
    if chat_id not in globals.chat_histories: 
         globals.chat_histories[chat_id] = deque()
    
    # Clear any stale custom reply targets for this turn
    globals.chat_reply_targets.pop(chat_id, None)

    is_input_voice = False
    model_to_use = MODEL_ID
    
    now_str = datetime.now(USER_TIMEZONE).strftime("%A, %B %d, %Y at %I:%M %p")
    sender_name = update.effective_user.first_name or "User"

    if update.message.voice:
        is_input_voice = True
        v_file = await update.message.voice.get_file()
        transcription = await transcribe_audio(await v_file.download_as_bytearray())
        if not transcription: 
            return
        content_text = transcription
    elif update.message.photo:
        p_file = await update.message.photo[-1].get_file()
        photo_bytes = await p_file.download_as_bytearray()
        compressed_bytes = compress_image_bytes(photo_bytes)
        b64 = base64.b64encode(compressed_bytes).decode('utf-8')
        caption = update.message.caption or ""
        
        await update.message.reply_chat_action("typing")
        description = await get_image_description(b64, caption)
        
        content_text = "sent an image."
        if caption:
            content_text += f" Caption: {caption}"
        content_text += f"\nImage Description: {description}"
    elif update.message.sticker:
        sticker = update.message.sticker
        emoji = sticker.emoji or ""
        file_id = sticker.file_id
        set_name = sticker.set_name or "Unknown"
        
        if emoji:
            globals.learned_stickers[emoji] = file_id
            
        content_text = f"sent a sticker: {emoji} (File ID: {file_id}, Set: {set_name})"
    elif update.message.animation:
        anim = update.message.animation
        file_id = anim.file_id
        content_text = f"sent a GIF / Animation (File ID: {file_id})"
    elif update.message.document and update.message.document.mime_type == "video/mp4":
        doc = update.message.document
        file_id = doc.file_id
        content_text = f"sent a GIF / Animation (File ID: {file_id})"
    else:
        content_text = update.message.text or "[Non-text message]"
        
    content = f"[{now_str}] {sender_name}: {content_text}"
        
    # Thread Reply Context Builder
    reply_to = update.message.reply_to_message
    reply_info = ""
    if reply_to:
        reply_to_id = reply_to.message_id
        replied_text = ""
        for msg in globals.chat_histories[chat_id]:
            if msg.get("message_id") == reply_to_id or (isinstance(msg.get("message_ids"), list) and reply_to_id in msg["message_ids"]):
                replied_text = msg.get("content", "")
                replied_text = clean_thinking_tags(replied_text)
                break
        if not replied_text:
            replied_text = reply_to.text or "[Non-text message]"
        preview = safe_preview(replied_text, max_len=80)
        reply_info = f" (Replying to message ID {reply_to_id}: '{preview}')"

    runtime_context = await get_current_system_prompt(content_text, update.effective_user.id)
    history_content = f"{runtime_context}\n\n# New User Message\n{content}{reply_info}"

    logging.info(f"💬 USER (chat {chat_id}): {sender_name} -> {safe_preview(content_text, max_len=120)}{reply_info}")
    globals.chat_histories[chat_id].append({
        "role": "user", 
        "content": history_content,
        "message_id": update.message.message_id,
        "user_id": update.effective_user.id,
        "sender_name": sender_name,
        "reply_to_message_id": reply_to.message_id if reply_to else None,
        "message_thread_id": update.message.message_thread_id if update.message else None,
        "timestamp": datetime.now(USER_TIMEZONE)
    })
    
    # Check if this chat is a group chat
    is_group = (chat_id < 0)
    
    # By default, we reply in DMs (positive chat_id)
    should_reply = not is_group
    
    # If it is a group chat, we only reply if:
    # 1. The bot is mentioned (e.g. @EmeryBot)
    # 2. It is a reply to one of the bot's own messages
    # 3. The message starts with a slash command
    if is_group:
        bot_username = (await context.bot.get_me()).username.lower()
        
        # Check mentions in text or caption
        text_lower = ""
        if update.message.text:
            text_lower = update.message.text.lower()
        elif update.message.caption:
            text_lower = update.message.caption.lower()
            
        is_mentioned = f"@{bot_username}" in text_lower
        
        is_reply_to_bot = False
        if update.message.reply_to_message:
            is_reply_to_bot = (update.message.reply_to_message.from_user.id == context.bot.id)
            
        is_command = text_lower.startswith("/")
        
        if is_mentioned or is_reply_to_bot or is_command:
            should_reply = True

    if not should_reply:
        logging.debug(f"🤫 SILENT LISTEN: Recorded group message from {sender_name} (chat {chat_id}) for context, but not replying.")
        return

    # --- DEBOUNCE LOGIC ---
    # Cancel existing debounce task for this chat
    if chat_id in globals.chat_debounce_tasks:
        globals.chat_debounce_tasks[chat_id].cancel()
        logging.debug(f"⏱️ DEBOUNCE: Cancelled timer for chat {chat_id}")

    # Define the worker that will run after CHAT_DEBOUNCE_DELAY seconds
    async def debounce_worker(delay):
        try:
            await asyncio.sleep(delay)
            logging.debug(f"⏱️ DEBOUNCE: Delay of {delay}s expired, processing chat {chat_id}...")
            await run_engine_for_chat(update, context, model_to_use, is_input_voice)
        except asyncio.CancelledError:
            pass
        finally:
            globals.chat_debounce_tasks.pop(chat_id, None)

    # Start the debounce worker
    from emery.config import CHAT_DEBOUNCE_DELAY
    globals.chat_debounce_tasks[chat_id] = asyncio.create_task(debounce_worker(CHAT_DEBOUNCE_DELAY))

async def run_engine_for_chat(update: Update, context: ContextTypes.DEFAULT_TYPE, model_to_use: str, is_input_voice: bool) -> None:
    chat_id = update.effective_chat.id
    
    # Determine final reply target from globals
    reply_target_id = globals.chat_reply_targets.pop(chat_id, None)

    # --- TYPING INDICATOR LOOP ---
    typing_stop = asyncio.Event()

    async def keep_typing():
        while not typing_stop.is_set():
            try:
                await globals.application_bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception as e:
                logging.debug(f"Typing action failed: {e}")
            await asyncio.sleep(4)

    typing_task = asyncio.create_task(keep_typing())

    try:
        from emery.engine import emery_engine
        response_text, voice_sent_via_tool = await emery_engine(globals.chat_histories[chat_id], model_to_use=model_to_use)
    except Exception as e:
        logging.error(f"Error running engine in debounce worker: {e}", exc_info=True)
        response_text = "EMERYCHAT engine failure."
        voice_sent_via_tool = False
    finally:
        typing_stop.set()
        await typing_task

    # --- THINKING SPLITTER LOGIC ---
    start_tag = "<" + "think" + ">"
    end_tag = "</" + "think" + ">"
    pattern = re.escape(start_tag) + r"(.*?)" + re.escape(end_tag)
    thinking_blocks = [
        match.strip()
        for match in re.findall(pattern, response_text, re.DOTALL | re.IGNORECASE)
        if match.strip()
    ]

    clean_response = response_text

    if thinking_blocks:
        clean_response = re.sub(pattern, '', response_text, flags=re.DOTALL | re.IGNORECASE).strip()

    # --- SILENT HANDSHAKE DETECTION ---
    handshake_check = re.sub(r'[^a-zA-Z]', '', clean_response).upper()
    if handshake_check == "DONE":
        logging.debug("🤫 HANDSHAKE: Suppressed text reply (silent check)")
        globals.chat_histories[chat_id].append({
            "role": "assistant",
            "content": response_text,
            "message_thread_id": globals.CURRENT_THREAD_ID.get(),
            "timestamp": datetime.now(USER_TIMEZONE)
        })
        return

    # Display the thinking block if one exists
    if thinking_blocks:
        CHUNK_SIZE = 3900
        thinking_content = "\n\n".join(thinking_blocks)
        chunks = [thinking_content[i:i+CHUNK_SIZE] for i in range(0, len(thinking_content), CHUNK_SIZE)]

        for idx, chunk in enumerate(chunks):
            if len(chunks) > 1:
                header = f"🧠 <b>{telegram_escape(MODEL_NAME)}'s Thought Process (Part {idx+1}/{len(chunks)})</b> (Expand to read):\n"
            else:
                header = f"🧠 <b>{telegram_escape(MODEL_NAME)}'s Thought Process</b> (Expand to read):\n"

            thinking_msg = f"{header}<blockquote expandable><i>{telegram_escape(chunk)}</i></blockquote>"
            await globals.application_bot.send_message(chat_id=chat_id, text=thinking_msg, parse_mode="HTML", message_thread_id=globals.CURRENT_THREAD_ID.get())

    sent_msgs = []
    # --- SINGLE FINAL REPLY DISPATCHER ---
    if is_input_voice and not voice_sent_via_tool:
        await globals.application_bot.send_chat_action(chat_id=chat_id, action="record_voice")
        from emery.tools import get_voice_audio
        v_out = await get_voice_audio(clean_response)
        if v_out:
            reply_params = ReplyParameters(message_id=reply_target_id, allow_sending_without_reply=True) if reply_target_id else None
            sent_msg = await globals.application_bot.send_voice(
                chat_id=chat_id,
                voice=v_out,
                caption="Voice message",
                reply_parameters=reply_params,
                message_thread_id=globals.CURRENT_THREAD_ID.get()
            )
            sent_msgs = [sent_msg] if sent_msg else []
        else:
            sent_msgs = await send_safe_large_message_as_reply(chat_id, emery_format(clean_response), reply_to_message_id=reply_target_id, message_thread_id=globals.CURRENT_THREAD_ID.get())
    else:
        if clean_response:
            sent_msgs = await send_safe_large_message_as_reply(chat_id, emery_format(clean_response), reply_to_message_id=reply_target_id, message_thread_id=globals.CURRENT_THREAD_ID.get())

    # Save the assistant text to history
    assistant_entry = {
        "role": "assistant", 
        "content": response_text,
        "message_thread_id": globals.CURRENT_THREAD_ID.get(),
        "timestamp": datetime.now(USER_TIMEZONE)
    }
    if sent_msgs:
        assistant_entry["message_ids"] = [m.message_id for m in sent_msgs]
        assistant_entry["message_id"] = sent_msgs[-1].message_id
    globals.chat_histories[chat_id].append(assistant_entry)

    # Trigger background topic summarization
    from emery.memory import summarize_topics_background
    last_user_id = None
    for msg in reversed(globals.chat_histories[chat_id]):
        if msg.get("role") == "user" and msg.get("user_id"):
            last_user_id = msg.get("user_id")
            break
    asyncio.create_task(summarize_topics_background(chat_id, last_user_id))

async def send_safe_large_message(update: Update, text: str, reply_to_message_id: int = None):
    """
    Splits extremely long final responses at natural line breaks 
    to prevent Telegram's 4096 character limit crash.
    """
    chat_id = update.effective_chat.id
    thread_id = normalize_message_thread_id(
        chat_id,
        update.message.message_thread_id if update.message else None,
    )
    return await send_split_html_message(
        globals.application_bot,
        chat_id,
        text,
        reply_to_message_id=reply_to_message_id,
        message_thread_id=thread_id,
    )



async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Logs network drops and timeouts cleanly instead of crashing the thread."""
    if isinstance(context.error, TimedOut):
        logging.warning("⚠️ TELEGRAM: API timed out temporarily due to load. The message will retry.")
    else:
        logging.error(f"❌ TELEGRAM: Unhandled API exception: {context.error}", exc_info=True)

# --- REACTION AND HEARTBEAT FUNCTIONALITY ---

async def send_safe_large_message_as_reply(chat_id: int, text: str, reply_to_message_id: int = None, message_thread_id: int = None):
    """Sends a safe split message directly to a chat, replying to a specific message ID."""
    return await send_split_html_message(
        globals.application_bot,
        chat_id,
        text,
        reply_to_message_id=reply_to_message_id,
        message_thread_id=message_thread_id,
    )

async def handle_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram handler for message reaction changes (MessageReactionUpdated)."""
    if not is_user_allowed(update):
        return
        
    reaction_update = update.message_reaction
    if not reaction_update:
        return
        
    chat_id = reaction_update.chat.id
    message_id = reaction_update.message_id
    user = reaction_update.user
    
    is_bot = (user.id == context.bot.id) if user else False
    actor_key = "assistant" if is_bot else "user"
    
    emojis = []
    for r in reaction_update.new_reaction:
        if r.type == "emoji":
            emojis.append(r.emoji)
        elif r.type == "custom_emoji":
            emojis.append("✨") # Use sparkle emoji as placeholder for custom emojis
            
    if chat_id not in globals.chat_histories:
        globals.chat_histories[chat_id] = deque()
        
    found_msg = None
    for msg in globals.chat_histories[chat_id]:
        if msg.get("message_id") == message_id or (isinstance(msg.get("message_ids"), list) and message_id in msg["message_ids"]):
            found_msg = msg
            break
            
    if found_msg:
        logging.debug(f"🎭 REACTION: {actor_key} reaction on {message_id} -> {emojis}")
        globals.chat_histories[chat_id].append({
            "role": "user",
            "content": f"[Reaction update: {actor_key} reacted to message ID {message_id} with {', '.join(emojis) if emojis else 'no reaction'}]",
            "timestamp": datetime.now(USER_TIMEZONE),
        })
        
    # Trigger response evaluation if the user added/changed their reaction
    if not is_bot:
        old_emojis = []
        for r in reaction_update.old_reaction:
            if r.type == "emoji":
                old_emojis.append(r.emoji)
            elif r.type == "custom_emoji":
                old_emojis.append("✨")
                
        if set(emojis) != set(old_emojis):
            if emojis:
                logging.debug(f"🎭 REACTION: Triggering evaluation for reaction {emojis} on {message_id}")
                asyncio.create_task(handle_user_reaction_trigger(chat_id, message_id, emojis, user.id))

async def handle_user_reaction_trigger(chat_id: int, message_id: int, emojis: list[str], user_id: int):
    """Invokes the engine contextually when a user reacts to a message."""
    globals.TARGET_CHAT_ID.set(chat_id)
    globals.current_user_id.set(user_id)
    
    msg_text = ""
    for msg in globals.chat_histories.get(chat_id, []):
        if msg.get("message_id") == message_id or (isinstance(msg.get("message_ids"), list) and message_id in msg["message_ids"]):
            msg_text = msg.get("content", "")
            msg_text = clean_thinking_tags(msg_text)
            break
            
    if not msg_text:
        msg_text = "(older message)"
    else:
        msg_text = msg_text[:100] + "..." if len(msg_text) > 100 else msg_text
        
    emoji_str = ", ".join(emojis)
    trigger_content = (
        f"[System Trigger: User reacted with '{emoji_str}' to the message (ID: {message_id}): '{msg_text}'].\n"
        f"[SYSTEM DIRECTIVE: The user just updated their reaction to an earlier message. "
        f"Decide if a text response or a reaction back is natural. "
        f"If a reaction back is appropriate, use the `react_to_message` tool. "
        f"If no text response is necessary, you MUST reply with exactly 'DONE' to remain silent.]"
    )
    
    globals.current_user_id.set(user_id)
    trigger_msg = {
        "role": "user",
        "content": trigger_content,
        "user_id": user_id,
        "is_reaction_trigger": True,
        "timestamp": datetime.now(USER_TIMEZONE)
    }
    
    globals.chat_histories[chat_id].append(trigger_msg)
    
    typing_stop = asyncio.Event()

    async def keep_typing():
        while not typing_stop.is_set():
            try:
                await globals.application_bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception as e:
                logging.debug(f"Typing action failed: {e}")
            await asyncio.sleep(4)

    typing_task = asyncio.create_task(keep_typing())
    
    try:
        response_text, voice_sent_via_tool = await emery_engine(globals.chat_histories[chat_id])
    finally:
        typing_stop.set()
        await typing_task
        
    start_tag = "<" + "think" + ">"
    end_tag = "</" + "think" + ">"
    pattern = re.escape(start_tag) + r"(.*?)" + re.escape(end_tag)
    clean_response = re.sub(pattern, '', response_text, flags=re.DOTALL | re.IGNORECASE).strip()
    
    handshake_check = re.sub(r'[^a-zA-Z]', '', clean_response).upper()
    if handshake_check == "DONE":
        logging.debug("🤫 REACTION: Suppressed response (model chose silence)")
        return
        
    globals.chat_histories[chat_id].append({
        "role": "assistant",
        "content": response_text,
        "timestamp": datetime.now(USER_TIMEZONE)
    })
    
    try:
        sent_msgs = await send_safe_large_message_as_reply(chat_id, clean_response, message_id)
        if sent_msgs:
            last_entry = globals.chat_histories[chat_id][-1]
            last_entry["message_ids"] = [m.message_id for m in sent_msgs]
            last_entry["message_id"] = sent_msgs[-1].message_id
    except Exception as e:
        logging.error(f"Failed to send reaction reply: {e}")

def _is_heartbeat_sleep_window(now: datetime) -> bool:
    start_h, start_m = map(int, HEARTBEAT_SLEEP_START.split(':'))
    end_h, end_m = map(int, HEARTBEAT_SLEEP_END.split(':'))
    start_time = time(start_h, start_m)
    end_time = time(end_h, end_m)
    curr_time = now.time()

    if start_time <= end_time:
        return start_time <= curr_time <= end_time
    return curr_time >= start_time or curr_time <= end_time


def _heartbeat_daily_count(chat_id: int, now: datetime) -> int:
    date_key = now.date().isoformat()
    state = _heartbeat_daily_proactive_counts.get(chat_id)
    if not state or state.get("date") != date_key:
        state = {"date": date_key, "count": 0}
        _heartbeat_daily_proactive_counts[chat_id] = state
    return state["count"]


def _record_heartbeat_proactive(chat_id: int, now: datetime) -> None:
    _heartbeat_last_proactive[chat_id] = now
    date_key = now.date().isoformat()
    state = _heartbeat_daily_proactive_counts.get(chat_id)
    if not state or state.get("date") != date_key:
        state = {"date": date_key, "count": 0}
        _heartbeat_daily_proactive_counts[chat_id] = state
    state["count"] += 1


def _seconds_since(now: datetime, past: datetime) -> float:
    return max((now - past).total_seconds(), 0)


def _heartbeat_suppression_reason(chat_id: int, now: datetime) -> str:
    if HEARTBEAT_DAILY_PROACTIVE_LIMIT > 0 and _heartbeat_daily_count(chat_id, now) >= HEARTBEAT_DAILY_PROACTIVE_LIMIT:
        return f"daily proactive limit reached ({HEARTBEAT_DAILY_PROACTIVE_LIMIT})"

    last_proactive = _heartbeat_last_proactive.get(chat_id)
    if last_proactive and _seconds_since(now, last_proactive) < HEARTBEAT_PROACTIVE_COOLDOWN_SECONDS:
        return "proactive message cooldown active"

    last_evaluation = _heartbeat_last_evaluation.get(chat_id)
    if last_evaluation and _seconds_since(now, last_evaluation) < HEARTBEAT_SILENT_RETRY_SECONDS:
        return "silent retry cooldown active"

    return ""


def _last_chat_activity(history) -> datetime | None:
    for msg in reversed(history):
        if msg.get("is_heartbeat_trigger") or msg.get("is_reaction_trigger"):
            continue
        if msg.get("role") not in {"user", "assistant"}:
            continue
        timestamp = msg.get("timestamp")
        if timestamp:
            return timestamp
    return None


def _last_user_id_from_history(history) -> int | None:
    for msg in reversed(history):
        if msg.get("role") == "user" and not msg.get("is_heartbeat_trigger") and msg.get("user_id"):
            return msg.get("user_id")
    return globals.current_user_id.get()


def _clean_heartbeat_text(text: str, max_len: int = 220) -> str:
    text = clean_thinking_tags(str(text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0].rstrip() + "..."
    return text


def _is_excluded_heartbeat_context(text: str) -> bool:
    return bool(_HEARTBEAT_EXCLUDED_CONTEXT_RE.search(text or ""))


def _extract_heartbeat_hooks(history, max_hooks: int = 5) -> list[str]:
    hooks = []
    seen = set()
    for msg in reversed(list(history)[-30:]):
        if msg.get("is_heartbeat_trigger") or msg.get("is_reaction_trigger"):
            continue
        if msg.get("role") not in {"user", "assistant"}:
            continue

        content = _clean_heartbeat_text(msg.get("content"))
        if not content or _is_excluded_heartbeat_context(content):
            continue
        if not _HEARTBEAT_HOOK_RE.search(content):
            continue

        sender = msg.get("sender_name") or ("Assistant" if msg.get("role") == "assistant" else "User")
        hook = f"- {sender}: {content}"
        hook_key = hook.lower()
        if hook_key in seen:
            continue
        hooks.append(hook)
        seen.add(hook_key)
        if len(hooks) >= max_hooks:
            break

    hooks.reverse()
    return hooks


def _filter_heartbeat_memory(memory_text: str, max_lines: int = 12) -> str:
    lines = []
    for raw_line in str(memory_text or "").splitlines():
        line = raw_line.strip()
        if not line or _is_excluded_heartbeat_context(line):
            continue
        lines.append(line)
        if len(lines) >= max_lines:
            break
    return "\n".join(lines)


def _format_heartbeat_duration(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


async def build_heartbeat_context_packet(chat_id: int, now: datetime, silence_seconds: float) -> str:
    history = globals.chat_histories.get(chat_id, [])
    hooks = _extract_heartbeat_hooks(history)

    user_id = _last_user_id_from_history(history)
    memory_context = ""
    if user_id:
        try:
            memory_query = (
                "heartbeat check-in: unresolved follow-ups, recent projects, household plans, "
                "important personal context, and natural reasons to circle back"
            )
            memory_context = _filter_heartbeat_memory(await retrieve_relevant_memories(memory_query, user_id))
        except Exception as e:
            logging.warning("⚠️ HEARTBEAT: Unable to retrieve memory context: %s", e)

    weather_alerts = ""
    if ENABLE_WEATHER:
        try:
            weather_alerts = await get_noaa_weather_alerts()
        except Exception as e:
            logging.warning("⚠️ HEARTBEAT: Unable to retrieve weather alerts: %s", e)

    daily_count = _heartbeat_daily_count(chat_id, now)
    packet = [
        "Heartbeat context:",
        f"- Silence duration: {_format_heartbeat_duration(silence_seconds)}",
        f"- Proactive messages sent today: {daily_count} / {HEARTBEAT_DAILY_PROACTIVE_LIMIT}",
        "",
        "Recent conversation hooks:",
        "\n".join(hooks) if hooks else "None found.",
        "",
        "Relevant memory/history:",
        memory_context if memory_context else "None found.",
        "",
        "Weather alerts:",
        weather_alerts if weather_alerts else "None.",
    ]
    return "\n".join(packet)


async def heartbeat_check(context: ContextTypes.DEFAULT_TYPE):
    """Callback for Telegram JobQueue that runs periodically to check if the bot should spontaneously send a message."""
    if not ENABLE_HEARTBEAT:
        return
        
    logging.debug("💓 HEARTBEAT: Checking activity...")
    
    if TELEGRAM_GROUP_CHAT_ID is None:
        logging.debug("💓 HEARTBEAT: TELEGRAM_GROUP_CHAT_ID not set, skipping check.")
        return
    group_chat_id = TELEGRAM_GROUP_CHAT_ID
        
    history = globals.chat_histories.get(group_chat_id)
    if not history:
        return
        
    now = datetime.now(USER_TIMEZONE)
    
    # Check if current time falls within user's sleep window
    try:
        if _is_heartbeat_sleep_window(now):
            logging.debug(f"💓 HEARTBEAT: Suppressed check-in (inside sleep window: {HEARTBEAT_SLEEP_START}-{HEARTBEAT_SLEEP_END})")
            return
    except Exception as e:
        logging.error(f"❌ HEARTBEAT: Error checking sleep window range: {e}")
        
    last_time = _last_chat_activity(history)
    if not last_time:
        return
        
    elapsed = (now - last_time).total_seconds()
    if elapsed > HEARTBEAT_SILENCE_THRESHOLD_SECONDS:
        suppression_reason = _heartbeat_suppression_reason(group_chat_id, now)
        if suppression_reason:
            logging.debug("💓 HEARTBEAT: Suppressed check-in (%s).", suppression_reason)
            return

        logging.info(f"💓 HEARTBEAT: Chat {group_chat_id} silent for {elapsed:.1f}s, evaluating check-in...")
        _heartbeat_last_evaluation[group_chat_id] = now
        await handle_heartbeat_trigger(group_chat_id, elapsed)

async def handle_heartbeat_trigger(chat_id: int, silence_seconds: float = None):
    """Triggers the model to potentially circle back or check in on a silent chat."""
    globals.TARGET_CHAT_ID.set(chat_id)
    
    # Determine the topic/thread ID for the heartbeats
    message_thread_id = None
    if CHAT_TOPIC_ID is not None:
        message_thread_id = CHAT_TOPIC_ID
            
    if message_thread_id is None and globals.chat_histories.get(chat_id):
        for msg in reversed(globals.chat_histories[chat_id]):
            if msg.get("message_id") and msg.get("message_thread_id"):
                message_thread_id = msg.get("message_thread_id")
                break
                
    globals.CURRENT_THREAD_ID.set(message_thread_id)

    now = datetime.now(USER_TIMEZONE)
    if silence_seconds is None:
        last_time = _last_chat_activity(globals.chat_histories.get(chat_id, []))
        silence_seconds = _seconds_since(now, last_time) if last_time else 0

    context_packet = await build_heartbeat_context_packet(chat_id, now, silence_seconds)
    
    trigger_content = (
        f"[System Trigger (Heartbeat)]: It has been several hours since the last message in this chat. "
        f"Review the conversation history and the private context below. Be selective and human-like. "
        f"Send one short, natural message only if there is a timely, personally relevant, or socially natural reason to check in. "
        f"Good reasons include an unresolved thread, a prior promise to follow up, a relevant remembered topic, or an active weather alert. "
        f"Do not summarize the private context, mention the trigger, or perform additional lookups. "
        f"If the message would be generic filler, or if the conversation has reached a natural pause, "
        f"you MUST reply with exactly 'DONE' to remain completely silent.\n\n{context_packet}"
    )
    
    trigger_msg = {
        "role": "user",
        "content": trigger_content,
        "is_heartbeat_trigger": True,
        "timestamp": datetime.now(USER_TIMEZONE)
    }
    
    globals.chat_histories[chat_id].append(trigger_msg)
    
    try:
        response_text, voice_sent_via_tool = await emery_engine(globals.chat_histories[chat_id], allow_tools=False)
    except Exception as e:
        logging.error(f"Error executing heartbeat engine: {e}")
        response_text = "DONE"
        
    start_tag = "<" + "think" + ">"
    end_tag = "</" + "think" + ">"
    pattern = re.escape(start_tag) + r"(.*?)" + re.escape(end_tag)
    clean_response = re.sub(pattern, '', response_text, flags=re.DOTALL | re.IGNORECASE).strip()
    
    handshake_check = re.sub(r'[^a-zA-Z]', '', clean_response).upper()
    if handshake_check == "DONE":
        logging.debug(f"🤫 HEARTBEAT: Chat {chat_id} remains silent.")
        return

    if not clean_response:
        logging.debug(f"🤫 HEARTBEAT: Chat {chat_id} produced an empty response; remaining silent.")
        return
        
    reply_to_id = None
    for msg in reversed(globals.chat_histories[chat_id]):
        if msg.get("message_id"):
            reply_to_id = msg.get("message_id")
            break
            
    try:
        sent_msgs = await send_safe_large_message_as_reply(chat_id, clean_response, reply_to_id, message_thread_id)
        if sent_msgs:
            _record_heartbeat_proactive(chat_id, datetime.now(USER_TIMEZONE))
            globals.chat_histories[chat_id].append({
                "role": "assistant",
                "content": response_text,
                "message_thread_id": message_thread_id,
                "timestamp": datetime.now(USER_TIMEZONE),
                "message_ids": [m.message_id for m in sent_msgs],
                "message_id": sent_msgs[-1].message_id,
            })
    except Exception as e:
        logging.error(f"Failed to send heartbeat message: {e}")

async def start_bot_heartbeat(application) -> None:
    """Registers the bot heartbeat job in the Telegram JobQueue on startup."""
    if not ENABLE_HEARTBEAT:
        logging.debug("💓 HEARTBEAT: Spontaneous heartbeat disabled.")
        return
        
    if not application.job_queue:
        logging.warning("⚠️ HEARTBEAT: JobQueue is not available. Heartbeat cannot be registered.")
        return
        
    application.job_queue.run_repeating(
        heartbeat_check,
        interval=HEARTBEAT_INTERVAL_SECONDS,
        first=60,
        name="bot_heartbeat"
    )
    logging.info(f"💓 HEARTBEAT: Active (checking every {HEARTBEAT_INTERVAL_SECONDS}s)")

async def bot_post_init(application) -> None:
    """Consolidated post_init wrapper to launch Reolink polling and start the bot heartbeat."""
    await validate_telegram_routing(application)
    from emery.tools import start_reolink_polling
    await start_reolink_polling(application)
    await start_bot_heartbeat(application)
    
    # Preload sticker set if configured in environment
    sticker_set_name = TELEGRAM_STICKER_SET
    if sticker_set_name:
        try:
            sticker_set = await application.bot.get_sticker_set(sticker_set_name)
            for sticker in sticker_set.stickers:
                if sticker.emoji:
                    globals.learned_stickers[sticker.emoji] = sticker.file_id
            logging.info(f"🎨 STICKERS: Preloaded {len(sticker_set.stickers)} stickers from '{sticker_set_name}'")
        except Exception as e:
            logging.error(f"⚠️ STICKERS: Failed to preload sticker set '{sticker_set_name}': {e}")
