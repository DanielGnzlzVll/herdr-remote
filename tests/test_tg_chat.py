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


class CardActionTests(unittest.TestCase):
    def test_new_agent_is_sent_silently(self):
        action = chat.card_action(KEY, None, view(), 2000.0)
        self.assertEqual(action, chat.Send(KEY, view(), alert=False))

    def test_new_blocked_agent_is_sent_with_alert(self):
        self.assertTrue(chat.card_action(KEY, None, view(status="blocked"), 2000.0).alert)

    def test_entering_blocked_reposts_with_alert(self):
        action = chat.card_action(KEY, shown(view()), view(status="blocked"), 2000.0)
        self.assertIsInstance(action, chat.Repost)
        self.assertTrue(action.alert)

    def test_blocked_message_after_blocked_snapshot_is_an_edit(self):
        action = chat.card_action(KEY, shown(view(status="blocked")), view(blocked=blocked_msg()), 2000.0)
        self.assertIsInstance(action, chat.Edit)

    def test_blocked_message_before_snapshot_reposts_once(self):
        first = chat.card_action(KEY, shown(view()), view(blocked=blocked_msg()), 2000.0)
        self.assertIsInstance(first, chat.Repost)
        after = shown(view(blocked=blocked_msg()))
        again = chat.card_action(KEY, after, view(status="blocked", blocked=blocked_msg()), 2000.0)
        self.assertIsNone(again)

    def test_new_prompt_id_reposts(self):
        action = chat.card_action(KEY, shown(view(blocked=blocked_msg("p1"))),
                                  view(blocked=blocked_msg("p2")), 2000.0)
        self.assertIsInstance(action, chat.Repost)
        self.assertTrue(action.alert)

    def test_update_with_same_prompt_id_is_an_edit(self):
        before = shown(view(blocked=blocked_msg(multi_options=["A"], selected_options=[])))
        after = view(blocked=blocked_msg(prompt="Proceed? [x] A", multi_options=["A"], selected_options=["A"]))
        self.assertIsInstance(chat.card_action(KEY, before, after, 2000.0), chat.Edit)

    def test_finishing_reposts_with_alert(self):
        for previous in ("working", "blocked"):
            action = chat.card_action(KEY, shown(view(status=previous)), view(status="done", last="ok"), 2000.0)
            self.assertIsInstance(action, chat.Repost)
            self.assertTrue(action.alert)

    def test_other_changes_are_silent_edits(self):
        action = chat.card_action(KEY, shown(view(status="idle")), view(status="working"), 2000.0)
        self.assertIsInstance(action, chat.Edit)
        action = chat.card_action(KEY, shown(view()), view(title="new title"), 2000.0)
        self.assertIsInstance(action, chat.Edit)

    def test_unchanged_card_needs_nothing(self):
        self.assertIsNone(chat.card_action(KEY, shown(view()), view(), 2000.0))

    def test_old_card_is_reposted_silently(self):
        action = chat.card_action(KEY, shown(view(), sent_at=0.0), view(), chat.CARD_MAX_AGE)
        self.assertEqual(action, chat.Repost(KEY, view(), alert=False))


def state_with_card(v=None, message_id=10, absent=0.0, high_water=10, swept_to=10):
    v = v or view()
    return chat.ChatState(cards={KEY: shown(v, message_id, absent=absent)},
                          high_water=high_water, swept_to=swept_to)


