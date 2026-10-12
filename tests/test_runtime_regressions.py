import unittest
from types import SimpleNamespace

from ai_agent import AgentManager
from game_engine import GameEngine


class AgentManagerRegressionTests(unittest.TestCase):
    def test_parse_json_payload_supports_markdown_wrappers(self):
        raw = "```json\n{\"reasoning\": \"ok\", \"speech\": \"hello\"}\n```"
        self.assertEqual(
            AgentManager._parse_json_payload(raw),
            {"reasoning": "ok", "speech": "hello"},
        )

    def test_coerce_seat_handles_text_and_bounds(self):
        self.assertEqual(AgentManager._coerce_seat("投给7号"), 7)
        self.assertEqual(AgentManager._coerce_seat(99), 0)
        self.assertEqual(AgentManager._coerce_seat(False), 0)

    def test_normalize_fills_safe_defaults(self):
        manager = AgentManager(api_key="test-key", base_url="https://api.example.com")
        normalized = manager._normalize({"speech": "过。"})
        self.assertEqual(normalized["speech"], "过。")
        self.assertEqual(normalized["vote_target"], 0)
        self.assertFalse(normalized["is_explode"])
        self.assertFalse(normalized["withdraw"])


class GameEngineRegressionTests(unittest.TestCase):
    def test_check_winner_returns_good_when_no_wolves_remain(self):
        engine = GameEngine(SimpleNamespace())
        engine.players = {seat: {"role": "平民", "alive": True} for seat in range(1, 13)}
        engine.roles = {
            seat: SimpleNamespace(camp="好人") for seat in range(1, 13)
        }
        engine.wolves = []
        self.assertEqual(engine.check_winner(), "好人")

    def test_check_winner_detects_wolf_win_when_villagers_are_eliminated(self):
        engine = GameEngine(SimpleNamespace())
        engine.players = {
            1: {"role": "狼人", "alive": True},
            2: {"role": "狼人", "alive": True},
            3: {"role": "平民", "alive": False},
            4: {"role": "平民", "alive": False},
            5: {"role": "平民", "alive": False},
            6: {"role": "平民", "alive": False},
            7: {"role": "平民", "alive": False},
            8: {"role": "平民", "alive": False},
            9: {"role": "预言家", "alive": False},
            10: {"role": "女巫", "alive": False},
            11: {"role": "猎人", "alive": False},
            12: {"role": "守卫", "alive": False},
        }
        engine.roles = {
            1: SimpleNamespace(camp="狼人"),
            2: SimpleNamespace(camp="狼人"),
            3: SimpleNamespace(camp="好人"),
            4: SimpleNamespace(camp="好人"),
            5: SimpleNamespace(camp="好人"),
            6: SimpleNamespace(camp="好人"),
            7: SimpleNamespace(camp="好人"),
            8: SimpleNamespace(camp="好人"),
            9: SimpleNamespace(camp="好人"),
            10: SimpleNamespace(camp="好人"),
            11: SimpleNamespace(camp="好人"),
            12: SimpleNamespace(camp="好人"),
        }
        engine.wolves = [1, 2]
        self.assertEqual(engine.check_winner(), "狼人")


if __name__ == "__main__":
    unittest.main()
