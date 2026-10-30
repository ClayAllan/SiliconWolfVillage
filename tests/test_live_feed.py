import unittest

from engine import config
from engine.game_engine import GameEngine
from service.live_feed import (feed_identity, project_events, prepare_feed_state,
                       request_scroll, unread_events, select_feed_window, schedule_page_refresh)
from test_runtime_regressions import MemoryOnlyManager
from service.view_projection import project_view
from storage.immutable_log import FrozenDict, freeze_log_row


class CountingRows(list):
    def __init__(self, rows):
        super().__init__(rows)
        self.read_count = 0

    def __getitem__(self, index):
        self.read_count += 1
        return super().__getitem__(index)


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


    def test_project_events_shares_only_frozen_full_view_rows(self):
        frozen = freeze_log_row({
            "game_id": "g", "event_seq": 1, "kind": "player",
            "role": "狼人", "text": "公开发言",
            "meta": {"labels": ["public", "verified"]},
        })

        full, issues = project_events([frozen], "g", None)
        self.assertFalse(issues)
        self.assertIs(full[0], frozen)

        limited, issues = project_events([frozen], "g", None, limited=True)
        self.assertFalse(issues)
        self.assertIsNot(limited[0], frozen)
        self.assertIsInstance(limited[0], dict)
        self.assertNotIn("role", limited[0])
        self.assertIs(limited[0]["meta"], frozen["meta"])
        limited[0]["text"] = "view-only edit"
        self.assertEqual(frozen["text"], "公开发言")
        with self.assertRaises(TypeError):
            limited[0]["meta"]["labels"].append("changed")

    def test_project_events_deepcopies_mutable_legacy_rows(self):
        source = {
            "game_id": "g", "event_seq": 1, "kind": "player",
            "text": "legacy", "meta": {"labels": ["source"]},
        }
        projected, issues = project_events([source], "g", None)
        self.assertFalse(issues)
        self.assertIsNot(projected[0], source)
        self.assertIsNot(projected[0]["meta"], source["meta"])
        projected[0]["meta"]["labels"].append("projection")
        self.assertEqual(source["meta"]["labels"], ["source"])

    def test_project_view_shares_only_frozen_full_rows_and_sanitizes_limited_rows(self):
        engine = GameEngine(MemoryOnlyManager())
        engine.setup(config.GAME_BOARDS[config.DEFAULT_BOARD])
        viewer = next(
            seat for seat in range(1, 13)
            if engine.roles[seat].camp != "狼人"
        )
        engine._append_public_item({
            "kind": "player", "seat": viewer, "role": "狼人",
            "text": "公开发言", "meta": {"labels": ["public"]},
        })

        full = project_view(engine)
        limited = project_view(engine, viewer_seat=viewer)
        source = engine.public_log[-1]
        self.assertIsNot(full.public_log, engine.public_log)
        self.assertIs(full.public_log[-1], source)
        self.assertIsInstance(full.public_log[-1], FrozenDict)
        self.assertIsNot(limited.public_log, engine.public_log)
        self.assertIsNot(limited.public_log[-1], source)
        self.assertNotIn("role", limited.public_log[-1])
        self.assertIs(limited.public_log[-1]["meta"], source["meta"])
        with self.assertRaises(TypeError):
            full.public_log[-1]["meta"]["labels"].append("changed")
        with self.assertRaises(TypeError):
            limited.public_log[-1]["meta"]["labels"].append("changed")

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

    def test_bounded_feed_window_matches_channel_and_counts_unread_without_full_projection(self):
        rows = [
            {"event_seq": i, "kind": "player" if i % 3 == 0 else "judge", "text": str(i)}
            for i in range(1, 301)
        ]
        state = {"feed_follow_latest": True}
        visible, unseen, truncated, tail = select_feed_window(
            state, rows, show_god=True, channel="玩家发言", full_history=False, limit=5,
        )
        self.assertEqual([row["event_seq"] for row in visible], [288, 291, 294, 297, 300])
        self.assertEqual(unseen, 0)
        self.assertTrue(truncated)
        self.assertEqual(tail, 300)
        self.assertEqual(state["_feed_seen_seq"], 300)

        state["feed_follow_latest"] = False
        rows.extend([
            {"event_seq": 301, "kind": "judge", "text": "not player"},
            {"event_seq": 302, "kind": "player", "text": "new player"},
            {"event_seq": 303, "kind": "player", "text": "new player"},
        ])
        visible, unseen, truncated, tail = select_feed_window(
            state, rows, show_god=True, channel="玩家发言", full_history=False, limit=5,
        )
        self.assertEqual(unseen, 2)
        self.assertEqual([row["event_seq"] for row in visible], [294, 297, 300, 302, 303])
        self.assertEqual(tail, 303)
        self.assertTrue(truncated)

    def test_full_history_feed_window_preserves_all_filtered_events(self):
        rows = [{"event_seq": i, "kind": "god" if i % 2 else "player"} for i in range(1, 8)]
        state = {"feed_follow_latest": False, "_feed_seen_seq": 2}
        visible, unseen, truncated, tail = select_feed_window(
            state, rows, show_god=False, channel="全部动态", full_history=True, limit=2,
        )
        self.assertEqual([row["event_seq"] for row in visible], [2, 4, 6])
        self.assertEqual(unseen, 2)
        self.assertFalse(truncated)
        self.assertEqual(tail, 6)

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


    def test_follow_latest_stops_after_first_matching_row_beyond_cap(self):
        rows = CountingRows(
            {"event_seq": i, "kind": "player"} for i in range(1, 1001)
        )
        state = {"feed_follow_latest": True, "_feed_seen_seq": 0}
        visible, unseen, truncated, tail = select_feed_window(
            state, rows, show_god=True, channel="全部动态",
            full_history=False, limit=5,
        )

        self.assertEqual([row["event_seq"] for row in visible], [996, 997, 998, 999, 1000])
        self.assertEqual(unseen, 0)
        self.assertTrue(truncated)
        self.assertEqual(tail, 1000)
        self.assertEqual(state["_feed_seen_seq"], 1000)
        self.assertEqual(rows.read_count, 6)

    def test_paused_window_counts_new_rows_until_seen_cursor_then_stops(self):
        rows = CountingRows(
            {"event_seq": i, "kind": "player"} for i in range(1, 1001)
        )
        state = {"feed_follow_latest": False, "_feed_seen_seq": 990}
        visible, unseen, truncated, tail = select_feed_window(
            state, rows, show_god=True, channel="全部动态",
            full_history=False, limit=5,
        )

        self.assertEqual([row["event_seq"] for row in visible], [996, 997, 998, 999, 1000])
        self.assertEqual(unseen, 10)
        self.assertTrue(truncated)
        self.assertEqual(tail, 1000)
        self.assertEqual(state["_feed_seen_seq"], 990)
        # Five visible rows, five additional unread rows, then the seen cursor.
        self.assertEqual(rows.read_count, 11)

    def test_mark_seen_stops_after_window_without_enabling_follow(self):
        rows = CountingRows(
            {"event_seq": i, "kind": "player"} for i in range(1, 1001)
        )
        state = {
            "feed_follow_latest": False,
            "_feed_seen_seq": 100,
            "_feed_mark_seen": True,
        }
        visible, unseen, truncated, tail = select_feed_window(
            state, rows, show_god=True, channel="全部动态",
            full_history=False, limit=5,
        )

        self.assertEqual([row["event_seq"] for row in visible], [996, 997, 998, 999, 1000])
        self.assertEqual(unseen, 0)
        self.assertTrue(truncated)
        self.assertEqual(tail, 1000)
        self.assertEqual(state["_feed_seen_seq"], 1000)
        self.assertFalse(state["feed_follow_latest"])
        self.assertNotIn("_feed_mark_seen", state)
        self.assertEqual(rows.read_count, 6)

    def test_paused_from_zero_cursor_counts_all_unread_matching_rows(self):
        rows = CountingRows(
            {"event_seq": i, "kind": "player"} for i in range(1, 21)
        )
        state = {"feed_follow_latest": False, "_feed_seen_seq": 0}
        visible, unseen, truncated, tail = select_feed_window(
            state, rows, show_god=True, channel="全部动态",
            full_history=False, limit=5,
        )

        self.assertEqual([row["event_seq"] for row in visible], [16, 17, 18, 19, 20])
        self.assertEqual(unseen, 20)
        self.assertTrue(truncated)
        self.assertEqual(tail, 20)
        # With no prior cursor, exact unread count requires scanning all rows.
        self.assertEqual(rows.read_count, 20)

    def test_judge_channel_filters_before_bounded_window_and_early_exit(self):
        kinds = ("god", "player", "judge", "win", "votechart", "other")
        rows = CountingRows(
            {"event_seq": i, "kind": kinds[(i - 1) % len(kinds)]}
            for i in range(1, 31)
        )
        state = {"feed_follow_latest": True, "_feed_seen_seq": 0}
        visible, unseen, truncated, tail = select_feed_window(
            state, rows, show_god=False, channel="法官播报",
            full_history=False, limit=2,
        )

        self.assertEqual([row["event_seq"] for row in visible], [28, 29])
        self.assertEqual(unseen, 0)
        self.assertTrue(truncated)
        self.assertEqual(tail, 29)
        # Skip row 30 (not in the channel), then read two visible and one extra.
        self.assertEqual(rows.read_count, 4)

    def test_single_player_projection_is_applied_before_feed_selection(self):
        engine = GameEngine(MemoryOnlyManager())
        engine.setup(config.GAME_BOARDS[config.DEFAULT_BOARD])
        viewer = next(
            seat for seat in range(1, 13)
            if engine.roles[seat].camp != "狼人"
        )
        hidden = next(seat for seat in range(1, 13) if seat != viewer)
        engine.public_log = [
            {
                "game_id": engine.game_id, "event_seq": 1, "kind": "god",
                "role": "狼人", "text": "法官私密信息",
            },
            {
                "game_id": engine.game_id, "event_seq": 2, "kind": "player",
                "seat": hidden, "role": "狼人", "text": "公开发言",
            },
        ]
        engine.reasoning_log[hidden].append({
            "label": "D1 私密推理",
            "reasoning": "其他玩家的私密推理不应泄露",
            "speech": "公开发言",
        })

        view = project_view(engine, viewer_seat=viewer)
        self.assertEqual(view.players[hidden]["role"], "未知")
        self.assertEqual(view.reasoning_log[hidden], [])
        visible, _unseen, _truncated, _tail = select_feed_window(
            {"feed_follow_latest": True, "_feed_seen_seq": 0},
            view.public_log, show_god=False, channel="全部动态",
            full_history=False, limit=5,
        )

        self.assertEqual([row["text"] for row in visible], ["公开发言"])
        self.assertNotIn("role", visible[0])
        self.assertNotIn("法官私密信息", [row["text"] for row in visible])


if __name__ == "__main__":
    unittest.main()
