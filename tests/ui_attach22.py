"""Pasting and dropping images in the chat composer (offscreen)."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["OPENGROKBOT_HOME"] = tempfile.mkdtemp(prefix="gbtest-att22-")

from PySide6.QtCore import QMimeData, QUrl  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ui import theme  # noqa: E402
from ui.api import Api, Connection  # noqa: E402
from ui.chat_view import ChatPage  # noqa: E402
from ui.store import Store  # noqa: E402
from ui.widgets import ImageCache  # noqa: E402

app = QApplication(sys.argv)
app.setStyleSheet(theme.qss())
api = Api(Connection("local", "http://127.0.0.1:1", "t"))
page = ChatPage(api, Store(api), ImageCache(api))
fails = []


def check(c, m):
    print(("ok   " if c else "FAIL ") + m)
    if not c:
        fails.append(m)


img = QImage(40, 30, QImage.Format.Format_RGB32)
img.fill(QColor("#7c9cff"))
md = QMimeData()
md.setImageData(img)
page.input.insertFromMimeData(md)
check(len(page.attachments) == 1 and page.attachments[0]["name"].startswith("pasted-"), "pasted image becomes an attachment")
check(page.input.toPlainText() == "", "nothing typed into the box")
p = Path(tempfile.mkdtemp()) / "x.png"
img.save(str(p))
page.add_image_file(str(p))
check(len(page.attachments) == 2, "a dropped png is attached")
t = Path(tempfile.mkdtemp()) / "x.txt"
t.write_text("hi")
page.add_image_file(str(t))
check(len(page.attachments) == 2, "a non-image is refused")
print("ALL OK" if not fails else "FAILED")
sys.stdout.flush()
os._exit(1 if fails else 0)
