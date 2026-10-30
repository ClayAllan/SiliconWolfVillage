"""真人入座模式的无网络回归测试。"""
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from engine import config
from engine.game_engine import GameEngine
from service.game_service import GameService
from service.view_projection import project_view
from test_runtime_regressions import MemoryOnlyManager


class HumanSeatTests(unittest.TestCase):
    def setUp(self):
        self.manager = MemoryOnlyManager()
        self.manager.has_pending = lambda: False
        self.manager.get_usage_snapshot = lambda: {}
        self.engine = GameEngine(self.manager)
        self.engine.human_seat = 1
        self.engine.setup(config.GAME_BOARDS[config.DEFAULT_BOARD])

    def test_human_action_is_requested_without_ai_call(self):
        gen = self.engine._ask(1, "private prompt must not be displayed", "D1 发言", public=True)
        event = next(gen)
        self.assertEqual(event["type"], "human_input_required")
        pending = self.engine.pending_human_action
        self.assertEqual(pending["seat"], 1)
        self.assertEqual(pending["kind"], "speech")
        self.engine.submit_human_action(pending["action_id"], {"speech": "我先说明判断依据。"})
        event = next(gen)
        self.assertEqual(event["type"], "done")
        self.assertEqual(self.engine.reasoning_log[1][-1]["speech"], "我先说明判断依据。")
        self.assertEqual(self.engine.reasoning_log[1][-1]["actor_source"], "human")
        self.assertIsNone(self.engine.pending_human_action)

    def test_human_wolf_is_prompted_during_night_pack_discussion(self):
        # Exercise the actual night path, not only a direct call to _ask().
        self.engine.human_seat = self.engine.wolves[0]
        gen = self.engine._run_wolf_pack_night("D1 夜晚")
        event = next(gen)
        self.assertEqual(event["type"], "human_input_required")
        self.assertEqual(self.engine.pending_human_action["seat"], self.engine.human_seat)
        self.assertIn("狼人讨论", self.engine.pending_human_action["label"])

    def test_ai_response_cannot_be_recorded_as_human_action(self):
        with self.assertRaisesRegex(RuntimeError, "真人席位 01"):
            self.engine._record_reasoning(
                1, "D1 警下投票", "AI生成的理由", "（暗投：2号）",
                response={"reasoning": "AI生成的理由", "vote_target": 2}, request_id="ai-request")

    def test_stale_or_duplicate_action_is_rejected(self):
        gen = self.engine._ask(1, "prompt", "D1 投票", response_kind="vote")
        next(gen)
        pending = self.engine.pending_human_action
        with self.assertRaises(ValueError):
            self.engine.submit_human_action("stale-id", {"vote_target": 2})
        self.engine.submit_human_action(pending["action_id"], {"vote_target": 2})
        with self.assertRaises(RuntimeError):
            self.engine.submit_human_action(pending["action_id"], {"vote_target": 2})

    def test_invalid_vote_target_is_rejected(self):
        gen = self.engine._ask(1, "prompt", "D1 投票", response_kind="vote", allowed_targets=[0, 2])
        next(gen)
        pending = self.engine.pending_human_action
        with self.assertRaises(Exception):
            self.engine.submit_human_action(pending["action_id"], {"vote_target": 3})

    def test_service_pauses_and_resumes_after_human_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            gen = self.engine._ask(1, "prompt", "D1 发言", public=True)
            service = GameService(self.engine, directory, automatic=False, generator=gen)
            try:
                self.assertTrue(service.step())
                self.assertEqual(service.snapshot().last_event_type, "human_input_required")
                pending = service.snapshot().engine.pending_human_action
                self.assertTrue(service.submit_human_action(pending["action_id"], {"speech": "提交发言。"}))
                self.assertEqual(service.snapshot().last_event_type, "done")
            finally:
                service.close()

    def test_service_pauses_if_human_seat_has_an_ai_action_in_history(self):
        with tempfile.TemporaryDirectory() as directory:
            self.engine.reasoning_log[1].append({
                "label": "D1 警下投票", "reasoning": "AI生成的理由",
                "speech": "（暗投：2号）",
                "response": {"reasoning": "AI生成的理由", "vote_target": 2},
            })
            service = GameService(self.engine, directory, automatic=False,
                                  generator=self.engine._ask(2, "prompt", "D1 发言"))
            try:
                self.assertFalse(service.step())
                snapshot = service.snapshot()
                self.assertIn("真人席位 01", snapshot.error)
                self.assertFalse(snapshot.running)
                self.assertEqual(snapshot.last_event_type, "human_action_integrity_error")
            finally:
                service.close()

    def test_round_trip_save_preserves_human_seat_and_private_messages(self):
        seat = self.engine.wolves[0]
        self.engine.human_seat = seat
        self.engine.human_private_messages = ["私人查验结果，仅真人可见"]
        data = self.engine.export_state()
        self.assertEqual(data["human_seat"], seat)
        self.assertEqual(data["human_private_messages"], ["私人查验结果，仅真人可见"])
        restored = GameEngine(MemoryOnlyManager())
        restored.load_state(data)
        self.assertEqual(restored.human_seat, seat)
        self.assertEqual(restored.human_private_messages, ["私人查验结果，仅真人可见"])
        self.assertIsNone(restored.pending_human_action)
        self.assertIsNone(restored._pending_human_context)
        # The protected seat is still routed through a human-only form.
        request = restored._ask(seat, "PRIVATE", "D2 狼人讨论")
        self.assertEqual(next(request)["type"], "human_input_required")
        self.assertEqual(restored.pending_human_action["seat"], seat)

    def test_invalid_human_seat_is_rejected_before_restore(self):
        data = self.engine.export_state()
        for invalid in (True, False, 0, 13, "1", 1.0):
            with self.subTest(invalid=invalid):
                data["human_seat"] = invalid
                with self.assertRaisesRegex(ValueError, "真人座位无效"):
                    GameEngine(MemoryOnlyManager()).load_state(data)

    def test_legacy_save_with_human_actions_fails_closed(self):
        data = self.engine.export_state()
        data.pop("human_seat")
        data.pop("human_private_messages")
        data["reasoning_log"]["1"].append({
            "label": "D1 发言", "actor_source": "human",
            "speech": "真人已经行动", "response": {"decision_source": "human"},
        })
        with self.assertRaisesRegex(ValueError, "真人行动记录与真人座位不一致"):
            GameEngine(MemoryOnlyManager()).load_state(data)

    def test_human_waiting_event_is_not_recorded_repeatedly(self):
        emitted = []
        self.engine._record_event = lambda event, **payload: emitted.append(event)
        self.engine._ev("human_input_required", "等待真人")
        for _ in range(50):
            self.engine._ev("human_waiting", "等待真人")
        self.assertEqual(emitted, ["engine_yield"])

    def test_automatic_mode_waits_for_real_action_and_resumes_ai_flow(self):
        self.engine.human_seat = 1
        self.manager.ready = True

        def scripted_game():
            yield from self.engine._ask(2, "AI 回合", "D1 AI 发言", public=True)
            yield from self.engine._ask(1, "真人回合", "D1 真人发言", public=True)
            self.engine.winner = "好人"
            self.engine.phase = "游戏结束"
            yield self.engine._ev("end", "游戏结束")

        with tempfile.TemporaryDirectory() as directory:
            service = GameService(self.engine, directory, automatic=True, delay=0,
                                  turbo=True, generator=scripted_game())
            try:
                service.play()
                with service._condition:
                    stopped = service._condition.wait_for(
                        lambda: bool(self.engine.pending_human_action)
                        and not service._running and service._worker is None,
                        timeout=4,
                    )
                self.assertTrue(stopped, "未正常进入真人输入等待边界")
                self.assertEqual(self.manager.submitted[0][0], 2)
                self.assertEqual(self.engine.reasoning_log[2][-1]["actor_source"], "ai")
                pending = service.snapshot().engine.pending_human_action
                self.assertEqual(service.snapshot().runtime_diagnostics["wait_kind"], "human_input")
                diag = service.snapshot().runtime_diagnostics
                self.assertIsNotNone(diag["waiting_since"])
                self.assertGreaterEqual(diag["waiting_age_seconds"], 0.0)

                # The reported age is rounded to one decimal place. Advance the
                # service clock deterministically so the test proves it grows,
                # rather than merely accepting any non-negative initial value.
                waiting_since = diag["waiting_since"]
                with patch("service.game_service.time.time",
                           return_value=waiting_since + 1.26):
                    advanced_age = service.snapshot().runtime_diagnostics["waiting_age_seconds"]
                self.assertEqual(advanced_age, 1.3)
                self.assertGreater(advanced_age, diag["waiting_age_seconds"])

                revision = service.revision
                service.play()  # UI's old "continue" must not spin on human_waiting.
                self.assertFalse(service.step())
                self.assertEqual(service.revision, revision)
                self.assertFalse(service.snapshot().running)
                self.assertIsNone(service._worker)
                self.assertEqual(len(self.manager.submitted), 1)
                with self.assertRaisesRegex(RuntimeError, "提交当前真人行动"):
                    service.save(directory)

                self.assertTrue(service.submit_human_action(pending["action_id"], {
                    "speech": "我是本局真人，明确提交发言。",
                }))
                with service._condition:
                    finished = service._condition.wait_for(lambda: service._done, timeout=4)
                self.assertTrue(finished, "真人提交后自动模式未能恢复到终局")
                self.assertEqual(self.engine.reasoning_log[1][-1]["actor_source"], "human")
                self.assertEqual(self.engine.reasoning_log[1][-1]["speech"],
                                 "我是本局真人，明确提交发言。")
                self.assertEqual([seat for seat, *_ in self.manager.submitted], [2])
                self.assertIsNone(self.engine.pending_human_action)
                terminal_diag = service.snapshot().runtime_diagnostics
                self.assertIsNone(terminal_diag["waiting_since"])
                self.assertIsNone(terminal_diag["waiting_age_seconds"])
            finally:
                service.close()

    def test_manual_mode_rejects_save_until_human_submits(self):
        # The offline manager intentionally lacks persistence; add just the
        # minimal save contract to test the real service serialization path.
        self.manager.export_state = lambda: {"memory": self.manager.memory}
        with tempfile.TemporaryDirectory() as directory:
            gen = self.engine._ask(1, "private", "D1 真人发言", public=True)
            service = GameService(self.engine, directory, automatic=False, generator=gen)
            try:
                self.assertTrue(service.step())
                self.assertFalse(service.step())
                with self.assertRaisesRegex(RuntimeError, "提交当前真人行动"):
                    service.save(directory)
                pending = service.snapshot().engine.pending_human_action
                self.assertTrue(service.submit_human_action(pending["action_id"], {
                    "speech": "完成本次行动后可以保存。",
                }))
                filename = service.save(directory)
                self.assertTrue((Path(directory) / filename).exists())
                saved = json.loads((Path(directory) / filename).read_text(
                    encoding="utf-8"
                ))
                self.assertEqual(saved["engine"]["human_seat"], 1)
            finally:
                service.close()

    def test_human_projection_hides_unpublished_death_state(self):
        viewer = next(s for s in range(1, 13)
                      if self.engine.roles[s].camp != "狼人" and s != 2)
        self.engine.human_seat = viewer
        self.engine.players[2]["alive"] = False
        self.engine.published_alive[2] = True
        view = project_view(self.engine, viewer_seat=viewer)
        self.assertEqual(view.players[2]["role"], "未知")
        self.assertTrue(view.players[2]["alive"])
        self.assertEqual(view.role_seats, {})
        self.assertIsNone(view.counts)


if __name__ == "__main__":
    unittest.main()
