from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

try:
    from PySide6.QtWidgets import QApplication, QPlainTextEdit
    from jorek_build_studio.stage_logs import StageLogDialog
    QT_AVAILABLE = True
except ImportError:
    QApplication = None
    QPlainTextEdit = None
    StageLogDialog = None
    QT_AVAILABLE = False


@unittest.skipUnless(QT_AVAILABLE, "PySide6 is not installed")
class StageLogDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def test_live_log_follows_appended_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "live.log"
            path.write_text("first line\n", encoding="utf-8")
            dialog = StageLogDialog("live stage", str(path), running=True)
            self.app.processEvents()
            self.assertIn("first line", dialog.editor.toPlainText())

            with path.open("a", encoding="utf-8") as handle:
                handle.write("second line\n")
                handle.flush()
            dialog.refresh()
            self.assertIn("second line", dialog.editor.toPlainText())
            dialog.set_running(False)
            self.assertFalse(dialog.timer.isActive())
            dialog.close()

    def test_live_refresh_preserves_user_vertical_and_horizontal_scroll(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "scroll.log"
            line = "X" * 500 + "\n"
            path.write_text(line * 120, encoding="utf-8")
            dialog = StageLogDialog("scroll stage", str(path), running=True)
            dialog.resize(620, 360)
            dialog.show()
            self.app.processEvents()

            vertical = dialog.editor.verticalScrollBar()
            horizontal = dialog.editor.horizontalScrollBar()
            self.assertGreater(vertical.maximum(), 10)
            self.assertGreater(horizontal.maximum(), 10)
            vertical.setValue(4)
            horizontal.setValue(0)
            old_vertical = vertical.value()
            old_horizontal = horizontal.value()

            with path.open("a", encoding="utf-8") as handle:
                handle.write(("Y" * 700) + "\n")
                handle.flush()
            dialog.refresh()
            self.app.processEvents()

            self.assertEqual(vertical.value(), old_vertical)
            self.assertEqual(horizontal.value(), old_horizontal)
            dialog.close()

    def test_live_refresh_follows_tail_only_when_already_at_tail(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "follow.log"
            path.write_text(("line " + "A" * 300 + "\n") * 100, encoding="utf-8")
            dialog = StageLogDialog("follow stage", str(path), running=True)
            dialog.resize(620, 360)
            dialog.show()
            self.app.processEvents()
            dialog.scroll_to_bottom()
            dialog.scroll_to_right()

            with path.open("a", encoding="utf-8") as handle:
                handle.write(("new " + "B" * 600 + "\n") * 3)
                handle.flush()
            dialog.refresh()
            self.app.processEvents()

            self.assertEqual(
                dialog.editor.verticalScrollBar().value(),
                dialog.editor.verticalScrollBar().maximum())
            self.assertEqual(
                dialog.editor.horizontalScrollBar().value(),
                dialog.editor.horizontalScrollBar().maximum())
            dialog.close()

    def test_directional_navigation_buttons_reach_all_four_edges(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "navigation.log"
            path.write_text(("Z" * 600 + "\n") * 100, encoding="utf-8")
            dialog = StageLogDialog("navigation stage", str(path), running=False)
            dialog.resize(620, 360)
            dialog.show()
            self.app.processEvents()
            self.assertEqual(
                dialog.editor.horizontalScrollBar().value(),
                dialog.editor.horizontalScrollBar().minimum())

            dialog.scroll_to_bottom()
            dialog.scroll_to_right()
            self.assertEqual(
                dialog.editor.verticalScrollBar().value(),
                dialog.editor.verticalScrollBar().maximum())
            self.assertEqual(
                dialog.editor.horizontalScrollBar().value(),
                dialog.editor.horizontalScrollBar().maximum())

            dialog.scroll_to_top()
            dialog.scroll_to_left()
            self.assertEqual(
                dialog.editor.verticalScrollBar().value(),
                dialog.editor.verticalScrollBar().minimum())
            self.assertEqual(
                dialog.editor.horizontalScrollBar().value(),
                dialog.editor.horizontalScrollBar().minimum())
            dialog.close()

    def test_line_numbers_are_visible_by_default_and_can_be_hidden(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "numbers.log"
            path.write_text("one\ntwo\nthree\n", encoding="utf-8")
            dialog = StageLogDialog("number stage", str(path), running=False)
            self.assertTrue(dialog.line_numbers.isChecked())
            self.assertTrue(dialog.editor.line_numbers_visible)
            dialog.line_numbers.setChecked(False)
            self.assertFalse(dialog.editor.line_numbers_visible)
            dialog.line_numbers.setChecked(True)
            self.assertTrue(dialog.editor.line_numbers_visible)
            self.assertTrue(dialog.follow_check.isChecked())
            dialog.follow_check.setChecked(False)
            self.assertFalse(dialog.follow_check.isChecked())
            dialog.wrap_check.setChecked(True)
            self.assertEqual(
                dialog.editor.lineWrapMode(),
                QPlainTextEdit.LineWrapMode.WidgetWidth)
            dialog.wrap_check.setChecked(False)
            self.assertEqual(
                dialog.editor.lineWrapMode(),
                QPlainTextEdit.LineWrapMode.NoWrap)
            dialog.close()

    def test_log_statistics_report_bytes_chars_lines_words_and_max_line(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "stats.log"
            payload = "one two\nthree\n"
            path.write_bytes(payload.encode("utf-8"))
            dialog = StageLogDialog("stats stage", str(path), running=False)
            stats = dialog.stats_label.text()
            self.assertIn("Bytes: %s" % len(payload.encode("utf-8")), stats)
            self.assertIn("Chars: %s" % len(payload), stats)
            self.assertIn("Lines: 2", stats)
            self.assertIn("Words: 3", stats)
            self.assertIn("Max line length: 7", stats)
            dialog.close()

    def test_regex_search_supports_alternation_and_navigation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "search.log"
            path.write_text(
                "INFO ready\nWARNING first\nERROR second\nWARNING third\n",
                encoding="utf-8")
            dialog = StageLogDialog("search stage", str(path), running=False)
            dialog.search_edit.setText(r"WARNING|ERROR")
            self.app.processEvents()
            self.assertEqual(len(dialog.matches), 3)
            self.assertEqual(dialog.match_label.text(), "1 / 3")
            dialog.search_next()
            self.assertEqual(dialog.match_label.text(), "2 / 3")
            dialog.search_next()
            self.assertEqual(dialog.match_label.text(), "3 / 3")
            dialog.search_next()
            self.assertEqual(dialog.match_label.text(), "1 / 3")
            dialog.search_previous()
            self.assertEqual(dialog.match_label.text(), "3 / 3")
            dialog.close()

    def test_regex_search_reports_invalid_expression(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "invalid-regex.log"
            path.write_text("some output\n", encoding="utf-8")
            dialog = StageLogDialog("regex stage", str(path), running=False)
            dialog.search_edit.setText("(")
            self.app.processEvents()
            self.assertIn("Invalid regex", dialog.match_label.text())
            dialog.close()

    def test_completed_log_reads_beyond_one_poll_chunk(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "large.log"
            payload = "A" * 300000 + "\nEND-OF-LOG\n"
            path.write_text(payload, encoding="utf-8")
            dialog = StageLogDialog("completed stage", str(path), running=False)
            while dialog.offset < path.stat().st_size:
                dialog.refresh()
            displayed = dialog.editor.toPlainText()
            self.assertIn("END-OF-LOG", displayed)
            self.assertEqual(len(displayed), len(payload))
            dialog.close()


if __name__ == "__main__":
    unittest.main()
