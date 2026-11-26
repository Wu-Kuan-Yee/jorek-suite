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
import solvers  # noqa: E402
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
        self.cmake = "cmake"
        self.ar = "ar"
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
        self.assertIn("-DPASTIX_BUILD_EXAMPLES=OFF", dependency.cmake_args)
        self.assertIn("-DPASTIX_BUILD_TESTS=OFF", dependency.cmake_args)
        self.assertEqual(dependency.recipe_revision, "4")

    def test_hwloc_static_build_uses_internal_xml_backend(self):
        dependency = deps.DEPENDENCIES["hwloc"]
        self.assertIn("--disable-shared", dependency.configure_args)
        self.assertIn("--enable-static", dependency.configure_args)
        self.assertIn("--disable-libxml2", dependency.configure_args)
        self.assertEqual(dependency.recipe_revision, "1")

    def test_pastix6_does_not_pull_scotch_when_using_metis_ordering(self):
        names = deps.core_dependencies("standard", "pastix6")
        self.assertIn("pastix6", names)
        self.assertIn("metis", names)
        self.assertNotIn("scotch", names)
        self.assertIn("scotch", deps.core_dependencies("complete", "pastix6"))

    def test_parmetis_source_is_pinned_to_the_official_403_commit(self):
        dependency = deps.DEPENDENCIES["parmetis"]
        self.assertEqual(dependency.version, "4.0.3")
        self.assertEqual(dependency.source_kind, "git")
        self.assertEqual(dependency.url, "https://github.com/KarypisLab/ParMETIS.git")
        self.assertEqual(
            dependency.git_ref,
            "fc99804e3f043dd951e37775e30db697444816b8")
        self.assertFalse(dependency.git_submodules)
        self.assertIn("Makefile", dependency.required_paths)
        self.assertIn("include/parmetis.h", dependency.required_paths)
        self.assertIn("libparmetis/CMakeLists.txt", dependency.required_paths)
        self.assertEqual(dependency.recipe_revision, "1")

    def test_git_clone_checks_out_a_pinned_commit_sha(self):
        commit = "a" * 40
        dependency = deps.Dependency(
            name="commit-fixture", version="1", url="https://example.invalid/repo.git",
            source_kind="git", git_ref=commit,
            required_paths=("CMakeLists.txt",),
        )
        calls = []
        destination = self.context.work / "src" / "commit-fixture-1"

        def fake_run(argv, **kwargs):
            command = list(argv)
            calls.append((command, kwargs))
            if command[1] == "clone":
                destination.mkdir(parents=True, exist_ok=True)
                (destination / ".git").mkdir()
            elif command[1:3] == ["-C", str(destination)] and command[3:5] == [
                    "checkout", "--detach"]:
                (destination / "CMakeLists.txt").write_text(
                    "# pinned fixture\\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, b"", b"")

        with patch.object(deps, "which", return_value="git"), \
                patch.object(deps, "run", side_effect=fake_run):
            source = deps._clone_git_source(
                self.context, dependency, ["https://example.invalid/repo.git"])

        self.assertEqual(source, destination)
        self.assertEqual(len(calls), 2)
        clone = calls[0][0]
        self.assertEqual(clone[:2], ["git", "clone"])
        self.assertIn("--no-checkout", clone)
        self.assertNotIn("--depth", clone)
        self.assertNotIn("--branch", clone)
        checkout = calls[1][0]
        self.assertEqual(
            checkout,
            ["git", "-C", str(destination), "checkout", "--detach", commit])

    def test_strumpack_static_link_patch_moves_scalapack_before_mpi(self):
        source = self.root / "strumpack-source"
        source.mkdir()
        mpi_block = (
            "if(STRUMPACK_USE_MPI)\n"
            "  target_link_libraries(strumpack PUBLIC\n"
            "    MPI::MPI_CXX MPI::MPI_C ${MPI_Fortran_LIBRARIES})\n"
            "  ## adding the MPI::MPI_Fortran target risks adding unwanted compile flags\n"
            "endif()\n"
        )
        scalapack_block = (
            "if(STRUMPACK_USE_MPI)\n"
            "  if(SCALAPACK_FOUND)\n"
            "    target_link_libraries(strumpack PUBLIC ${SCALAPACK_LIBRARIES})\n"
            "  endif()\n"
            "endif()\n"
        )
        cmake_lists = source / "CMakeLists.txt"
        cmake_lists.write_text(
            mpi_block + "\nif(OpenMP_FOUND)\nendif()\n\n" + scalapack_block,
            encoding="utf-8",
        )

        self.assertTrue(deps.patch_strumpack_static_link_order(source))
        patched = cmake_lists.read_text(encoding="utf-8")
        marker = "# JOREK Build Studio static-link fix: ScaLAPACK may reference MPI symbols."
        marker_at = patched.index(marker)
        scalapack_at = patched.index(
            "target_link_libraries(strumpack PUBLIC ${SCALAPACK_LIBRARIES})",
            marker_at)
        mpi_at = patched.index("MPI::MPI_CXX", marker_at)
        self.assertLess(scalapack_at, mpi_at)
        self.assertEqual(patched.count("MPI::MPI_CXX"), 1)
        self.assertFalse(deps.patch_strumpack_static_link_order(source))
        self.assertEqual(cmake_lists.read_text(encoding="utf-8"), patched)

    def test_strumpack_static_link_patch_rejects_unknown_upstream_layout(self):
        source = self.root / "strumpack-unknown-source"
        source.mkdir()
        (source / "CMakeLists.txt").write_text(
            "project(STRUMPACK VERSION 7.2.0)\n", encoding="utf-8")
        with self.assertRaisesRegex(BuildError, "cannot safely patch STRUMPACK"):
            deps.patch_strumpack_static_link_order(source)

    def test_strumpack_declares_its_required_numerical_dependencies(self):
        dependency = deps.DEPENDENCIES["strumpack"]
        self.assertIn("mpich", dependency.deps)
        self.assertIn("openblas", dependency.deps)
        self.assertIn("scalapack", dependency.deps)
        self.assertIn("metis", dependency.deps)
        self.assertEqual(dependency.recipe_revision, "2")

    def test_strumpack_build_uses_private_libraries_core_target_and_consumer_link_check(self):
        dependency = deps.DEPENDENCIES["strumpack"]
        source = self.root / "strumpack-source"
        source.mkdir()
        mpi_block = (
            "if(STRUMPACK_USE_MPI)\n"
            "  target_link_libraries(strumpack PUBLIC\n"
            "    MPI::MPI_CXX MPI::MPI_C ${MPI_Fortran_LIBRARIES})\n"
            "  ## adding the MPI::MPI_Fortran target risks adding unwanted compile flags\n"
            "endif()\n"
        )
        scalapack_block = (
            "if(STRUMPACK_USE_MPI)\n"
            "  if(SCALAPACK_FOUND)\n"
            "    target_link_libraries(strumpack PUBLIC ${SCALAPACK_LIBRARIES})\n"
            "  endif()\n"
            "endif()\n"
        )
        (source / "CMakeLists.txt").write_text(
            mpi_block + "\nif(OpenMP_FOUND)\nendif()\n\n" + scalapack_block,
            encoding="utf-8",
        )
        libdir = self.context.prefix / "lib"
        includedir = self.context.prefix / "include"
        libdir.mkdir()
        includedir.mkdir()
        for name in ("libopenblas.a", "libscalapack.a", "libmetis.a"):
            (libdir / name).write_bytes(b"static archive fixture")
        (includedir / "metis.h").write_text(
            "#define IDXTYPEWIDTH 32\n", encoding="utf-8")
        package_dir = libdir / "cmake" / "STRUMPACK"
        package_dir.mkdir(parents=True)
        (package_dir / "strumpack-config.cmake").write_text(
            "# package config fixture\n", encoding="utf-8")
        (package_dir / "strumpack-targets.cmake").write_text(
            "# exported targets fixture\n", encoding="utf-8")

        calls = []

        def fake_run(argv, **kwargs):
            command = list(argv)
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, b"", b"")

        with patch.object(deps, "run", side_effect=fake_run):
            deps.build_strumpack(self.context, dependency, source)

        self.assertEqual(len(calls), 5)
        configure = calls[0][0]
        self.assertEqual(configure[:2], ["cmake", "-S"])
        self.assertIn("-DCMAKE_PREFIX_PATH=%s" % self.context.prefix, configure)
        self.assertIn("-DBUILD_TESTING=OFF", configure)
        self.assertNotIn("-DSTRUMPACK_BUILD_TESTS=OFF", configure)
        self.assertIn(
            "-DTPL_BLAS_LIBRARIES=%s" % (libdir / "libopenblas.a"), configure)
        self.assertIn(
            "-DTPL_LAPACK_LIBRARIES=%s" % (libdir / "libopenblas.a"), configure)
        self.assertIn(
            "-DTPL_SCALAPACK_LIBRARIES=%s" % (libdir / "libscalapack.a"),
            configure)
        self.assertIn("-DTPL_METIS_PREFIX=%s" % self.context.prefix, configure)
        self.assertIn(
            "-DTPL_METIS_INCLUDE_DIRS=%s" % includedir, configure)
        self.assertIn(
            "-DTPL_METIS_LIBRARY_DIR=%s" % libdir, configure)
        self.assertIn(
            "-DTPL_METIS_LIBRARIES=%s" % (libdir / "libmetis.a"), configure)
        for option in (
                "SLATE", "PARMETIS", "SCOTCH", "PTSCOTCH", "BPACK", "ZFP"):
            self.assertIn("-DTPL_ENABLE_%s=OFF" % option, configure)

        build_dir = self.context.work / "build" / "strumpack-7.1.0"
        core_build = calls[1][0]
        self.assertEqual(
            core_build,
            ["cmake", "--build", str(build_dir),
             "--target", "strumpack", "--parallel", "1"])
        self.assertEqual(calls[2][0], ["cmake", "--install", str(build_dir)])

        verify_configure = calls[3][0]
        self.assertEqual(verify_configure[:3], [
            "cmake", "-S", str(build_dir / "consumer-link-check" / "src")])
        self.assertIn(
            "-DCMAKE_PREFIX_PATH=%s" % self.context.prefix, verify_configure)
        self.assertIn(
            "-DMPI_C_COMPILER=%s" % (self.context.prefix / "bin" / "mpicc"),
            verify_configure)
        self.assertIn(
            "-DMPI_CXX_COMPILER=%s" % (self.context.prefix / "bin" / "mpicxx"),
            verify_configure)
        self.assertIn(
            "-DMPI_Fortran_COMPILER=%s" % (self.context.prefix / "bin" / "mpifort"),
            verify_configure)
        self.assertEqual(calls[4][0], [
            "cmake", "--build", str(build_dir / "consumer-link-check" / "build"),
            "--target", "strumpack_link_probe", "--parallel", "1"])

        patched = (source / "CMakeLists.txt").read_text(encoding="utf-8")
        marker_at = patched.index(
            "# JOREK Build Studio static-link fix: ScaLAPACK may reference MPI symbols.")
        self.assertLess(
            patched.index(
                "target_link_libraries(strumpack PUBLIC ${SCALAPACK_LIBRARIES})",
                marker_at),
            patched.index("MPI::MPI_CXX", marker_at))
        probe = (build_dir / "consumer-link-check" / "src" /
                 "strumpack_link_probe.cpp").read_text(encoding="utf-8")
        self.assertIn("#include <StrumpackSparseSolverMPIDist.hpp>", probe)
        self.assertIn("STRUMPACK_FC_GLOBAL(pdgemm, PDGEMM)", probe)

    def test_parmetis_build_uses_static_default_and_dependency_prefix(self):
        dependency = deps.DEPENDENCIES["parmetis"]
        source = self.root / "parmetis-source"
        source.mkdir()
        calls = []

        def fake_run(argv, **kwargs):
            command = list(argv)
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, b"", b"")

        mpicc = str(self.context.prefix / "bin" / "mpicc")
        with patch.object(deps, "which", return_value=mpicc), \
                patch.object(deps, "run", side_effect=fake_run):
            deps.build_parmetis(self.context, dependency, source)

        self.assertEqual(len(calls), 3)
        configure = calls[0][0]
        self.assertEqual(configure[:2], ["make", "config"])
        self.assertIn("cc=%s" % mpicc, configure)
        self.assertIn("prefix=%s" % self.context.prefix, configure)
        self.assertIn("gklib_path=%s" % self.context.prefix, configure)
        self.assertIn("metis_path=%s" % self.context.prefix, configure)
        self.assertFalse(any(item.startswith("shared=") for item in configure))
        self.assertEqual(calls[1][0], ["make", "-j1"])
        self.assertEqual(calls[2][0], ["make", "install"])

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

    def test_pastix5_builds_into_a_separate_prefix_with_32bit_abi(self):
        dependency = deps.DEPENDENCIES["pastix5"]
        source = self.root / "pastix5-source"
        (source / "common" / "src").mkdir(parents=True)
        (source / "CMakeLists.txt").write_text("# fixture\n", encoding="utf-8")
        (source / "common" / "src" / "pastix_fortran.h").write_text(
            "/* fixture header */\n", encoding="utf-8")
        self.context.cmake = "cmake"
        self.context.fc = "gfortran"
        calls = []

        def fake_run(argv, **kwargs):
            command = list(argv)
            calls.append(command)
            if "-B" in command:
                build_dir = Path(command[command.index("-B") + 1])
                build_dir.mkdir(parents=True, exist_ok=True)
            if "--build" in command:
                build_dir = Path(command[command.index("--build") + 1])
                (build_dir / "libpastix.a").write_bytes(b"pastix5-fixture")
            return subprocess.CompletedProcess(command, 0, b"", b"")

        with patch.object(deps, "which", return_value=None), \
                patch.object(deps, "run", side_effect=fake_run):
            deps.build_pastix5(self.context, dependency, source)

        private = self.context.prefix / "pastix5"
        self.assertTrue((private / "lib" / "libpastix.a").is_file())
        self.assertTrue((private / "include" / "pastix_fortran.h").is_file())
        configure = calls[0]
        self.assertIn("-DPASTIX_INT64=OFF", configure)
        self.assertIn("-DPASTIX_ORDERING_METIS=ON", configure)
        self.assertIn("-DPASTIX_ORDERING_SCOTCH=OFF", configure)
        self.assertIn("-DCMAKE_INSTALL_PREFIX=%s" % private, configure)

    def test_pastix_discards_cmake_cache_from_the_old_archive_root(self):
        dependency = deps.DEPENDENCIES["pastix6"]
        source = self.root / "pastix-source"
        for relative in dependency.required_paths:
            target = source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# fixture\n", encoding="utf-8")
        module = (source / "cmake_modules" / "morse_cmake" /
                  "modules" / "find" / "FindMETIS.cmake")
        module.parent.mkdir(parents=True, exist_ok=True)
        module.write_text(
            '  list(APPEND METIS_LIBRARIES ${M_LIBRARY})\\n'
            '  list(APPEND METIS_LIBRARIES MORSE::M)\\n',
            encoding="utf-8",
        )
        build_dir = self.context.work / "build" / "pastix6-6.3.2"
        build_dir.mkdir(parents=True)
        stale_cache = build_dir / "CMakeCache.txt"
        stale_cache.write_text("CMAKE_HOME_DIRECTORY=old/archive/root\n", encoding="utf-8")
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


