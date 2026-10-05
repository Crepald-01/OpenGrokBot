"""Design system: calm dark theme (default) and a light variant.

Principles: one accent colour, quiet surfaces separated by hairlines instead of boxes, generous spacing on a 4px grid,
no motion or colour flashes while a Bot works. Status is shown with small dots and soft tinted pills.
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QPainter, QPen, QPixmap

DARK = {
    "bg": "#0e1014", "panel": "#13161c", "panel2": "#1a1e26", "raised": "#20252f", "line": "#232832", "line2": "#2d3340",
    "text": "#e8eaf0", "muted": "#8a92a6", "faint": "#5d6577", "accent": "#7c9cff", "accent_hi": "#98b2ff", "accent_text": "#0b0e16",
    "accent_dim": "#232f55", "accent_soft": "#1a2240", "ok": "#5bc48a", "ok_bg": "#15291f", "warn": "#e3b45f", "warn_bg": "#2e2514",
    "bad": "#e47c7c", "bad_bg": "#2e1a1d", "user": "#222d52", "code": "#0b0d12", "hover": "#191d25", "select": "#1b2236",
}
LIGHT = {
    "bg": "#f5f6f9", "panel": "#ffffff", "panel2": "#f0f2f7", "raised": "#e8ebf2", "line": "#e3e6ee", "line2": "#d3d8e3",
    "text": "#1b2030", "muted": "#667089", "faint": "#98a0b3", "accent": "#3d63dd", "accent_hi": "#5476e8", "accent_text": "#ffffff",
    "accent_dim": "#dbe4ff", "accent_soft": "#eaf0ff", "ok": "#2a9460", "ok_bg": "#e3f4eb", "warn": "#a8761a", "warn_bg": "#fbf0d9",
    "bad": "#c24747", "bad_bg": "#fbe6e6", "user": "#e1e9ff", "code": "#eef0f6", "hover": "#eceff5", "select": "#e6ecff",
}
_current = dict(DARK)
_name = "dark"        # the theme in use: dark or light
_pref = "dark"        # what the user chose: dark, light or auto (match Windows)

FONT = '"Segoe UI Variable Text", "Segoe UI", "Inter", "SF Pro Text", "Helvetica Neue", Arial, sans-serif'

# Appearance options (stored per user in the UI config, not in the service): accent colour, density and text size.
ACCENTS = {   # key: (label, dark-theme colour, light-theme colour)
    "indigo": ("Indigo", "#7c9cff", "#3d63dd"),
    "violet": ("Violet", "#a58bff", "#6d4fe0"),
    "teal": ("Teal", "#47c7bd", "#0f8f89"),
    "green": ("Green", "#5bc48a", "#2a9460"),
    "amber": ("Amber", "#f0b45a", "#b36a00"),
    "rose": ("Rose", "#f27a9d", "#d03c6c"),
}
DENSITIES = {"comfortable": ("Comfortable", 1.0), "compact": ("Compact", 0.78)}
TEXT_SIZES = {"small": ("Small", 12), "default": ("Default", 13), "large": ("Large", 15)}
_opts = {"accent": "indigo", "density": "comfortable", "text": "default"}


def _mix(a: str, b: str, t: float) -> str:
    """Blend colour a towards b by t (0..1)."""
    ca, cb = QColor(a), QColor(b)
    return QColor(round(ca.red() + (cb.red() - ca.red()) * t), round(ca.green() + (cb.green() - ca.green()) * t),
                  round(ca.blue() + (cb.blue() - ca.blue()) * t)).name()


def _luma(c: str) -> float:
    q = QColor(c)
    return 0.299 * q.redF() + 0.587 * q.greenF() + 0.114 * q.blueF()


def _rebuild() -> None:
    base = LIGHT if _name == "light" else DARK
    _current.clear()
    _current.update(base)
    label, dark_c, light_c = ACCENTS[_opts["accent"]]
    acc = light_c if _name == "light" else dark_c
    bg, panel = base["bg"], base["panel"]
    _current.update(
        accent=acc, accent_hi=_mix(acc, "#ffffff", 0.18 if _name == "dark" else 0.12),
        accent_text="#0b0e16" if _luma(acc) > 0.55 else "#ffffff",
        accent_dim=_mix(panel, acc, 0.30 if _name == "dark" else 0.22), accent_soft=_mix(panel, acc, 0.14 if _name == "dark" else 0.09),
        select=_mix(panel, acc, 0.16 if _name == "dark" else 0.11), user=_mix(bg, acc, 0.26 if _name == "dark" else 0.16))


def palette() -> dict:
    return _current


def theme_name() -> str:
    return _name


def _apps_use_light() -> bool | None:
    """Windows' own light/dark setting for apps (None when it cannot be read, e.g. on other systems)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return bool(winreg.QueryValueEx(k, "AppsUseLightTheme")[0])
    except (ImportError, OSError):
        return None


