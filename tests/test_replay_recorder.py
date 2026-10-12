# -*- coding: utf-8 -*-
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from replay_recorder import GameRecorder
import config
from ai_agent import AgentManager
from game_engine import GameEngine


class RecorderTests(unittest.TestCase):
    def test_jsonl_is_immediately_readable_and_sequence_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            first = GameRecorder(directory, "g1", "2026-07-27T10:00:00+08:00")
            first.record_event("start", value=1)
            first.record_event("step", value=2)
            path = Path(first.paths()["events"])
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["seq"] for row in rows], [1, 2])

            resumed = GameRecorder(
                directory, "g1", "2026-07-27T10:00:00+08:00", basename=first.basename
            )
            resumed.record_event("resume", value=3)
            rows = resumed.read_jsonl("events")
            self.assertEqual(rows[-1]["seq"], 3)
            self.assertEqual(rows[-1]["event"], "resume")

    def test_recording_before_setup_preserves_prompts_without_api_key(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = AgentManager(
                api_key="super-secret-key", base_url="https://example.invalid/v1",
                good_model="good", wolf_model="wolf", fallback_model="",
                max_retries=1, agent_timeout=1,
            )
            engine = GameEngine(manager)
            engine.board_name = config.DEFAULT_BOARD
            recorder = engine.enable_recording(directory)
            engine.setup(config.GAME_BOARDS[config.DEFAULT_BOARD])

            memory_rows = recorder.read_jsonl("memory")
            initialized = [r for r in memory_rows if r.get("event") == "agent_initialized"]
            self.assertEqual(len(initialized), 12)
            self.assertIn("你的身份", initialized[0]["system_prompt"])
            all_text = "\n".join(
                p.read_text(encoding="utf-8")
                for p in Path(recorder.directory).iterdir() if p.is_file()
            )
            self.assertNotIn("super-secret-key", all_text)
            manager._request_pool.shutdown(wait=True, cancel_futures=True)


if __name__ == "__main__":
    unittest.main()
