import logging
from collections import deque
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, MessageReactionHandler, filters
from telegram.request import HTTPXRequest

from emery.config import TELEGRAM_TOKEN, USER_TIMEZONE, MODEL_ID, VISION_MODEL_ID, ENABLE_SCHEDULER
import emery.globals as globals
from emery.bot import (
    error_handler,
    handle_clear_command,
    handle_wipe_command,
    handle_message,
    handle_reaction,
    bot_post_init
)

if __name__ == '__main__':
    t_request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    
    application = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .request(t_request)
        .post_init(bot_post_init)  # Registers reolink polling and bot heartbeat
        .build()
    )
    
    # Store bot instance in shared global state
    globals.application_bot = application.bot
    globals.application = application
    
    if str(ENABLE_SCHEDULER).lower() == "true":
        from emery.scheduler import load_and_register_all_jobs
        load_and_register_all_jobs()
    
    application.add_error_handler(error_handler)

    

    
    application.add_handler(CommandHandler("clear", handle_clear_command))
    application.add_handler(CommandHandler("wipe", handle_wipe_command))
    application.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.VOICE, handle_message))
    application.add_handler(MessageReactionHandler(handle_reaction))

    logging.info(f"🚀 EMERYCHAT ONLINE — model: {MODEL_ID} | vision: {VISION_MODEL_ID}")
    application.run_polling()