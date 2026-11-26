from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from jorek_build_studio.backend import inspect_checkout, parse_doctor_output
from jorek_build_studio.config import (
    build_arguments, load_config, normalize_config, save_config, validate_config,
)
from jorek_build_studio.legacy import import_legacy_text


class ConfigTests(unittest.TestCase):
    def test_argument_vector_preserves_paths_with_spaces(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "checkout with spaces" / "build.py"
            script.parent.mkdir()
            script.write_text("# placeholder\n", encoding="utf-8")
            cfg = normalize_config({
                "build_script": str(script),
                "jorek_root": str(root / "JOREK source"),
                "work_dir": str(root / "build work"),
                "command": "dist",
                "model": "502",
                "matrix": "maximal",
                "jobs": 8,
                "dry_run": True,
            })
            argv = build_arguments(cfg)
            self.assertIn(str(root / "build work"), argv)
            self.assertIn(str(root / "JOREK source"), argv)
            self.assertEqual(argv[argv.index("--model") + 1], "502")
            self.assertIn("dist", argv)
            self.assertNotIn(";", argv)

    def test_feature_overrides_only_emit_changes_from_source_defaults(self):
        cfg = normalize_config({
            "work_dir": "/tmp/work",
            "feature_defaults": {"hdf5": True, "fftw": False},
            "feature_states": {"hdf5": True, "fftw": True},
        })
        argv = build_arguments(cfg, "matrix")
        self.assertIn("--enable", argv)
        self.assertIn("fftw", argv)
        self.assertNotIn("hdf5", argv)

    def test_all_models_and_family_override_are_explicit(self):
        cfg = normalize_config({
            "work_dir": "/tmp/work",
            "model": "all",
            "matrix": "one",
            "family_toggle_defaults": {"with_TiTe": False},
            "family_toggle_states": {"with_TiTe": True},
        })
        argv = build_arguments(cfg, "dist")
        self.assertEqual(argv[argv.index("--matrix") + 1], "models")
        self.assertEqual(argv[argv.index("--family-toggle") + 1], "with_TiTe=1")

    def test_run_command_preserves_suite_arguments_without_a_shell(self):
        cfg = normalize_config({
            "work_dir": "/tmp/work",
            "suite_path": "/tmp/suite with spaces",
            "run_model": "502",
            "stdin_file": "/tmp/input.nml",
            "run_args": "--mpi -np 4",
        })
        argv = build_arguments(cfg, "run")
        self.assertEqual(argv[argv.index("--suite") + 1], "/tmp/suite with spaces")
        self.assertEqual(argv[argv.index("--stdin") + 1], "/tmp/input.nml")
        self.assertEqual(argv[-4:], ["--", "--mpi", "-np", "4"])

    def test_builder_help_exposes_family_toggle_for_matrix_build_and_full(self):
        script = Path(__file__).resolve().parents[1] / "build.py"
        for command in ("matrix", "build", "full"):
            result = subprocess.run(
                [sys.executable, str(script), command, "--help"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("--family-toggle", result.stdout)

    def test_parameter_grid_and_maximum_conflict_is_reported(self):
        cfg = normalize_config({
            "build_script": "/tmp/build.py",
            "work_dir": "/tmp/work",
            "param_grid": "n_tor=1,3,5",
            "param_max": "n_tor=9",
        })
        errors = validate_config(cfg)
        self.assertTrue(any("cannot use both" in item for item in errors))

    def test_config_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nested" / "studio.json"
            cfg = normalize_config({"jobs": 12, "work_dir": "/tmp/work",
                                    "feature_states": {"hdf5": False}})
            save_config(cfg, path)
            loaded = load_config(path)
            self.assertEqual(loaded["jobs"], 12)
            self.assertFalse(loaded["feature_states"]["hdf5"])
            self.assertEqual(json.loads(path.read_text())["jobs"], 12)


class LegacyImportTests(unittest.TestCase):
    def test_imports_literals_and_never_executes_shell(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "must-not-exist"
            script = "\n".join([
                "CC='/opt/tool chain/bin/gcc'",
                "JOBS=16",
                "USE_HDF5=1",
                "USE_FFTW=.false.",
                "RUN=$(touch %s)" % marker,
                "make all",
            ])
            mapped, ignored = import_legacy_text(script)
            self.assertEqual(mapped["cc"], "/opt/tool chain/bin/gcc")
            self.assertEqual(mapped["jobs"], 16)
            self.assertTrue(mapped["feature_states"]["hdf5"])
            self.assertFalse(mapped["feature_states"]["fftw"])
            self.assertFalse(marker.exists())
            self.assertTrue(any("shell expression" in item for item in ignored))


class BackendTests(unittest.TestCase):
    def test_catalog_is_read_from_checkout_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            (base / "src").mkdir()
            (base / "models" / "model303").mkdir(parents=True)
            (base / "models" / "model303" / "mod_model_settings.f90").write_text(
                "logical, parameter :: with_TiTe = .true.\n", encoding="utf-8")
            (base / "jorek2_main.f90").write_text("program jorek2_main\nend\n")
            (base / "build.py").write_text("print('placeholder')\n")
            (base / "src" / "jorek.py").write_text(
                "MODELS = ('303',)\n"
                "FAMILY_TOGGLES = {'303': ()}\n"
                "BOOLEAN_FEATURES = {'hdf5': ('USE_HDF5', '-DUSE_HDF5')}\n"
                "DEFAULT_FEATURES = {'hdf5': True}\n",
                encoding="utf-8",
            )
            (base / "src" / "deps.py").write_text(
                "def dep(*args, **kwargs): pass\n"
                "ITEM = dep('fftw', '3.3.10', 'https://example.invalid/fftw.tar.gz', "
                "license='BSD-3-Clause', builder='autotools')\n",
                encoding="utf-8",
            )
            data = inspect_checkout(str(base / "build.py"), str(base))
            self.assertEqual(data["models"], ["303"])
            self.assertEqual(data["features"], ["hdf5"])
            self.assertTrue(data["feature_defaults"]["hdf5"])
            self.assertTrue(data["model_toggle_defaults"]["303"]["with_TiTe"])
            self.assertEqual(data["dependencies"][0]["name"], "fftw")
            self.assertEqual(data["dependencies"][0]["license"], "BSD-3-Clause")

    def test_doctor_json_parser(self):
        payload = {"platform": {"system": "Linux"}, "tools": {}}
        self.assertEqual(parse_doctor_output(json.dumps(payload)), payload)


if __name__ == "__main__":
    unittest.main()
