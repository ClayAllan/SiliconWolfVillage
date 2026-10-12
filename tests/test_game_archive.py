# -*- coding: utf-8 -*-
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from game_archive import _usage_totals, archive_game, should_archive


class FakeManager:
    good_model = "good-model"
    wolf_model = "wolf-model"

    def export_state(self):
        return {"agent_memory": {"1": {"summary": "逻辑摘要", "recent": []}}}


class FakeEngine:
    def __init__(self, winner="好人"):
        self.manager = FakeManager()
        self.game_id = "game123"
        self.started_at = "2026-07-24T12:34:56+08:00"
        self.board_name = "测试板"
        self.archive_basename = None
        self.archive_complete = False
        self.winner = winner
        self.day = 3
        self.phase = "游戏结束" if winner else "第3天"
        self.sheriff = 2
        self.players = {
            1: {"role": "狼人", "alive": False},
            2: {"role": "预言家", "alive": True},
        }
        self.replay_logs = ["# 复盘", "- 1号出局"]
        self.public_log = [
            {"kind": "player", "seat": 2, "text": "发言"},
            {"kind": "votechart", "title": "D1 放逐", "totals": {1: 2}, "votes": {2: 1}, "winner": 1},
        ]
        self.reasoning_log = {2: [{"reasoning": "锁定1号"}]}

    def export_replay_text(self):
        return "\n".join(self.replay_logs)

    def export_state(self):
        return {"game_id": self.game_id, "winner": self.winner}


class ArchiveTests(unittest.TestCase):
    def test_completed_game_is_archived_once_as_v2_folder(self):
        engine = FakeEngine()
        with tempfile.TemporaryDirectory() as directory:
            first = archive_game(engine, directory, reason="game_end")
            second = archive_game(engine, directory, reason="stop_iteration")

            self.assertEqual(first["json"], second["json"])
            game_dir = Path(first["directory"])
            self.assertTrue((game_dir / "manifest.json").exists())
            self.assertTrue((game_dir / "summary.md").exists())
            self.assertTrue((game_dir / "final_state.json").exists())
            self.assertTrue((Path(directory) / "index.csv").exists())
            payload = json.loads(Path(first["json"]).read_text(encoding="utf-8"))
            self.assertEqual(payload["metadata"]["winner"], "好人")
            self.assertEqual(payload["metadata"]["status"], "completed")
            self.assertEqual(payload["metrics"]["vote_rounds"][0]["winner"], 1)
            self.assertEqual(payload["analysis"]["good_vote_accuracy"], 1.0)
            self.assertEqual(payload["analysis"]["good_vote_cohesion"], 1.0)
            self.assertIn("prompt_tuning_flags", payload["analysis"])
            self.assertIn("manager_state", payload)
            summary = Path(first["markdown"]).read_text(encoding="utf-8")
            self.assertIn("N/A（无有效样本）", summary)

    def test_interrupted_game_can_be_overwritten_by_final_result(self):
        engine = FakeEngine(winner=None)
        with tempfile.TemporaryDirectory() as directory:
            partial = archive_game(engine, directory, reason="manual_reset")
            self.assertFalse(partial["completed"])
            engine.winner = "狼人"
            engine.phase = "游戏结束"
            final = archive_game(engine, directory, reason="game_end")
            payload = json.loads(Path(final["json"]).read_text(encoding="utf-8"))
            self.assertTrue(final["completed"])
            self.assertEqual(payload["metadata"]["winner"], "狼人")

    def test_explosion_metric_uses_structured_events_not_will_mentions(self):
        engine = FakeEngine()
        engine.explosion_events = [
            {"day": 2, "seat": 4, "role": "狼人", "context": "regular_day", "target": 0}
        ]
        engine.replay_logs = [
            "- 💥 狼人自爆：4号自爆，白天强制结束",
            "- 🕯️ 5号遗言：我早就说4号可能会自爆。",
            "- 🗣️ 7号发言：不要再讨论自爆了。",
        ]
        with tempfile.TemporaryDirectory() as directory:
            result = archive_game(engine, directory, reason="game_end")
            payload = json.loads(Path(result["json"]).read_text(encoding="utf-8"))

        self.assertEqual(payload["analysis"]["wolf_explosions"], 1)

    def test_blank_engine_is_not_archived(self):
        engine = FakeEngine(winner=None)
        engine.day = 0
        engine.public_log = []
        engine.replay_logs = []
        self.assertFalse(should_archive(engine))

    def test_canceled_late_response_is_not_double_counted_as_success(self):
        rows = [
            {"record_type": "submitted", "request_id": "a", "response_kind": "speech"},
            {"record_type": "canceled", "request_id": "a", "status": "canceled"},
            {"record_type": "completed", "request_id": "a", "response_kind": "speech",
             "status": "success", "usage": {"total_tokens": 20}},
            {"record_type": "submitted", "request_id": "b", "response_kind": "speech"},
            {"record_type": "completed", "request_id": "b", "response_kind": "speech",
             "status": "success", "usage": {
                 "prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10,
                 "prompt_cache_hit_tokens": 4, "prompt_cache_miss_tokens": 3,
                 "completion_tokens_details": {"reasoning_tokens": 2},
             }},
            {"record_type": "completed", "request_id": "summary-a", "response_kind": "memory_summary",
             "status": "success", "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10}},
        ]
        result = _usage_totals(rows)
        self.assertEqual(result["requests"], 2)
        self.assertEqual(result["success"], 1)
        self.assertEqual(result["canceled"], 1)
        self.assertEqual(result["fallback_rate"], 0.5)
        self.assertEqual(result["total_tokens"], 40)
        self.assertEqual(result["cache_hit_tokens"], 4)
        self.assertEqual(result["cache_miss_tokens"], 11)
        self.assertEqual(result["reasoning_tokens"], 2)
        self.assertEqual(result["summary_requests"], 1)
        self.assertEqual(result["summary_total_tokens"], 10)
        self.assertEqual(result["unmetered_returns"], 0)

    def test_usage_totals_counts_every_paid_retry_attempt(self):
        rows = [{
            "record_type": "completed", "request_id": "retry", "response_kind": "speech",
            "status": "success", "usage": {"total_tokens": 20},
            "attempts": [
                {"status": "error", "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10}},
                {"status": "success", "usage": {"prompt_tokens": 16, "completion_tokens": 4, "total_tokens": 20}},
            ],
        }]
        result = _usage_totals(rows)
        self.assertEqual(result["total_tokens"], 30)
        self.assertEqual(result["by_response_kind"]["speech"]["requests"], 2)

    def test_usage_totals_keeps_unmetered_api_return_visible(self):
        rows = [{
            "record_type": "completed", "request_id": "no-usage",
            "response_kind": "memory_summary", "status": "success",
        }]
        result = _usage_totals(rows)
        self.assertEqual(result["summary_requests"], 1)
        self.assertEqual(result["unmetered_returns"], 1)


if __name__ == "__main__":
    unittest.main()
