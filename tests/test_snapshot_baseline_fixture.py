import unittest

from benchmarks.snapshot_baseline import fixture


class SnapshotBaselineFixtureTests(unittest.TestCase):
    def test_reasoning_records_are_balanced_across_all_twelve_seats(self):
        # 120 public events produce 40 reasoning records: four seats receive
        # four records and the remaining eight seats receive three.
        engine, _human_seat = fixture(120)
        counts = [len(engine.reasoning_log[seat]) for seat in range(1, 13)]

        self.assertEqual(sum(counts), 40)
        self.assertEqual(len([count for count in counts if count > 0]), 12)
        self.assertEqual(min(counts), 3)
        self.assertEqual(max(counts), 4)
        self.assertLessEqual(max(counts) - min(counts), 1)


if __name__ == "__main__":
    unittest.main()
