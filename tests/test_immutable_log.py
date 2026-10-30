"""Tests for immutable, deepcopy-shareable append-only game log rows."""
import copy
import json
import unittest

from engine.game_engine import GameEngine
from storage.immutable_log import FrozenDict, FrozenList, freeze_log_row


class ImmutableLogTests(unittest.TestCase):
    def test_freeze_recursively_copies_and_blocks_normal_mutation(self):
        original = {
            "kind": "votechart",
            "totals": {1: 2.5},
            "notes": [{"tags": ["public", "verified"]}],
            "tuple_value": ("stable", {"nested": [1, 2]}),
        }
        frozen = freeze_log_row(original)

        self.assertIsInstance(frozen, FrozenDict)
        self.assertIsInstance(frozen["totals"], FrozenDict)
        self.assertIsInstance(frozen["notes"], FrozenList)
        self.assertIsInstance(frozen["notes"][0], FrozenDict)
        self.assertIsInstance(frozen["notes"][0]["tags"], FrozenList)
        self.assertIsInstance(frozen["tuple_value"][1]["nested"], FrozenList)

        original["totals"][1] = 99
        original["notes"][0]["tags"].append("later")
        self.assertEqual(frozen["totals"][1], 2.5)
        self.assertEqual(list(frozen["notes"][0]["tags"]), ["public", "verified"])

        with self.assertRaises(TypeError):
            frozen["kind"] = "changed"
        with self.assertRaises(TypeError):
            frozen["totals"][1] = 0
        with self.assertRaises(TypeError):
            frozen["notes"][0]["tags"].append("changed")
        with self.assertRaises(TypeError):
            frozen["notes"].clear()
        with self.assertRaises(TypeError):
            frozen.update({"other": True})
        with self.assertRaises(TypeError):
            frozen.__init__({"other": True})
        with self.assertRaises(TypeError):
            frozen["notes"].__init__(["mutable"])

        self.assertIs(copy.deepcopy(frozen), frozen)
        self.assertIs(copy.copy(frozen), frozen)
        self.assertEqual(json.loads(json.dumps(frozen))["totals"]["1"], 2.5)

    def test_unsupported_or_cyclic_values_fall_back_without_sharing_them(self):
        unsupported = {"payload": object()}
        self.assertIs(freeze_log_row(unsupported), unsupported)

        cyclic = {}
        cyclic["self"] = cyclic
        self.assertIs(freeze_log_row(cyclic), cyclic)

    def test_engine_freezes_public_and_reasoning_rows_but_keeps_outer_logs_appendable(self):
        engine = GameEngine(object())
        engine._append_public_item({
            "kind": "votechart",
            "totals": {1: 2.0},
            "votes": {2: 1},
            "labels": ["first", "second"],
        })
        engine._record_reasoning(
            1, "D1 发言", "reasoning", "speech",
            response={"decision_source": "model", "meta": {"targets": [2, 3]}},
        )

        public_row = engine.public_log[0]
        reasoning_row = engine.reasoning_log[1][0]
        self.assertIsInstance(public_row, FrozenDict)
        self.assertIsInstance(public_row["totals"], FrozenDict)
        self.assertIsInstance(public_row["labels"], FrozenList)
        self.assertIsInstance(reasoning_row, FrozenDict)
        self.assertIsInstance(reasoning_row["response"]["meta"]["targets"], FrozenList)

        engine._append_public_item({"kind": "judge", "text": "next"})
        engine.reasoning_log[1].append(freeze_log_row({"label": "next"}))
        self.assertEqual(len(engine.public_log), 2)
        self.assertEqual(len(engine.reasoning_log[1]), 2)

    def test_load_state_freezes_restored_rows_and_keeps_them_serializable(self):
        engine = GameEngine(object())
        engine._append_public_item({
            "kind": "votechart", "totals": {1: 1.0}, "votes": {2: 1},
        })
        engine._record_reasoning(
            1, "D1 发言", "reasoning", "speech",
            response={"decision_source": "model", "metadata": {"choices": [1, 2]}},
        )
        saved = engine.export_state()

        restored = GameEngine(object())
        restored.load_state(saved)

        self.assertIsInstance(restored.public_log[0], FrozenDict)
        self.assertIsInstance(restored.public_log[0]["totals"], FrozenDict)
        self.assertIsInstance(restored.reasoning_log[1][0], FrozenDict)
        self.assertIsInstance(
            restored.reasoning_log[1][0]["response"]["metadata"]["choices"], FrozenList
        )
        self.assertEqual(
            json.loads(json.dumps(restored.export_state()))["public_log"][0]["kind"],
            "votechart",
        )
        restored._log_judge("读档后追加")
        self.assertEqual(restored.public_log[-1]["event_seq"], 2)


if __name__ == "__main__":
    unittest.main()
