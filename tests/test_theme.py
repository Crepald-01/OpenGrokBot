"""Appearance options (accent, density, text size) and the command-palette matcher."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtGui import QColor  # noqa: E402

from ui import theme  # noqa: E402
from ui.main_window import _score  # noqa: E402


def luma(c: str) -> float:
    q = QColor(c)
    return 0.299 * q.redF() + 0.587 * q.greenF() + 0.114 * q.blueF()


class ThemeOptions(unittest.TestCase):
    def tearDown(self):
        theme.configure("indigo", "comfortable", "default")
        theme.set_theme("dark")

    def test_every_accent_has_readable_button_text(self):
        for name in ("dark", "light"):
            theme.set_theme(name)
            for key in theme.ACCENTS:
                theme.configure(accent=key)
                p = theme.palette()
                self.assertGreater(abs(luma(p["accent"]) - luma(p["accent_text"])), 0.3, f"{key}/{name}: button text is hard to read")

    def test_accent_drives_derived_colours(self):
        theme.configure(accent="indigo")
        a = dict(theme.palette())
        theme.configure(accent="rose")
        b = theme.palette()
        for k in ("accent", "accent_hi", "accent_dim", "accent_soft", "select", "user"):
            self.assertNotEqual(a[k], b[k], k)
        self.assertEqual(a["bg"], b["bg"])           # surfaces do not change

    def test_unknown_values_are_ignored(self):
        theme.configure(accent="nope", density="nope", text="nope")
        self.assertEqual(theme.options(), {"accent": "indigo", "density": "comfortable", "text": "default"})
        theme.configure(None, None, None)
        self.assertEqual(theme.options()["accent"], "indigo")

    def test_density_and_text_size(self):
        theme.configure(density="compact", text="large")
        self.assertLess(theme.dp(10), 10)
        self.assertGreater(theme.base_size(), 13)
        self.assertIn("font-size: 15px", theme.qss())
        theme.configure(density="comfortable", text="small")
        self.assertEqual(theme.dp(10), 10)
        self.assertEqual(theme.base_size(), 12)

    def test_set_theme_keeps_options(self):
        theme.configure(accent="teal")
        theme.set_theme("light")
        self.assertEqual(theme.palette()["accent"], theme.ACCENTS["teal"][2])
        theme.set_theme("dark")
        self.assertEqual(theme.palette()["accent"], theme.ACCENTS["teal"][1])


class PaletteMatching(unittest.TestCase):
    def test_ranking(self):
        self.assertGreater(_score("inb", "Inbox"), _score("inb", "My inbox"))       # prefix beats word start
        self.assertGreater(_score("box", "Inbox"), _score("ibx", "Inbox"))          # substring beats loose match
        self.assertGreater(_score("ibx", "Inbox"), 0)                                # loose subsequence still matches
        self.assertEqual(_score("zzz", "Inbox"), 0)
        self.assertGreater(_score("set", "Settings"), _score("set", "Reset the Bot"))


if __name__ == "__main__":
    unittest.main()
