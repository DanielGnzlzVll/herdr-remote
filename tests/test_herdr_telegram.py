import asyncio
import importlib.util
import os
from pathlib import Path
import sys
import types
import unittest
from unittest import mock
import uuid


TELEGRAM_PATH = Path(__file__).resolve().parents[1] / "relay" / "herdr_telegram.py"


def _telegram_stubs():
    telegram = types.ModuleType("telegram")

    class InlineKeyboardButton:
        def __init__(self, text, callback_data=None):
            self.text = text
            self.callback_data = callback_data

    class InlineKeyboardMarkup:
        def __init__(self, inline_keyboard):
            self.inline_keyboard = inline_keyboard

    class ForceReply:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    telegram.Update = object
    telegram.ForceReply = ForceReply
    telegram.InlineKeyboardButton = InlineKeyboardButton
    telegram.InlineKeyboardMarkup = InlineKeyboardMarkup

    error = types.ModuleType("telegram.error")

    class NetworkError(Exception):
        pass

    class BadRequest(NetworkError):
        pass

    class RetryAfter(Exception):
        def __init__(self, retry_after):
            super().__init__(f"retry after {retry_after}")
            self.retry_after = retry_after

    error.NetworkError = NetworkError
    error.BadRequest = BadRequest
    error.RetryAfter = RetryAfter

    ext = types.ModuleType("telegram.ext")
    ext.Application = object
    ext.CallbackQueryHandler = object
    ext.CommandHandler = object
    ext.ContextTypes = types.SimpleNamespace(DEFAULT_TYPE=object)
    ext.MessageHandler = object
    ext.filters = types.SimpleNamespace()
    return {"telegram": telegram, "telegram.error": error, "telegram.ext": ext}


def loaded_telegram():
    module_name = f"herdr_telegram_test_{uuid.uuid4().hex}"
    with mock.patch.dict(os.environ, {"HERDR_TG_TOKEN": "test"}, clear=False), mock.patch.dict(
        sys.modules, _telegram_stubs(), clear=False
    ), mock.patch.object(sys, "path", [str(TELEGRAM_PATH.parent), *sys.path]):
        spec = importlib.util.spec_from_file_location(module_name, TELEGRAM_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return types.SimpleNamespace(message_id=42)


class TelegramQuestionTests(unittest.TestCase):
    def test_multi_select_notification_offers_toggles_and_submit(self):
        telegram = loaded_telegram()
        telegram.CHAT_ID = "1"
        bot = _Bot()
        app = types.SimpleNamespace(bot=bot)

        asyncio.run(
            telegram.notify_blocked(app, {
                "pane_id": "pane-1",
                "agent": "omp",
                "project": "demo",
                "prompt": "Which capabilities?",
                "prompt_id": "q1",
                "options": [],
                "multi": True,
                "interaction": "omp_question",
                "multi_options": ["Color", "Font"],
                "selected_options": [],
            })
        )

        markup = bot.sent[0]["reply_markup"]
        self.assertEqual(
            [button.text for row in markup.inline_keyboard for button in row],
            ["☐ Color", "☐ Font", "Submit", "Open output & reply"],
        )
        self.assertEqual(telegram.pending[(1, 42)], "pane-1")


if __name__ == "__main__":
    unittest.main()
