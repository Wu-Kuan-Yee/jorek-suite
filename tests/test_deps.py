#!/usr/bin/env python3
"""Tests for dependency source acquisition and private-prefix installation."""

from __future__ import annotations

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
