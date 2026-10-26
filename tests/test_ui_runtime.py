# -*- coding: utf-8 -*-
import unittest

from ui.ui_runtime import AUTO_POLL_SECONDS, VISIBLE_RERUN_FLOOR_SECONDS, rerun_delay


class UiRuntimeTests(unittest.TestCase):
    def test_wait_events_are_throttled(self):
        self.assertEqual(rerun_delay("waiting", 0), AUTO_POLL_SECONDS)
        self.assertEqual(rerun_delay("compress_waiting", 0.2), AUTO_POLL_SECONDS)

    def test_visible_events_keep_a_small_floor(self):
        self.assertEqual(rerun_delay("done", 0), VISIBLE_RERUN_FLOOR_SECONDS)
        self.assertEqual(rerun_delay("night", 0.8), 0.8)

    def test_poll_interval_is_not_a_busy_loop(self):
        self.assertGreaterEqual(AUTO_POLL_SECONDS, 1.0)


if __name__ == "__main__":
    unittest.main()
