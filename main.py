import logging
from collections import deque
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, MessageHandler, MessageReactionHandler, filters
from telegram.request import HTTPXRequest

from emery.config import (
    TELEGRAM_TOKEN, USER_TIMEZONE, MODEL_ID, VISION_MODEL_ID,
    FAST_MODEL_ID, EMBEDDING_MODEL_ID, ENABLE_SCHEDULER, ALLOWED_BOT_IDS
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
from emery.debate import handle_debate_callback, handle_debate_command
from emery.expert import handle_expert_callback, handle_expert_command
from emery.inter_agent_bridge import handle_bridge_command, handle_bridge_direct_message


class HumanSenderFilter(filters.MessageFilter):
    def filter(self, message):
        return bool(message.from_user and not message.from_user.is_bot)


HUMAN_SENDER = HumanSenderFilter()


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

    

    
    application.add_handler(CommandHandler("bridge", handle_bridge_command))
    if ALLOWED_BOT_IDS:
        application.add_handler(
            MessageHandler(
                filters.User(user_id=ALLOWED_BOT_IDS) & filters.TEXT & ~filters.COMMAND,
                handle_bridge_direct_message,
            )
        )
    application.add_handler(CommandHandler("help", handle_help_command, filters=HUMAN_SENDER))
    application.add_handler(CommandHandler("clear", handle_clear_command, filters=HUMAN_SENDER))
    application.add_handler(CommandHandler("wipe", handle_wipe_command, filters=HUMAN_SENDER))
    application.add_handler(CommandHandler("expert", handle_expert_command, filters=HUMAN_SENDER))
    application.add_handler(CommandHandler("debate", handle_debate_command, filters=HUMAN_SENDER))
    application.add_handler(CallbackQueryHandler(handle_expert_callback, pattern=r"^expert:"))
    application.add_handler(CallbackQueryHandler(handle_debate_callback, pattern=r"^debate:"))
    general_message_filter = (
        filters.TEXT | filters.PHOTO | filters.VOICE | filters.Sticker.ALL
        | filters.ANIMATION | filters.Document.ALL
    ) & HUMAN_SENDER
    application.add_handler(MessageHandler(general_message_filter, handle_message))
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
