import unittest

from ui import ui_design as design
from ui.ui_preview import preview_engine


class UiDesignTests(unittest.TestCase):
    def test_model_text_is_escaped_in_both_public_and_private_views(self):
        payload = '<img src=x onerror="alert(1)"> & <script>bad()</script>'
        for rendered in [design.event({"kind":"player","seat":1,"text":payload,"phase":payload}),
                         design.reasoning({"reasoning":payload,"speech":payload,"label":payload})]:
            self.assertNotIn("<script>", rendered)
            self.assertNotIn("<img ", rendered)
            self.assertIn("&lt;script&gt;", rendered)
            self.assertIn("&amp;", rendered)

    def test_preview_stage_preserves_all_seats_and_player_states(self):
        engine = preview_engine()
        html = design.stage(engine.players, engine.current_speaker, selected=7,
                            sheriff=engine.sheriff, engine=engine)
        for seat in range(1,13):
            self.assertIn(f'aria-label="{seat}号 ', html)
        self.assertIn('aria-label="10号 平民 已出局"', html)
        self.assertIn('aria-label="7号 平民 正在思考 / 发言"', html)
        self.assertIn("wv-sheriff", html)
        self.assertFalse(hasattr(engine.manager, "client"))


if __name__ == "__main__":
    unittest.main()
