"""Offline acceptance tests for the owner-only historical game archive ZIP."""
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from storage.archive_zip import (
    build_private_archive_zip,
    recent_archive_directories,
)


class PrivateArchiveZipTests(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name) / "replays"
        self.root.mkdir()
        self.archive = self.root / "20261029_game1"
        self.archive.mkdir()
        (self.archive / "manifest.json").write_text(
            json.dumps({"metadata": {"game_id": "game1", "status": "completed"}}),
            encoding="utf-8",
        )
        (self.archive / "summary.md").write_text("赛后复盘", encoding="utf-8")
        (self.archive / "final_state.json").write_text(
            json.dumps({"reasoning_log": {"1": "PRIVATE_REASONS"}}),
            encoding="utf-8",
        )
        (self.archive / "events.jsonl").write_text(
            '{"event":"public_log","entry":{"text":"发言"}}\n', encoding="utf-8"
        )
        (self.archive / "api_calls.jsonl").write_text(
            '{"status":"completed","request_id":"x"}\n', encoding="utf-8"
        )
        (self.archive / "memory.jsonl").write_text(
            '{"seat":1,"private_memory":"PRIVATE_MEMORY"}\n', encoding="utf-8"
        )
        (self.archive / "runtime.log").write_text("ARCHIVE completed\n", encoding="utf-8")
        (self.archive / "replay_frames.jsonl").write_text(
            '{"seq":1,"frame":{}}\n', encoding="utf-8"
        )
        (self.root / "index.csv").write_text("game_id\ngame1\n", encoding="utf-8")
        (self.archive / "unknown_notes.txt").write_text("SHOULD_NOT_INCLUDE", encoding="utf-8")

    def test_private_zip_contains_exactly_one_game_and_standard_forensic_files(self):
        data = build_private_archive_zip(self.root, self.archive)
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            names = set(package.namelist())
            self.assertIn("20261029_game1/manifest.json", names)
            self.assertIn("20261029_game1/final_state.json", names)
            self.assertIn("20261029_game1/summary.md", names)
            self.assertIn("20261029_game1/events.jsonl", names)
            self.assertIn("20261029_game1/api_calls.jsonl", names)
            self.assertIn("20261029_game1/memory.jsonl", names)
            self.assertIn("20261029_game1/replay_frames.jsonl", names)
            self.assertIn("20261029_game1/PRIVATE_ARCHIVE_NOTICE.txt", names)
            self.assertNotIn("index.csv", names)
            self.assertFalse(any("unknown_notes" in name for name in names))
            self.assertFalse(any(".." in name for name in names))
            self.assertEqual(package.read("20261029_game1/final_state.json").decode(),
                             (self.archive / "final_state.json").read_text())
            self.assertIn("PRIVATE_MEMORY", package.read("20261029_game1/memory.jsonl").decode())

    def test_rejects_other_session_and_path_traversal(self):
        with tempfile.TemporaryDirectory() as unrelated:
            other = Path(unrelated) / "other_game"
            other.mkdir()
            (other / "manifest.json").write_text('{"metadata": {}}')
            with self.assertRaisesRegex(ValueError, "直接子目录"):
                build_private_archive_zip(self.root, other)
        with self.assertRaises(ValueError):
            build_private_archive_zip(self.root, self.root / ".." / self.archive.name)

    def test_rejects_missing_corrupt_manifest_and_symlinks(self):
        manifest = self.archive / "manifest.json"
        manifest.write_text("{bad", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "损坏"):
            build_private_archive_zip(self.root, self.archive)
        manifest.unlink()
        with self.assertRaisesRegex(ValueError, "缺少 manifest"):
            build_private_archive_zip(self.root, self.archive)
        manifest.write_text('{"metadata": {}}', encoding="utf-8")
        outside = self.root / "other"
        outside.write_text("PRIVATE_OUTSIDE", encoding="utf-8")
        (self.archive / "runtime.log").unlink()
        (self.archive / "runtime.log").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            build_private_archive_zip(self.root, self.archive)

    def test_oversized_file_and_total_archive_fail_closed(self):
        with patch("storage.archive_zip.MAX_ARCHIVE_FILE_BYTES", 100):
            (self.archive / "final_state.json").write_bytes(b"x" * 101)
            with self.assertRaisesRegex(ValueError, "单文件上限"):
                build_private_archive_zip(self.root, self.archive)
        (self.archive / "final_state.json").write_text('{"ok":true}', encoding="utf-8")
        with patch("storage.archive_zip.MAX_ARCHIVE_TOTAL_BYTES", 20):
            with self.assertRaisesRegex(ValueError, "总大小"):
                build_private_archive_zip(self.root, self.archive)

    def test_recent_archives_only_show_finalized_directories(self):
        (self.root / "20261030_game2").mkdir()
        archived = recent_archive_directories(self.root)
        self.assertEqual(archived, [self.archive])
        alias = self.root / "20261031_alias"
        alias.symlink_to(self.archive, target_is_directory=True)
        self.assertEqual(recent_archive_directories(self.root), [self.archive])


if __name__ == "__main__":
    unittest.main()
