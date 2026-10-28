import json
import tempfile
import unittest
from pathlib import Path

from storage.archive_review import load_archive_review


def write_archive(root, name="game-1"):
    directory = Path(root) / name
    directory.mkdir()
    manifest = {
        "metadata": {
            "game_id": "g1", "started_at": "2026-10-28", "archived_at": "2026-10-29",
            "status": "completed", "winner": "好人", "board_name": "标准板",
            "good_model": "good", "wolf_model": "wolf", "day": 4,
        },
        "api_quality": {
            "requests": 20, "success": 18, "fallback": 2, "canceled": 0,
            "prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500,
            "fallback_rate": 0.1,
        },
        "analysis": {
            "observed_metrics": {
                "good_vote_accuracy": {
                    "label": "好人投狼比例", "value": 0.75, "numerator": 3,
                    "denominator": 4, "definition": "按有效投票计数",
                },
            },
            "prompt_tuning_flags": ["检查格式稳定性"],
        },
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    (directory / "summary.md").write_text("# 私密复盘\n包含真实身份", encoding="utf-8")
    rows = [
        {"seq": 1, "timestamp": "t1", "event": "player_died",
         "state": {"role": "狼人", "secret": "HIDDEN"}, "seat": 3, "role": "狼人"},
        {"seq": 2, "timestamp": "t2", "event": "public_log",
         "state": {"role": "狼人", "secret": "HIDDEN"},
         "entry": {"kind": "player", "phase": "第1天", "seat": 4, "text": "我认为3号可疑",
                   "nested_secret": "HIDDEN"}},
        {"seq": 3, "timestamp": "t3", "event": "public_log",
         "entry": {"kind": "votechart", "phase": "第1天", "title": "D1放逐",
                   "votes": {"1": 3, "2": 0, "99": 5}, "totals": {"3": 2, "99": 5},
                   "winner": 3, "sheriff": 2, "secret": "HIDDEN"}},
        {"seq": 4, "timestamp": "t4", "event": "public_log",
         "entry": {"kind": "god", "text": "HIDDEN"}},
    ]
    (directory / "events.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )
    return directory


class ArchiveReviewTests(unittest.TestCase):
    def test_loads_structured_metrics_and_only_curated_public_events(self):
        with tempfile.TemporaryDirectory() as root:
            directory = write_archive(root)
            result = load_archive_review(root, directory)
            self.assertEqual(result["metadata"]["winner"], "好人")
            self.assertEqual(result["metadata"]["day"], 4)
            self.assertEqual(result["api_quality"]["total_tokens"], 1500)
            self.assertEqual(result["observed_metrics"]["good_vote_accuracy"]["numerator"], 3)
            self.assertEqual(len(result["events"]), 2)
            self.assertEqual(result["events"][0]["text"], "我认为3号可疑")
            self.assertEqual(result["events"][1]["votes"], {"1": 3, "2": 0})
            self.assertEqual(result["events"][1]["totals"], {"3": 2})
            self.assertNotIn("secret", json.dumps(result, ensure_ascii=False).lower())
            self.assertNotIn("HIDDEN", json.dumps(result, ensure_ascii=False))

    def test_rejects_archive_path_outside_root(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as other:
            directory = write_archive(other)
            with self.assertRaisesRegex(ValueError, "直接子目录"):
                load_archive_review(root, directory)

    def test_rejects_oversized_manifest(self):
        with tempfile.TemporaryDirectory() as root:
            directory = write_archive(root)
            (directory / "manifest.json").write_bytes(b" " * (2 * 1024 * 1024 + 1))
            with self.assertRaisesRegex(ValueError, "超过读取上限"):
                load_archive_review(root, directory)

    def test_missing_events_is_a_review_warning_not_a_hard_failure(self):
        with tempfile.TemporaryDirectory() as root:
            directory = write_archive(root)
            (directory / "events.jsonl").unlink()
            result = load_archive_review(root, directory)
            self.assertEqual(result["events"], [])
            self.assertIn("没有 events.jsonl", result["issue"])


if __name__ == "__main__":
    unittest.main()
