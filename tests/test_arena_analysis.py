import csv
import json
import tempfile
import unittest
from pathlib import Path

from storage.arena_analysis import build_arena_report, write_arena_report


def add_manifest(root, name, *, game_id, status="completed", quality="valid",
                 winner="好人", board="标准板", good_model="good-A", wolf_model="wolf-B",
                 base_url="https://api.example.com/v1", temperature=0.7, agent_timeout=60,
                 react_budget=3, source_id="source-v1", numerator=1, denominator=2, extra=None):
    directory = Path(root) / name
    directory.mkdir()
    manifest = {
        "metadata": {
            "game_id": game_id, "archived_at": f"2026-10-{len(name):02d}",
            "status": status, "winner": winner, "board_name": board,
            "good_model": good_model, "wolf_model": wolf_model,
        },
        "config": {
            "base_url": base_url, "temperature": temperature,
            "agent_timeout": agent_timeout, "react_budget": react_budget,
        },
        "source_snapshot": {
            "runtime": {"version_id": source_id},
            "files": {"config.py": "cfg-" + source_id, "game_engine.py": "engine-" + source_id,
                      "ai_agent.py": "agent-" + source_id, "roles.py": "roles-" + source_id},
            "prompts": {"system_template_sha256": "system-" + source_id,
                        "summary_template_sha256": "summary-" + source_id},
        },
        "api_quality": {"quality": quality},
        "analysis": {
            "observed_metrics": {
                "good_vote_accuracy": {
                    "numerator": numerator, "denominator": denominator,
                    "value": numerator / denominator if denominator else None,
                },
            },
        },
    }
    if extra:
        manifest.update(extra)
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return directory


class ArenaAnalysisTests(unittest.TestCase):
    def test_weighted_metrics_and_outcomes_use_only_valid_completed_games(self):
        with tempfile.TemporaryDirectory() as root:
            add_manifest(root, "g1", game_id="g1", numerator=1, denominator=2, winner="好人")
            add_manifest(root, "g2", game_id="g2", numerator=8, denominator=10, winner="狼人")
            add_manifest(root, "g3", game_id="g3", numerator=1, denominator=1, quality="degraded")
            add_manifest(root, "g4", game_id="g4", numerator=1, denominator=1, quality="invalid")
            add_manifest(root, "g5", game_id="g5", numerator=1, denominator=1, status="interrupted")
            report = build_arena_report(root)
            self.assertEqual(report["archive_count"], 5)
            self.assertEqual(report["quality_counts"]["valid"], 3)
            self.assertEqual(report["quality_counts"]["degraded"], 1)
            self.assertEqual(report["quality_counts"]["invalid"], 1)
            self.assertEqual(len(report["cohorts"]), 1)
            cohort = report["cohorts"][0]
            self.assertEqual(cohort["eligible_games"], 2)
            self.assertEqual((cohort["good_wins"], cohort["wolf_wins"]), (1, 1))
            self.assertEqual(cohort["good_win_rate"], 0.5)
            metric = cohort["metrics"]["good_vote_accuracy"]
            self.assertEqual((metric["numerator"], metric["denominator"], metric["games"]), (9, 12, 2))
            self.assertEqual(metric["value"], 0.75)  # weighted by vote counts, not mean(.5, .8)
            self.assertEqual(report["policy"]["model_api_calls"], 0)
            self.assertFalse(report["policy"]["paid_experiments_started"])

    def test_invalid_or_unknown_records_never_become_zero_score_samples(self):
        with tempfile.TemporaryDirectory() as root:
            add_manifest(root, "no-votes", game_id="no-votes", numerator=0, denominator=0)
            add_manifest(root, "unknown-quality", game_id="unknown", quality="mystery", numerator=0, denominator=0)
            report = build_arena_report(root)
            cohort = report["cohorts"][0]
            self.assertEqual(cohort["eligible_games"], 1)  # unknown-quality sample is excluded
            self.assertIsNone(cohort["metrics"]["good_vote_accuracy"]["value"])
            self.assertEqual(cohort["metrics"]["good_vote_accuracy"]["games"], 0)
            self.assertEqual(report["quality_counts"]["unknown"], 1)


    def test_endpoint_settings_and_source_fingerprint_split_cohorts(self):
        with tempfile.TemporaryDirectory() as root:
            add_manifest(root, "g1", game_id="g1")
            add_manifest(root, "g2", game_id="g2", temperature=0.9)
            add_manifest(root, "g3", game_id="g3", source_id="source-v2")
            add_manifest(root, "g4", game_id="g4",
                         base_url="https://user:secret@api.example.com/v1?token=do-not-report")
            report = build_arena_report(root)
            self.assertEqual(len(report["cohorts"]), 3)
            self.assertEqual(sum(row["eligible_games"] for row in report["cohorts"]), 4)
            self.assertTrue(all(row["source_fingerprint"] != "unknown" for row in report["cohorts"]))
            self.assertTrue(all(row["base_url"] == "https://api.example.com/v1" for row in report["cohorts"]))
            self.assertNotIn("secret", json.dumps(report).lower())
            self.assertNotIn("do-not-report", json.dumps(report))

    def test_malformed_and_oversized_manifests_are_reported_as_skipped(self):
        with tempfile.TemporaryDirectory() as root:
            malformed = Path(root) / "bad"
            malformed.mkdir()
            (malformed / "manifest.json").write_text("{bad", encoding="utf-8")
            large = Path(root) / "large"
            large.mkdir()
            (large / "manifest.json").write_bytes(b" " * (2 * 1024 * 1024 + 1))
            report = build_arena_report(root)
            self.assertEqual(report["archive_count"], 0)
            self.assertEqual(report["skipped_count"], 2)
            self.assertEqual({item["archive"] for item in report["skipped_archives"]}, {"bad", "large"})

    def test_duplicate_game_id_is_counted_once_and_report_files_are_written(self):
        with tempfile.TemporaryDirectory() as root:
            add_manifest(root, "g1", game_id="same")
            add_manifest(root, "g2", game_id="same")
            report = build_arena_report(root)
            self.assertEqual(report["archive_count"], 1)
            self.assertEqual(report["skipped_count"], 1)
            output = Path(root) / "analysis"
            json_path, csv_path = write_arena_report(report, output)
            self.assertTrue(json_path.exists())
            self.assertTrue(csv_path.exists())
            data = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(data["archive_count"], 1)
            with csv_path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["good_vote_accuracy_numerator"], "1")


if __name__ == "__main__":
    unittest.main()
