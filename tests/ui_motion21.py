"""The motion helpers: they animate when enabled, always land on the final state, and never leave a graphics effect behind.

    python tests/ui_motion21.py
"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["OPENGROKBOT_MOTION"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication, QDialog, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget  # noqa: E402

from ui import motion  # noqa: E402

app = QApplication([])
bad = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        bad.append(msg)


def pump(sec):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


check(motion.enabled(), "motion is on when asked for")

host = QWidget()
lay = QVBoxLayout(host)
w = QLabel("hello")
lay.addWidget(w)
host.show()
pump(0.1)
motion.fade_in(w, 150)
pump(0.05)
check(w.graphicsEffect() is not None, "a fade is running")
pump(0.4)
check(w.graphicsEffect() is None, "the opacity effect is removed when the fade ends")

lb = QLabel("0")
lay.addWidget(lb)
lb.show()
motion.count_to(lb, "34.2k", 200)
pump(0.1)
mid = lb.text()
pump(0.4)
check(lb.text() == "34.2k" and mid != "34.2k", f"a number counts up and lands exactly ({mid!r} -> {lb.text()!r})")
motion.count_to(lb, "$4.30", 120)
pump(0.3)
check(lb.text() == "$4.30", "currency lands exactly")
motion.count_to(lb, "1,250", 120)
pump(0.3)
check(lb.text() == "1,250", "thousands separators survive")
motion.count_to(lb, "all clear", 120)
check(lb.text() == "all clear", "plain text is set at once")
motion.count_to(lb, "all clear", 120)
check(lb.text() == "all clear", "setting the same text again is harmless")

bar = QProgressBar()
bar.setRange(0, 100)
lay.addWidget(bar)
motion.animate_value(bar, 80, 150)
pump(0.4)
check(bar.value() == 80, "a progress bar lands on its value")

b = QPushButton("Go")
b.setProperty("primary", True)
lay.addWidget(b)
motion.glow(b, "#7c9cff")
pump(0.1)
check(b.graphicsEffect() is not None, "primary buttons carry the glow effect")

d = QDialog()
motion.install(app)
d.show()
pump(0.4)
check(d.windowOpacity() > 0.95, f"a dialog fades in to full opacity ({d.windowOpacity():.2f})")
d.close()

motion.set_enabled(False)
lb2 = QLabel("x")
lay.addWidget(lb2)
motion.count_to(lb2, "99%")
check(lb2.text() == "99%", "with motion off a number is set at once")
motion.fade_in(lb2)
check(lb2.graphicsEffect() is None, "with motion off no effect is added")
motion.set_enabled(None)

os.environ.pop("OPENGROKBOT_MOTION")
check(not motion.enabled(), "motion is off headless by default (so tests stay deterministic)")
print("FAIL: " + "; ".join(bad) if bad else "ALL OK")
sys.stdout.flush()
os._exit(1 if bad else 0)
