import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "relay"))

import tg_chat as chat  # noqa: E402

OPEN = (("Open output & reply", "cb"),)


def agent(pane="w1:p1", status="working", **extra):
    return {"pane_id": pane, "agent": "claude", "status": status, "project": "anima",
            "host": "local", **extra}


def view(pane="w1:p1", status="working", blocked=None, last="", tab="[1] logs", **extra):
    return chat.render_card(agent(pane, status, **extra), tab, blocked, last, (OPEN,))


def shown(v, message_id=10, sent_at=1000.0, absent=0.0):
    return chat.CardState(message_id, sent_at, v.status, v.prompt_id, v.rendered_hash,
                          v.last_message, absent)


def blocked_msg(prompt_id="p1", **extra):
    return {"type": "blocked", "pane_id": "w1:p1", "host": "local", "prompt": "Proceed?",
            "prompt_id": prompt_id, "options": ["Yes", "No"], "interaction": "numbered", **extra}


KEY = chat.card_key("local", "w1:p1")


class ExtractLastMessageTests(unittest.TestCase):
    def test_keeps_last_marker_block_and_strips_chrome(self):
        screen = "● first\n\n● All tests pass.\nDetails here.\n\n────\n❯ \n  41% of 200k tokens | x\n"
        self.assertEqual(chat.extract_last_message(screen), "All tests pass.\nDetails here.")

    def test_without_marker_returns_dechromed_tail(self):
        self.assertEqual(chat.extract_last_message("hello\nworld\n────\n"), "hello\nworld")

    def test_empty_screen_is_empty(self):
        self.assertEqual(chat.extract_last_message("\n────\n"), "")


class RenderCardTests(unittest.TestCase):
    def test_working_card_header(self):
        v = view(title="Jira ticket ANIMA-154")
        self.assertEqual(v.text, "🟡 WORKING · [1] logs\n📁 anima · claude\nJira ticket ANIMA-154")
        self.assertEqual(v.status, "working")

    def test_tab_name_fallbacks(self):
        self.assertIn("· my-label", view(tab="", label="my-label").text.splitlines()[0])
        self.assertIn("· anima", view(tab="").text.splitlines()[0])
        bare = chat.render_card({"pane_id": "w9:p9", "status": "idle"}, "", None, "", ())
        self.assertEqual(bare.text, "🟢 IDLE · w9:p9")

    def test_remote_host_is_shown(self):
        self.assertIn("📁 anima · claude @build-box", view(host="build-box").text)

    def test_done_card_carries_last_message_trimmed(self):
        v = view(status="done", last="x" * 700 + "END")
        self.assertTrue(v.text.endswith("END"))
        self.assertEqual(len(v.text.split("\n\n", 1)[1]), chat.EXCERPT_CHARS)

    def test_working_card_has_no_body_or_last_message(self):
        v = view(status="working", last="stale")
        self.assertNotIn("stale", v.text)
        self.assertEqual(v.last_message, "")

    def test_blocked_message_makes_card_blocked_with_prompt(self):
        v = view(status="working", blocked=blocked_msg())
        self.assertEqual(v.status, "blocked")
        self.assertEqual(v.prompt_id, "p1")
        self.assertTrue(v.text.startswith("🔴 BLOCKED"))
        self.assertIn("Proceed?", v.text)

    def test_blocked_snapshot_without_message_has_no_prompt(self):
        v = view(status="blocked")
        self.assertEqual((v.status, v.prompt_id), ("blocked", ""))

    def test_text_field_shows_waiting_line_and_typed_value(self):
        v = view(blocked=blocked_msg(text_field=True, text_value="dark mode"))
        self.assertIn("Typed so far: dark mode", v.text)
        self.assertTrue(v.text.endswith(chat.WAITING_FOR_TEXT))

    def test_prompt_without_options_asks_for_reply(self):
        v = view(blocked=blocked_msg(options=[], interaction="prompt"))
        self.assertTrue(v.text.endswith(chat.REPLY_TO_ANSWER))

    def test_multi_question_has_no_reply_hint(self):
        v = view(blocked=blocked_msg(options=[], multi_options=["A", "B"], interaction="multi_question"))
        self.assertNotIn(chat.REPLY_TO_ANSWER, v.text)

    def test_text_is_capped_at_telegram_limit(self):
        v = view(blocked=blocked_msg(prompt="y" * 5000), title="t" * 3900)
        self.assertLessEqual(len(v.text), chat.TEXT_LIMIT)

    def test_hash_changes_with_buttons(self):
        a = chat.render_card(agent(), "t", None, "", (OPEN,))
        b = chat.render_card(agent(), "t", None, "", ())
        self.assertNotEqual(a.rendered_hash, b.rendered_hash)


if __name__ == "__main__":
    unittest.main()
