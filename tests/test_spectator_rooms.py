"""Sharing and replay tests with no model requests."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from engine import config
from engine.game_engine import GameEngine
from service.game_service import GameService
from storage.replay_frames import DisplayReplay
from storage.replay_reader import legacy_public_rows
from service.room_registry import RoomRegistry
from service.room_view import project_room
from test_game_service import ServiceManager


class SpectatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.engine = GameEngine(ServiceManager())
        self.engine.setup(config.GAME_BOARDS[config.DEFAULT_BOARD])
        self.service = GameService(self.engine, self.tmp.name, automatic=False, record_display=True)
        self.addCleanup(self.service.close)
        self.registry = RoomRegistry()
        self.room, self.owner = self.registry.register(self.service)

    def join(self, view="public", seat=None, browser="browser"):
        grant, secret = self.registry.issue(self.room, self.owner, view, seat)
        token = self.registry.join(self.room, secret, browser)
        return grant, token

    def test_viewer_gets_no_engine_secrets_or_identities(self):
        _, token = self.join()
        packet = self.registry.read(self.room, token, "browser")
        v = packet["view"]
        self.assertTrue(all(p["role"] == "未知" for p in v["players"].values()))
        self.assertFalse(v["skill_states"])
        self.assertFalse(v["reasoning_log"])
        self.assertIsNone(v["war_board"])
        self.assertFalse({"engine", "manager", "recorder", "random_seed", "role_seats"} & set(v))

    def test_wolf_sees_team_but_no_team_reasoning(self):
        wolf = self.engine.wolves[0]
        mate = self.engine.wolves[1]
        self.engine.reasoning_log[mate].append({"label": "秘密", "reasoning": "SECRET", "speech": ""})
        view = project_room(self.engine, "seat", wolf)
        self.assertEqual(view["players"][str(mate)]["role"], self.engine.players[mate]["role"])
        self.assertNotIn("SECRET", json.dumps(view))
        self.assertEqual(list(view["skill_states"]), [str(wolf)])

    def test_seven_boards_role_projection(self):
        for board in config.GAME_BOARDS.values():
            e = GameEngine(ServiceManager())
            e.setup(board)
            for seat in range(1, 13):
                v = project_room(e, "seat", seat)
                for other in range(1, 13):
                    expected = e.players[other]["role"] if other == seat or seat in e.wolves and other in e.wolves else "未知"
                    self.assertEqual(v["players"][str(other)]["role"], expected)

    def test_night_death_is_not_published_before_announcement(self):
        self.engine.phase, self.engine.day = "第2夜", 2
        self.engine._mark_dead(1, "夜间结算")
        self.service._publish_display()
        before = self.service.display_replay.timeline("public")[-1]["cursor"]
        self.assertTrue(project_room(self.engine)["players"]["1"]["alive"])
        self.engine.phase = "第2天"
        self.service._publish_display()
        self.assertTrue(project_room(self.engine)["players"]["1"]["alive"])
        gen = self.engine._announce_morning({1: "wolf"}, False)
        next(gen)
        gen.close()
        self.service._publish_display()
        self.assertFalse(project_room(self.engine)["players"]["1"]["alive"])
        self.assertTrue(self.service.display_replay.read("public", before)["players"]["1"]["alive"])

    def test_hidden_action_does_not_create_public_timeline_position(self):
        before = deepcopy(self.service.display_replay.timeline("public"))
        self.engine._log_god("狼刀 SECRET")
        self.engine.current_speaker = 4
        self.service._publish_display()
        self.assertEqual(before, self.service.display_replay.timeline("public"))
        self.assertGreater(len(self.service.display_replay.timeline("omniscient")), 1)

    def test_revoked_existing_session_and_expiry_reject_reads(self):
        grant, token = self.join()
        self.registry.revoke(self.room, self.owner, grant)
        with self.assertRaises(PermissionError):
            self.registry.read(self.room, token, "browser")
        _, token = self.join()
        self.registry.clock = lambda: 10**12
        with self.assertRaises(PermissionError):
            self.registry.read(self.room, token, "browser")

    def test_wrong_browser_room_and_control_are_rejected(self):
        _, token = self.join("omniscient")
        with self.assertRaises(PermissionError):
            self.registry.read(self.room, token, "someone_else")
        with self.assertRaises(PermissionError):
            self.registry.control(self.room, token, "cmd", 0, "step")
        room2, _ = self.registry.register(replay=self.service.display_replay)
        with self.assertRaises(PermissionError):
            self.registry.read(room2, token, "browser")

    def test_read_does_not_advance_and_returns_independent_data(self):
        _, token = self.join()
        with patch.object(self.service, "_advance_locked", side_effect=AssertionError("read advanced")):
            with ThreadPoolExecutor(max_workers=10) as pool:
                packets = list(pool.map(lambda _: self.registry.read(self.room, token, "browser"), range(30)))
        packets[0]["view"]["players"]["1"]["role"] = "tampered"
        self.assertEqual(packets[1]["view"]["players"]["1"]["role"], "未知")

    def test_old_cursor_is_stable_after_terminal_event(self):
        _, token = self.join()
        first = self.registry.read(self.room, token, "browser")["view"]["cursor"]
        self.engine.winner = "好人"
        self.engine._log_win("好人获胜")
        self.service._publish_display()
        self.assertIsNone(self.registry.read(self.room, token, "browser", first)["view"]["winner"])
        self.assertEqual(self.registry.read(self.room, token, "browser")["view"]["winner"], "好人")

    def test_foreign_audience_cursor_is_invalid(self):
        cursor = self.service.display_replay.timeline("omniscient")[0]["cursor"]
        with self.assertRaises(ValueError):
            self.service.display_replay.read("public", cursor)

    def test_disk_round_trip_and_partial_tail(self):
        self.engine._log_player(1, "发言")
        self.service._publish_display()
        directory = self.service.display_replay.directory
        loaded = DisplayReplay.load(directory)
        self.assertEqual(loaded.read("public"), self.service.display_replay.read("public"))
        with (directory / "replay_frames.jsonl").open("ab") as handle:
            handle.write(b'{"schema":')
        loaded = DisplayReplay.load(directory)
        self.assertTrue(loaded.issue)
        self.assertEqual(loaded.read("public"), self.service.display_replay.read("public"))

    def test_write_failure_pauses_without_replaying_action(self):
        self.service.play()
        self.engine._log_judge("new event")
        with patch.object(self.service.display_replay, "_append", side_effect=OSError("disk")):
            self.assertFalse(self.service._publish_display())
        self.assertFalse(self.service._running)
        self.assertTrue(self.service.display_replay.issue)
        self.service.play()
        self.assertFalse(self.service.display_replay.issue)
        self.assertEqual(self.service.display_replay.read("public")["public_log"][-1]["text"], "new event")

    def test_owner_control_deduplicates_and_checks_revision(self):
        self.registry.control(self.room, self.owner, "one", 0, "step")
        revision = self.service.revision
        self.registry.control(self.room, self.owner, "one", 0, "step")
        self.assertEqual(revision, self.service.revision)
        with self.assertRaises(ValueError):
            self.registry.control(self.room, self.owner, "two", 0, "step")
        with self.assertRaises(ValueError):
            self.registry.control(self.room, self.owner, "one", revision, "pause")

    def test_unfinished_win_row_does_not_leak_to_observers(self):
        self.engine._log_win("狼人获胜")
        self.assertFalse(any(r["kind"] == "win" for r in project_room(self.engine)["public_log"]))

    def test_terminal_recording_can_retry_without_advancing(self):
        self.engine.winner = "好人"
        self.engine._log_win("好人获胜")
        self.service._done = True
        with patch.object(self.service.display_replay, "_append", side_effect=OSError("disk")):
            self.assertFalse(self.service._publish_display())
        with patch.object(self.service, "_advance_locked", side_effect=AssertionError("advanced")):
            self.assertTrue(self.service.retry_display_recording())
        self.assertEqual(self.service.display_replay.read("public")["winner"], "好人")

    def test_archive_rejects_swapped_audience_reference(self):
        directory = self.service.display_replay.directory
        path = directory / "replay_frames.jsonl"
        row = json.loads(path.read_text(encoding="utf-8"))
        row["changes"]["public"] = row["changes"]["omniscient"]
        path.write_text(json.dumps(row), encoding="utf-8")
        loaded = DisplayReplay.load(directory)
        self.assertTrue(loaded.issue)
        self.assertEqual(loaded.frames, 0)

    def test_legacy_parser_does_not_export_nested_secrets(self):
        raw = json.dumps({"public_log": [{"kind": "player", "seat": 1, "text": "你好", "role": "狼人", "state": {"secret": "SECRET"}},
                                         {"kind": "god", "text": "SECRET"}], "manager_state": {"key": "SECRET"}}).encode()
        result = legacy_public_rows(raw)
        self.assertEqual(len(result["rows"]), 1)
        self.assertNotIn("SECRET", json.dumps(result))

    def test_unknown_old_save_does_not_invent_public_liveness(self):
        state = self.engine.export_state()
        state.pop("published_alive")
        self.engine.load_state(state)
        self.assertTrue(all(p["alive"] is None for p in project_room(self.engine)["players"].values()))

    def test_lovers_get_relation_not_partner_identity(self):
        seats = [s for s in self.engine.players if s not in self.engine.wolves][:2]
        self.engine.lovers = tuple(seats)
        v = project_room(self.engine, "seat", seats[0])
        self.assertEqual(v["lovers"], seats)
        self.assertEqual(v["players"][str(seats[1])]["role"], "未知")
        self.assertIsNone(project_room(self.engine)["lovers"])


if __name__ == "__main__":
    unittest.main()
