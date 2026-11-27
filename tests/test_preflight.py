from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import deps  # noqa: E402
import solvers  # noqa: E402
from verification import CheckResult, VerificationCheck, run_checks  # noqa: E402


class RecipeCatalogTests(unittest.TestCase):
    def test_entire_dependency_catalog_is_consistent(self):
        self.assertEqual(deps.validate_dependency_registry(), [])

    def test_entire_solver_source_catalog_is_consistent(self):
        self.assertEqual(solvers.validate_solver_source_recipes(), [])

    def test_full_and_dist_paths_share_common_dependency_recipes(self):
        for name in ("hwloc", "gklib", "metis", "pastix6", "pastix5", "strumpack"):
            with self.subTest(name=name):
                self.assertIs(
                    solvers.SOLVER_SOURCE_RECIPES[name],
                    deps.DEPENDENCIES[name],
                )
        pastix = solvers.SOLVER_SOURCE_RECIPES["pastix6"]
        self.assertEqual(pastix.source_kind, "git")
        self.assertEqual(pastix.git_ref, "v6.3.2")
        self.assertTrue(pastix.git_submodules)
        pastix5 = solvers.SOLVER_SOURCE_RECIPES["pastix5"]
        self.assertEqual(pastix5.version, "5.2.3")
        self.assertEqual(pastix5.filename, "pastix-5.2.3.tar.bz2")
        self.assertTrue(pastix5.required_any_paths)
        self.assertEqual(pastix5.builder, "pastix5")

    def test_source_recipe_validation_is_package_agnostic(self):
        errors = deps.validate_source_recipe(
            "fixture", "1.0", "https://example.invalid/fixture.tar.gz",
            source_kind="archive", git_submodules=True,
            required_paths=("../outside",),
        )
        self.assertTrue(any("requests submodules" in item for item in errors))
        self.assertTrue(any("unsafe required path" in item for item in errors))


class VerificationRunnerTests(unittest.TestCase):
    def test_runner_collects_pass_warning_and_failure_without_stopping(self):
        with tempfile.TemporaryDirectory() as temporary:
            checks = [
                VerificationCheck(
                    "catalog", "Check catalog", "Dependencies",
                    lambda: ["all recipes valid"]),
                VerificationCheck(
                    "optional", "Check optional feature", "Features",
                    lambda: CheckResult("warning", ["optional feature unavailable"]),
                    severity="warning"),
                VerificationCheck(
                    "broken", "Check deliberately broken item", "Environment",
                    lambda: (_ for _ in ()).throw(RuntimeError("fixture failure"))),
                VerificationCheck(
                    "after-failure", "Check after failure", "Environment",
                    lambda: ["runner continued"]),
            ]
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                report = run_checks(checks, Path(temporary) / "logs")
            self.assertFalse(report["ok"])
            self.assertEqual(report["summary"]["total"], 4)
            self.assertEqual(report["summary"]["passed"], 2)
            self.assertEqual(report["summary"]["warnings"], 1)
            self.assertEqual(report["summary"]["failed"], 1)
            self.assertEqual(report["checks"][-1]["status"], "passed")
            self.assertTrue((Path(temporary) / "logs" / "catalog.log").is_file())
            self.assertTrue((Path(temporary) / "logs" / "preflight-report.json").is_file())
            self.assertIn("runner continued", output.getvalue())


if __name__ == "__main__":
    unittest.main()
