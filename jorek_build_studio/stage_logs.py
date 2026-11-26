"""Feature-rich live and historical per-stage build log viewer."""

from __future__ import annotations

import codecs
import re
from pathlib import Path

from PySide6.QtCore import QRegularExpression, QSize, QTimer, Qt
from PySide6.QtGui import (
    QColor, QFontDatabase, QKeySequence, QPainter, QShortcut,
    QTextCharFormat, QTextCursor, QSyntaxHighlighter,
)
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QTextEdit, QVBoxLayout, QWidget,
)


class LogHighlighter(QSyntaxHighlighter):
    """Colour important build diagnostics while de-emphasising expected probes."""

    def __init__(self, document):
        super().__init__(document)
        self.rules = [
            (re.compile(r"(?i)(?:ignored|expected|non-fatal).*(?:error|failure)|(?:error|failure).*(?:ignored|expected|non-fatal)"),
             "#aab2c0", None, False),
            (re.compile(r"(?i)\b(?:fatal error|cmake error|traceback|assertionerror|undefined reference|error:|error occurred)\b"),
             "#ff6b6b", "#3b171b", True),
            (re.compile(r"(?i)\bwarning\s*:"),
             "#ffca68", "#352b16", True),
            (re.compile(r"(?i)performing test .+ - failed"),
             "#aab2c0", None, False),
            (re.compile(r"(?i)\b(?:ignored|skipped|disabled|not found|not enabled)\b"),
             "#9aa4b2", None, False),
            (re.compile(r"(?i)\b(?:success|passed|completed|installed)\b"),
             "#83d6a5", None, False),
        ]

    def highlightBlock(self, text):
        for pattern, foreground, background, bold in self.rules:
            if pattern.search(text):
                fmt = QTextCharFormat()
                fmt.setForeground(QColor(foreground))
                if background:
                    fmt.setBackground(QColor(background))
                if bold:
                    fmt.setFontWeight(700)
                self.setFormat(0, len(text), fmt)
                return


class LineNumberArea(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self):
        return QSize(self.editor.line_number_area_width(), 0)

    def paintEvent(self, event):
        self.editor.line_number_area_paint_event(event)


