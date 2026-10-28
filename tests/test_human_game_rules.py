# -*- coding: utf-8 -*-
import unittest

from engine import config
from engine.game_engine import GameEngine
from service.view_projection import project_view
from test_runtime_regressions import MemoryOnlyManager


class HumanGameRulesTests(unittest.TestCase):
    def setUp(self):
        self.manager = MemoryOnlyManager()
        self.engine = GameEngine(self.manager)
        self.engine.setup(config.GAME_BOARDS[config.DEFAULT_BOARD])

    def test_hunter_shot_does_not_publish_target_role(self):
        hunter_seat = next(seat for seat, role in self.engine.roles.items()
                           if role.role_name == "猎人")
        target_seat = next(seat for seat, role in self.engine.roles.items()
                           if role.camp == "狼人" and seat != hunter_seat)
        target_role = self.engine.players[target_seat]["role"]
        self.engine.phase = "第2天"

        self.engine.roles[hunter_seat].parse_death_response(
            self.engine, {"vote_target": target_seat, "speech": f"开枪{target_seat}号"}
        )

        public_text = "\n".join(row.get("text", "") for row in self.engine.public_log)
        self.assertIn(f"猎人开枪带走了 {target_seat}号！", public_text)
        self.assertNotIn(f"{target_seat}号（{target_role}）", public_text)
        shot_line = next(line for line in self.engine.replay_logs
                         if f"猎人({hunter_seat}号)开枪带走" in line)
        self.assertNotIn(f"{target_seat}号({target_role})", shot_line)
        viewer_seat = next(
            seat for seat in self.engine.alive_seats()
            if seat not in {hunter_seat, target_seat}
            and self.engine.roles[seat].camp != "狼人"
        )
        self.engine.human_seat = viewer_seat
        view = project_view(self.engine, viewer_seat=viewer_seat)
        self.assertEqual(view.players[target_seat]["role"], "未知")
        self.assertFalse(view.players[target_seat]["alive"])

    def test_day_prompt_teaches_guarded_wolf_kill_and_witch_poison_are_consistent(self):
        seat = next(s for s in self.engine.players if self.engine.roles[s].camp != "狼人")
        self.engine.day = 2
        self.engine.phase = "第2天"
        prompt = self.engine._day_speech_prompt(
            seat, [], {3: "poison"}, False, order=self.engine.alive_seats()
        )
        self.assertIn("女巫毒药可独立造成死亡", prompt)
        self.assertIn("完全自洽", prompt)
        self.assertIn("不得称为机械矛盾或平安夜", prompt)


    def test_witch_poison_kills_even_when_wolf_kill_is_empty(self):
        self.engine.day = 2
        self.engine.phase = "第2夜"
        self.engine.night.update({
            "wolf_kill": None,
            "guard_target": 4,
            "witch_save": None,
            "witch_poison": 3,
            "dream_target": None,
            "seer_check": None,
            "magician_swap": None,
        })

        deaths = self.engine.settle_night()
        self.assertEqual(deaths, {3: "poison"})
        self.engine._mark_dead_batch(deaths.keys(), "夜间结算")
        self.engine.phase = "第2天"
        before = len(self.engine.public_log)
        list(self.engine._announce_morning(deaths, peace_first=False))
        public_text = "\n".join(
            row.get("text", "") for row in self.engine.public_log[before:]
        )

        self.assertFalse(self.engine.players[3]["alive"])
        self.assertIn("3号倒下", public_text)
        self.assertNotIn("平安夜", public_text)

    def test_eliminated_human_can_see_all_roles_but_not_private_wolf_channels(self):
        viewer = next(s for s in self.engine.players if self.engine.roles[s].camp != "狼人")
        self.engine.human_seat = viewer
        self.engine.players[viewer]["alive"] = False
        self.engine.war_board = {"day": 2, "discussion": [(1, "狼队秘密")], "kill": 3}
        self.engine.current_wolf_talk = [(1, "狼队秘密")]

        # Death has been settled internally but not announced yet: keep the other roles hidden.
        before_announcement = project_view(self.engine, viewer_seat=viewer)
        self.assertEqual(before_announcement.players[next(s for s in self.engine.players if s != viewer)]["role"], "未知")
        self.engine.published_alive[viewer] = False
        view = project_view(self.engine, viewer_seat=viewer)

        for seat in self.engine.players:
            self.assertEqual(view.players[seat]["role"], self.engine.players[seat]["role"])
        self.assertEqual(view.role_seats, self.engine.role_seats)
        self.assertIsNotNone(view.counts)
        self.assertIsNone(view.war_board)
        self.assertEqual(view.wolf_talk, [])
        self.assertEqual(view.wolves, [])

    def test_spectator_cannot_unlock_roles_by_selecting_a_dead_seat(self):
        viewer = next(s for s in self.engine.players if self.engine.roles[s].camp != "狼人")
        self.engine.players[viewer]["alive"] = False
        self.engine.published_alive[viewer] = False
        self.engine.human_seat = None

        view = project_view(self.engine, viewer_seat=viewer)

        other = next(s for s in self.engine.players if s != viewer)
        self.assertEqual(view.players[other]["role"], "未知")
        self.assertEqual(view.role_seats, {})

    def test_live_human_sees_all_roles_after_game_ends(self):
        viewer = next(s for s in self.engine.players if self.engine.roles[s].camp != "狼人")
        self.engine.human_seat = viewer
        self.engine.winner = "好人"

        view = project_view(self.engine, viewer_seat=viewer)

        for seat in self.engine.players:
            self.assertEqual(view.players[seat]["role"], self.engine.players[seat]["role"])
        self.assertEqual(view.role_seats, self.engine.role_seats)
        self.assertIsNotNone(view.counts)

    def test_day_exploded_wolf_gets_one_final_night_discussion_and_kill_vote(self):
        exploded = next(s for s, role in self.engine.roles.items()
                        if role.role_name == "狼人")
        self.engine.human_seat = exploded
        self.engine.day = 2
        self.engine.phase = "第2天"
        self.engine._last_resp = {"is_explode": True}
        list(self.engine._handle_regular_explode(exploded))

        self.assertFalse(self.engine.players[exploded]["alive"])
        self.assertEqual(self.engine.pending_wolf_night_seats, [exploded])
        self.assertEqual(self.engine.pending_wolf_night_day, 3)

        # A save made before the next night must retain the scheduled final action.
        restored = GameEngine(MemoryOnlyManager())
        restored.load_state(self.engine.export_state())
        self.assertEqual(restored.pending_wolf_night_seats, [exploded])
        self.assertEqual(restored.pending_wolf_night_day, 3)

        self.engine.day = 3
        self.engine.phase = "第3夜"
        alive_target = next(s for s in self.engine.alive_seats()
                            if self.engine.roles[s].camp != "狼人")
        calls = []
        during_action_views = []

        def fake_ask(seat, prompt, label, public=False, response_kind="speech", allowed_targets=None):
            calls.append((seat, label, prompt, response_kind))
            if seat == exploded:
                during_action_views.append(project_view(self.engine, viewer_seat=exploded))
            if response_kind == "vote":
                self.engine._last_resp = {
                    "reasoning": "测试落刀", "speech": "", "vote_target": alive_target
                }
            else:
                self.engine._last_resp = {
                    "reasoning": "测试讨论", "speech": f"{seat}号测试战术"
                }
            if False:
                yield None

        self.engine._ask = fake_ask
        list(self.engine._run_wolf_pack_night("D3 夜晚"))

        self.assertEqual(sum(1 for seat, label, _, _ in calls
                             if seat == exploded and "狼人讨论" in label), 1)
        self.assertEqual(sum(1 for seat, label, _, kind in calls
                             if seat == exploded and "狼人落刀" in label and kind == "vote"), 1)
        self.assertTrue(during_action_views)
        self.assertEqual(during_action_views[0].players[alive_target]["role"], "未知")
        self.assertFalse(self.engine.players[exploded]["alive"])
        self.assertEqual(self.engine.pending_wolf_night_seats, [])
        self.assertEqual(self.engine.active_last_stand_wolves, [])

        after = project_view(self.engine, viewer_seat=exploded)
        for seat in self.engine.players:
            self.assertEqual(after.players[seat]["role"], self.engine.players[seat]["role"])
        self.assertIsNone(after.war_board)
        self.assertEqual(after.wolf_talk, [])


if __name__ == "__main__":
    unittest.main()