class MetisArchiveTests(unittest.TestCase):
    def test_metis_recipe_revision_invalidates_pre_merge_install_stamps(self):
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary)
            dependency = deps.DEPENDENCIES["metis"]
            (prefix / "lib").mkdir()
            for library in dependency.static_libs:
                (prefix / "lib" / library).write_bytes(b"fixture")
            (prefix / "include").mkdir()
            (prefix / "include" / "metis.h").write_text(
                "/* fixture */", encoding="utf-8")

            context = object.__new__(deps.BuildContext)
            context.force = False
            context.prefix = prefix
            context.stamps = {
                "metis": {
                    "name": "metis",
                    "version": dependency.version,
                    "url": dependency.url,
                }
            }
            self.assertFalse(context.has_stamp(dependency))

            context.stamps["metis"]["recipe_revision"] = dependency.recipe_revision
            self.assertTrue(context.has_stamp(dependency))

    def test_merge_static_archives_namespaces_object_members(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            primary = root / "libmetis.a"
            dependency = root / "libGKlib.a"
            primary.write_bytes(b"primary archive")
            dependency.write_bytes(b"dependency archive")
            calls = []

            def fake_run(argv, **kwargs):
                command = list(argv)
                calls.append((command, kwargs))
                if command[1] == "x":
                    archive = Path(command[2])
                    member = Path(kwargs["cwd"]) / "common.o"
                    member.write_text(archive.name, encoding="utf-8")
                elif command[1] == "rcs":
                    output = Path(command[2])
                    output.write_text(
                        "\n".join(Path(item).name for item in command[3:]),
                        encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, b"", b"")

            output = root / "merged" / "libmetis-merged.a"
            with patch.object(deps, "run", side_effect=fake_run):
                result = deps.merge_static_archives(
                    "ar", root / "work",
                    (("metis", primary), ("gklib", dependency)),
                    output)

            self.assertEqual(result, output)
            members = output.read_text(encoding="utf-8").splitlines()
            self.assertEqual(members, ["metis_common.o", "gklib_common.o"])
            self.assertEqual(sum(1 for command, _ in calls if command[1] == "x"), 2)
            self.assertTrue(any(command[1] == "rcs" for command, _ in calls))

    def test_merge_static_archives_uses_libtool_when_requested(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            primary = root / "libmetis.a"
            dependency = root / "libGKlib.a"
            primary.write_bytes(b"primary archive")
            dependency.write_bytes(b"dependency archive")
            calls = []

            def fake_run(argv, **kwargs):
                command = list(argv)
                calls.append(command)
                output = Path(command[command.index("-o") + 1])
                output.write_bytes(b"libtool merged archive")
                return subprocess.CompletedProcess(command, 0, b"", b"")

            output = root / "merged" / "libmetis-merged.a"
            with patch.object(deps, "run", side_effect=fake_run):
                result = deps.merge_static_archives(
                    "ar", root / "work",
                    (("metis", primary), ("gklib", dependency)),
                    output, libtool="libtool")

            self.assertEqual(result, output)
            self.assertEqual(output.read_bytes(), b"libtool merged archive")
            self.assertEqual(calls[0][0], "libtool")
            self.assertIn("-static", calls[0])

    def test_build_metis_merges_gklib_into_the_primary_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = FakeContext(root)
            source = root / "metis-source"
            include = source / "include"
            include.mkdir(parents=True)
            (include / "metis.h").write_text("/* METIS fixture */", encoding="utf-8")
            (include / "CMakeLists.txt").write_text("# fixture\n", encoding="utf-8")
            libdir = context.prefix / "lib"
            libdir.mkdir(parents=True)
            primary = libdir / "libmetis.a"
            gklib = libdir / "libGKlib.a"
            primary.write_bytes(b"split metis")
            gklib.write_bytes(b"gklib")

            merge_calls = []

            def fake_run(argv, **kwargs):
                return subprocess.CompletedProcess(list(argv), 0, b"", b"")

            def fake_merge(ar, work_dir, archives, output_archive, log=None, libtool=None):
                merge_calls.append((ar, tuple(archives), log))
                output_archive = Path(output_archive)
                output_archive.parent.mkdir(parents=True, exist_ok=True)
                output_archive.write_bytes(b"merged metis + gklib")
                return output_archive

            with patch.object(deps, "which", return_value=None), \
                    patch.object(deps, "run", side_effect=fake_run), \
                    patch.object(deps, "merge_static_archives", side_effect=fake_merge):
                deps.build_metis(context, deps.DEPENDENCIES["metis"], source)

            self.assertEqual(primary.read_bytes(), b"merged metis + gklib")
            self.assertEqual(gklib.read_bytes(), b"gklib")
            self.assertEqual(len(merge_calls), 1)
            self.assertEqual(merge_calls[0][0], "ar")
            self.assertEqual(
                merge_calls[0][1],
                (("metis", primary), ("gklib", gklib)))



class PastixMetisFindModuleTests(unittest.TestCase):
    def test_findmetis_uses_the_math_library_variables_exported_by_findm(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            module = (source / "cmake_modules" / "morse_cmake" /
                      "modules" / "find" / "FindMETIS.cmake")
            module.parent.mkdir(parents=True)
            module.write_text(
                '  find_package(M QUIET)\n'
                '  if ( M_FOUND )\n'
                '    set( METIS_LIBRARIES "${METIS_LIBRARIES};${M_LIBRARIES}" )\n'
                '  endif()\n',
                encoding="utf-8",
            )

            self.assertTrue(solvers.patch_pastix_metis_find_module(source))
            patched = module.read_text(encoding="utf-8")
            self.assertIn("list(APPEND METIS_LIBRARIES ${M_LIBRARY})", patched)
            self.assertIn("list(APPEND METIS_LIBRARIES MORSE::M)", patched)
            self.assertFalse(solvers.patch_pastix_metis_find_module(source))

    def test_dependency_builder_patches_module_before_cmake_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = FakeContext(root)
            source = root / "pastix"
            module = (source / "cmake_modules" / "morse_cmake" /
                      "modules" / "find" / "FindMETIS.cmake")
            module.parent.mkdir(parents=True)
            module.write_text(
                '  find_package(M QUIET)\n'
                '  if ( M_FOUND )\n'
                '    set( METIS_LIBRARIES "${METIS_LIBRARIES};${M_LIBRARIES}" )\n'
                '  endif()\n',
                encoding="utf-8",
            )
            build_dir = context.work / "build" / "pastix6-6.3.2"
            build_dir.mkdir(parents=True)
            (build_dir / "CMakeCache.txt").write_text("stale cache", encoding="utf-8")
            (context.prefix / "bin").mkdir(parents=True)

            dependency = deps.Dependency(
                name="pastix6", version="6.3.2", url="",
                builder="pastix", required_paths=(),
            )

            def configure(ctx, item, source_dir):
                self.assertFalse(build_dir.exists())
                patched = module.read_text(encoding="utf-8")
                self.assertIn("list(APPEND METIS_LIBRARIES ${M_LIBRARY})", patched)
                self.assertIn("list(APPEND METIS_LIBRARIES MORSE::M)", patched)

            with patch.object(deps, "build_cmake", side_effect=configure) as configure_mock:
                deps.build_pastix(context, dependency, source)

            configure_mock.assert_called_once_with(context, dependency, source)
            self.assertTrue((context.prefix / "bin" / "pastix-conf").is_file())


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
    def test_git_source_identity_is_checked_when_reusing_a_stamp(self):
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary)
            (prefix / "lib").mkdir()
            (prefix / "include").mkdir()
            (prefix / "lib" / "libfixture.a").write_bytes(b"fixture")
            (prefix / "include" / "fixture.h").write_text(
                "/* fixture */", encoding="utf-8")
            dependency = deps.Dependency(
                name="fixture", version="1", url="https://example.invalid/fixture.git",
                source_kind="git", git_ref="a" * 40,
                static_libs=("libfixture.a",), includes=("fixture.h",))
            context = object.__new__(deps.BuildContext)
            context.force = False
            context.prefix = prefix
            context.stamps = {
                "fixture": {
                    "version": dependency.version,
                    "url": dependency.url,
                    "source_kind": "git",
                    "git_ref": dependency.git_ref,
                    "git_submodules": False,
                }
            }

            self.assertTrue(context.has_stamp(dependency))
            context.stamps["fixture"]["git_ref"] = "b" * 40
            self.assertFalse(context.has_stamp(dependency))

    def test_mark_built_records_git_source_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = object.__new__(deps.BuildContext)
            context.stamp_dir = Path(temporary) / "stamps"
            context.stamp_dir.mkdir()
            context.stamps = {}
            dependency = deps.DEPENDENCIES["parmetis"]

            context.mark_built(dependency)

            saved = json.loads(
                context.stamp_path(dependency).read_text(encoding="utf-8"))
            self.assertEqual(saved["source_kind"], "git")
            self.assertEqual(saved["git_ref"], dependency.git_ref)
            self.assertFalse(saved["git_submodules"])

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