class LogTextEdit(QPlainTextEdit):
    """Read-only plain-text editor with a toggleable line-number gutter."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.line_number_area = LineNumberArea(self)
        self.line_numbers_visible = True
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setMaximumBlockCount(0)
        self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.blockCountChanged.connect(self.update_line_number_area_width)
        self.updateRequest.connect(self.update_line_number_area)
        self.update_line_number_area_width(0)

    def line_number_area_width(self):
        if not self.line_numbers_visible:
            return 0
        digits = max(3, len(str(max(1, self.blockCount()))))
        return 10 + self.fontMetrics().horizontalAdvance("9") * digits

    def update_line_number_area_width(self, _):
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def update_line_number_area(self, rect, dy):
        if dy:
            self.line_number_area.scroll(0, dy)
        else:
            self.line_number_area.update(
                0, rect.y(), self.line_number_area.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self.update_line_number_area_width(0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        self.line_number_area.setGeometry(
            cr.left(), cr.top(), self.line_number_area_width(), cr.height())

    def set_line_numbers_visible(self, visible):
        self.line_numbers_visible = bool(visible)
        self.line_number_area.setVisible(self.line_numbers_visible)
        self.update_line_number_area_width(0)

    def line_number_area_paint_event(self, event):
        if not self.line_numbers_visible:
            return
        painter = QPainter(self.line_number_area)
        painter.fillRect(event.rect(), QColor("#202630"))
        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        top = int(self.blockBoundingGeometry(block).translated(
            self.contentOffset()).top())
        bottom = top + int(self.blockBoundingRect(block).height())
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.setPen(QColor("#aab4c2"))
                painter.drawText(
                    0, top, self.line_number_area.width() - 5,
                    self.fontMetrics().height(),
                    Qt.AlignmentFlag.AlignRight,
                    str(block_number + 1))
            block = block.next()
            top = bottom
            bottom = top + int(self.blockBoundingRect(block).height())
            block_number += 1


class StageLogDialog(QDialog):
    """Read complete logs with live following, search, syntax colours and stats."""

    def __init__(self, title: str, path: str, running: bool = False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Build log — " + title)
        self.resize(1180, 760)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.path = Path(path)
        self.running = bool(running)
        self.offset = 0
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._stats_chars = 0
        self._stats_lines = 0
        self._stats_words = 0
        self._stats_word_tail = ""
        self._stats_line_tail = ""
        self._stats_max_line_length = 0
        self._stats_finalized = False
        self.matches = []
        self.match_index = -1
        self._search_stale = False

        outer = QVBoxLayout(self)
        header = QHBoxLayout()
        self.status_label = QLabel(
            "Following live output" if self.running else "Loading complete log")
        header.addWidget(self.status_label)
        header.addStretch(1)

        self.line_numbers = QCheckBox("Line numbers")
        self.line_numbers.setChecked(True)
        self.line_numbers.toggled.connect(self.editor_line_numbers_changed)
        header.addWidget(self.line_numbers)
        self.wrap_check = QCheckBox("Wrap lines")
        self.wrap_check.toggled.connect(self.editor_wrap_changed)
        header.addWidget(self.wrap_check)
        self.follow_check = QCheckBox("Auto-follow")
        self.follow_check.setChecked(True)
        self.follow_check.setToolTip(
            "Automatically follow new output only while already at the end")
        header.addWidget(self.follow_check)

        self.top_button = QPushButton("Top")
        self.bottom_button = QPushButton("Bottom")
        self.left_button = QPushButton("Left")
        self.right_button = QPushButton("Right")
        self.refresh_button = QPushButton("Refresh")
        self.copy_button = QPushButton("Copy all")
        self.save_button = QPushButton("Save as…")
        self.top_button.clicked.connect(self.scroll_to_top)
        self.bottom_button.clicked.connect(self.scroll_to_bottom)
        self.left_button.clicked.connect(self.scroll_to_left)
        self.right_button.clicked.connect(self.scroll_to_right)
        self.refresh_button.clicked.connect(self.refresh)
        self.copy_button.clicked.connect(self.copy_all)
        self.save_button.clicked.connect(self.save_as)
        for button in (
            self.top_button, self.bottom_button, self.left_button,
            self.right_button, self.refresh_button, self.copy_button,
            self.save_button,
        ):
            header.addWidget(button)
        outer.addLayout(header)

        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Find"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search text or regular expression…")
        self.search_edit.returnPressed.connect(self.search_next)
        search_row.addWidget(self.search_edit, 1)
        self.regex_check = QCheckBox("Regex")
        self.regex_check.setChecked(False)
        self.regex_check.toggled.connect(self.update_search)
        search_row.addWidget(self.regex_check)
        self.case_check = QCheckBox("Case sensitive")
        self.case_check.toggled.connect(self.update_search)
        search_row.addWidget(self.case_check)
        self.previous_button = QPushButton("Previous")
        self.next_button = QPushButton("Next")
        self.previous_button.clicked.connect(self.search_previous)
        self.next_button.clicked.connect(self.search_next)
        search_row.addWidget(self.previous_button)
        search_row.addWidget(self.next_button)
        self.match_label = QLabel("0 matches")
        search_row.addWidget(self.match_label)
        outer.addLayout(search_row)
        self.search_edit.textChanged.connect(self.update_search)

        self.find_shortcut = QShortcut(QKeySequence.StandardKey.Find, self)
        self.find_shortcut.activated.connect(self.search_edit.setFocus)
        self.next_shortcut = QShortcut(QKeySequence("F3"), self)
        self.next_shortcut.activated.connect(self.search_next)
        self.previous_shortcut = QShortcut(QKeySequence("Shift+F3"), self)
        self.previous_shortcut.activated.connect(self.search_previous)

        self.editor = LogTextEdit()
        self.editor.setPlaceholderText("The complete stage output will appear here.")
        self.highlighter = LogHighlighter(self.editor.document())
        outer.addWidget(self.editor, 1)

        self.stats_label = QLabel(
            "Bytes: 0    Chars: 0    Lines: 0    Words: 0    Max line length: 0")
        self.stats_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        outer.addWidget(self.stats_label)

        footer = QHBoxLayout()
        footer.addWidget(QLabel(
            "Red: errors  •  Amber: warnings  •  Grey: ignored/expected checks  •  "
            "Green: success indicators"))
        footer.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        footer.addWidget(close)
        outer.addLayout(footer)

        self.stats_timer = QTimer(self)
        self.stats_timer.setInterval(1000)
        self.stats_timer.timeout.connect(self.update_statistics)
        self.stats_timer.start()

        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.refresh)
        if self.running:
            self.timer.start()
        self.refresh()
        self.update_statistics()

    def editor_line_numbers_changed(self, visible):
        self.editor.set_line_numbers_visible(visible)

    def editor_wrap_changed(self, enabled):
        self.editor.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.WidgetWidth if enabled
            else QPlainTextEdit.LineWrapMode.NoWrap)

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
        follow_vertical = (
            self.follow_check.isChecked() and
            old_vertical >= vertical.maximum() - 1)
        follow_horizontal = (
            self.follow_check.isChecked() and horizontal.maximum() > 0 and
            old_horizontal >= horizontal.maximum() - 1)
        cursor = QTextCursor(self.editor.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        vertical.setValue(
            vertical.maximum() if follow_vertical
            else min(old_vertical, vertical.maximum()))
        horizontal.setValue(
            horizontal.maximum() if follow_horizontal
            else min(old_horizontal, horizontal.maximum()))
        self._accumulate_statistics(text)
        self.update_statistics()
        if self.search_edit.text():
            self._search_stale = True
            self.match_label.setText(
                "%s matches (new output; press Enter to refresh)" %
                format(len(self.matches), ",d"))

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

    def _accumulate_statistics(self, text):
        if not text:
            return
        self._stats_finalized = False
        self._stats_chars += len(text)
        self._stats_lines += text.count("\n")

        line_text = self._stats_line_tail + text
        line_parts = line_text.split("\n")
        for complete_line in line_parts[:-1]:
            self._stats_max_line_length = max(
                self._stats_max_line_length, len(complete_line.rstrip("\r")))
        self._stats_line_tail = line_parts[-1]

        word_text = self._stats_word_tail + text
        tokens = re.findall(r"\S+", word_text)
        if word_text and not word_text[-1].isspace():
            self._stats_words += max(0, len(tokens) - 1)
            self._stats_word_tail = tokens[-1]
        else:
            self._stats_words += len(tokens)
            self._stats_word_tail = ""

    def _finalize_statistics(self):
        if self._stats_finalized:
            return
        if self._stats_word_tail:
            self._stats_words += 1
            self._stats_word_tail = ""
        self._stats_max_line_length = max(
            self._stats_max_line_length, len(self._stats_line_tail.rstrip("\r")))
        self._stats_finalized = True

    def update_statistics(self):
        line_count = self._stats_lines + (1 if self._stats_line_tail else 0)
        self.stats_label.setText(
            "Bytes: %s    Chars: %s    Lines: %s    Words: %s    "
            "Max line length: %s" % (
                format(self.offset, ",d"), format(self._stats_chars, ",d"),
                format(line_count, ",d"), format(self._stats_words + bool(self._stats_word_tail), ",d"),
                format(self._stats_max_line_length, ",d")))

    def update_search(self):
        query = self.search_edit.text()
        old_position = self.editor.textCursor().selectionStart()
        self._search_stale = False
        self.matches = []
        self.match_index = -1
        self.editor.setExtraSelections([])
        if not query:
            self.match_label.setText("0 matches")
            return
        if self.regex_check.isChecked():
            pattern = query
        else:
            pattern = QRegularExpression.escape(query)
        options = QRegularExpression.PatternOption.NoPatternOption
        if not self.case_check.isChecked():
            options |= QRegularExpression.PatternOption.CaseInsensitiveOption
        regex = QRegularExpression(pattern, options)
        if not regex.isValid():
            self.match_label.setText(
                "Invalid regex at %s: %s" % (
                    regex.patternErrorOffset(), regex.errorString()))
            return
        cursor = QTextCursor(self.editor.document())
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        selections = []
        fmt = QTextCharFormat()
        fmt.setBackground(QColor("#665b22"))
        fmt.setForeground(QColor("#fff3b0"))
        while True:
            found = self.editor.document().find(regex, cursor)
            if found.isNull():
                break
            self.matches.append((found.selectionStart(), found.selectionEnd()))
            selection = QTextEdit.ExtraSelection()
            selection.cursor = found
            selection.format = fmt
            selections.append(selection)
            next_position = found.selectionEnd()
            if next_position <= found.selectionStart():
                next_position = found.selectionStart() + 1
            cursor = QTextCursor(self.editor.document())
            cursor.setPosition(next_position)
        self.editor.setExtraSelections(selections)
        self.match_label.setText("%s matches" % format(len(self.matches), ",d"))
        if self.matches:
            self.match_index = next(
                (i for i, (start, _end) in enumerate(self.matches)
                 if start >= old_position), 0)
            self._select_match(self.match_index)

    def _select_match(self, index):
        if not self.matches:
            return
        start, end = self.matches[index]
        cursor = self.editor.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        self.editor.setTextCursor(cursor)
        self.editor.centerCursor()
        self.match_label.setText(
            "%s / %s" % (format(index + 1, ",d"), format(len(self.matches), ",d")))

    def search_next(self):
        if self._search_stale or not self.matches:
            self.update_search()
        if not self.matches:
            return
        position = self.editor.textCursor().selectionEnd()
        next_index = next(
            (i for i, (start, _end) in enumerate(self.matches) if start >= position),
            0)
        self.match_index = next_index
        self._select_match(next_index)

    def search_previous(self):
        if not self.matches:
            self.update_search()
        if not self.matches:
            return
        position = self.editor.textCursor().selectionStart()
        previous = [
            i for i, (_start, end) in enumerate(self.matches) if end <= position
        ]
        self.match_index = previous[-1] if previous else len(self.matches) - 1
        self._select_match(self.match_index)

    def copy_all(self):
        cursor = self.editor.textCursor()
        vertical = self.editor.verticalScrollBar().value()
        horizontal = self.editor.horizontalScrollBar().value()
        self.editor.selectAll()
        self.editor.copy()
        self.editor.setTextCursor(cursor)
        self.editor.verticalScrollBar().setValue(vertical)
        self.editor.horizontalScrollBar().setValue(horizontal)

    def save_as(self):
        filename, _ = QFileDialog.getSaveFileName(
            self, "Save build log", self.path.name, "Log files (*.log *.txt);;All files (*)")
        if not filename:
            return
        try:
            with Path(filename).open("w", encoding="utf-8", newline="") as handle:
                handle.write(self.editor.toPlainText())
        except OSError as exc:
            QMessageBox.warning(self, "Save build log", str(exc))

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
            self._stats_chars = 0
            self._stats_lines = 0
            self._stats_words = 0
            self._stats_word_tail = ""
            self._stats_line_tail = ""
            self._stats_max_line_length = 0
            self._stats_finalized = False
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
            self._finalize_statistics()
            self.status_label.setText(
                "Log complete — %s bytes" % format(self.offset, ",d"))
            self.timer.stop()
        else:
            self.status_label.setText(
                "Following live output — %s bytes received" % format(self.offset, ",d"))
        self.update_statistics()

    def closeEvent(self, event) -> None:
        self.timer.stop()
        self.stats_timer.stop()
        super().closeEvent(event)
