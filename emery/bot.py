import re
import logging
import asyncio
import base64
from datetime import datetime
from collections import deque

from telegram import Update, ReplyParameters
from telegram.ext import ContextTypes
from telegram.error import TimedOut

from emery.config import (
    MODEL_ID, USER_TIMEZONE, VISION_MODEL_ID, USER_BIRTHDAY, MAX_HISTORY_LEN,
    ENABLE_HEARTBEAT, HEARTBEAT_INTERVAL_SECONDS, HEARTBEAT_SILENCE_THRESHOLD_SECONDS,
    HEARTBEAT_SLEEP_START, HEARTBEAT_SLEEP_END
)
import emery.globals as globals
from emery.helpers import (
    emery_format, transcribe_audio, compress_image_bytes,
    get_image_description
)
from emery.memory import wipe_memory
from emery.engine import emery_engine
from emery.tools import get_voice_audio

# --- TELEGRAM HANDLERS ---
async def handle_wipe_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram handler for /wipe command."""
    if wipe_memory():
        await update.message.reply_text("🧠 Memory wiped successfully and re-initialized to baseline template.")
    else:
        await update.message.reply_text("❌ Failed to wipe memory due to a filesystem error.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    globals.TARGET_CHAT_ID = chat_id
    
    # Dynamically associate user chat ID with any pending jobs (like default briefings)
    from emery.scheduler import update_jobs_with_chat_id
    update_jobs_with_chat_id(chat_id)
    
    if chat_id not in globals.chat_histories: 
        globals.chat_histories[chat_id] = deque(maxlen=MAX_HISTORY_LEN)
    
    # Clear any stale custom reply targets for this turn
    globals.chat_reply_targets.pop(chat_id, None)

    is_input_voice = False
    model_to_use = MODEL_ID
    
    now_str = datetime.now(USER_TIMEZONE).strftime("%A, %B %d, %Y at %I:%M %p")

    if update.message.voice:
        is_input_voice = True
        v_file = await update.message.voice.get_file()
        transcription = await transcribe_audio(await v_file.download_as_bytearray())
        if not transcription: 
            return
        content = f"[{now_str}] {transcription}"
    elif update.message.photo:
        p_file = await update.message.photo[-1].get_file()
        photo_bytes = await p_file.download_as_bytearray()
        compressed_bytes = compress_image_bytes(photo_bytes)
        b64 = base64.b64encode(compressed_bytes).decode('utf-8')
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
        
    # Thread Reply Context Builder
    reply_to = update.message.reply_to_message
    reply_info = ""
    if reply_to:
        reply_to_id = reply_to.message_id
        replied_text = ""
        for msg in globals.chat_histories[chat_id]:
            if msg.get("message_id") == reply_to_id or (isinstance(msg.get("message_ids"), list) and reply_to_id in msg["message_ids"]):
                replied_text = msg.get("content", "")
                replied_text = re.sub(r'<think>.*?</think>', '', replied_text, flags=re.DOTALL | re.IGNORECASE).strip()
                break
        if not replied_text:
            replied_text = reply_to.text or "[Non-text message]"
        preview = replied_text[:80] + "..." if len(replied_text) > 80 else replied_text
        reply_info = f" (Replying to message ID {reply_to_id}: '{preview}')"

    logging.info(f"💬 USER (chat {chat_id}): {str(content + reply_info)[:120]}")
    globals.chat_histories[chat_id].append({
        "role": "user", 
        "content": content + reply_info,
        "message_id": update.message.message_id,
        "reply_to_message_id": reply_to.message_id if reply_to else None,
        "timestamp": datetime.now(USER_TIMEZONE)
    })
    
    # --- TYPING INDICATOR LOOP ---
    typing_stop = asyncio.Event()

    async def keep_typing():
        while not typing_stop.is_set():
            try:
                await update.message.reply_chat_action("typing")
            except Exception as e:
                logging.debug(f"Typing action failed: {e}")
            await asyncio.sleep(4)

    typing_task = asyncio.create_task(keep_typing())

    try:
        response_text, voice_sent_via_tool = await emery_engine(globals.chat_histories[chat_id], model_to_use=model_to_use)
    finally:
        typing_stop.set()
        await typing_task

    # --- THINKING SPLITTER LOGIC ---
    start_tag = "<" + "think" + ">"
    end_tag = "</" + "think" + ">"
    pattern = re.escape(start_tag) + r"(.*?)" + re.escape(end_tag)
    think_match = re.search(pattern, response_text, re.DOTALL | re.IGNORECASE)

    clean_response = response_text
    thinking_content = ""

    if think_match:
        thinking_content = think_match.group(1).strip()
        clean_response = re.sub(pattern, '', response_text, flags=re.DOTALL | re.IGNORECASE).strip()

    # --- SILENT HANDSHAKE DETECTION ---
    handshake_check = re.sub(r'[^a-zA-Z]', '', clean_response).upper()
    if handshake_check == "DONE":
        logging.info("🤫 HANDSHAKE: Suppressing final text message because camera photo was already delivered or model chose silence.")
        globals.chat_histories[chat_id].append({
            "role": "assistant",
            "content": response_text,
            "timestamp": datetime.now(USER_TIMEZONE)
        })
        return

    # Display the thinking block if one exists
    if think_match and thinking_content:
        CHUNK_SIZE = 3900
        chunks = [thinking_content[i:i+CHUNK_SIZE] for i in range(0, len(thinking_content), CHUNK_SIZE)]

        for idx, chunk in enumerate(chunks):
            if len(chunks) > 1:
                header = f"🧠 <b>Emery's Thought Process (Part {idx+1}/{len(chunks)})</b> (Expand to read):\n"
            else:
                header = f"🧠 <b>Emery's Thought Process</b> (Expand to read):\n"

            thinking_msg = f"{header}<blockquote expandable><i>{chunk}</i></blockquote>"
            await update.message.reply_text(thinking_msg, parse_mode="HTML")

    # Determine final reply target
    reply_target_id = globals.chat_reply_targets.pop(chat_id, None) or update.message.message_id

    sent_msgs = []
    # --- SINGLE FINAL REPLY DISPATCHER ---
    if is_input_voice and not voice_sent_via_tool:
        await update.message.reply_chat_action("record_voice")
        v_out = await get_voice_audio(clean_response)
        if v_out:
            sent_msg = await update.message.reply_voice(
                voice=v_out, 
                caption="Voice message",
                reply_parameters=ReplyParameters(message_id=reply_target_id, allow_sending_without_reply=True)
            )
            sent_msgs = [sent_msg] if sent_msg else []
        else:
            sent_msgs = await send_safe_large_message(update, emery_format(clean_response), reply_to_message_id=reply_target_id)
    else:
        if clean_response:
            sent_msgs = await send_safe_large_message(update, emery_format(clean_response), reply_to_message_id=reply_target_id)

    # Save the assistant text (with raw think tags intact) to history
    assistant_entry = {
        "role": "assistant", 
        "content": response_text,
        "timestamp": datetime.now(USER_TIMEZONE)
    }
    if sent_msgs:
        assistant_entry["message_ids"] = [m.message_id for m in sent_msgs]
        assistant_entry["message_id"] = sent_msgs[-1].message_id
    globals.chat_histories[chat_id].append(assistant_entry)

    # Trigger background topic summarization
    from emery.memory import summarize_topics_background
    asyncio.create_task(summarize_topics_background(chat_id))

async def send_safe_large_message(update: Update, text: str, reply_to_message_id: int = None):
    """
    Splits extremely long final responses at natural line breaks 
    to prevent Telegram's 4096 character limit crash.
    """
    MAX_LIMIT = 4000
    reply_params = None
    if reply_to_message_id:
        reply_params = ReplyParameters(message_id=reply_to_message_id, allow_sending_without_reply=True)

    sent_msgs = []
    
    if len(text) <= MAX_LIMIT:
        sent_msg = await update.message.reply_text(text, parse_mode="HTML", reply_parameters=reply_params)
        return [sent_msg]

    while len(text) > 0:
        if len(text) <= MAX_LIMIT:
            sent_msg = await update.message.reply_text(text, parse_mode="HTML", reply_parameters=reply_params)
            sent_msgs.append(sent_msg)
            break
            
        split_index = text.rfind('\n', 0, MAX_LIMIT)
        if split_index == -1 or split_index < 3000:
            split_index = MAX_LIMIT
            
        chunk = text[:split_index]
        sent_msg = await update.message.reply_text(chunk, parse_mode="HTML", reply_parameters=reply_params)
        sent_msgs.append(sent_msg)
        text = text[split_index:].strip()
        
    return sent_msgs



async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Logs network drops and timeouts cleanly instead of crashing the thread."""
    if isinstance(context.error, TimedOut):
        logging.warning("⚠️ Telegram API timed out temporarily due to high CPU load. The message will retry.")
    else:
        logging.error(f"⚠️ Telegram API Exception: {context.error}", exc_info=True)

