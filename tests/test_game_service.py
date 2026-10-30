"""不访问真实 API，验证对局生命周期与前端脱离后的执行行为。"""
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from engine import config
from engine.game_engine import GameEngine
from service.game_service import GameService
from test_runtime_regressions import MemoryOnlyManager


class ServiceManager(MemoryOnlyManager):
    good_model = wolf_model = "test-model"

    def set_recorder(self, recorder, context_provider=None):
        self.recorder = recorder

    def has_pending(self):
        return bool(self.pending)

    def export_state(self):
        return {"memory": self.memory}

    def get_usage_snapshot(self):
        return {"total_tokens": 0}


class CountingEngine(GameEngine):
    def run(self):
        while True:
            self.day += 1
            time.sleep(0.002)
            yield {"type": "night", "status": f"step {self.day}"}


class FiniteEngine(GameEngine):
    def run(self):
        for day in range(1, 4):
            self.day = day
            yield {"type": "night", "status": str(day)}
        self.winner = "好人"
        self.phase = "游戏结束"
        yield {"type": "end", "status": "游戏结束"}


class FatalEngine(GameEngine):
    def run(self):
        self.day = 1
        self.fatal_error = "HTTP 402"
        yield {"type": "done", "status": "请求返回"}
        self.day = 2
        yield {"type": "night", "status": "下一步"}


class AskEngine(GameEngine):
    def run(self):
        yield from self._ask(1, "请发言", "D1 发言", public=True)
        self.day += 1
        yield {"type": "night", "status": "下一步"}


class GameServiceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.directory = directory.name
        self.addCleanup(directory.cleanup)
        # 缩短事件间隔，仍通过真实 Condition 和后台线程执行。
        pacing = patch("service.game_service.rerun_delay", return_value=0.005)
        pacing.start()
        self.addCleanup(pacing.stop)

    def make_service(self, engine_class=CountingEngine, **kwargs):
        engine = engine_class(ServiceManager())
        engine.setup(config.GAME_BOARDS[config.DEFAULT_BOARD])
        service = GameService(engine, self.directory, **kwargs)
        self.addCleanup(service.close)
        return engine, service

    def wait_until(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            threading.Event().wait(0.005)
        self.fail("后台服务未在时限内到达预期状态")

    def test_background_finishes_and_archives_without_a_viewer(self):
        engine, service = self.make_service(FiniteEngine)
        service.play()
        # 等待磁盘结果，期间不调用 snapshot/step 或任何前端操作。
        self.wait_until(lambda: (Path(self.directory) / "index.csv").exists())
        snapshot = service.snapshot()
        self.assertTrue(snapshot.done)
        self.assertFalse(snapshot.running)
        self.assertEqual(engine.day, 3)
        archive = Path(snapshot.last_archive["directory"])
        self.assertTrue((archive / "final_state.json").exists())
        self.assertTrue((archive / "summary.md").exists())

    def test_runtime_snapshot_reports_worker_and_progress_state(self):
        _, service = self.make_service()
        initial = service.snapshot().runtime_diagnostics
        self.assertEqual(initial["wait_kind"], "paused")
        self.assertFalse(initial["worker_expected"])
        self.assertIsNotNone(initial["last_progress_at"])

        service.play()
        self.wait_until(lambda: service.snapshot().runtime_diagnostics["worker_alive"])
        running = service.snapshot().runtime_diagnostics
        self.assertTrue(running["worker_expected"])
        self.assertTrue(running["worker_alive"])
        self.assertGreaterEqual(running["revision"], 1)
        self.assertIn("last_progress_age_seconds", running)

        service.pause()
        self.wait_until(lambda: not service.snapshot().runtime_diagnostics["worker_alive"])
        paused = service.snapshot().runtime_diagnostics
        self.assertFalse(paused["worker_expected"])
        self.assertEqual(paused["wait_kind"], "paused")

    def test_pause_is_stable_and_resume_advances(self):
        engine, service = self.make_service()
        service.play()
        self.wait_until(lambda: service.snapshot().engine.day >= 2)
        service.pause()
        paused_day = engine.day
        threading.Event().wait(0.04)
        self.assertEqual(engine.day, paused_day)
        service.play()
        self.wait_until(lambda: service.snapshot().engine.day > paused_day)

    def test_unexpected_scheduler_error_pauses_worker_and_allows_resume(self):
        engine, service = self.make_service()
        with patch("service.game_service.rerun_delay", side_effect=RuntimeError("scheduler fault")):
            service.play()
            self.wait_until(lambda: bool(service.snapshot().error))
            failed = service.snapshot()
            self.assertFalse(failed.running)
            self.assertFalse(failed.done)
            self.assertIn("scheduler fault", failed.error)
            self.assertEqual(failed.last_event_type, "worker_error")
            self.assertIsNone(service._worker)
            self.assertEqual(failed.runtime_diagnostics["wait_kind"], "error_paused")
            self.assertFalse(failed.runtime_diagnostics["worker_expected"])
            self.assertFalse(failed.runtime_diagnostics["worker_alive"])

        paused_day = engine.day
        service.play()
        self.wait_until(lambda: engine.day > paused_day)
        self.assertTrue(service.snapshot().running)

    def test_repeated_play_and_concurrent_manual_steps_are_serialized(self):
        engine, service = self.make_service(automatic=False)
        for _ in range(20):
            service.play()
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertTrue(all(pool.map(lambda _: service.step(), range(32))))
        self.assertEqual(engine.day, 32)
        self.assertEqual(service.snapshot().error, "")

    def test_disabling_automatic_and_manual_step(self):
        engine, service = self.make_service()
        service.play()
        with self.assertRaisesRegex(RuntimeError, "先暂停"):
            service.step()
        service.configure(automatic=False, delay=0)
        before = engine.day
        threading.Event().wait(0.03)
        self.assertEqual(engine.day, before)
        service.step()
        self.assertEqual(engine.day, before + 1)

    def test_snapshot_does_not_share_mutable_game_state_or_client(self):
        engine, service = self.make_service()
        public_index = len(engine.public_log)
        reasoning_index = len(engine.reasoning_log[1])
        engine._append_public_item({
            "kind": "votechart", "totals": {1: 2.0}, "notes": [{"targets": [2, 3]}],
        })
        engine._record_reasoning(
            1, "D1 发言", "reasoning", "speech",
            response={"decision_source": "model", "meta": {"targets": [2, 3]}},
        )
        view = service.snapshot().engine
        view.players[1]["alive"] = False
        view.public_log.append({"kind": "judge", "text": "污染"})
        view.roles[1].seat = 99

        # Outer containers are independent; recursively frozen rows are shared safely.
        self.assertIsNot(view.public_log, engine.public_log)
        self.assertIsNot(view.reasoning_log, engine.reasoning_log)
        self.assertIsNot(view.reasoning_log[1], engine.reasoning_log[1])
        self.assertIs(view.public_log[public_index], engine.public_log[public_index])
        self.assertIs(view.reasoning_log[1][reasoning_index], engine.reasoning_log[1][reasoning_index])
        with self.assertRaises(TypeError):
            view.public_log[public_index]["notes"][0]["targets"].append(4)
        with self.assertRaises(TypeError):
            view.reasoning_log[1][reasoning_index]["response"]["meta"]["targets"][0] = 99

        self.assertTrue(engine.players[1]["alive"])
        self.assertNotEqual(engine.roles[1].seat, 99)
        self.assertEqual(len(engine.public_log), public_index + 1)
        self.assertFalse(hasattr(view.manager, "client"))
        self.assertFalse(hasattr(view.manager, "inject"))
        self.assertIsNone(view.recorder)

    def test_paused_skip_collects_fallback_without_starting_next_player(self):
        engine, service = self.make_service(AskEngine, automatic=False)
        service.step()
        self.assertTrue(service.snapshot().engine.has_pending_action())
        self.assertTrue(service.request_skip_current())
        snapshot = service.snapshot()
        self.assertFalse(snapshot.engine.has_pending_action())
        self.assertFalse(snapshot.running)
        self.assertEqual(snapshot.last_event_type, "done")
        self.assertFalse(engine.manager.has_pending())
        self.assertEqual(engine.day, 0)
        self.assertEqual(engine._last_resp["vote_target"], 0)

    def test_fatal_response_pauses_until_explicit_resume(self):
        engine, service = self.make_service(FatalEngine)
        service.play()
        self.wait_until(lambda: bool(service.snapshot().error))
        self.assertFalse(service.snapshot().running)
        self.assertFalse(service.snapshot().done)
        threading.Event().wait(0.03)
        self.assertEqual(engine.day, 1)
        service.play()
        self.wait_until(lambda: service.snapshot().engine.day == 2)
        self.assertEqual(service.snapshot().error, "")

    def test_save_requires_paused_and_no_pending_task(self):
        engine, service = self.make_service()
        service.play()
        with self.assertRaisesRegex(RuntimeError, "暂停"):
            service.save(self.directory)
        service.pause()
        engine.manager.pending["request"] = (1, "speech")
        with self.assertRaisesRegex(RuntimeError, "后台任务"):
            service.save(self.directory)
        engine.manager.pending.clear()
        first = service.save(self.directory)
        second = service.save(self.directory)
        self.assertNotEqual(first, second)
        payload = json.loads((Path(self.directory) / first).read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], "v6")
        self.assertEqual(payload["engine"]["day"], engine.day)
        self.assertNotIn("api_key", json.dumps(payload))
        self.assertFalse(list(Path(self.directory).glob("*.tmp")))

    def test_close_cancels_requests_and_prevents_further_progress(self):
        engine, service = self.make_service(AskEngine, automatic=False)
        service.step()
        service.close("manual_reset")
        self.assertFalse(engine.manager.has_pending())
        service.play()
        self.assertFalse(service.step())
        self.assertIsNotNone(service.snapshot().last_archive)
        self.assertFalse(service.snapshot().running)

    def test_archive_failure_is_reported_without_losing_terminal_state(self):
        _, service = self.make_service(FiniteEngine, automatic=False)
        with patch("service.game_service.archive_game", side_effect=OSError("磁盘不可写")):
            for _ in range(4):
                service.step()
            snapshot = service.snapshot()
        self.assertTrue(snapshot.done)
        self.assertIn("磁盘不可写", snapshot.archive_error)
        self.assertEqual(snapshot.engine.winner, "好人")

    def test_adopts_existing_generator_without_restarting(self):
        engine = CountingEngine(ServiceManager())
        engine.setup(config.GAME_BOARDS[config.DEFAULT_BOARD])
        generator = engine.run()
        next(generator)
        service = GameService(engine, self.directory, automatic=False, generator=generator)
        self.addCleanup(service.close)
        service.step()
        self.assertEqual(engine.day, 2)


if __name__ == "__main__":
    unittest.main()
