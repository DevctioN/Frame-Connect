"""Exercise Qt widgets offscreen without interacting with the user's desktop."""

import os
from pathlib import Path
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QFontDatabase, QImage, QPainter
from PySide6.QtWidgets import QApplication


APPLICATION = QApplication.instance() or QApplication([])
APPLICATION.setQuitOnLastWindowClosed(False)
# The Windows offscreen plugin has no system font database. The actual Windows
# platform loads these fonts normally; load them explicitly for accurate QA.
if os.name == "nt":
    for filename in ("segoeui.ttf", "segoeuib.ttf"):
        font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / filename
        if font.is_file():
            QFontDatabase.addApplicationFont(str(font))


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        APPLICATION.processEvents()
        time.sleep(.01)
    APPLICATION.processEvents()
    return predicate()


def dispose(widget):
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    APPLICATION.processEvents()


def render(widget):
    APPLICATION.processEvents()
    image = QImage(widget.size(), QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    widget.render(painter, widget.rect().topLeft())
    painter.end()
    return image
