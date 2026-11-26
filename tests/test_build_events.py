from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from common import BUILD_EVENT_PREFIX, emit_build_event, run


class BuildEventTests(unittest.TestCase):
    def test_event_protocol_is_opt_in_and_machine_readable(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"JOREK_BUILD_STUDIO_EVENTS": "1"}):
            with contextlib.redirect_stdout(output):
                emit_build_event(
                    "stage_start", id="dependency:mpich",
                    label="MPICH 4.3.0", category="Dependency")
        line = output.getvalue().strip()
        self.assertTrue(line.startswith(BUILD_EVENT_PREFIX))
        payload = json.loads(line[len(BUILD_EVENT_PREFIX):])
        self.assertEqual(payload["event"], "stage_start")
        self.assertEqual(payload["id"], "dependency:mpich")
        self.assertEqual(payload["category"], "Dependency")

    def test_event_protocol_does_not_change_normal_cli_output(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"JOREK_BUILD_STUDIO_EVENTS": "0"}):
            with contextlib.redirect_stdout(output):
                emit_build_event("stage_start", id="hidden")
        self.assertEqual(output.getvalue(), "")

    def test_run_streams_merged_output_and_writes_complete_log(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "logs" / "component.log"
            output = io.StringIO()
            command = [
                sys.executable, "-c",
                "import sys; print('stdout-line', flush=True); "
                "print('stderr-line', file=sys.stderr, flush=True)",
            ]
            with contextlib.redirect_stdout(output):
                result = run(command, log=log)
            self.assertEqual(result.returncode, 0)
            self.assertIn("stdout-line", output.getvalue())
            self.assertIn("stderr-line", output.getvalue())
            self.assertIn(b"stdout-line", result.stdout)
            self.assertIn(b"stderr-line", result.stdout)
            saved = log.read_text(encoding="utf-8")
            self.assertIn("$ ", saved)
            self.assertIn("stdout-line", saved)
            self.assertIn("stderr-line", saved)
            self.assertIn("[exit 0", saved)


if __name__ == "__main__":
    unittest.main()
