"""真人入座模式的无网络回归测试。"""
import tempfile
import unittest

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
        self.assertIsNone(self.engine.pending_human_action)

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
