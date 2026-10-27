# -*- coding: utf-8 -*-
"""真人入座模式：行动协议、服务暂停/恢复与玩家视角隔离。"""
import tempfile
import time
import unittest
from unittest.mock import patch

from engine import config
from engine.game_engine import GameEngine
from service.game_service import GameService
from service.view_projection import project_view


class HumanTestManager:
    def __init__(self):
        self.memory = {seat: [] for seat in range(1, 13)}
        self.pending = {}
        self.submitted = []
        self.ready = False

    def clear(self):
        self.memory = {seat: [] for seat in range(1, 13)}
        self.pending.clear()
        self.submitted.clear()

    def init_agent(self, seat, role, style, private_memory, roster=None):
        self.memory[seat].append(("init", role))

    def inject(self, seat, text):
        self.memory[seat].append(("inject", text))

    def set_truth(self, text):
        self.truth = text

    def set_hard_facts(self, seat, text):
        self.memory[seat].append(("hard_facts", text))

    def submit_ai_response(self, seat, prompt, response_kind="speech"):
        request_id = f"ai-{seat}-{len(self.submitted)}"
        self.submitted.append((seat, response_kind, prompt))
        self.pending[request_id] = (seat, response_kind)
        return request_id

    def poll_ai_response(self, request_id):
        if not self.ready:
            return False, None
        self.pending.pop(request_id, None)
        return True, {"reasoning": "AI response", "speech": "过。", "vote_target": 0}

    def cancel_ai_response(self, request_id, reason=""):
        self.pending.pop(request_id, None)
        return {"reasoning": reason or "跳过", "speech": "过。", "vote_target": 0}

    def cancel_all_pending(self, reason=""):
        self.pending.clear()

    def has_pending(self):
        return bool(self.pending)

    def export_state(self):
        return {"memory": self.memory}

    def get_usage_snapshot(self):
        return {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0}

    def shutdown(self):
        pass


class HumanActionTests(unittest.TestCase):
    def make_engine(self):
        manager = HumanTestManager()
        engine = GameEngine(manager)
        engine.setup(config.GAME_BOARDS[config.DEFAULT_BOARD])
        return engine, manager

    def test_human_speech_waits_without_submitting_ai_request(self):
        engine, manager = self.make_engine()
        engine.human_seat = 1
        gen = engine._ask(1, "这段 AI 私密提示词不应进入真人表单", "D1 发言", public=True)
        event = next(gen)
        self.assertEqual(event["type"], "human_input_required")
        self.assertEqual(manager.submitted, [])
        action = engine.pending_human_action
        self.assertEqual(action["seat"], 1)
        self.assertEqual(action["kind"], "speech")
        self.assertNotIn("prompt", action)
        with self.assertRaises(RuntimeError):
            engine.submit_human_action("stale-action-id", {"speech": "测试发言"})
        with self.assertRaises(ValueError):
            engine.submit_human_action(action["action_id"], {"speech": "测试发言", "_is_fallback": True})
        engine.submit_human_action(action["action_id"], {"speech": "我先听听大家的发言"})
        done = next(gen)
        self.assertEqual(done["type"], "done")
        self.assertEqual(engine._last_resp["decision_source"], "human")
        self.assertEqual(engine._last_resp["speech"], "我先听听大家的发言")
        self.assertIsNone(engine.pending_human_action)
        self.assertEqual(manager.submitted, [])

    def test_human_target_is_validated_against_current_legal_targets(self):
        engine, manager = self.make_engine()
        engine.human_seat = 1
        gen = engine._ask(1, "私密投票提示", "D1 投票", response_kind="vote", allowed_targets=[0, 2])
        next(gen)
        action = engine.pending_human_action
        with self.assertRaises(ValueError):
            engine.submit_human_action(action["action_id"], {"vote_target": 3})
        engine.submit_human_action(action["action_id"], {"vote_target": 2})
        self.assertEqual(next(gen)["type"], "done")
        self.assertEqual(engine._last_resp["vote_target"], 2)
        self.assertEqual(manager.submitted, [])

    def test_ai_seat_filter_excludes_human(self):
        engine, _ = self.make_engine()
        engine.human_seat = 5
        self.assertEqual(engine._ai_seats([1, 5, 12]), [1, 12])

    def test_player_projection_hides_private_roles_and_unannounced_night_death(self):
        engine, _ = self.make_engine()
        human = next(seat for seat in engine.players if engine.roles[seat].camp != "狼人")
        engine.human_seat = human
        hidden = next(seat for seat in engine.players if seat != human)
        engine.players[hidden]["alive"] = False
        engine.published_alive[hidden] = True
        view = project_view(engine, human)
        self.assertEqual(view.players[human]["role"], engine.players[human]["role"])
        self.assertEqual(view.players[hidden]["role"], "未知")
        self.assertTrue(view.players[hidden]["alive"])
        self.assertIsNone(view.counts)
        self.assertEqual(view.role_seats, {})
        self.assertIsNone(view.human_action)

    def test_service_pauses_for_human_then_resumes_without_ai_call(self):
        engine, manager = self.make_engine()
        engine.human_seat = 1
        generator = engine._ask(1, "私密提示词", "D1 发言", public=True)
        with tempfile.TemporaryDirectory() as directory:
            service = GameService(engine, directory, automatic=True, delay=0, generator=generator)

            def wait_until(predicate, timeout=3):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if predicate():
                        return
                    time.sleep(0.01)
                self.fail("真人行动服务未在时限内到达预期状态")

            with patch("service.game_service.archive_game", return_value={"directory": directory}):
                try:
                    service.play()
                    wait_until(lambda: service.snapshot().last_event_type == "human_input_required")
                    snapshot = service.snapshot()
                    self.assertFalse(snapshot.running)
                    action = snapshot.engine.pending_human_action
                    self.assertIsNotNone(action)
                    self.assertEqual(manager.submitted, [])
                    service.submit_human_action(action["action_id"], {"speech": "真人提交"})
                    wait_until(lambda: service.snapshot().done)
                    self.assertEqual(engine._last_resp["speech"], "真人提交")
                    self.assertEqual(manager.submitted, [])
                finally:
                    service.close("test_cleanup")


if __name__ == "__main__":
    unittest.main()