def system_theme() -> str:
    return "light" if _apps_use_light() else "dark"


def preference() -> str:
    return _pref


def set_theme(name: str) -> dict:
    """name: dark | light | auto. `auto` follows Windows' app mode."""
    global _name, _pref
    _pref = name if name in ("light", "dark", "auto") else "dark"
    _name = system_theme() if _pref == "auto" else _pref
    _rebuild()
    return _current


def configure(accent: str | None = None, density: str | None = None, text: str | None = None) -> None:
    """Set appearance options (unknown values are ignored) and recompute the palette."""
    if accent in ACCENTS:
        _opts["accent"] = accent
    if density in DENSITIES:
        _opts["density"] = density
    if text in TEXT_SIZES:
        _opts["text"] = text
    _rebuild()


def options() -> dict:
    return dict(_opts)


def dp(n: float) -> int:
    """Scale a spacing value by the density setting (compact = tighter)."""
    return max(1, round(n * DENSITIES[_opts["density"]][1]))


def base_size() -> int:
    return TEXT_SIZES[_opts["text"]][1]


def _arrow_url(color: str) -> str:
    """Render a small chevron to a temp PNG so combo boxes show a dropdown arrow (QSS needs a file)."""
    try:
        import os
        import tempfile

        from . import icons
        d = os.path.join(tempfile.gettempdir(), "opengrokbot-ui")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"chevron-{color.strip('#')}.png")
        if not os.path.exists(path):
            icons.pixmap("chevron-down", color, 12, 2.2).save(path, "PNG")
        return path.replace("\\", "/")
    except Exception:
        return ""


