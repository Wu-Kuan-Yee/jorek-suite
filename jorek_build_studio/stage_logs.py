"""Live and historical per-stage build log windows."""

from __future__ import annotations

import codecs
from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout,
)


class StageLogDialog(QDialog):
    """Read a complete stage log and follow new output without stealing scroll."""

    def __init__(self, title: str, path: str, running: bool = False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Build log — " + title)
        self.resize(1000, 680)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.path = Path(path)
        self.running = bool(running)
        self.offset = 0
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")

        outer = QVBoxLayout(self)
        header = QHBoxLayout()
        self.status_label = QLabel(
            "Following live output" if self.running else "Loading complete log")
        header.addWidget(self.status_label)
        header.addStretch(1)

        self.top_button = QPushButton("Top")
        self.bottom_button = QPushButton("Bottom")
        self.left_button = QPushButton("Left")
        self.right_button = QPushButton("Right")
        self.refresh_button = QPushButton("Refresh")
        self.top_button.setToolTip("Scroll to the first line")
        self.bottom_button.setToolTip("Scroll to the latest line")
        self.left_button.setToolTip("Scroll to the beginning of long lines")
        self.right_button.setToolTip("Scroll to the end of long lines")
        self.top_button.clicked.connect(self.scroll_to_top)
        self.bottom_button.clicked.connect(self.scroll_to_bottom)
        self.left_button.clicked.connect(self.scroll_to_left)
        self.right_button.clicked.connect(self.scroll_to_right)
        self.refresh_button.clicked.connect(self.refresh)
        for button in (
            self.top_button, self.bottom_button,
            self.left_button, self.right_button, self.refresh_button,
        ):
            header.addWidget(button)
        outer.addLayout(header)

        self.editor = QPlainTextEdit()
        self.editor.setReadOnly(True)
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.editor.setMaximumBlockCount(0)
        self.editor.setPlaceholderText("The complete stage output will appear here.")
        outer.addWidget(self.editor, 1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        footer.addWidget(close)
        outer.addLayout(footer)

        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.refresh)
        if self.running:
            self.timer.start()
        self.refresh()

    def set_running(self, running: bool) -> None:
        self.running = bool(running)
        if self.running:
            self.status_label.setText("Following live output")
            if not self.timer.isActive():
                self.timer.start()
        else:
            self.refresh()

    def _append_text(self, text: str) -> None:
        if not text:
            return

        vertical = self.editor.verticalScrollBar()
        horizontal = self.editor.horizontalScrollBar()
        old_vertical = vertical.value()
        old_horizontal = horizontal.value()
        follow_vertical = old_vertical >= vertical.maximum() - 1
        # A newly opened log has no horizontal range yet; keep it at the
        # natural left edge when long lines first arrive. Follow right only
        # after the reader has actually scrolled to the right edge.
        follow_horizontal = (
            horizontal.maximum() > 0 and
            old_horizontal >= horizontal.maximum() - 1)

        # Insert through an independent document cursor.  Setting the widget's
        # visible cursor to End or calling ensureCursorVisible() would steal the
        # user's scroll position on every live refresh.
        cursor = QTextCursor(self.editor.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)

        vertical.setValue(
            vertical.maximum() if follow_vertical
            else min(old_vertical, vertical.maximum()))
        horizontal.setValue(
            horizontal.maximum() if follow_horizontal
            else min(old_horizontal, horizontal.maximum()))

    def scroll_to_top(self) -> None:
        self.editor.verticalScrollBar().setValue(
            self.editor.verticalScrollBar().minimum())

    def scroll_to_bottom(self) -> None:
        self.editor.verticalScrollBar().setValue(
            self.editor.verticalScrollBar().maximum())

    def scroll_to_left(self) -> None:
        self.editor.horizontalScrollBar().setValue(
            self.editor.horizontalScrollBar().minimum())

    def scroll_to_right(self) -> None:
        self.editor.horizontalScrollBar().setValue(
            self.editor.horizontalScrollBar().maximum())

    def refresh(self) -> None:
        try:
            size = self.path.stat().st_size
        except OSError:
            if not self.editor.toPlainText():
                self.status_label.setText("Waiting for log file: %s" % self.path)
            return

        if size < self.offset:
            self.offset = 0
            self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
            self.editor.clear()

        try:
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                data = handle.read(1024 * 1024)
        except OSError as exc:
            self.status_label.setText("Cannot read log: %s" % exc)
            return

        if data:
            self.offset += len(data)
            self._append_text(self.decoder.decode(data, final=False))

        if not self.running and self.offset < size:
            self.status_label.setText(
                "Loading complete log… %s read" % format(self.offset, ",d"))
            if not self.timer.isActive():
                self.timer.start()
        elif not self.running:
            tail = self.decoder.decode(b"", final=True)
            self._append_text(tail)
            self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
            self.status_label.setText(
                "Log complete — %s bytes" % format(self.offset, ",d"))
            self.timer.stop()
        else:
            self.status_label.setText(
                "Following live output — %s bytes received" % format(self.offset, ",d"))

    def closeEvent(self, event) -> None:
        self.timer.stop()
        super().closeEvent(event)
