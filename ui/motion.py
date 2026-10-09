"""Small, cheap animations used across the UI. Everything here is a no-op when motion is off (headless tests, or Settings > App
"Reduce motion" via the ui config key `reduce_motion`), so the app behaves identically, just without the easing.

Rules of thumb: durations 120-260 ms, ease-out for things that arrive, never block input, and never leave a graphics effect on a
widget once its animation is over (a lingering opacity effect makes text blurry and repaints slow)."""
from __future__ import annotations

import os
import re
from typing import Callable

from PySide6.QtCore import QAbstractAnimation, QEasingCurve, QEvent, QObject, QPoint, QPropertyAnimation, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QDialog, QGraphicsDropShadowEffect, QGraphicsOpacityEffect, QProgressBar, QPushButton, QWidget

OUT = QEasingCurve.Type.OutCubic
INOUT = QEasingCurve.Type.InOutCubic

_forced: bool | None = None
_live: set = set()          # animations in flight (a Python reference keeps them alive)


def set_enabled(on: bool | None) -> None:
    """Force motion on/off (None = automatic)."""
    global _forced
    _forced = on


def enabled() -> bool:
    if _forced is not None:
        return _forced
    if os.environ.get("OPENGROKBOT_MOTION") == "1":
        return True
    if os.environ.get("QT_QPA_PLATFORM", "").lower() in ("offscreen", "minimal"):
        return False
    try:
        from .api import load_ui_config
        return not load_ui_config().get("reduce_motion", False)
    except Exception:  # noqa: BLE001
        return True


def _keep(a: QAbstractAnimation) -> None:
    _live.add(a)
    a.finished.connect(lambda: _live.discard(a))


def _alive(w: QWidget) -> bool:
    try:
        return w is not None and w.isVisible()
    except RuntimeError:       # the C++ widget is already gone
        return False


# ------------------------------------------------------------------------------------------------------------ fades
def fade_in(w: QWidget, ms: int = 220, slide: int = 0, delay: int = 0, on_done: Callable | None = None) -> None:
    """Fade a widget in (and optionally let it rise `slide` px). The graphics effect is removed again when it is done."""
    if not enabled() or w is None:
        if on_done:
            on_done()
        return
    eff = QGraphicsOpacityEffect(w)
    eff.setOpacity(0.0)
    w.setGraphicsEffect(eff)
    a = QPropertyAnimation(eff, b"opacity", w)
    a.setDuration(ms)
    a.setStartValue(0.0)
    a.setEndValue(1.0)
    a.setEasingCurve(QEasingCurve(OUT))

    def done() -> None:
        try:
            if w.graphicsEffect() is eff:
                w.setGraphicsEffect(None)
        except RuntimeError:
            pass
        if on_done:
            on_done()
    a.finished.connect(done)
    _keep(a)
    group: list[QAbstractAnimation] = [a]
    if slide:
        end = w.pos()
        w.move(end.x(), end.y() + slide)
        p = QPropertyAnimation(w, b"pos", w)
        p.setDuration(ms)
        p.setStartValue(QPoint(end.x(), end.y() + slide))
        p.setEndValue(end)
        p.setEasingCurve(QEasingCurve(OUT))
        _keep(p)
        group.append(p)

    def go() -> None:
        if w is None:
            return
        for x in group:
            x.start()
    if delay:
        QTimer.singleShot(delay, go)
    else:
        go()


def stagger(widgets: list[QWidget], ms: int = 240, step: int = 45, slide: int = 0) -> None:
    """Reveal widgets one after the other (capped, so a long list does not take seconds)."""
    if not enabled():
        return
    for i, w in enumerate(widgets[:12]):
        fade_in(w, ms, slide=slide, delay=i * step)


