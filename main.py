import os
import re
import logging
import asyncio
from contextlib import asynccontextmanager
from collections import deque
import uvicorn
from fastapi import FastAPI, HTTPException, Header, Depends
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime

from emery.config import USER_TIMEZONE, MODEL_ID, VISION_MODEL_ID, ENABLE_SCHEDULER, MAX_HISTORY_LEN
import emery.globals as globals

# Verification API key setup
expected_api_key = os.getenv("API_KEY")

async def verify_api_key(authorization: Optional[str] = Header(None)):
    if expected_api_key:
        if not authorization or authorization != f"Bearer {expected_api_key}":
            raise HTTPException(status_code=401, detail="Unauthorized")

# API models
class ChatRequest(BaseModel):
    message: Optional[str] = None
    chat_id: str
    user_id: str
    sender_name: Optional[str] = "User"
    photo: Optional[str] = None          # Base64 JPEG/PNG
    photo_caption: Optional[str] = None
    voice: Optional[str] = None          # Base64 Audio (ogg/mp3/etc)

class ClearRequest(BaseModel):
    chat_id: str

class WipeRequest(BaseModel):
    user_id: int

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize scheduler and custom jobs
    from emery.scheduler import start_scheduler, load_and_register_all_jobs
    start_scheduler()
    
    if str(ENABLE_SCHEDULER).lower() == "true":
        load_and_register_all_jobs()
        
    # Start Reolink polling if configured
    if os.getenv("ENABLE_REOLINK_POLLING", "false").lower() == "true":
        from emery.tools import start_reolink_polling
        await start_reolink_polling(None)
        
    logging.info(f"🚀 EMERYCHAT ONLINE (API Mode) — model: {MODEL_ID} | vision: {VISION_MODEL_ID}")
    yield
    # Shutdown: Stop scheduler
    if globals.scheduler:
        globals.scheduler.shutdown()
        logging.info("💤 EMERYCHAT OFFLINE")

app = FastAPI(title="EmeryChat API", lifespan=lifespan)

