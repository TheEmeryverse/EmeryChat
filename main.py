import logging
from collections import deque
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, MessageHandler, MessageReactionHandler, filters
from telegram.request import HTTPXRequest

from emery.config import (
    TELEGRAM_TOKEN, USER_TIMEZONE, MODEL_ID, VISION_MODEL_ID,
    FAST_MODEL_ID, EMBEDDING_MODEL_ID, ENABLE_SCHEDULER
)
import emery.globals as globals
from emery.bot import (
    error_handler,
    handle_help_command,
    handle_clear_command,
    handle_wipe_command,
    handle_message,
    handle_reaction,
    bot_post_init,
    validate_telegram_access_policy
)
from emery.expert import handle_expert_callback, handle_expert_command


EMERYCHAT_BANNER = r"""
 ______     __    __     ______     ______     __  __     ______     __  __     ______     ______
/\  ___\   /\ "-./  \   /\  ___\   /\  == \   /\ \_\ \   /\  ___\   /\ \_\ \   /\  __ \   /\__  _\
\ \  __\   \ \ \-./\ \  \ \  __\   \ \  __<   \ \____ \  \ \ \____  \ \  __ \  \ \  __ \  \/_/\ \/
 \ \_____\  \ \_\ \ \_\  \ \_____\  \ \_\ \_\  \/\_____\  \ \_____\  \ \_\ \_\  \ \_\ \_\    \ \_\
  \/_____/   \/_/  \/_/   \/_____/   \/_/ /_/   \/_____/   \/_____/   \/_/\/_/   \/_/\/_/     \/_/
"""


def log_startup_banner():
    logging.info("%s", EMERYCHAT_BANNER)


if __name__ == '__main__':
    log_startup_banner()
    validate_telegram_access_policy()

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

    

    
    application.add_handler(CommandHandler("help", handle_help_command))
    application.add_handler(CommandHandler("clear", handle_clear_command))
    application.add_handler(CommandHandler("wipe", handle_wipe_command))
    application.add_handler(CommandHandler("expert", handle_expert_command))
    application.add_handler(CallbackQueryHandler(handle_expert_callback, pattern=r"^expert:"))
    application.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.VOICE | filters.Sticker.ALL | filters.ANIMATION | filters.Document.ALL, handle_message))
    application.add_handler(MessageReactionHandler(handle_reaction))

    scheduler_status = "enabled" if str(ENABLE_SCHEDULER).lower() == "true" else "disabled"
    logging.info(
        "EMERYCHAT ONLINE | model=%s | fast=%s | vision=%s | embed=%s | scheduler=%s",
        MODEL_ID,
        FAST_MODEL_ID,
        VISION_MODEL_ID,
        EMBEDDING_MODEL_ID,
        scheduler_status,
    )
    application.run_polling()