def page_in(page: QWidget) -> None:
    """The main area switches to another page: it slides in a little from the right while fading, then its cards cascade in."""
    if not enabled():
        return
    end = page.pos()
    eff = QGraphicsOpacityEffect(page)
    eff.setOpacity(0.0)
    page.setGraphicsEffect(eff)
    fade = QPropertyAnimation(eff, b"opacity", page)
    fade.setDuration(300)
    fade.setStartValue(0.0)
    fade.setEndValue(1.0)
    fade.setEasingCurve(QEasingCurve(OUT))
    page.move(end.x() + 36, end.y())
    slide = QPropertyAnimation(page, b"pos", page)
    slide.setDuration(340)
    slide.setStartValue(QPoint(end.x() + 36, end.y()))
    slide.setEndValue(end)
    slide.setEasingCurve(QEasingCurve(OUT))

    def done() -> None:
        try:
            if page.graphicsEffect() is eff:
                page.setGraphicsEffect(None)
            page.move(end)
        except RuntimeError:
            pass
    fade.finished.connect(done)
    slide.finished.connect(lambda: page.move(end))
    _keep(fade)
    _keep(slide)
    fade.start()
    slide.start()
    QTimer.singleShot(120, lambda: _cascade(page))


def _cascade(page: QWidget) -> None:
    """The visible cards and tiles of a page rise in one after another (top to bottom)."""
    try:
        cards = [w for w in page.findChildren(QWidget) if w.property("card") and w.isVisible() and w.width() > 80 and w.graphicsEffect() is None]
    except RuntimeError:
        return
    cards.sort(key=lambda w: (w.mapTo(page, QPoint(0, 0)).y(), w.mapTo(page, QPoint(0, 0)).x()))
    for i, w in enumerate(cards[:10]):
        fade_in(w, 320, slide=18, delay=i * 55)


def glide(w: QWidget, end_pos: QPoint, ms: int = 300) -> None:
    """Move a floating widget (the sidebar's active indicator) to `end_pos` with an ease-out; jumps when motion is off."""
    prev = getattr(w, "_glide", None)
    if prev is not None:
        prev.stop()
    if not enabled() or not w.isVisible():
        w.move(end_pos)
        return
    a = QPropertyAnimation(w, b"pos", w)
    a.setDuration(ms)
    a.setStartValue(w.pos())
    a.setEndValue(end_pos)
    curve = QEasingCurve(QEasingCurve.Type.OutBack)
    curve.setOvershoot(0.8)
    a.setEasingCurve(curve)
    w._glide = a
    _keep(a)
    a.start()


# ------------------------------------------------------------------------------------------------------------ numbers
_NUM = re.compile(r"^(?P<pre>[^\d-]*)(?P<num>-?[\d,]*\.?\d+)(?P<suf>.*)$")


def count_to(label, text: str, ms: int = 650) -> None:
    """Set a QLabel to `text`; when it starts with a number (`34.2k`, `$4.30`, `92%`), count up to it instead of jumping."""
    m = _NUM.match(text or "")
    old = getattr(label, "_motion_text", None)
    label._motion_text = text
    if not enabled() or not m or old == text or not _alive(label):
        label.setText(text)
        return
    num = m.group("num")
    try:
        target = float(num.replace(",", ""))
    except ValueError:
        label.setText(text)
        return
    decimals = len(num.split(".")[1]) if "." in num else 0
    comma = "," in num
    pre, suf = m.group("pre"), m.group("suf")
    prev = getattr(label, "_motion_anim", None)
    if prev is not None:
        prev.stop()
    a = QVariantAnimation(label)
    a.setDuration(ms)
    a.setStartValue(0.0)
    a.setEndValue(target)
    a.setEasingCurve(QEasingCurve(OUT))

    def fmt(v: float) -> str:
        s = f"{v:,.{decimals}f}" if comma else f"{v:.{decimals}f}"
        return pre + s + suf
    a.valueChanged.connect(lambda v: label.setText(fmt(float(v))))
    a.finished.connect(lambda: label.setText(text))
    label._motion_anim = a
    _keep(a)
    label.setText(fmt(0.0))
    a.start()