def qss() -> str:
    c = _current
    arrow = _arrow_url(c["muted"])
    fs, small, tiny = base_size(), base_size() - 1, base_size() - 2
    bv, bh = dp(6), dp(14) if dp(14) > 9 else 9
    iv = dp(8)
    arrow_rule = f"QComboBox::down-arrow {{ image: url({arrow}); width: 12px; height: 12px; }}" if arrow else ""
    return f"""
* {{ font-family: {FONT}; font-size: {fs}px; color: {c['text']}; outline: none; }}
QWidget {{ background: transparent; }}
QMainWindow, QDialog, QMessageBox, QInputDialog, QWidget#root {{ background: {c['bg']}; }}
QToolTip {{ background: {c['raised']}; color: {c['text']}; border: 1px solid {c['line2']}; padding: 5px 8px; border-radius: 6px; }}

QLabel[muted="true"] {{ color: {c['muted']}; }}
QLabel[faint="true"] {{ color: {c['faint']}; font-size: {small}px; }}
QLabel[h1="true"] {{ font-size: 22px; font-weight: 600; letter-spacing: -0.2px; }}
QLabel[h2="true"] {{ font-size: 14px; font-weight: 600; }}
QLabel[eyebrow="true"] {{ color: {c['faint']}; font-size: 11px; font-weight: 600; letter-spacing: 1px; }}
QLabel[chip="true"] {{ background: {c['panel2']}; color: {c['muted']}; border-radius: 9px; padding: 2px 10px; font-size: {tiny}px; }}
QLabel[chip="ok"] {{ background: {c['ok_bg']}; color: {c['ok']}; border-radius: 9px; padding: 2px 10px; font-size: {tiny}px; }}
QLabel[chip="warn"] {{ background: {c['warn_bg']}; color: {c['warn']}; border-radius: 9px; padding: 2px 10px; font-size: {tiny}px; }}
QLabel[chip="bad"] {{ background: {c['bad_bg']}; color: {c['bad']}; border-radius: 9px; padding: 2px 10px; font-size: {tiny}px; }}
QLabel[chip="work"] {{ background: {c['accent_dim']}; color: {c['accent']}; border-radius: 9px; padding: 2px 10px; font-size: {tiny}px; }}
QLabel[badge="true"] {{ background: {c['accent']}; color: {c['accent_text']}; border-radius: 9px; padding: 0px 6px; font-size: 11px; font-weight: 600; }}

QFrame[card="true"] {{ background: {c['panel']}; border: 1px solid {c['line']}; border-radius: 14px; }}
QFrame[card="hover"] {{ background: {c['panel']}; border: 1px solid {c['line']}; border-radius: 12px; }}
QFrame[card="hover"]:hover {{ border: 1px solid {c['accent_dim']}; background: {c['hover']}; }}
QFrame[card="approval"] {{ background: {c['warn_bg']}; border: 1px solid {c['line2']}; border-left: 3px solid {c['warn']}; border-radius: 12px; }}
QFrame[card="question"] {{ background: {c['accent_soft']}; border: 1px solid {c['line2']}; border-left: 3px solid {c['accent']}; border-radius: 12px; }}
QFrame[card="tile"] {{ background: {c['panel']}; border: 1px solid {c['line']}; border-radius: 14px; }}
QFrame[card="tile"][hot="true"] {{ background: {c['warn_bg']}; border: 1px solid {c['line2']}; }}
QFrame[card="plain"] {{ background: transparent; border: none; }}
QFrame[sep="true"] {{ background: {c['line']}; max-height: 1px; min-height: 1px; border: none; }}
QFrame[vsep="true"] {{ background: {c['line']}; max-width: 1px; min-width: 1px; border: none; }}
QFrame#sidebar {{ background: {c['panel']}; border-right: 1px solid {c['line']}; }}
QFrame#composer {{ background: {c['panel']}; border: 1px solid {c['line2']}; border-radius: 18px; }}
QFrame#composer:focus-within {{ border: 1px solid {c['accent']}; }}
QFrame#activity {{ background: {c['panel']}; border-left: 1px solid {c['line']}; }}
QFrame#toolgroup {{ background: {c['panel']}; border: 1px solid {c['line']}; border-radius: 10px; }}

QPushButton {{ background: {c['panel2']}; border: 1px solid {c['line2']}; border-radius: 9px; padding: {bv}px {bh}px; min-height: 18px; }}
QPushButton:hover {{ background: {c['raised']}; }}
QPushButton:pressed {{ background: {c['line2']}; }}
QPushButton:disabled {{ color: {c['faint']}; background: {c['panel']}; border-color: {c['line']}; }}
QPushButton[primary="true"] {{ background: {c['accent']}; color: {c['accent_text']}; border: 1px solid {c['accent']}; font-weight: 600; }}
QPushButton[primary="true"]:hover {{ background: {c['accent_hi']}; border-color: {c['accent_hi']}; }}
QPushButton[primary="true"]:disabled {{ background: {c['accent_dim']}; color: {c['faint']}; border-color: {c['accent_dim']}; }}
QPushButton[danger="true"] {{ background: transparent; border: 1px solid {c['line2']}; color: {c['bad']}; }}
QPushButton[danger="true"]:hover {{ background: {c['bad_bg']}; border-color: {c['bad']}; }}
QPushButton[flat="true"] {{ background: transparent; border: none; color: {c['muted']}; padding: 5px 9px; }}
QPushButton[flat="true"]:hover {{ color: {c['text']}; background: {c['hover']}; }}
QPushButton[iconbtn="true"] {{ background: transparent; border: none; border-radius: 8px; padding: 0px; min-width: 32px; max-width: 32px; min-height: 32px; max-height: 32px; }}
QPushButton[iconbtn="true"]:hover {{ background: {c['hover']}; }}
QPushButton[iconbtn="accent"] {{ background: {c['accent']}; border: none; border-radius: 16px; padding: 0px; min-width: 32px; max-width: 32px; min-height: 32px; max-height: 32px; }}
QPushButton[iconbtn="accent"]:hover {{ background: {c['accent_hi']}; }}
QPushButton[iconbtn="accent"]:disabled {{ background: {c['accent_dim']}; }}
QPushButton[iconbtn="danger"] {{ background: {c['bad_bg']}; border: 1px solid {c['bad']}; border-radius: 16px; padding: 0px; min-width: 32px; max-width: 32px; min-height: 32px; max-height: 32px; }}
QPushButton[side="true"] {{ background: transparent; border: none; border-radius: 8px; text-align: left; padding: 7px 10px; color: {c['muted']}; }}
QPushButton[side="true"]:hover {{ background: {c['hover']}; color: {c['text']}; }}
QPushButton[side="true"]:checked {{ background: {c['select']}; color: {c['text']}; }}
QPushButton[chipbtn="true"] {{ background: {c['panel']}; border: 1px solid {c['line2']}; border-radius: 14px; padding: 6px 14px; color: {c['muted']}; }}
QPushButton[chipbtn="true"]:hover {{ border-color: {c['accent']}; color: {c['text']}; background: {c['accent_soft']}; }}

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
  background: {c['panel']}; border: 1px solid {c['line2']}; border-radius: 8px; padding: 6px 10px; selection-background-color: {c['accent_dim']}; }}
QLineEdit:hover, QPlainTextEdit:hover, QComboBox:hover, QSpinBox:hover {{ border-color: {c['faint']}; }}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus {{ border: 1px solid {c['accent']}; }}
QLineEdit[bare="true"], QPlainTextEdit[bare="true"] {{ border: none; background: transparent; padding: 2px 4px; }}
QLineEdit[search="true"] {{ border-radius: 14px; padding: 6px 12px 6px 12px; background: {c['panel2']}; border: 1px solid transparent; }}
QComboBox {{ padding-right: 24px; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
{arrow_rule}
QComboBox QAbstractItemView {{ background: {c['panel2']}; border: 1px solid {c['line2']}; border-radius: 8px; selection-background-color: {c['accent_dim']}; padding: 4px; }}
QSpinBox::up-button, QSpinBox::down-button {{ width: 0; border: none; }}
QCheckBox {{ spacing: 9px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 5px; border: 1px solid {c['line2']}; background: {c['panel']}; }}
QCheckBox::indicator:hover {{ border-color: {c['accent']}; }}
QCheckBox::indicator:checked {{ background: {c['accent']}; border-color: {c['accent']}; image: none; }}

QListWidget, QTreeWidget, QTableWidget, QListView {{ background: {c['panel']}; border: 1px solid {c['line']}; border-radius: 12px; }}
QListWidget::item {{ padding: {iv}px 10px; border-radius: 8px; margin: 1px 4px; }}
QListWidget::item:selected {{ background: {c['select']}; color: {c['text']}; }}
QListWidget::item:hover:!selected {{ background: {c['hover']}; }}
QHeaderView {{ background: transparent; }}
QHeaderView::section {{ background: transparent; color: {c['faint']}; border: none; border-bottom: 1px solid {c['line']}; padding: 9px 10px; font-size: 11px; font-weight: 600; text-transform: uppercase; }}
QTableWidget {{ gridline-color: transparent; }}
QTableWidget::item {{ padding: 6px 10px; border-bottom: 1px solid {c['line']}; }}
QTableWidget::item:selected {{ background: {c['select']}; color: {c['text']}; }}
QTableWidget::item:hover {{ background: {c['hover']}; }}
QTableCornerButton::section {{ background: transparent; border: none; }}

QTabWidget::pane {{ border: none; top: 0px; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{ background: transparent; padding: 9px 14px; margin-right: 2px; color: {c['muted']}; border: none; border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: {c['text']}; border-bottom: 2px solid {c['accent']}; }}
QTabBar::tab:hover:!selected {{ color: {c['text']}; }}

QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 12px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {c['line2']}; border-radius: 4px; min-height: 36px; margin: 0 2px; }}
QScrollBar::handle:vertical:hover {{ background: {c['faint']}; }}
QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {c['line2']}; border-radius: 4px; min-width: 36px; margin: 2px 0; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ width: 0; height: 0; background: transparent; }}
QSplitter::handle {{ background: {c['line']}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}
QProgressBar {{ background: {c['panel2']}; border: none; border-radius: 4px; max-height: 8px; min-height: 8px; text-align: center; color: transparent; }}
QProgressBar::chunk {{ background: {c['accent']}; border-radius: 4px; }}
QMenu {{ background: {c['raised']}; border: 1px solid {c['line2']}; border-radius: 10px; padding: 6px; }}
QMenu::item {{ padding: 7px 18px 7px 14px; border-radius: 6px; }}
QMenu::item:selected {{ background: {c['accent_dim']}; }}
QMenu::separator {{ height: 1px; background: {c['line']}; margin: 5px 8px; }}
QTextBrowser {{ background: transparent; border: none; }}
QGroupBox {{ border: 1px solid {c['line']}; border-radius: 12px; margin-top: 16px; padding: 16px 14px 12px 14px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 14px; padding: 0 6px; color: {c['muted']}; font-weight: 600; }}
QMessageBox, QInputDialog {{ background: {c['bg']}; }}
QDialogButtonBox QPushButton:default {{ background: {c['accent']}; color: {c['accent_text']}; border: 1px solid {c['accent']}; font-weight: 600; }}
QDialogButtonBox QPushButton:default:hover {{ background: {c['accent_hi']}; }}
QFrame[siderow="true"] {{ background: transparent; border-radius: 9px; }}
QFrame[siderow="true"]:hover {{ background: {c['hover']}; }}
QFrame[siderow="true"][checked="true"] {{ background: {c['select']}; }}
QFrame[swatch="true"] {{ border-radius: 13px; border: 2px solid transparent; }}
QFrame[swatch="true"][on="true"] {{ border: 2px solid {c['text']}; }}
QLabel[kbd="true"] {{ background: {c['panel2']}; color: {c['muted']}; border: 1px solid {c['line2']}; border-radius: 5px; padding: 1px 6px; font-size: {tiny}px; }}
"""