# --- REACTION AND HEARTBEAT FUNCTIONALITY ---

async def send_safe_large_message_as_reply(chat_id: int, text: str, reply_to_message_id: int = None):
    """Sends a safe split message directly to a chat, replying to a specific message ID."""
    MAX_LIMIT = 4000
    reply_params = None
    if reply_to_message_id:
        reply_params = ReplyParameters(message_id=reply_to_message_id, allow_sending_without_reply=True)
        
    sent_msgs = []
    
    if len(text) <= MAX_LIMIT:
        sent_msg = await globals.application_bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_parameters=reply_params
        )
        return [sent_msg]

    while len(text) > 0:
        if len(text) <= MAX_LIMIT:
            sent_msg = await globals.application_bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                reply_parameters=reply_params
            )
            sent_msgs.append(sent_msg)
            break
            
        split_index = text.rfind('\n', 0, MAX_LIMIT)
        if split_index == -1 or split_index < 3000:
            split_index = MAX_LIMIT
            
        chunk = text[:split_index]
        sent_msg = await globals.application_bot.send_message(
            chat_id=chat_id,
            text=chunk,
            parse_mode="HTML",
            reply_parameters=reply_params
        )
        sent_msgs.append(sent_msg)
        text = text[split_index:].strip()
        
    return sent_msgs