class PlanTests(unittest.TestCase):
    def test_plan_does_not_mutate_state(self):
        state = state_with_card(absent=5.0)
        before = chat.state_to_json(state)
        chat.plan(state, {}, 2000.0, True, 3.0, 20)
        self.assertEqual(chat.state_to_json(state), before)

    def test_absent_timer_advances_only_while_connected(self):
        state = state_with_card()
        self.assertEqual(chat.plan(state, {}, 2000.0, True, 3.0, 20), [chat.AdvanceTimers({KEY: 3.0})])
        self.assertEqual(chat.plan(state, {}, 2000.0, False, 3.0, 20), [])

    def test_reappearing_agent_resets_timer(self):
        state = state_with_card(absent=15.0)
        self.assertEqual(chat.plan(state, {KEY: view()}, 2000.0, True, 3.0, 20),
                         [chat.AdvanceTimers({KEY: 0.0})])

    def test_card_deleted_after_grace_with_linked_messages(self):
        state = state_with_card(absent=18.0, high_water=12, swept_to=12)
        chat.record_transient(state, 11, "linked", owner=KEY)
        chat.record_transient(state, 12, "linked", owner="local|other")
        actions = chat.plan(state, {}, 2000.0, True, 3.0, 20)
        self.assertEqual(actions, [chat.Delete((10, 11, 12), (KEY,))])

    def test_expired_transients_are_deleted(self):
        state = chat.ChatState(high_water=21, swept_to=21)
        chat.record_transient(state, 20, "ephemeral", deadline=1999.0)
        chat.record_transient(state, 21, "command", owner="start", deadline=3000.0)
        self.assertEqual(chat.plan(state, {}, 2000.0, True, 1.0, 20), [chat.Delete((20,), ())])

    def test_rerunning_a_command_supersedes_its_output(self):
        state = chat.ChatState()
        chat.record_transient(state, 30, "command", "agents", 9999.0, 100.0, supersede=True)
        chat.record_transient(state, 31, "command", "agents", 9999.0, 100.0)
        self.assertEqual(state.transients[30].deadline, 9999.0)
        chat.record_transient(state, 32, "command", "agents", 9999.0, 200.0, supersede=True)
        self.assertEqual((state.transients[30].deadline, state.transients[31].deadline), (200.0, 200.0))
        self.assertEqual(state.transients[32].deadline, 9999.0)

    def test_first_sweep_covers_whole_history_except_desired(self):
        state = chat.ChatState(high_water=250, swept_to=0)
        state.cards[KEY] = shown(view(), message_id=240)
        chat.record_transient(state, 245, "command", owner="start", deadline=9999.0)
        sweep = chat.plan(state, {KEY: view()}, 2000.0, True, 1.0, 20)[-1]
        self.assertEqual(sweep.new_swept_to, 250)
        self.assertEqual(len(sweep.ids), 248)
        self.assertNotIn(240, sweep.ids)
        self.assertNotIn(245, sweep.ids)
        self.assertEqual([len(b) for b in chat.batches(sweep.ids)], [100, 100, 48])

    def test_sweep_is_bounded_per_pass(self):
        state = chat.ChatState(high_water=1200, swept_to=0)
        sweep = chat.plan(state, {}, 2000.0, True, 1.0, 20)[-1]
        self.assertEqual(sweep.new_swept_to, chat.SWEEP_PASS_SPAN)
        self.assertEqual(max(sweep.ids), chat.SWEEP_PASS_SPAN)

    def test_nothing_to_sweep_when_watermarks_meet(self):
        state = state_with_card()
        self.assertEqual(chat.plan(state, {KEY: view()}, 2000.0, True, 1.0, 20), [])


class FakeTelegram:
    """Plays a plan against a fake chat, the way ChatWorker does."""

    def __init__(self, existing=()):
        self.messages = set(existing)
        self.next_id = max(self.messages, default=0) + 1
        self.online = True

    def run_pass(self, state, views, now, connected=True, elapsed=1.0):
        for action in chat.plan(state, views, now, connected, elapsed, 20):
            if isinstance(action, chat.AdvanceTimers):
                chat.apply(state, action, now)
                continue
            if not self.online:
                return
            if isinstance(action, (chat.Send, chat.Repost)):
                mid = self.next_id
                self.next_id += 1
                self.messages.add(mid)
                old = state.cards.get(action.key)
                chat.apply(state, action, now, mid)
                if isinstance(action, chat.Repost) and old:
                    self.messages.discard(old.message_id)
                    chat.apply(state, chat.Delete((old.message_id,)), now)
            elif isinstance(action, chat.Edit):
                chat.apply(state, action, now)
            elif isinstance(action, (chat.Delete, chat.Sweep)):
                self.messages.difference_update(action.ids)
                chat.apply(state, action, now)