def mono() -> QFont:
    f = QFont("Cascadia Mono")
    f.setStyleHint(QFont.StyleHint.Monospace)
    f.setPointSize(10)
    return f


def app_icon(size: int = 256) -> QIcon:
    """Draw the app icon (a friendly bot head) so no binary asset is required at runtime."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = size / 128.0
    p.setBrush(QBrush(QColor("#12151c")))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(QRectF(0, 0, size, size), 28 * s, 28 * s)
    p.setBrush(QBrush(QColor("#7c9cff")))
    p.drawRoundedRect(QRectF(26 * s, 34 * s, 76 * s, 62 * s), 18 * s, 18 * s)
    p.drawRoundedRect(QRectF(60 * s, 18 * s, 8 * s, 18 * s), 4 * s, 4 * s)
    p.setBrush(QBrush(QColor("#0f1115")))
    p.drawEllipse(QRectF(42 * s, 55 * s, 16 * s, 16 * s))
    p.drawEllipse(QRectF(70 * s, 55 * s, 16 * s, 16 * s))
    p.drawRoundedRect(QRectF(56 * s, 76 * s, 16 * s, 5 * s), 2.5 * s, 2.5 * s)
    p.setBrush(QBrush(QColor("#e6e8ee")))
    p.drawEllipse(QRectF(57 * s, 9 * s, 14 * s, 14 * s))
    p.end()
    return QIcon(pm)


def status_dot(color: str, size: int = 10) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QBrush(QColor(color)))
    p.setPen(QPen(Qt.PenStyle.NoPen))
    p.drawEllipse(1, 1, size - 2, size - 2)
    p.end()
    return QIcon(pm)
