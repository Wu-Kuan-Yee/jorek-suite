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
    """Read a stage log from disk and follow it while its stage is running."""

    def __init__(self, title: str, path: str, running: bool = False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Build log — " + title)
        self.resize(900, 620)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.path = Path(path)
        self.running = bool(running)
        self.offset = 0
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")

        outer = QVBoxLayout(self)
        header = QHBoxLayout()
        self.status_label = QLabel("Following live output" if self.running else "Completed log")
        header.addWidget(self.status_label)
        header.addStretch(1)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        header.addWidget(refresh)
        outer.addLayout(header)

        self.editor = QPlainTextEdit()
        self.editor.setReadOnly(True)
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.editor.setMaximumBlockCount(100000)
        outer.addWidget(self.editor, 1)

        close = QPushButton("Close")
        close.clicked.connect(self.close)
        outer.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)

        self.timer = QTimer(self)
        self.timer.setInterval(300)
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
            self.status_label.setText("Log complete")
            self.timer.stop()

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
                data = handle.read(262144)
        except OSError as exc:
            self.status_label.setText("Cannot read log: %s" % exc)
            return
        if data:
            self.offset += len(data)
            text = self.decoder.decode(data, final=False)
            if text:
                cursor = self.editor.textCursor()
                cursor.movePosition(cursor.MoveOperation.End)
                self.editor.setTextCursor(cursor)
                self.editor.insertPlainText(text)
                self.editor.ensureCursorVisible()
        if not self.running:
            tail = self.decoder.decode(b"", final=True)
            if tail:
                self.editor.moveCursor(QTextCursor.MoveOperation.End)
                self.editor.insertPlainText(tail)
            self.timer.stop()

    def closeEvent(self, event) -> None:
        self.timer.stop()
        super().closeEvent(event)
