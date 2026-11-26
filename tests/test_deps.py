#!/usr/bin/env python3
"""Tests for dependency source acquisition and private-prefix installation."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import deps  # noqa: E402
from common import BuildError  # noqa: E402


class FakeContext:
    def __init__(self, root: Path) -> None:
        self.work = root / "work"
        self.prefix = root / "prefix"
        self.work.mkdir(parents=True)
        self.prefix.mkdir(parents=True)
        self.path = []
        self.cc = "cc"
        self.make = "make"
        self.jobs = 1
        self.force = False
        self.platform = SimpleNamespace(
            system="linux", machine="x86_64", is_macos=False)
        self.source_dir = None
        self.source_dirs = {}
        self.url_overrides = {}

    def build_env(self, dependency):
        return {}

    def log_path(self, name):
        return self.work / "logs" / (name + ".log")

    def stamp_path(self, dependency):
        return self.work / "stamps" / (dependency.name + ".json")

    def has_stamp(self, dependency):
        return False

    def mark_built(self, dependency):
        self.mark_built_calls.append(dependency.name)

    def refresh_tools(self):
        pass


class GitSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.context = FakeContext(self.root)

    def test_pastix5_is_registered_with_an_isolated_private_prefix(self):
        dependency = deps.DEPENDENCIES["pastix5"]
        self.assertEqual(dependency.version, "5.2.3")
        self.assertEqual(dependency.builder, "pastix5")
        self.assertEqual(dependency.filename, "pastix-5.2.3.tar.bz2")
        self.assertEqual(dependency.static_libs, ("pastix5/lib/libpastix.a",))
        self.assertEqual(
            dependency.includes, ("pastix5/include/pastix_fortran.h",))
        self.assertTrue(dependency.required_any_paths)
        names = deps.core_dependencies("standard", "pastix5")
        self.assertIn("pastix5", names)
        self.assertIn("metis", names)
        self.assertIn("hwloc", names)

    def test_pastix_source_is_a_recursive_tagged_git_checkout(self):
        dependency = deps.DEPENDENCIES["pastix6"]
        self.assertEqual(dependency.source_kind, "git")
        self.assertEqual(dependency.git_ref, "v6.3.2")
        self.assertTrue(dependency.git_submodules)
        self.assertIn("spm/CMakeLists.txt", dependency.required_paths)
        self.assertIn(
            "cmake_modules/morse_cmake/modules/MorseInit.cmake",
            dependency.required_paths)
        self.assertIn("-DGIT_SUBMODULE=OFF", dependency.cmake_args)
        self.assertIn("-DPASTIX_INT64=OFF", dependency.cmake_args)
        self.assertIn("-DPASTIX_ORDERING_METIS=ON", dependency.cmake_args)
        self.assertIn("-DPASTIX_ORDERING_SCOTCH=OFF", dependency.cmake_args)
        self.assertEqual(dependency.recipe_revision, "2")

    def test_pastix6_does_not_pull_scotch_when_using_metis_ordering(self):
        names = deps.core_dependencies("standard", "pastix6")
        self.assertIn("pastix6", names)
        self.assertIn("metis", names)
        self.assertNotIn("scotch", names)
        self.assertIn("scotch", deps.core_dependencies("complete", "pastix6"))

    def test_git_clone_includes_recursive_submodules_and_validates_them(self):
        dependency = deps.Dependency(
            name="pastix-fixture", version="v1", url="https://example.invalid/repo.git",
            source_kind="git", git_ref="v1", git_submodules=True,
            required_paths=("spm/CMakeLists.txt", "cmake_modules/morse_cmake/modules/MorseInit.cmake"),
        )
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((list(argv), kwargs))
            destination = Path(argv[-1])
            for relative in dependency.required_paths:
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("# fixture\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        with patch.object(deps, "which", return_value="git"), \
                patch.object(deps, "run", side_effect=fake_run):
            source = deps._clone_git_source(
                self.context, dependency, ["https://example.invalid/repo.git"])

        self.assertEqual(source, self.context.work / "src" / "pastix-fixture-v1")
        self.assertTrue((source / "spm/CMakeLists.txt").is_file())
        argv, kwargs = calls[0]
        self.assertEqual(argv[:2], ["git", "clone"])
        self.assertIn("--depth", argv)
        self.assertIn("--branch", argv)
        self.assertEqual(argv[argv.index("--branch") + 1], "v1")
        self.assertIn("--recurse-submodules", argv)
        self.assertIn("--shallow-submodules", argv)
        self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")

    def test_pastix_discards_cmake_cache_from_the_old_archive_root(self):
        dependency = deps.DEPENDENCIES["pastix6"]
        source = self.root / "pastix-source"
        for relative in dependency.required_paths:
            target = source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# fixture\\n", encoding="utf-8")
        build_dir = self.context.work / "build" / "pastix6-6.3.2"
        build_dir.mkdir(parents=True)
        stale_cache = build_dir / "CMakeCache.txt"
        stale_cache.write_text("CMAKE_HOME_DIRECTORY=old/archive/root\\n", encoding="utf-8")
        (self.context.prefix / "bin").mkdir()

        with patch.object(deps, "build_cmake") as cmake_mock:
            deps.build_pastix(self.context, dependency, source)

        self.assertFalse(stale_cache.exists())
        cmake_mock.assert_called_once_with(self.context, dependency, source)
        self.assertTrue((self.context.prefix / "bin" / "pastix-conf").is_file())

    def test_git_clone_rejects_missing_required_submodule_content(self):
        dependency = deps.Dependency(
            name="pastix-fixture", version="v1", url="https://example.invalid/repo.git",
            source_kind="git", git_ref="v1", git_submodules=True,
            required_paths=("spm/CMakeLists.txt",),
        )

        with patch.object(deps, "which", return_value="git"), \
                patch.object(
                    deps, "run",
                    return_value=subprocess.CompletedProcess(["git"], 0, b"", b"")):
            with self.assertRaisesRegex(BuildError, "missing required source paths"):
                deps._clone_git_source(
                    self.context, dependency, ["https://example.invalid/repo.git"])


class ScotchInstallTests(unittest.TestCase):
    def test_install_target_receives_the_private_prefix(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = FakeContext(root)
            source = root / "scotch"
            make_inc = source / "src" / "Make.inc"
            make_inc.mkdir(parents=True)
            (make_inc / "Makefile.inc.x86-64_linux2").write_text(
                "prefix = /usr/local\n"
                "CCS = gcc\nCCP = gcc\nCCD = gcc\n"
                "CFLAGS = -O2\n",
                encoding="utf-8",
            )
            dependency = deps.Dependency(
                name="scotch", version="6.1.2", url="",
                builder="scotch", static_libs=(), includes=())

            with patch.object(deps, "run") as run_mock:
                run_mock.return_value = subprocess.CompletedProcess([], 0, b"", b"")
                deps.build_scotch(context, dependency, source)

            self.assertEqual(run_mock.call_count, 3)
            install_argv = run_mock.call_args_list[2].args[0]
            self.assertIn("prefix=%s" % context.prefix, install_argv)
            self.assertEqual(install_argv[-1], "install")


class RecipeStampTests(unittest.TestCase):
    def test_old_scotch_stamp_is_invalidated_by_recipe_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary)
            dependency = deps.DEPENDENCIES["scotch"]
            (prefix / "lib").mkdir()
            for library in dependency.static_libs:
                (prefix / "lib" / library).write_bytes(b"fixture")
            include = prefix / "include"
            include.mkdir()
            for header in dependency.includes:
                target = include / header
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"fixture")

            context = object.__new__(deps.BuildContext)
            context.force = False
            context.prefix = prefix
            context.stamps = {
                "scotch": {
                    "name": "scotch",
                    "version": dependency.version,
                    "url": dependency.url,
                    "time": "before-prefix-fix",
                }
            }
            self.assertFalse(context.has_stamp(dependency))

            context.stamps["scotch"]["recipe_revision"] = dependency.recipe_revision
            self.assertTrue(context.has_stamp(dependency))

    def test_mark_built_persists_recipe_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = object.__new__(deps.BuildContext)
            context.stamp_dir = Path(temporary) / "stamps"
            context.stamp_dir.mkdir()
            context.stamps = {}
            dependency = deps.DEPENDENCIES["scotch"]

            context.mark_built(dependency)

            saved = json.loads(context.stamp_path(dependency).read_text(encoding="utf-8"))
            self.assertEqual(saved["recipe_revision"], dependency.recipe_revision)


class DependencyStampTests(unittest.TestCase):
    def test_failed_static_library_validation_does_not_write_a_stamp(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = FakeContext(root)
            source = root / "fixture-source"
            source.mkdir()
            context.source_dirs = {"fixture": source}
            context.mark_built_calls = []
            dependency = deps.Dependency(
                name="fixture", version="1", url="",
                builder="fixture", static_libs=("libmissing.a",))

            manager = deps.DepManager(context)
            with patch.dict(deps.BUILDERS, {"fixture": lambda ctx, item, src: None}), \
                    patch.object(deps, "_find_in_prefix", return_value=None):
                with self.assertRaisesRegex(BuildError, "libmissing.a"):
                    manager._build_one(dependency)

            self.assertEqual(context.mark_built_calls, [])


if __name__ == "__main__":
    unittest.main()
