import unittest

import emery.globals as globals
from emery import tools


class _RecordingBot:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    async def send_photo(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return object()


class ReolinkDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_alert_photo_uses_media_timeouts_and_preserves_arguments(self):
        original_bot = globals.application_bot
        bot = _RecordingBot()
        globals.application_bot = bot
        try:
            photo = b"jpeg-bytes"
            result = await tools._send_reolink_alert_photo(
                chat_id=-100123,
                photo=photo,
                caption="<b>Live</b>",
                parse_mode="HTML",
                reply_to_message_id=456,
                message_thread_id=4,
                disable_notification=True,
            )
        finally:
            globals.application_bot = original_bot

        self.assertIsNotNone(result)
        self.assertEqual(len(bot.calls), 1)
        self.assertEqual(
            bot.calls[0],
            {
                "chat_id": -100123,
                "photo": photo,
                "caption": "<b>Live</b>",
                "parse_mode": "HTML",
                "reply_to_message_id": 456,
                "message_thread_id": 4,
                "disable_notification": True,
                "read_timeout": tools.REOLINK_TELEGRAM_READ_TIMEOUT_SECONDS,
                "write_timeout": tools.REOLINK_TELEGRAM_WRITE_TIMEOUT_SECONDS,
            },
        )
        self.assertGreater(tools.REOLINK_TELEGRAM_READ_TIMEOUT_SECONDS, 30.0)

    async def test_alert_photo_failure_is_not_retried(self):
        original_bot = globals.application_bot
        bot = _RecordingBot(error=RuntimeError("ambiguous timeout"))
        globals.application_bot = bot
        try:
            with self.assertRaisesRegex(RuntimeError, "ambiguous timeout"):
                await tools._send_reolink_alert_photo(
                    chat_id=-100123,
                    photo=b"jpeg-bytes",
                )
        finally:
            globals.application_bot = original_bot

        self.assertEqual(len(bot.calls), 1)


if __name__ == "__main__":
    unittest.main()
