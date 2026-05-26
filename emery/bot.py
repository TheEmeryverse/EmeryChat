import re
import logging
import asyncio
import base64
from datetime import datetime
from collections import deque

from telegram import Update
from telegram.ext import ContextTypes
from telegram.error import TimedOut

from emery.config import (
    MODEL_ID, USER_TIMEZONE, VISION_MODEL_ID, USER_BIRTHDAY, MAX_HISTORY_LEN
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
        
    logging.info(f"💬 USER (chat {chat_id}): {str(content)[:120]}")
    globals.chat_histories[chat_id].append({"role": "user", "content": content})
    
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

    # Save the assistant text (with raw think tags intact) to history
    globals.chat_histories[chat_id].append({"role": "assistant", "content": response_text})

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
        logging.info("🤫 HANDSHAKE: Suppressing final text message because camera photo was already delivered.")
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

    # --- SINGLE FINAL REPLY DISPATCHER ---
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

async def send_safe_large_message(update: Update, text: str):
    """
    Splits extremely long final responses at natural line breaks 
    to prevent Telegram's 4096 character limit crash.
    """
    MAX_LIMIT = 4000
    if len(text) <= MAX_LIMIT:
        await update.message.reply_text(text, parse_mode="HTML")
        return

    while len(text) > 0:
        if len(text) <= MAX_LIMIT:
            await update.message.reply_text(text, parse_mode="HTML")
            break
            
        split_index = text.rfind('\n', 0, MAX_LIMIT)
        if split_index == -1 or split_index < 3000:
            split_index = MAX_LIMIT
            
        chunk = text[:split_index]
        await update.message.reply_text(chunk, parse_mode="HTML")
        text = text[split_index:].strip()



async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Logs network drops and timeouts cleanly instead of crashing the thread."""
    if isinstance(context.error, TimedOut):
        logging.warning("⚠️ Telegram API timed out temporarily due to high CPU load. The message will retry.")
    else:
        logging.error(f"⚠️ Telegram API Exception: {context.error}", exc_info=True)