async def handle_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram handler for message reaction changes (MessageReactionUpdated)."""
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
        globals.chat_histories[chat_id] = deque(maxlen=MAX_HISTORY_LEN)
        
    found_msg = None
    for msg in globals.chat_histories[chat_id]:
        if msg.get("message_id") == message_id or (isinstance(msg.get("message_ids"), list) and message_id in msg["message_ids"]):
            found_msg = msg
            break
            
    if found_msg:
        if "reactions" not in found_msg:
            found_msg["reactions"] = {}
        found_msg["reactions"][actor_key] = emojis
        logging.info(f"🎭 REACTION: Updated {actor_key} reactions on message {message_id}: {emojis}")
        
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
                logging.info(f"🎭 REACTION: Triggering reaction check for user reaction {emojis} on message {message_id}...")
                asyncio.create_task(handle_user_reaction_trigger(chat_id, message_id, emojis))

async def handle_user_reaction_trigger(chat_id: int, message_id: int, emojis: list[str]):
    """Invokes the engine contextually when a user reacts to a message."""
    globals.TARGET_CHAT_ID = chat_id
    
    msg_text = ""
    for msg in globals.chat_histories.get(chat_id, []):
        if msg.get("message_id") == message_id or (isinstance(msg.get("message_ids"), list) and message_id in msg["message_ids"]):
            msg_text = msg.get("content", "")
            msg_text = re.sub(r'<think>.*?</think>', '', msg_text, flags=re.DOTALL | re.IGNORECASE).strip()
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
    
    trigger_msg = {
        "role": "user",
        "content": trigger_content,
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
        
    if trigger_msg in globals.chat_histories[chat_id]:
        globals.chat_histories[chat_id].remove(trigger_msg)
        
    start_tag = "<" + "think" + ">"
    end_tag = "</" + "think" + ">"
    pattern = re.escape(start_tag) + r"(.*?)" + re.escape(end_tag)
    clean_response = re.sub(pattern, '', response_text, flags=re.DOTALL | re.IGNORECASE).strip()
    
    handshake_check = re.sub(r'[^a-zA-Z]', '', clean_response).upper()
    if handshake_check == "DONE":
        logging.info("🤫 REACTION HANDSHAKE: Suppressing final text response because model chose to remain silent.")
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

async def heartbeat_check(context: ContextTypes.DEFAULT_TYPE):
    """Callback for Telegram JobQueue that runs periodically to check if the bot should spontaneously send a message."""
    if not ENABLE_HEARTBEAT:
        return
        
    logging.info("💓 HEARTBEAT: Running check...")
    now = datetime.now(USER_TIMEZONE)
    
    # Check if current time falls within user's sleep window
    try:
        from datetime import time
        start_h, start_m = map(int, HEARTBEAT_SLEEP_START.split(':'))
        end_h, end_m = map(int, HEARTBEAT_SLEEP_END.split(':'))
        start_time = time(start_h, start_m)
        end_time = time(end_h, end_m)
        curr_time = now.time()
        
        is_asleep = False
        if start_time <= end_time:
            if start_time <= curr_time <= end_time:
                is_asleep = True
        else:
            if curr_time >= start_time or curr_time <= end_time:
                is_asleep = True
                
        if is_asleep:
            logging.info(f"💓 HEARTBEAT: Suppressing check-in because current time {curr_time.strftime('%H:%M')} is inside sleep window ({HEARTBEAT_SLEEP_START} - {HEARTBEAT_SLEEP_END}).")
            return
    except Exception as e:
        logging.error(f"❌ HEARTBEAT: Error checking sleep window range: {e}")
        
    for chat_id, history in list(globals.chat_histories.items()):
        if not history:
            continue
            
        last_msg = history[-1]
        last_time = last_msg.get("timestamp")
        
        if not last_time:
            last_msg["timestamp"] = now
            continue
            
        elapsed = (now - last_time).total_seconds()
        if elapsed > HEARTBEAT_SILENCE_THRESHOLD_SECONDS:
            logging.info(f"💓 HEARTBEAT: Chat {chat_id} has been silent for {elapsed:.1f}s. Evaluating spontaneous message...")
            await handle_heartbeat_trigger(chat_id)

async def handle_heartbeat_trigger(chat_id: int):
    """Triggers the model to potentially circle back or check in on a silent chat."""
    globals.TARGET_CHAT_ID = chat_id
    
    trigger_content = (
        f"[System Trigger (Heartbeat)]: It has been several hours since the last message in this chat. "
        f"Review the conversation history. You should be extremely conservative about initiating contact. "
        f"Only send a message if there is an important outstanding question, a topic you promised to follow up on, "
        f"or a highly natural reason to check in. If the conversation has reached a natural pause, or if a human "
        f"would typically let it rest, you MUST reply with exactly 'DONE' to remain completely silent. Do not send conversational filler."
    )
    
    trigger_msg = {
        "role": "user",
        "content": trigger_content,
        "is_heartbeat_trigger": True,
        "timestamp": datetime.now(USER_TIMEZONE)
    }
    
    globals.chat_histories[chat_id].append(trigger_msg)
    
    try:
        response_text, voice_sent_via_tool = await emery_engine(globals.chat_histories[chat_id])
    except Exception as e:
        logging.error(f"Error executing heartbeat engine: {e}")
        response_text = "DONE"
        
    if trigger_msg in globals.chat_histories[chat_id]:
        globals.chat_histories[chat_id].remove(trigger_msg)
        
    start_tag = "<" + "think" + ">"
    end_tag = "</" + "think" + ">"
    pattern = re.escape(start_tag) + r"(.*?)" + re.escape(end_tag)
    clean_response = re.sub(pattern, '', response_text, flags=re.DOTALL | re.IGNORECASE).strip()
    
    handshake_check = re.sub(r'[^a-zA-Z]', '', clean_response).upper()
    if handshake_check == "DONE":
        logging.info(f"🤫 HEARTBEAT HANDSHAKE: Chat {chat_id} remains silent.")
        if globals.chat_histories[chat_id]:
            globals.chat_histories[chat_id][-1]["timestamp"] = datetime.now(USER_TIMEZONE)
        return
        
    globals.chat_histories[chat_id].append({
        "role": "assistant",
        "content": response_text,
        "timestamp": datetime.now(USER_TIMEZONE)
    })
    
    reply_to_id = None
    for msg in reversed(globals.chat_histories[chat_id]):
        if msg.get("message_id"):
            reply_to_id = msg.get("message_id")
            break
            
    try:
        sent_msgs = await send_safe_large_message_as_reply(chat_id, clean_response, reply_to_id)
        if sent_msgs:
            last_entry = globals.chat_histories[chat_id][-1]
            last_entry["message_ids"] = [m.message_id for m in sent_msgs]
            last_entry["message_id"] = sent_msgs[-1].message_id
    except Exception as e:
        logging.error(f"Failed to send heartbeat message: {e}")

async def start_bot_heartbeat(application) -> None:
    """Registers the bot heartbeat job in the Telegram JobQueue on startup."""
    if not ENABLE_HEARTBEAT:
        logging.info("💓 HEARTBEAT: Spontaneous heartbeat is disabled.")
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
    logging.info(f"💓 HEARTBEAT: Registered heartbeat job to check every {HEARTBEAT_INTERVAL_SECONDS}s.")

async def bot_post_init(application) -> None:
    """Consolidated post_init wrapper to launch Reolink polling and start the bot heartbeat."""
    from emery.tools import start_reolink_polling
    await start_reolink_polling(application)
    await start_bot_heartbeat(application)
