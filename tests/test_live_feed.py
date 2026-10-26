import unittest

from engine import config
from engine.game_engine import GameEngine
from service.live_feed import (feed_identity, project_events, prepare_feed_state,
                       request_scroll, unread_events, schedule_page_refresh)
from test_runtime_regressions import MemoryOnlyManager


class LiveFeedTests(unittest.TestCase):
    def test_foreign_game_and_premature_win_are_isolated_without_mutating_rows(self):
        rows = [{"game_id":"old","kind":"win","text":"狼人胜"},
                {"game_id":"new","kind":"win","text":"不应存在"},
                {"game_id":"new","event_seq":3,"kind":"judge","text":"继续"}]
        result, issues = project_events(rows, "new", None)
        self.assertEqual([r["text"] for r in result], ["继续"])
        self.assertEqual(issues, ("foreign_game", "premature_win"))
        self.assertEqual(len(rows), 3)

    def test_terminal_row_is_kept_only_for_own_terminal_game(self):
        result, issues = project_events([{"game_id":"own","kind":"win","text":"结束"}], "own", "好人")
        self.assertEqual(len(result), 1)
        self.assertFalse(issues)

    def test_legacy_snapshot_is_bound_and_single_view_removes_private_rows_and_role(self):
        original = [{"kind":"god","text":"秘密"}, {"kind":"player","seat":3,"role":"狼人","text":"我是民"}]
        result, _ = project_events(original, "snapshot-owner", None, limited=True)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["game_id"], "snapshot-owner")
        self.assertNotIn("role", result[0])
        self.assertNotIn("game_id", original[1])

    def test_display_identity_is_stable_within_game_and_changes_on_context_switch(self):
        identity = feed_identity("a", "全知视角", 0, "全部动态")
        self.assertEqual(identity, feed_identity("a", "全知视角", 0, "全部动态"))
        for values in [("b","全知视角",0,"全部动态"),("a","单一视角",7,"全部动态"),("a","全知视角",0,"玩家发言")]:
            self.assertNotEqual(identity, feed_identity(*values))

    def test_new_game_resets_scroll_without_resetting_model_or_game_settings(self):
        state = {"custom_model":"keep"}
        prepare_feed_state(state, "old")
        request_scroll(state, "top")
        state["_feed_seen_seq"] = 100
        prepare_feed_state(state, "new")
        self.assertTrue(state["feed_follow_latest"])
        self.assertEqual(state["_feed_seen_seq"], 0)
        self.assertIsNone(state["_feed_scroll_action"])
        self.assertEqual(state["custom_model"], "keep")

    def test_top_exits_follow_and_bottom_once_does_not_reenable_it(self):
        state = {}
        prepare_feed_state(state, "game")
        request_scroll(state, "top")
        self.assertFalse(state["feed_follow_latest"])
        request_scroll(state, "bottom")
        self.assertFalse(state["feed_follow_latest"])
        self.assertEqual(state["_feed_scroll_action"], "bottom")

    def test_unread_uses_event_cursor_even_when_90_row_window_stays_same_size(self):
        state = {"feed_follow_latest":True}
        rows = [{"event_seq":i} for i in range(1,91)]
        self.assertEqual(unread_events(state, rows), 0)
        state["feed_follow_latest"] = False
        rows = [{"event_seq":i} for i in range(2,92)]
        self.assertEqual(unread_events(state, rows), 1)
        request_scroll(state, "bottom")
        self.assertEqual(unread_events(state, rows), 0)
        self.assertFalse(state["feed_follow_latest"])

    def test_refresh_first_call_returns_normally_and_only_later_tick_requests_app_refresh(self):
        class UI:
            def fragment(self, **kwargs):
                self.interval = kwargs["run_every"]
                def decorate(fn):
                    self.tick = fn
                    return fn
                return decorate
            def rerun(self, **kwargs):
                self.requested = kwargs["scope"]
        ui = UI()
        schedule_page_refresh(ui, True, 1.5)
        self.assertFalse(hasattr(ui, "requested"))
        ui.tick()
        self.assertEqual(ui.requested, "app")
        self.assertEqual(ui.interval, 1.5)

    def test_each_public_event_gets_own_game_id_and_monotonic_sequence_after_load(self):
        engine = GameEngine(MemoryOnlyManager())
        engine.setup(config.GAME_BOARDS[config.DEFAULT_BOARD])
        engine._log_judge("开始")
        engine._log_player(1, "我暂不站边")
        engine._log_votechart("第1天放逐", {2:1}, {1:2}, 2)
        seqs = [r["event_seq"] for r in engine.public_log]
        self.assertEqual(seqs, sorted(set(seqs)))
        self.assertTrue(all(r["game_id"] == engine.game_id for r in engine.public_log))
        restored = GameEngine(MemoryOnlyManager())
        restored.load_state(engine.export_state())
        restored._log_judge("恢复")
        self.assertEqual(restored.public_log[-1]["event_seq"], seqs[-1] + 1)


if __name__ == "__main__":
    unittest.main()
