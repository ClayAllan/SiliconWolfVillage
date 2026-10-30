# -*- coding: utf-8 -*-
import unittest

from engine import config
from engine.game_engine import GameEngine
from engine.roles import ROLE_CLASSES
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


    def test_human_self_explode_interrupts_pending_human_action(self):
        exploded = self.engine.wolves[0]
        self.engine.human_seat = exploded
        self.engine.day = 2
        self.engine.phase = "第2天"

        def pending_human_speech(deaths, peace_first):
            yield from self.engine._ask(
                exploded, "真人发言", "D2 发言", public=True
            )

        self.engine._run_day_discussion_and_vote_impl = pending_human_speech
        stage = self.engine._run_day_discussion_and_vote({}, False)
        event = next(stage)
        self.assertEqual(event["type"], "human_input_required")
        self.assertEqual(self.engine._human_explode_window, "discussion")

        self.engine.request_human_explode()
        with self.assertRaises(StopIteration):
            next(stage)

        self.assertFalse(self.engine.players[exploded]["alive"])
        self.assertEqual(self.engine.pending_wolf_night_seats, [exploded])
        self.assertEqual(self.engine.pending_wolf_night_day, 3)
        self.assertIsNone(self.engine.pending_human_action)
        self.assertFalse(self.engine._human_explode_requested)
        self.assertIsNone(self.engine._human_explode_window)

    def test_human_self_explode_cancels_inflight_ai_action_without_recording_it(self):
        exploded = self.engine.wolves[0]
        ai_seat = next(
            seat for seat in self.engine.alive_seats()
            if seat != exploded and self.engine.roles[seat].camp != "狼人"
        )
        self.engine.human_seat = exploded
        self.engine.day = 2
        self.engine.phase = "第2天"

        def pending_ai_speech(deaths, peace_first):
            yield from self.engine._ask(
                ai_seat, "AI发言", "D2 发言", public=True
            )

        self.engine._run_day_discussion_and_vote_impl = pending_ai_speech
        stage = self.engine._run_day_discussion_and_vote({}, False)
        event = next(stage)
        self.assertEqual(event["type"], "turn")
        self.assertTrue(self.manager.pending)

        self.engine.request_human_explode()
        with self.assertRaises(StopIteration):
            next(stage)

        self.assertFalse(self.manager.pending)
        self.assertFalse(self.engine.players[exploded]["alive"])
        self.assertFalse(any(
            row.get("label") == "D2 发言"
            for row in self.engine.reasoning_log[ai_seat]
        ))
        self.assertEqual(self.engine.pending_wolf_night_seats, [exploded])

    def test_human_self_explode_rejects_wrong_phase_and_good_role(self):
        wolf = self.engine.wolves[0]
        self.engine.human_seat = wolf
        self.engine.day = 2
        self.engine.phase = "第2天"
        with self.assertRaisesRegex(RuntimeError, "白天行动阶段"):
            self.engine.request_human_explode()

        good = next(
            seat for seat in self.engine.alive_seats()
            if self.engine.roles[seat].camp != "狼人"
        )
        self.engine.human_seat = good
        self.engine._human_explode_window = "discussion"
        with self.assertRaisesRegex(RuntimeError, "没有自爆权"):
            self.engine.request_human_explode()


    def test_human_self_explode_interrupts_sheriff_election(self):
        exploded = self.engine.wolves[0]
        self.engine.human_seat = exploded
        self.engine.day = 1
        self.engine.phase = "第1天"
        self.engine.election_state = 0

        election = self.engine._run_election()
        event = next(election)
        self.assertEqual(event["type"], "election")
        self.assertEqual(self.engine._human_explode_window, "election")

        self.engine.request_human_explode()
        with self.assertRaises(StopIteration):
            next(election)

        self.assertFalse(self.engine.players[exploded]["alive"])
        self.assertEqual(self.engine.election_state, 2)
        self.assertEqual(self.engine.pending_wolf_night_day, 2)
        self.assertEqual(self.engine.pending_wolf_night_seats, [exploded])

    def test_white_wolf_king_human_explosion_uses_selected_target(self):
        exploded = self.engine.wolves[0]
        self.engine.roles[exploded].role_name = "白狼王"
        self.engine.players[exploded]["role"] = "白狼王"
        target = next(
            seat for seat in self.engine.alive_seats()
            if seat != exploded
            and self.engine.roles[seat].camp != "狼人"
            and self.engine.roles[seat].role_name != "猎人"
        )
        self.engine.human_seat = exploded
        self.engine.day = 2
        self.engine.phase = "第2天"

        def pending_human_speech(deaths, peace_first):
            yield from self.engine._ask(
                exploded, "真人发言", "D2 发言", public=True
            )

        self.engine._run_day_discussion_and_vote_impl = pending_human_speech
        stage = self.engine._run_day_discussion_and_vote({}, False)
        self.assertEqual(next(stage)["type"], "human_input_required")
        self.engine.request_human_explode(target)
        with self.assertRaises(StopIteration):
            next(stage)

        self.assertFalse(self.engine.players[exploded]["alive"])
        self.assertFalse(self.engine.players[target]["alive"])
        self.assertEqual(self.engine.explosion_events[-1]["target"], target)

    def test_human_private_skill_results_are_visible_after_actions(self):
        seat = 1
        scenarios = [
            ("狼美人", {"vote_target": 2}, "狼美人魅惑结果"),
            ("摄梦人", {"vote_target": 2}, "摄梦结果"),
            ("魔术师", {"targets": [2, 3]}, "魔术师交换结果"),
            ("守墓人", {"speech": "已确认"}, "守墓人验墓结果"),
            ("女巫", {"use_antidote": True}, "女巫解药结果"),
        ]
        for role_name, payload, expected in scenarios:
            with self.subTest(role=role_name):
                role = ROLE_CLASSES[role_name](seat)
                self.engine.roles[seat] = role
                self.engine.players[seat]["role"] = role_name
                self.engine.human_seat = seat
                self.engine.day = 2
                self.engine.phase = "第2夜"
                self.engine.night = {}
                self.engine.human_private_messages = []
                if role_name == "守墓人":
                    self.engine.last_exiled_seat = 12
                    self.engine.last_exiled_camp = "狼人"
                elif role_name == "女巫":
                    self.engine.night["wolf_kill"] = 2

                action = self.engine._run_solo_night(role)
                self.assertEqual(next(action)["type"], "human_input_required")
                pending = self.engine.pending_human_action
                self.engine.submit_human_action(pending["action_id"], payload)
                self.assertEqual(next(action)["type"], "done")
                with self.assertRaises(StopIteration):
                    next(action)
                self.assertTrue(any(
                    expected in message for message in self.engine.human_private_messages
                ))

        cupid = ROLE_CLASSES["丘比特"](seat)
        self.engine.roles[seat] = cupid
        self.engine.players[seat]["role"] = "丘比特"
        self.engine.human_seat = seat
        self.engine.lovers = None
        self.engine.human_private_messages = []
        link_action = self.engine._run_cupid_link()
        self.assertEqual(next(link_action)["type"], "cupid")
        self.assertEqual(next(link_action)["type"], "human_input_required")
        pending = self.engine.pending_human_action
        self.engine.submit_human_action(pending["action_id"], {"targets": [2, 3]})
        self.assertEqual(next(link_action)["type"], "done")
        with self.assertRaises(StopIteration):
            next(link_action)
        self.assertTrue(any(
            "丘比特连线结果" in message for message in self.engine.human_private_messages
        ))


if __name__ == "__main__":
    unittest.main()
