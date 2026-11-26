#!/usr/bin/env python3
"""Build the static PaStiX/STRUMPACK solver stack.

This module is deliberately Python-only: the one-command build must work on
Linux and macOS without a Bash dependency.  It installs PaStiX 6, STRUMPACK and
PaStiX 5 into a private prefix; the core MUMPS/MPI/BLAS stack comes from
deps.py.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from deps import DEPENDENCIES, merge_static_archives, validate_source_recipe

from common import (
    BuildError,
    Downloader,
    PlatformInfo,
    ensure_dir,
    extract_archive,
    read_text,
    run,
    write_text,
    emit_build_event,
)


@dataclass(frozen=True)
class SolverSourceRecipe:
    name: str
    version: str
    url: str
    filename: str
    directory: str
    source_kind: str = "archive"
    git_ref: str = ""
    git_submodules: bool = False
    required_paths: Tuple[str, ...] = ()
    required_any_paths: Tuple[Tuple[str, ...], ...] = ()


# Solver-only source recipes live here; PaStiX 6 reuses the authoritative
# dependency record so the dist and full build paths cannot silently diverge.
SOLVER_SOURCE_RECIPES = {
    "hwloc": DEPENDENCIES["hwloc"],
    "gklib": DEPENDENCIES["gklib"],
    "metis": DEPENDENCIES["metis"],
    "pastix6": DEPENDENCIES["pastix6"],
    "strumpack": DEPENDENCIES["strumpack"],
    "pastix5": DEPENDENCIES["pastix5"],
}

SOLVER_STAGE_ARTIFACTS = {
    "hwloc": ("lib/libhwloc.a", "include/hwloc.h"),
    "gklib-metis": (
        "lib/libGKlib.a", "lib/libmetis.a",
        "include/GKlib.h", "include/metis.h",
    ),
    "pastix6": (
        "lib/libpastix.a", "lib/libpastix_kernels.a", "lib/libspm.a",
        "include/pastix.h", "include/spm.h",
    ),
    "strumpack": (
        "lib/libstrumpack.a",
        "include/StrumpackConfig.h", "include/StrumpackSparseSolverMPIDist.hpp",
    ),
    "pastix5": (
        "pastix5/lib/libpastix.a", "pastix5/include/pastix_fortran.h",
    ),
}


def validate_solver_source_recipes() -> List[str]:
    """Validate all source recipes used by the full-solver build path."""
    errors: List[str] = []
    for name, recipe in sorted(SOLVER_SOURCE_RECIPES.items()):
        if name != recipe.name:
            errors.append("solver source key %r does not match recipe name %r" % (
                name, recipe.name))
        errors.extend(validate_source_recipe(
            recipe.name, recipe.version, recipe.url, recipe.source_kind,
            recipe.git_ref, recipe.git_submodules, recipe.required_paths,
            getattr(recipe, "required_any_paths", ())))
        for alternatives in getattr(recipe, "required_any_paths", ()):
            for relative in alternatives:
                path = Path(relative)
                if path.is_absolute() or ".." in path.parts or not relative:
                    errors.append("%s has unsafe alternative required path %r" % (
                        recipe.name, relative))
    return errors




def patch_pastix_metis_find_module(source: Path) -> bool:
    """Repair the pinned MORSE FindMETIS/FindM variable mismatch.

    The pinned FindMETIS.cmake appends M_LIBRARIES, while its paired
    FindM.cmake exports M_LIBRARY and MORSE::M.  Consequently a successful
    find_package(M) can still leave the METIS link probe without libm.
    Return True when the module was patched and False when already patched.
    """
    module = (Path(source) / "cmake_modules" / "morse_cmake" /
              "modules" / "find" / "FindMETIS.cmake")
    if not module.is_file():
        raise BuildError("PaStiX MORSE FindMETIS.cmake is missing: %s" % module)

    content = read_text(module)
    old = '''  find_package(M QUIET)
  if ( M_FOUND )
    set( METIS_LIBRARIES "${METIS_LIBRARIES};${M_LIBRARIES}" )
  endif()
'''
    new = '''  find_package(M QUIET)
  if (M_FOUND)
    # The pinned FindM.cmake exports M_LIBRARY and MORSE::M, not
    # M_LIBRARIES.  Fall back to the variables/target it actually exports.
    if (M_LIBRARIES)
      list(APPEND METIS_LIBRARIES ${M_LIBRARIES})
    elseif (M_LIBRARY)
      list(APPEND METIS_LIBRARIES ${M_LIBRARY})
    elseif (TARGET MORSE::M)
      list(APPEND METIS_LIBRARIES MORSE::M)
    endif()
  endif()
'''
    if old in content:
        write_text(module, content.replace(old, new, 1))
        return True
    if ("list(APPEND METIS_LIBRARIES ${M_LIBRARY})" in content and
            "list(APPEND METIS_LIBRARIES MORSE::M)" in content):
        return False
    raise BuildError(
        "unrecognized math-library handling in PaStiX FindMETIS.cmake; "
        "refusing to configure with an unverified METIS link probe")


class SolverBuild:
    def __init__(self, prefix: Path, core_prefix: Path, work: Path, jobs: int,
                 platform: PlatformInfo, downloader: Downloader,
                 cc: str, cxx: str, fc: str, mpicc: str, mpicxx: str, mpifc: str,
                 log_dir: Path, source_dirs: Optional[dict] = None,
                 url_overrides: Optional[dict] = None,
                 source_dir: Optional[Path] = None):
        self.prefix = Path(prefix)
        self.core = Path(core_prefix)
        self.work = Path(work)
        self.jobs = max(1, jobs)
        self.platform = platform
        self.downloader = downloader
        self.cc = cc
        self.cxx = cxx
        self.fc = fc
        self.mpicc = mpicc
        self.mpicxx = mpicxx
        self.mpifc = mpifc
        self.log_dir = ensure_dir(log_dir)
        self.source_dirs = source_dirs or {}
        self.url_overrides = url_overrides or {}
        self.source_dir = Path(source_dir).expanduser() if source_dir else None
        cmake_path = shutil.which("cmake", path=str(self.core / "bin") + os.pathsep + os.environ.get("PATH", ""))
        self.cmake = cmake_path or "cmake"
        self.ar = shutil.which("ar") or "ar"
        self.libtool = shutil.which("libtool")

    def log(self, name: str) -> Path:
        return self.log_dir / ("solver-" + name + ".log")

    def fetch(self, url: str, filename: str) -> Path:
        return self.downloader.fetch(url, filename)

    def source(self, name: str) -> Path:
        """Acquire a source tree from its shared, validated recipe."""
        recipe = SOLVER_SOURCE_RECIPES[name]
        recipe_name = recipe.name
        directory = getattr(recipe, "directory", recipe.name)
        basename = os.path.basename(recipe.url.split("?", 1)[0]) or "source"
        filename = getattr(
            recipe, "filename",
            "%s-%s-%s" % (recipe.name, recipe.version, basename))
        supplied = self.source_dirs.get(directory)
        if supplied is None and self.source_dir:
            for candidate in (
                self.source_dir / ("%s-%s" % (recipe_name, recipe.version)),
                self.source_dir / recipe_name,
            ):
                if candidate.is_dir():
                    supplied = candidate
                    break
        if supplied is not None:
            supplied = Path(supplied).expanduser().resolve()
            if not supplied.is_dir():
                raise BuildError("source directory for %s is not a directory: %s" % (
                    recipe_name, supplied))
            source = supplied
            print("  %-18s using source tree %s" % (recipe_name, source))
        elif recipe.source_kind == "git":
            git = shutil.which("git")
            if not git:
                raise BuildError("git is required to fetch the %s source tree" % recipe_name)
            destination = self.work / "src" / (
                "%s-%s" % (recipe_name, recipe.version))
            destination.parent.mkdir(parents=True, exist_ok=True)
            env = self.env
            env["GIT_TERMINAL_PROMPT"] = "0"
            if getattr(self.downloader, "offline", False):
                if not (destination / ".git").is_dir():
                    raise BuildError(
                        "offline mode has no cached Git checkout for %s at %s" %
                        (recipe_name, destination))
                head = run(
                    [git, "-C", str(destination), "rev-parse", "HEAD"],
                    env=env, log=self.log(recipe_name), check=False)
                actual = head.stdout.decode("utf-8", "replace").strip()
                if head.returncode != 0:
                    raise BuildError("could not inspect cached Git checkout for %s" % recipe_name)
                if recipe.git_ref:
                    expected = run(
                        [git, "-C", str(destination), "rev-parse",
                         recipe.git_ref + "^{commit}"],
                        env=env, log=self.log(recipe_name), check=False)
                    if (expected.returncode != 0 or
                            expected.stdout.decode("utf-8", "replace").strip() != actual):
                        raise BuildError(
                            "cached Git checkout for %s does not match pinned ref %s" %
                            (recipe_name, recipe.git_ref))
                if recipe.git_submodules:
                    status = run(
                        [git, "-C", str(destination), "submodule", "status", "--recursive"],
                        env=env, log=self.log(recipe_name), check=False)
                    lines = status.stdout.decode("utf-8", "replace").splitlines()
                    incomplete = [line for line in lines if line[:1] in ("-", "+", "U")]
                    if status.returncode != 0 or incomplete:
                        raise BuildError(
                            "cached recursive submodules for %s are incomplete or mismatched" %
                            recipe_name)
                source = destination
            else:
                urls = list(self.url_overrides.get(directory, [])) + [recipe.url]
                failures = []
                source = None
                for url in urls:
                    if destination.exists():
                        shutil.rmtree(destination)
                    command = [git, "clone", "--depth", "1"]
                    if recipe.git_ref:
                        command.extend(["--branch", recipe.git_ref])
                    if recipe.git_submodules:
                        command.extend(["--recurse-submodules", "--shallow-submodules"])
                    command.extend([url, str(destination)])
                    result = run(command, cwd=self.work, env=env,
                                 log=self.log(recipe_name), check=False)
                    if result.returncode != 0:
                        failures.append("%s: git clone exited %d" % (url, result.returncode))
                        continue
                    if recipe.git_submodules:
                        status = run(
                            [git, "-C", str(destination), "submodule", "status", "--recursive"],
                            env=env, log=self.log(recipe_name), check=False)
                        lines = status.stdout.decode("utf-8", "replace").splitlines()
                        incomplete = [line for line in lines if line[:1] in ("-", "+", "U")]
                        if status.returncode != 0 or incomplete:
                            failures.append("%s: recursive submodule checkout is incomplete" % url)
                            shutil.rmtree(destination, ignore_errors=True)
                            continue
                    source = destination
                    break
                if source is None:
                    raise BuildError("could not clone %s:\n  %s" % (
                        recipe_name, "\n  ".join(failures)))
        else:
            urls = list(self.url_overrides.get(directory, [])) + [recipe.url]
            archive = self.downloader.fetch(urls, filename)
            destination = ensure_dir(self.work / "src" / directory)
            source = extract_archive(archive, destination)

        missing = [
            relative for relative in recipe.required_paths
            if not (source / relative).exists()
        ]
        missing_alternatives = [
            " or ".join(alternatives)
            for alternatives in getattr(recipe, "required_any_paths", ())
            if not any((source / relative).exists() for relative in alternatives)
        ]
        if missing or missing_alternatives:
            details = missing + missing_alternatives
            raise BuildError(
                "%s source tree is incomplete; missing required paths: %s" %
                (recipe_name, ", ".join(details)))
        return source

    def verify_stage_artifacts(self, stage_name: str) -> None:
        missing = [
            relative for relative in SOLVER_STAGE_ARTIFACTS.get(stage_name, ())
            if not (self.prefix / relative).is_file()
        ]
        if missing:
            raise BuildError(
                "solver stage %s did not produce required artifacts: %s" %
                (stage_name, ", ".join(missing)))

    def cmake_configure(self, source: Path, build: Path, args: List[str],
                        name: str) -> None:
        ensure_dir(build)
        run([self.cmake, "-S", str(source), "-B", str(build)] + args,
            env=self.env, log=self.log(name))

    def cmake_build(self, build: Path, name: str, target: Optional[str] = None,
                    install: bool = False) -> None:
        cmd = [self.cmake, "--build", str(build), "--parallel", str(self.jobs)]
        if target:
            cmd += ["--target", target]
        run(cmd, env=self.env, log=self.log(name))
        if install:
            run([self.cmake, "--install", str(build)], env=self.env,
                log=self.log(name))

    @property
    def env(self):
        env = os.environ.copy()
        for key in ("CC", "CXX", "FC"):
            pass
        env.setdefault("CC", self.cc)
        env.setdefault("CXX", self.cxx)
        env.setdefault("FC", self.fc)
        pkg = str(self.prefix / "lib" / "pkgconfig")
        existing = env.get("PKG_CONFIG_PATH", "")
        env["PKG_CONFIG_PATH"] = pkg + (os.pathsep + existing if existing else "")
        return env

    # -- hwloc -------------------------------------------------------------
    def hwloc(self) -> None:
        if (self.prefix / "lib" / "libhwloc.a").exists():
            return
        src = self.source("hwloc")
        # The upstream release is an autotools tree; its CMake files live only
        # under contrib/windows-cmake.
        run([str(src / "configure"),
             "--prefix=%s" % self.prefix,
             "--disable-shared", "--enable-static",
             "--disable-libxml2", "--disable-cairo",
             "--disable-pci", "--disable-io", "--disable-libudev"],
            cwd=src, env=self.env, log=self.log("hwloc"))
        run(["make", "-j%d" % self.jobs], cwd=src, env=self.env,
            log=self.log("hwloc"))
        run(["make", "install"], cwd=src, env=self.env, log=self.log("hwloc"))

    # -- GKlib / METIS -----------------------------------------------------
    def gklib_metis(self) -> None:
        if not (self.prefix / "lib" / "libmetis.a").exists():
            src = self.source("gklib")
            build = self.work / "build" / "gklib"
            self.cmake_configure(src, build, [
                "-DCMAKE_INSTALL_PREFIX=%s" % self.prefix,
                "-DSHARED=0", "-DCMAKE_POLICY_VERSION_MINIMUM=3.5",
            ], "gklib")
            self.cmake_build(build, "gklib", install=True)

            metis = self.source("metis")
            xinclude = ensure_dir(metis / "build" / "xinclude")
            write_text(xinclude / "metis.h",
                       "#define IDXTYPEWIDTH 32\n#define REALTYPEWIDTH 32\n" +
                       read_text(metis / "include" / "metis.h"))
            shutil.copy2(metis / "include" / "CMakeLists.txt",
                         xinclude / "CMakeLists.txt")
            build = self.work / "build" / "metis"
            self.cmake_configure(metis, build, [
                "-DCMAKE_INSTALL_PREFIX=%s" % self.prefix,
                "-DGKLIB_PATH=%s" % self.prefix,
                "-DSHARED=0", "-DCMAKE_POLICY_VERSION_MINIMUM=3.5",
            ], "metis")
            self.cmake_build(build, "metis", install=True)

        merged = self.prefix / "lib" / "libmetis-merged.a"
        marker = self.prefix / "lib" / ".metis-merged-ok"
        if not merged.exists() or not marker.exists():
            split = self.prefix / "lib" / "libmetis-split.a"
            if not split.exists():
                shutil.copy2(
                    self.prefix / "lib" / "libmetis.a", split)
            gklib = self.prefix / "lib" / "libGKlib.a"
            merge_static_archives(
                self.ar,
                self.work / "metis-merge",
                (("metis", split), ("gklib", gklib)),
                merged,
                log=self.log("metis-merge"),
                libtool=(self.libtool if self.platform.is_macos else None),
            )
            write_text(marker, "ok\n")
            shutil.copy2(merged, self.prefix / "lib" / "libmetis.a")

    # -- PaStiX 6 ----------------------------------------------------------
    def pastix6(self) -> None:
        if (self.prefix / "lib" / "libpastix.a").exists():
            return
        src = self.source("pastix6")
        # Fix the pinned MORSE CMake modules: FindMETIS expects M_LIBRARIES,
        # but FindM only exports M_LIBRARY/MORSE::M.  Without this, the
        # METIS_NodeND probe silently drops libm even when it was found.
        patch_pastix_metis_find_module(src)
        # PaStiX 6.3.2's no-hwloc fallback calls an undeclared
        # ``pastix_warning`` helper.  The public helper is
        # ``pastix_print_warning`` from common/out.h.
        nohwloc = src / "common" / "isched_nohwloc.c"
        if nohwloc.exists():
            text = nohwloc.read_text(encoding="utf-8", errors="replace")
            if "pastix_warning(" in text:
                nohwloc.write_text(text.replace("pastix_warning(", "pastix_print_warning("),
                                   encoding="utf-8")
        build = self.work / "build" / "pastix6"
        self.cmake_configure(src, build, [
            "-DCMAKE_INSTALL_PREFIX=%s" % self.prefix,
            "-DCMAKE_BUILD_TYPE=Release",
            "-DCMAKE_POLICY_VERSION_MINIMUM=3.5",
            "-DBUILD_SHARED_LIBS=OFF",
            "-DPASTIX_WITH_MPI=ON",
            "-DPASTIX_WITH_FORTRAN=OFF",
            "-DSPM_BUILD_FORTRAN=OFF",
            "-DSPM_WITH_FORTRAN=OFF",
            "-DPASTIX_INT64=OFF",
            "-DPASTIX_ORDERING_METIS=ON",
            "-DPASTIX_ORDERING_SCOTCH=OFF",
            "-DPASTIX_WITH_CUDA=OFF",
            "-DPASTIX_WITH_STARPU=OFF",
            "-DPASTIX_WITH_PARSEC=OFF",
            "-DPASTIX_GENERATE_MODEL=OFF",
            "-DCMAKE_C_COMPILER=%s" % self.mpicc,
            "-DCMAKE_Fortran_COMPILER=%s" % self.mpifc,
            "-DBLAS_LIBRARIES=%s" % (self.core / "lib" / "libopenblas.a"),
            "-DLAPACK_LIBRARIES=%s" % (self.core / "lib" / "libopenblas.a"),
            "-DBLA_VENDOR=OpenBLAS",
            "-DMETIS_LIBRARIES=%s" % (self.prefix / "lib" / "libmetis.a"),
            "-DMETIS_INCLUDE_DIRS=%s" % (self.prefix / "include"),
            "-DHWLOC_DIR=%s" % self.prefix,
            "-DHWLOC_LIBRARIES=%s;-lm" % (self.prefix / "lib" / "libhwloc.a"),
            "-DCMAKE_PREFIX_PATH=%s;%s" % (self.prefix, self.core),
        ], "pastix6")
        for target in ("spm", "pastix_kernels", "pastix"):
            self.cmake_build(build, "pastix6", target=target)
        # The upstream install target also builds examples, which pull in
        # optional SPM test drivers.  Install the library and public headers
        # needed by JOREK directly.
        ensure_dir(self.prefix / "lib")
        ensure_dir(self.prefix / "include")
        for rel, name in (
                ("libpastix.a", "libpastix.a"),
                ("kernels/libpastix_kernels.a", "libpastix_kernels.a"),
                ("spm/src/libspm.a", "libspm.a")):
            path = build / rel
            if path.exists():
                shutil.copy2(path, self.prefix / "lib" / name)
        shutil.copy2(src / "include" / "pastix.h", self.prefix / "include" / "pastix.h")
        pastix_inc = self.prefix / "include" / "pastix"
        if (src / "include" / "pastix").exists():
            shutil.copytree(src / "include" / "pastix", pastix_inc, dirs_exist_ok=True)
        config = build / "include" / "pastix" / "config.h"
        if config.exists():
            ensure_dir(self.prefix / "include" / "pastix")
            shutil.copy2(config, self.prefix / "include" / "pastix" / "config.h")
        shutil.copy2(src / "spm" / "include" / "spm.h", self.prefix / "include" / "spm.h")
        spm_inc = self.prefix / "include" / "spm"
        if spm_inc.exists():
            shutil.rmtree(spm_inc)
        shutil.copytree(src / "spm" / "include" / "spm", spm_inc)
        generated = build / "spm" / "include" / "spm"
        if generated.exists():
            for header in generated.iterdir():
                if header.is_file():
                    shutil.copy2(header, spm_inc / header.name)

    # -- STRUMPACK ---------------------------------------------------------
    def strumpack(self) -> None:
        if (self.prefix / "lib" / "libstrumpack.a").exists():
            return
        src = self.source("strumpack")
        build = self.work / "build" / "strumpack"
        blas = self.core / "lib" / "libopenblas.a"
        scalapack = self.core / "lib" / "libscalapack.a"
        self.cmake_configure(src, build, [
            "-DCMAKE_INSTALL_PREFIX=%s" % self.prefix,
            "-DCMAKE_BUILD_TYPE=Release",
            "-DBUILD_SHARED_LIBS=OFF",
            "-DSTRUMPACK_USE_MPI=ON",
            "-DSTRUMPACK_USE_OPENMP=ON",
            "-DTPL_ENABLE_SLATE=OFF",
            "-DTPL_ENABLE_PARMETIS=OFF",
            "-DTPL_ENABLE_SCOTCH=OFF",
            "-DTPL_ENABLE_PTSCOTCH=OFF",
            "-DTPL_ENABLE_BPACK=OFF",
            "-DTPL_ENABLE_ZFP=OFF",
            "-DTPL_METIS_PREFIX=%s" % self.prefix,
            "-DTPL_BLAS_LIBRARIES=%s" % blas,
            "-DTPL_LAPACK_LIBRARIES=%s" % blas,
            "-DTPL_SCALAPACK_LIBRARIES=%s" % scalapack,
            "-DCMAKE_C_COMPILER=%s" % self.cc,
            "-DCMAKE_CXX_COMPILER=%s" % self.cxx,
            "-DCMAKE_Fortran_COMPILER=%s" % self.fc,
            "-DCMAKE_PREFIX_PATH=%s;%s" % (self.prefix, self.core),
        ], "strumpack")
        self.cmake_build(build, "strumpack", target="strumpack")
        ensure_dir(self.prefix / "lib")
        ensure_dir(self.prefix / "include")
        shutil.copy2(build / "libstrumpack.a", self.prefix / "lib" / "libstrumpack.a")
        for name in ("StrumpackConfig.h",):
            path = build / name
            if path.exists():
                shutil.copy2(path, self.prefix / "include" / name)
        for entry in (src / "src").iterdir():
            if entry.is_file() and entry.suffix in (".h", ".hpp"):
                shutil.copy2(entry, self.prefix / "include" / entry.name)
            elif entry.is_dir() and entry.name not in ("test", "examples"):
                target = self.prefix / "include" / entry.name
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(entry, target)

    # -- PaStiX 5 ----------------------------------------------------------
    def pastix5(self) -> None:
        prefix = self.prefix / "pastix5"
        if (prefix / "lib" / "libpastix.a").exists():
            return
        ensure_dir(prefix)
        src = self.source("pastix5")
        root = src / "src" if (src / "src" / "CMakeLists.txt").exists() else src
        stub = root / "sopalin" / "src" / "murge_fortran.c"
        if not stub.exists():
            ensure_dir(stub.parent)
            write_text(stub, "int pastix_murge_dummy;\n")
        build = self.work / "build" / "pastix5"
        self.cmake_configure(root, build, [
            "-DCMAKE_INSTALL_PREFIX=%s" % prefix,
            "-DCMAKE_BUILD_TYPE=Release",
            "-DCMAKE_POLICY_VERSION_MINIMUM=3.5",
            "-DBUILD_SHARED_LIBS=OFF",
            "-DPASTIX_WITH_MPI=ON",
            "-DPASTIX_WITH_MULTITHREAD=ON",
            "-DPASTIX_INT64=OFF",
            "-DPASTIX_ORDERING_METIS=ON",
            "-DPASTIX_ORDERING_SCOTCH=OFF",
            "-DPASTIX_ORDERING_PTSCOTCH=OFF",
            "-DPASTIX_DISTRIBUTED=OFF",
            "-DPASTIX_BUILD_EXAMPLES=OFF",
            "-DCMAKE_C_FLAGS=-Wno-implicit-function-declaration",
            "-DCMAKE_C_COMPILER=%s" % self.mpicc,
            "-DCMAKE_Fortran_COMPILER=%s" % self.mpifc,
            "-DBLAS_LIBRARIES=%s" % (self.core / "lib" / "libopenblas.a"),
            "-DLAPACK_LIBRARIES=%s" % (self.core / "lib" / "libopenblas.a"),
            "-DBLA_VENDOR=OpenBLAS",
            "-DMETIS_LIBRARIES=%s;-lm" % (self.prefix / "lib" / "libmetis.a"),
            "-DMETIS_INCLUDE_DIRS=%s" % (self.prefix / "include"),
            "-DHWLOC_DIR=%s" % self.prefix,
            "-DHWLOC_LIBRARIES=%s;-lm" % (self.prefix / "lib" / "libhwloc.a"),
            "-DCMAKE_PREFIX_PATH=%s;%s" % (self.prefix, self.core),
        ], "pastix5")
        self.cmake_build(build, "pastix5", target="pastix")
        # Do not run the PaStiX install target: it pulls the matrix drivers,
        # which require SPM headers that the 5.x release does not ship.  JOREK
        # only needs the library and the Fortran interface header.
        ensure_dir(prefix / "lib")
        ensure_dir(prefix / "include")
        shutil.copy2(build / "libpastix.a", prefix / "lib" / "libpastix.a")
        murge = build / "libpastix_murge.a"
        if murge.exists():
            shutil.copy2(murge, prefix / "lib" / "libpastix_murge.a")
        header_top = build / "sopalin" / "src" / "pastix_fortran.h.top"
        header_inc = root / "common" / "src" / "pastix_fortran.inc"
        if header_top.exists() and header_inc.exists():
            (prefix / "include" / "pastix_fortran.h").write_text(
                header_top.read_text(encoding="utf-8", errors="replace")
                + "#define MURGE_COEF_KIND 8\n"
                + "#define MURGE_REAL_KIND 8\n"
                + "#define MURGE_INTS_KIND 4\n"
                + "#define MURGE_INTL_KIND 8\n"
                + header_inc.read_text(encoding="utf-8", errors="replace"),
                encoding="utf-8")
        else:
            shutil.copy2(root / "common" / "src" / "pastix_fortran.h",
                         prefix / "include" / "pastix_fortran.h")

    def build(self) -> None:
        stages = [
            ("hwloc", "Build hwloc", self.hwloc),
            ("gklib-metis", "Build GKlib and METIS", self.gklib_metis),
            ("pastix6", "Build PaStiX 6", self.pastix6),
            ("strumpack", "Build STRUMPACK", self.strumpack),
            ("pastix5", "Build PaStiX 5", self.pastix5),
        ]
        emit_build_event(
            "phase_start", phase="solvers", label="Solver backends",
            total=len(stages))
        emit_build_event(
            "stage_plan", phase="solvers",
            stages=[{
                "id": "solver:" + name,
                "label": label,
                "category": "Solver",
                "log_path": str(self.log(name)),
            } for name, label, _builder in stages])
        completed = 0
        for name, label, builder in stages:
            stage_id = "solver:" + name
            emit_build_event(
                "stage_start", id=stage_id, label=label,
                category="Solver", phase="solvers",
                log_path=str(self.log(name)))
            status = "completed"
            error = ""
            try:
                builder()
                self.verify_stage_artifacts(name)
            except Exception as exc:
                status = "failed"
                error = str(exc)
                raise
            finally:
                emit_build_event(
                    "stage_finish", id=stage_id, status=status, error=error)
                completed += 1
                emit_build_event(
                    "phase_progress", phase="solvers",
                    completed=completed, total=len(stages))