def animate_value(bar: QProgressBar, value: int, ms: int = 380) -> None:
    """Move a progress bar smoothly to `value`."""
    if not enabled() or not _alive(bar) or bar.maximum() == bar.minimum() == 0:
        bar.setValue(value)
        return
    prev = getattr(bar, "_motion_anim", None)
    if prev is not None:
        prev.stop()
    a = QPropertyAnimation(bar, b"value", bar)
    a.setDuration(ms)
    a.setStartValue(bar.value())
    a.setEndValue(value)
    a.setEasingCurve(QEasingCurve(OUT))
    bar._motion_anim = a
    _keep(a)
    a.start()


# ------------------------------------------------------------------------------------------------------------ hover
class _Glow(QObject):
    """A soft accent glow that eases in under a primary button while the pointer is on it, and presses in slightly on click."""

    def __init__(self, b: QPushButton, color: str):
        super().__init__(b)
        self.b = b
        self.fx = QGraphicsDropShadowEffect(b)
        c = QColor(color)
        c.setAlpha(0)
        self.fx.setColor(c)
        self.fx.setBlurRadius(0)
        self.fx.setOffset(0, 3)
        self.color = QColor(color)
        b.setGraphicsEffect(self.fx)
        self.anim = QVariantAnimation(self)
        self.anim.setDuration(160)
        self.anim.setEasingCurve(QEasingCurve(OUT))
        self.anim.valueChanged.connect(self._set)
        b.installEventFilter(self)

    def _set(self, t) -> None:
        t = float(t)
        c = QColor(self.color)
        c.setAlpha(int(110 * t))
        try:
            self.fx.setColor(c)
            self.fx.setBlurRadius(26 * t)
        except RuntimeError:
            pass

    def _to(self, end: float) -> None:
        if not enabled():
            return
        self.anim.stop()
        self.anim.setStartValue(self.anim.currentValue() if self.anim.currentValue() is not None else 0.0)
        self.anim.setEndValue(end)
        self.anim.start()

    def eventFilter(self, o, e) -> bool:
        t = e.type()
        if t == QEvent.Type.Enter and self.b.isEnabled():
            self._to(1.0)
        elif t in (QEvent.Type.Leave, QEvent.Type.Hide, QEvent.Type.EnabledChange):
            self._to(0.0)
        elif t == QEvent.Type.MouseButtonPress:
            self._to(0.45)
        elif t == QEvent.Type.MouseButtonRelease:
            self._to(1.0 if self.b.underMouse() else 0.0)
        return False


def glow(button: QPushButton, color: str) -> None:
    """Give a (primary) button the animated hover glow. Skipped when motion is off."""
    if enabled():
        _Glow(button, color)


# ------------------------------------------------------------------------------------------------------------ toasts and dialogs
def toast_in(lb: QWidget) -> None:
    """A toast slides up a little as it fades in."""
    fade_in(lb, 200, slide=10)


def toast_out(lb: QWidget, then: Callable) -> None:
    if not enabled() or not _alive(lb):
        then()
        return
    eff = QGraphicsOpacityEffect(lb)
    lb.setGraphicsEffect(eff)
    a = QPropertyAnimation(eff, b"opacity", lb)
    a.setDuration(180)
    a.setStartValue(1.0)
    a.setEndValue(0.0)
    a.finished.connect(then)
    _keep(a)
    a.start()


class _DialogFade(QObject):
    """App-wide: every dialog eases in (window opacity) the first time it is shown."""

    def eventFilter(self, o, e) -> bool:
        if e.type() == QEvent.Type.Show and isinstance(o, QDialog) and not o.property("_faded") and enabled():
            o.setProperty("_faded", True)
            o.setWindowOpacity(0.0)
            a = QPropertyAnimation(o, b"windowOpacity", o)
            a.setDuration(160)
            a.setStartValue(0.0)
            a.setEndValue(1.0)
            a.setEasingCurve(QEasingCurve(OUT))
            _keep(a)
            a.start()
        return False


_installed: _DialogFade | None = None


def install(app: QApplication) -> None:
    """Call once after the QApplication exists."""
    global _installed
    if _installed is None:
        _installed = _DialogFade(app)
        app.installEventFilter(_installed)
