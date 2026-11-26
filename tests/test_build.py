#!/usr/bin/env python3
"""Unit tests for variant discovery, configuration, and rewriting."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import build  # noqa: E402
import jorek  # noqa: E402


class SourceDependencyCacheTests(unittest.TestCase):
    def test_matching_complete_marker_does_not_skip_per_dependency_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            prefix = work / "deps"
            prefix.mkdir()
            marker = prefix / ".complete"
            marker.write_text("metis\n", encoding="utf-8")

            args = SimpleNamespace(
                work=str(work),
                profile="full",
                solver="all",
                programs="main",
                force=False,
                offline=False,
                no_checksum=False,
                jobs=2,
                cc="cc",
                cxx="c++",
                fc="gfortran",
            )
            platform_info = object()
            context = object()
            manager = Mock()

            with patch.object(build, "_platform", return_value=platform_info), \
                    patch.object(build.deps, "core_dependencies", return_value=["metis"]), \
                    patch.object(build, "Downloader", return_value=object()), \
                    patch.object(build.deps, "BuildContext", return_value=context), \
                    patch.object(build.deps, "DepManager", return_value=manager), \
                    patch.object(build, "_source_context", return_value={}), \
                    patch.object(build, "_host_tool", return_value="make"):
                build._ensure_source_deps(args)

            manager.build.assert_called_once_with(["metis"])
            self.assertEqual(marker.read_text(encoding="utf-8"), "metis\n")


class VariantTest(unittest.TestCase):
    def test_default_matrix(self) -> None:
        root = Path("/tmp/jorek-base")
        if not (root / "models" / "mod_settings.f90").exists():
            self.skipTest("JOREK checkout not available")
        models = jorek.discover_models(root)
        self.assertIn("303", models)
        self.assertIn("600", models)
        spec = jorek.make_variant("303", models, jorek.FeatureSet())
        self.assertEqual(spec.model, "303")
        self.assertEqual(spec.params["n_tor"], 3)
        self.assertEqual(spec.features.solver, "mumps")

    def test_complete_matrix(self) -> None:
        root = Path("/tmp/jorek-base")
        if not (root / "models" / "mod_settings.f90").exists():
            self.skipTest("JOREK checkout not available")
        models = jorek.discover_models(root)
        specs = jorek.complete_variants(models, jorek.FeatureSet())
        self.assertGreaterEqual(len(specs), 60)
        maximal = []
        for model in sorted(models):
            info = models[model]
            toggles = {} if model == "183" else {t: True for t in info.toggles if t.lower() != "with_refluid"}
            maximal.append(jorek.make_variant(model, models, jorek.FeatureSet(), toggles=toggles))
        self.assertEqual(len(maximal), len(models))
        names = [s.name for s in specs]
        self.assertEqual(len(names), len(set(names)))

    def test_feature_conflicts(self) -> None:
        features = jorek.FeatureSet()
        features.enable("boost")
        features.enable("std_besselk")
        with self.assertRaises(Exception):
            features.validate()

    def test_settings_rewrite(self) -> None:
        root = Path("/tmp/jorek-base")
        if not (root / "models" / "mod_settings.f90").exists():
            self.skipTest("JOREK checkout not available")
        models = jorek.discover_models(root)
        spec = jorek.make_variant("303", models, jorek.FeatureSet(), params={"n_tor": 5, "n_plane": 8})
        self.assertIn("n_tor", spec.overrides)
        self.assertIn("n_plane", spec.overrides)


if __name__ == "__main__":
    unittest.main()