class ConvergenceTests(unittest.TestCase):
    def test_outage_converges_with_one_action_per_card(self):
        tg = FakeTelegram()
        state = chat.ChatState()
        a, b = chat.card_key("local", "w1:p1"), chat.card_key("local", "w2:p1")
        tg.run_pass(state, {a: view("w1:p1"), b: view("w2:p1")}, 1000.0)
        cards_before = {k: c.message_id for k, c in state.cards.items()}

        tg.online = False
        tg.run_pass(state, {a: view("w1:p1", blocked=blocked_msg())}, 1001.0)
        tg.run_pass(state, {a: view("w1:p1", status="done", last="ok")}, 1002.0)
        tg.online = True
        actions = chat.plan(state, {a: view("w1:p1", status="done", last="ok")}, 1003.0, True, 1.0, 20)
        card_actions = [x for x in actions if isinstance(x, (chat.Send, chat.Edit, chat.Repost))]
        self.assertEqual(len(card_actions), 1)
        self.assertIsInstance(card_actions[0], chat.Repost)
        self.assertEqual(card_actions[0].view.status, "done")

        for t in range(20):
            tg.run_pass(state, {a: view("w1:p1", status="done", last="ok")}, 1003.0 + t)
        self.assertEqual(set(state.cards), {a})
        self.assertEqual(tg.messages, {state.cards[a].message_id})
        self.assertNotIn(cards_before[a], tg.messages)

    def test_crash_orphan_is_swept(self):
        tg = FakeTelegram()
        state = chat.ChatState()
        tg.run_pass(state, {KEY: view()}, 1000.0)
        saved = chat.state_from_json(chat.state_to_json(state))
        orphan = tg.next_id
        tg.messages.add(orphan)
        tg.next_id += 1
        tg.run_pass(saved, {KEY: view(status="done", last="ok")}, 1001.0)
        tg.run_pass(saved, {KEY: view(status="done", last="ok")}, 1002.0)
        self.assertNotIn(orphan, tg.messages)
        self.assertEqual(tg.messages, {saved.cards[KEY].message_id})

    def test_lost_state_sweeps_history_and_rebuilds(self):
        tg = FakeTelegram(existing=range(1, 51))
        state = chat.ChatState()
        tg.run_pass(state, {KEY: view()}, 1000.0)
        tg.run_pass(state, {KEY: view()}, 1001.0)
        self.assertEqual(tg.messages, {state.cards[KEY].message_id})

    def test_relay_disconnect_keeps_cards(self):
        tg = FakeTelegram()
        state = chat.ChatState()
        tg.run_pass(state, {KEY: view()}, 1000.0)
        for t in range(60):
            tg.run_pass(state, {}, 1001.0 + t, connected=False)
        self.assertIn(KEY, state.cards)
        self.assertIn(state.cards[KEY].message_id, tg.messages)


class ChatStoreTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = chat.ChatStore(os.path.join(tmp, "sub", "state.json"))
            state = state_with_card(high_water=40, swept_to=33)
            chat.record_transient(state, 40, "command", owner="start", deadline=5.0)
            store.save(state)
            loaded = store.load()
            self.assertEqual(chat.state_to_json(loaded), chat.state_to_json(state))
            self.assertEqual(os.listdir(os.path.join(tmp, "sub")), ["state.json"])

    def test_missing_or_corrupt_file_loads_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            self.assertEqual(chat.ChatStore(path).load(), chat.ChatState())
            Path(path).write_text("{not json")
            self.assertEqual(chat.ChatStore(path).load(), chat.ChatState())
            Path(path).write_text('{"cards": {}}')
            self.assertEqual(chat.ChatStore(path).load(), chat.ChatState())
            Path(path).write_text('{"cards": [], "transients": [], "high_water": 0, "swept_to": 0}')
            self.assertEqual(chat.ChatStore(path).load(), chat.ChatState())


if __name__ == "__main__":
    unittest.main()
