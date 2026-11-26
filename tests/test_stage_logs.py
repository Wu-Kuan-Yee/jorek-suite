from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

try:
    from PySide6.QtWidgets import QApplication
    from jorek_build_studio.stage_logs import StageLogDialog
    QT_AVAILABLE = True
except ImportError:
    QApplication = None
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
