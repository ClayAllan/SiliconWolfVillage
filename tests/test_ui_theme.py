import unittest
import tomllib
from pathlib import Path

from ui.ui_theme import DARK, LIGHT, contrast_ratio, theme_variables
from ui.ui_design import stylesheet


class ThemeTests(unittest.TestCase):
    def test_body_labels_and_primary_buttons_have_sufficient_contrast(self):
        for name,palette in [("dark",DARK),("light",LIGHT)]:
            for foreground in ("ink","body","muted","gold"):
                for background in ("bg","surface","surface2","stage1","stage4"):
                    with self.subTest(theme=name,foreground=foreground,background=background):
                        self.assertGreaterEqual(contrast_ratio(palette[foreground],palette[background]),4.5)
            self.assertGreaterEqual(contrast_ratio(palette["on-accent"],palette["accent-fill"]),4.5)
            for foreground,background in [("wolf","wolf-bg"),("god","god-bg"),("thinking","thinking-bg")]:
                self.assertGreaterEqual(contrast_ratio(palette[foreground],palette[background]),4.5)

    def test_native_theme_is_the_only_appearance_source(self):
        config_path = Path(__file__).resolve().parents[1] / ".streamlit/config.toml"
        theme = tomllib.loads(config_path.read_text(encoding="utf-8"))["theme"]
        self.assertNotIn("base", theme)
        for name, palette in [("dark", DARK), ("light", LIGHT)]:
            self.assertEqual(theme[name]["backgroundColor"], palette["bg"])
            self.assertEqual(theme[name]["sidebar"]["textColor"], palette["ink"])
        css = stylesheet()
        self.assertIn("light-dark(", theme_variables())
        self.assertNotIn("color-scheme:", css)
        self.assertNotIn("prefers-color-scheme", css)
        self.assertNotIn('[data-testid="stMarkdownContainer"]{color:', css)


if __name__ == "__main__":
    unittest.main()