@app.post("/api/chat", dependencies=[Depends(verify_api_key)])
async def chat_endpoint(req: ChatRequest):
    chat_id = req.chat_id
    
    # Track the active user context
    globals.current_user_id.set(req.user_id)
    globals.TARGET_CHAT_ID.set(chat_id)
    globals.CURRENT_THREAD_ID.set(None)
    
    # Initialize request-scoped outgoing responses container
    globals.outgoing_responses.set([])
    
    now_str = datetime.now(USER_TIMEZONE).strftime("%A, %B %d, %Y at %I:%M %p")
    sender_name = req.sender_name or "User"
    is_input_voice = False
    content_text = req.message or ""
    
    # Dynamically associate user chat ID with scheduler jobs
    from emery.scheduler import update_jobs_with_chat_id
    update_jobs_with_chat_id(chat_id)
    
    if chat_id not in globals.chat_histories:
        globals.chat_histories[chat_id] = deque(maxlen=MAX_HISTORY_LEN)
        
    # Handle incoming voice message
    if req.voice:
        is_input_voice = True
        try:
            import base64
            from emery.helpers import transcribe_audio
            audio_bytes = base64.b64decode(req.voice)
            transcription = await transcribe_audio(audio_bytes)
            if not transcription:
                raise HTTPException(status_code=400, detail="Could not transcribe audio input")
            content_text = transcription
        except Exception as e:
            logging.error(f"❌ API Voice Transcription Error: {e}")
            raise HTTPException(status_code=400, detail=f"Audio transcription error: {str(e)}")
            
    # Handle incoming photo
    elif req.photo:
        try:
            import base64
            from emery.helpers import compress_image_bytes, get_image_description
            photo_bytes = base64.b64decode(req.photo)
            compressed_bytes = compress_image_bytes(photo_bytes)
            b64_str = base64.b64encode(compressed_bytes).decode('utf-8')
            caption = req.photo_caption or ""
            
            description = await get_image_description(b64_str, caption)
            content_text = "sent an image."
            if caption:
                content_text += f" Caption: {caption}"
            content_text += f"\nImage Description: {description}"
        except Exception as e:
            logging.error(f"❌ API Photo Analysis Error: {e}")
            raise HTTPException(status_code=400, detail=f"Photo analysis error: {str(e)}")
            
    content = f"[{now_str}] {sender_name}: {content_text}"
    logging.info(f"💬 USER (chat {chat_id}): {sender_name} -> {content_text[:120]}")
    
    globals.chat_histories[chat_id].append({
        "role": "user",
        "content": content,
        "user_id": req.user_id,
        "sender_name": sender_name,
        "timestamp": datetime.now(USER_TIMEZONE)
    })
    
    try:
        from emery.engine import emery_engine
        response_text, voice_sent_via_tool = await emery_engine(globals.chat_histories[chat_id], model_to_use=MODEL_ID)
    except Exception as e:
        logging.error(f"❌ API Engine Failure: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="EMERYCHAT engine failure.")
        
    # Split reasoning/thinking tag out
    start_tag = "<" + "think" + ">"
    end_tag = "</" + "think" + ">"
    pattern = re.escape(start_tag) + r"(.*?)" + re.escape(end_tag)
    think_match = re.search(pattern, response_text, re.DOTALL | re.IGNORECASE)

    clean_response = response_text
    thinking_content = ""

    if think_match:
        thinking_content = think_match.group(1).strip()
        clean_response = re.sub(pattern, '', response_text, flags=re.DOTALL | re.IGNORECASE).strip()
        
    # Handle silent handshake DONE
    handshake_check = re.sub(r'[^a-zA-Z]', '', clean_response).upper()
    if handshake_check == "DONE":
        logging.info("🤫 HANDSHAKE: Suppressed response (silent check)")
        globals.chat_histories[chat_id].append({
            "role": "assistant",
            "content": response_text,
            "timestamp": datetime.now(USER_TIMEZONE)
        })
        return {
            "thinking": thinking_content,
            "responses": globals.outgoing_responses.get()
        }
        
    # Dispatch textual or voice output
    if is_input_voice and not voice_sent_via_tool:
        from emery.tools import get_voice_audio
        v_out = await get_voice_audio(clean_response)
        if v_out:
            import base64
            v_b64 = base64.b64encode(v_out).decode('utf-8')
            globals.outgoing_responses.get().append({
                "type": "voice",
                "data": v_b64,
                "caption": "Voice message"
            })
        else:
            from emery.helpers import emery_format
            globals.outgoing_responses.get().append({
                "type": "text",
                "content": emery_format(clean_response)
            })
    else:
        if clean_response:
            from emery.helpers import emery_format
            globals.outgoing_responses.get().append({
                "type": "text",
                "content": emery_format(clean_response)
            })
            
    # Append assistant's reply to history
    globals.chat_histories[chat_id].append({
        "role": "assistant",
        "content": response_text,
        "timestamp": datetime.now(USER_TIMEZONE)
    })
    
    # Trigger background topic summarization
    from emery.memory import summarize_topics_background
    asyncio.create_task(summarize_topics_background(chat_id, req.user_id))
    
    return {
        "thinking": thinking_content,
        "responses": globals.outgoing_responses.get()
    }

@app.post("/api/clear", dependencies=[Depends(verify_api_key)])
async def clear_endpoint(req: ClearRequest):
    chat_id = req.chat_id
    if chat_id in globals.chat_histories:
        globals.chat_histories[chat_id].clear()
    return {"status": "success", "message": "Context cleared."}

@app.post("/api/wipe", dependencies=[Depends(verify_api_key)])
async def wipe_endpoint(req: WipeRequest):
    globals.current_user_id.set(req.user_id)
    if wipe_memory(req.user_id):
        return {"status": "success", "message": "Memory wiped and re-initialized to baseline template."}
    else:
        raise HTTPException(status_code=500, detail="Failed to wipe memory due to a filesystem error.")

if __name__ == '__main__':
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main.py:app", host="0.0.0.0", port=port, reload=False)