import logging
from datetime import time
from collections import deque
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters
from telegram.request import HTTPXRequest

from emery.config import TELEGRAM_TOKEN, USER_TIMEZONE, MODEL_ID, VISION_MODEL_ID
import emery.globals as globals
from emery.tools import start_reolink_polling
from emery.bot import (
    error_handler,
    handle_wipe_command,
    handle_message,
    job_morning_briefing,
    job_morning_weather,
    job_calendar,
    job_nasa,
    job_today_in_history
)

if __name__ == '__main__':
    t_request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    
    application = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .request(t_request)
        .post_init(start_reolink_polling)  # Registers active-polling startup
        .build()
    )
    
    # Store bot instance in shared global state
    globals.application_bot = application.bot
    
    application.add_error_handler(error_handler)
    
    # Schedule the daily briefings & jobs
    application.job_queue.run_daily(job_morning_briefing, time=time(3, 0, tzinfo=USER_TIMEZONE))
    application.job_queue.run_daily(job_morning_weather, time=time(3, 5, tzinfo=USER_TIMEZONE))
    application.job_queue.run_daily(job_calendar, time=time(3, 10, tzinfo=USER_TIMEZONE))
    application.job_queue.run_daily(job_nasa, time=time(21, 0, tzinfo=USER_TIMEZONE))
    application.job_queue.run_daily(job_today_in_history, time=time(21, 5, tzinfo=USER_TIMEZONE))
    
    application.add_handler(CommandHandler("clear", lambda u, c: globals.chat_histories.get(u.effective_chat.id, deque()).clear() or u.message.reply_text("Context cleared.")))
    application.add_handler(CommandHandler("wipe", handle_wipe_command))
    application.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.VOICE, handle_message))

    logging.info(f"🚀 EMERYCHAT ONLINE — model: {MODEL_ID} | vision: {VISION_MODEL_ID}")
    application.run_polling()