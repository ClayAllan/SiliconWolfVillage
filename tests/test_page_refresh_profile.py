"""Offline tests for the page-refresh profiling harness."""
import ast
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

    def test_normal_active_rerun_uses_two_direct_syncs_and_no_sidebar_resync(self):
        app_path = snapshot_baseline.ROOT / "app.py"
        tree = ast.parse(app_path.read_text(encoding="utf-8"), filename=str(app_path))

        def is_sync_call(node):
            return (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "sync_game_state"
            )

        direct_sync_lines = []
        for statement in tree.body:
            if isinstance(statement, (ast.Assign, ast.Expr)):
                direct_sync_lines.extend(
                    node.lineno for node in ast.walk(statement) if is_sync_call(node)
                )
        self.assertEqual(len(direct_sync_lines), 2)

        def dotted_name(node):
            if isinstance(node, ast.Name):
                return node.id
            if isinstance(node, ast.Attribute):
                prefix = dotted_name(node.value)
                return f"{prefix}.{node.attr}" if prefix else node.attr
            return ""

        sidebar = next(
            statement for statement in tree.body
            if isinstance(statement, ast.With)
            and any(dotted_name(item.context_expr) == "st.sidebar"
                    for item in statement.items)
        )
        configure_block = next(
            node for node in ast.walk(sidebar)
            if isinstance(node, ast.If)
            and any(
                isinstance(call, ast.Call)
                and dotted_name(call.func) == "ss.game_service.configure"
                for call in ast.walk(node)
            )
        )
        self.assertFalse(any(is_sync_call(node) for node in ast.walk(configure_block)))

    def test_page_refresh_profiles_two_isolated_snapshots(self):
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

        self.assertEqual(len(profile["snapshots"]), 2)
        self.assertGreater(profile["total_ms"], 0)
        for sample in profile["snapshots"]:
            self.assertGreaterEqual(sample["total_ms"], sample["lock_hold_ms"])
            self.assertGreaterEqual(sample["lock_hold_ms"] + 0.01, sample["deepcopy_ms"])
            self.assertGreaterEqual(sample["lock_wait_ms"], 0)
            self.assertGreater(sample["deepcopy_calls"], 0)
        self.assertEqual(len(profile["integrity_guard_ms"]), 2)


if __name__ == "__main__":
    unittest.main()
