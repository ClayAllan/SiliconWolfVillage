import unittest

from benchmarks.feed_render_fixture import build_feed_rows, render_feed_entry
from storage.immutable_log import FrozenDict


class FeedRenderBenchmarkFixtureTests(unittest.TestCase):
    def test_rows_are_deterministic_ordered_and_recursively_frozen(self):
        first = build_feed_rows(13, game_id="fixture-game")
        second = build_feed_rows(13, game_id="fixture-game")

        self.assertEqual(first, second)
        self.assertEqual(len(first), 13)
        self.assertTrue(all(isinstance(row, FrozenDict) for row in first))
        self.assertEqual([row["event_seq"] for row in first], list(range(1, 14)))
        self.assertTrue(all(row["game_id"] == "fixture-game" for row in first))
        self.assertEqual(
            [row["kind"] for row in first[:4]],
            ["judge", "player", "god", "player"],
        )
        with self.assertRaises(TypeError):
            first[0]["text"] = "mutated"

    def test_fixture_uses_the_production_html_renderer_and_escapes_text(self):
        row = build_feed_rows(1)[0]
        html = render_feed_entry({**row, "text": "<script>alert(1)</script>"})
        self.assertIn('class="wv-event judge"', html)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_fixture_rejects_unbounded_or_empty_sizes(self):
        for count in (0, 15_001):
            with self.subTest(count=count):
                with self.assertRaises(ValueError):
                    build_feed_rows(count)


if __name__ == "__main__":
    unittest.main()
