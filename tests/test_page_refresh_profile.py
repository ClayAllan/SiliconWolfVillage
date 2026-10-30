"""Offline tests for the page-refresh profiling harness."""
import tempfile
import unittest
from unittest.mock import patch

from benchmarks import snapshot_baseline
from benchmarks.page_refresh_profile import (
    SnapshotProbe,
    load_integrity_guard,
    mark_human_rows,
    profile_page_refresh,
)
from service.game_service import GameService


class PageRefreshProfileTests(unittest.TestCase):
    def test_exact_app_guard_accepts_human_rows_and_rejects_ai_takeover(self):
        engine, human_seat = snapshot_baseline.fixture(120)
        mark_human_rows(engine, human_seat)
        guard = load_integrity_guard()
        self.assertEqual(guard(engine), "")

        row = engine.reasoning_log[human_seat][0]
        row["actor_source"] = "ai"
        row["response"].pop("_human_action", None)
        row["response"]["decision_source"] = "model"
        self.assertIn("安全保护", guard(engine))

    def test_page_refresh_profiles_three_isolated_snapshots(self):
        engine, human_seat = snapshot_baseline.fixture(120)
        mark_human_rows(engine, human_seat)
        guard = load_integrity_guard()

        with tempfile.TemporaryDirectory(prefix="swv-profile-test-") as directory:
            service = GameService(engine, directory, automatic=False)
            probe = SnapshotProbe(service)
            try:
                profile = profile_page_refresh(service, probe, human_seat, guard)
            finally:
                probe.close()
                with patch("service.game_service.archive_game", return_value=None):
                    service.close()

        self.assertEqual(len(profile["snapshots"]), 3)
        self.assertGreater(profile["total_ms"], 0)
        for sample in profile["snapshots"]:
            self.assertGreaterEqual(sample["total_ms"], sample["lock_hold_ms"])
            self.assertGreaterEqual(sample["lock_hold_ms"] + 0.01, sample["deepcopy_ms"])
            self.assertGreaterEqual(sample["lock_wait_ms"], 0)
            self.assertGreater(sample["deepcopy_calls"], 0)
        self.assertEqual(len(profile["integrity_guard_ms"]), 3)


if __name__ == "__main__":
    unittest.main()
