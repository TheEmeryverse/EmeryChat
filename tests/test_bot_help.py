import os
import sys
import types
import unittest


sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

telegram_module = sys.modules.setdefault("telegram", types.ModuleType("telegram"))
telegram_error_module = sys.modules.setdefault("telegram.error", types.ModuleType("telegram.error"))
telegram_ext_module = sys.modules.setdefault("telegram.ext", types.ModuleType("telegram.ext"))
tghtml_module = sys.modules.setdefault("tghtml", types.ModuleType("tghtml"))


class Update:
    pass


class ReplyParameters:
    def __init__(self, message_id, allow_sending_without_reply=False):
        self.message_id = message_id
        self.allow_sending_without_reply = allow_sending_without_reply


class InlineKeyboardButton:
    def __init__(self, text, callback_data=None):
        self.text = text
        self.callback_data = callback_data


class InlineKeyboardMarkup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = inline_keyboard


class BadRequest(Exception):
    pass


class TimedOut(Exception):
    pass


class ContextTypes:
    DEFAULT_TYPE = object


telegram_module.Update = getattr(telegram_module, "Update", Update)
telegram_module.ReplyParameters = getattr(telegram_module, "ReplyParameters", ReplyParameters)
telegram_module.InlineKeyboardButton = getattr(telegram_module, "InlineKeyboardButton", InlineKeyboardButton)
telegram_module.InlineKeyboardMarkup = getattr(telegram_module, "InlineKeyboardMarkup", InlineKeyboardMarkup)
telegram_error_module.BadRequest = getattr(telegram_error_module, "BadRequest", BadRequest)
telegram_error_module.TimedOut = getattr(telegram_error_module, "TimedOut", TimedOut)
telegram_ext_module.ContextTypes = getattr(telegram_ext_module, "ContextTypes", ContextTypes)
tghtml_module.TgHTML = getattr(tghtml_module, "TgHTML", type("TgHTML", (), {}))


from emery import bot


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append({"text": text, **kwargs})


class FakeUpdate:
    def __init__(self):
        self.effective_user = types.SimpleNamespace(id=123)
        self.message = FakeMessage()


class TestBotHelp(unittest.IsolatedAsyncioTestCase):
    async def test_help_command_lists_expert_options(self):
        original_allowed_ids = bot.ALLOWED_USER_IDS
        try:
            bot.ALLOWED_USER_IDS = {123}
            update = FakeUpdate()
            await bot.handle_help_command(update, types.SimpleNamespace())
        finally:
            bot.ALLOWED_USER_IDS = original_allowed_ids

        reply = update.message.replies[-1]
        self.assertEqual(reply["parse_mode"], "HTML")
        self.assertIn("/expert &lt;topic&gt;", reply["text"])
        self.assertIn("/expert resume &lt;id&gt;", reply["text"])
        self.assertIn("/expert clear", reply["text"])
        self.assertIn("/debate &lt;topic&gt;", reply["text"])
        self.assertIn("/debate clear", reply["text"])
        self.assertIn("/clear", reply["text"])
        self.assertIn("Natural language tools", reply["text"])


if __name__ == "__main__":
    unittest.main()
