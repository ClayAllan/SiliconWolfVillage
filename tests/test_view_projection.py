import unittest

from ui import ui_design as design
from ui.ui_preview import preview_engine
from service.view_projection import project_view, faction_counts


class ViewProjectionTests(unittest.TestCase):
    def test_each_single_view_only_receives_its_own_identity_and_reasoning(self):
        engine = preview_engine()
        engine.war_board = {"day":2,"note":"秘密刀口","discussion":[(1,"秘密讨论")]}
        engine.current_wolf_talk = [(1, "本夜实时队友讨论")]
        for seat in range(1,13):
            with self.subTest(seat=seat):
                view = project_view(engine,seat)
                is_wolf = engine.roles[seat].camp == "狼人"
                self.assertEqual(view.players[seat]["role"],engine.players[seat]["role"])
                self.assertEqual(view.reasoning_log[seat],engine.reasoning_log[seat])
                if is_wolf:
                    # 狼人视角：知晓自己与所有狼队友，且可查阅狼队夜间作战板
                    for w in engine.wolves:
                        self.assertEqual(view.players[w]["role"], engine.players[w]["role"])
                    for good in set(engine.players) - set(engine.wolves):
                        self.assertEqual(view.players[good]["role"], "未知")
                        self.assertEqual(view.reasoning_log[good], [])
                    self.assertIsNotNone(view.war_board)
                    self.assertEqual(view.wolf_talk, [(1, "本夜实时队友讨论")])
                    self.assertEqual(view.wolves, list(engine.wolves))
                else:
                    # 好人视角：仅知晓自己，所有其他玩家身份未知，无狼队情报
                    for other in set(engine.players)-{seat}:
                        self.assertEqual(view.players[other]["role"],"未知")
                        self.assertEqual(view.reasoning_log[other],[])
                        self.assertNotIn(other,view.skill_states)
                        self.assertNotIn("memory",view.players[other])
                    self.assertIsNone(view.war_board)
                    self.assertEqual(view.wolf_talk, [])
                    self.assertEqual(view.wolves,[])
                self.assertIsNone(view.counts)
                self.assertEqual(view.role_seats,{})
                self.assertFalse(any(row["kind"]=="god" for row in view.public_log))

    def test_hidden_identity_is_not_encoded_as_a_wolf_icon_or_color(self):
        view = project_view(preview_engine(),7)
        rendered = design.stage(view.players,view.current_speaker,engine=view)
        self.assertNotIn('class="wv-seat wolf',rendered)
        self.assertNotIn('class="wv-seat god',rendered)
        self.assertIn('aria-label="1号 未知 存活"',rendered)
        self.assertIn("7 号视角",rendered)

    def test_night_action_seat_is_only_visible_to_the_actor(self):
        engine = preview_engine()
        engine.phase = "第2夜"
        engine.current_speaker = 2
        self.assertIsNone(project_view(engine,7).current_speaker)
        self.assertEqual(project_view(engine,2).current_speaker,2)
        self.assertEqual(project_view(engine).current_speaker,2)

    def test_full_view_and_count_bar_use_actual_counts(self):
        engine = preview_engine()
        view = project_view(engine)
        self.assertEqual(view.counts,{"狼人":4,"神职":4,"平民":3,"出局":1})
        rendered = design.factions(view)
        self.assertIn('aria-label="狼人" aria-valuenow="4"',rendered)
        self.assertIn('aria-label="出局" aria-valuenow="1"',rendered)
        self.assertIn('width:8.3333%',rendered)

    def test_single_counts_are_unknown_until_explicitly_revealed(self):
        engine=preview_engine()
        view=project_view(engine,7)
        hidden=design.factions(view)
        self.assertIn('aria-label="身份未公开" aria-valuenow="11"',hidden)
        self.assertNotIn('aria-label="狼人"',hidden)
        self.assertIn('存活未知',hidden)
        revealed=design.factions(view,reveal_counts=faction_counts(engine))
        self.assertIn('aria-label="狼人" aria-valuenow="4"',revealed)
        self.assertIsNone(view.counts)

    def test_projection_does_not_mutate_the_real_game(self):
        engine=preview_engine()
        view=project_view(engine,7)
        view.players[7]["role"]="狼人"
        view.reasoning_log[7][0]["reasoning"]="被改写"
        view.public_log.clear()
        self.assertEqual(engine.players[7]["role"],"平民")
        self.assertNotEqual(engine.reasoning_log[7][0]["reasoning"],"被改写")
        self.assertTrue(engine.public_log)


if __name__ == "__main__":
    unittest.main()
