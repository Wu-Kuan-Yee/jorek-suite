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
from pathlib import Path
from typing import List, Optional

from common import (
    BuildError,
    Downloader,
    PlatformInfo,
    ensure_dir,
    extract_archive,
    read_text,
    run,
    write_text,
)


class SolverBuild:
    def __init__(self, prefix: Path, core_prefix: Path, work: Path, jobs: int,
                 platform: PlatformInfo, downloader: Downloader,
                 cc: str, cxx: str, fc: str, mpicc: str, mpicxx: str, mpifc: str,
                 log_dir: Path):
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
        cmake_path = shutil.which("cmake", path=str(self.core / "bin") + os.pathsep + os.environ.get("PATH", ""))
        self.cmake = cmake_path or "cmake"
        self.ar = shutil.which("ar") or "ar"
        self.libtool = shutil.which("libtool")

    def log(self, name: str) -> Path:
        return self.log_dir / ("solver-" + name + ".log")

    def fetch(self, url: str, filename: str) -> Path:
        return self.downloader.fetch(url, filename)

    def source(self, url: str, filename: str, directory: str) -> Path:
        archive = self.fetch(url, filename)
        dest = ensure_dir(self.work / "src" / directory)
        root = extract_archive(archive, dest)
        return root

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
        return env

    # -- GKlib / METIS -----------------------------------------------------
    def gklib_metis(self) -> None:
        if not (self.prefix / "lib" / "libmetis.a").exists():
            src = self.source(
                "https://github.com/KarypisLab/GKlib/archive/refs/heads/master.tar.gz",
                "gklib-master.tar.gz", "gklib")
            build = self.work / "build" / "gklib"
            self.cmake_configure(src, build, [
                "-DCMAKE_INSTALL_PREFIX=%s" % self.prefix,
                "-DSHARED=0", "-DCMAKE_POLICY_VERSION_MINIMUM=3.5",
            ], "gklib")
            self.cmake_build(build, "gklib", install=True)

            metis = self.source(
                "https://github.com/KarypisLab/METIS/archive/refs/tags/v5.2.1.tar.gz",
                "metis-5.2.1.tar.gz", "metis")
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
                split = self.prefix / "lib" / "libmetis.a"
                shutil.copy2(split, self.prefix / "lib" / "libmetis-split.a")
            gklib = self.prefix / "lib" / "libGKlib.a"
            if self.platform.is_macos and self.libtool:
                run([self.libtool, "-static", "-o", str(merged), str(split), str(gklib)],
                    log=self.log("metis-merge"))
            else:
                tmp = ensure_dir(self.work / "metis-merge")
                m = ensure_dir(tmp / "metis")
                g = ensure_dir(tmp / "gklib")
                run([self.ar, "x", str(split)], cwd=m, log=self.log("metis-merge"))
                run([self.ar, "x", str(gklib)], cwd=g, log=self.log("metis-merge"))
                objects = []
                for directory, tag in ((m, "metis_"), (g, "gklib_")):
                    for obj in directory.glob("*.o"):
                        target = tmp / (tag + obj.name)
                        shutil.copy2(obj, target)
                        objects.append(str(target))
                run([self.ar, "rcs", str(merged)] + objects, log=self.log("metis-merge"))
            write_text(marker, "ok\n")
            shutil.copy2(merged, self.prefix / "lib" / "libmetis.a")

    # -- PaStiX 6 ----------------------------------------------------------
    def pastix6(self) -> None:
        if (self.prefix / "lib" / "libpastix.a").exists():
            return
        src = self.source(
            "https://gitlab.inria.fr/solverstack/pastix/-/archive/v6.3.2/pastix-v6.3.2.tar.gz",
            "pastix-6.3.2.tar.gz", "pastix6")
        spm = src / "spm"
        morse = src / "cmake_modules" / "morse_cmake"
        if not (spm / "CMakeLists.txt").exists():
            spm_src = self.source(
                "https://gitlab.inria.fr/solverstack/spm/-/archive/master/spm-master.tar.gz",
                "spm-master.tar.gz", "spm")
            shutil.copytree(spm_src, spm, dirs_exist_ok=True)
        if not (morse / "modules" / "MorseInit.cmake").exists():
            morse_src = self.source(
                "https://gitlab.inria.fr/solverstack/morse_cmake/-/archive/master/morse_cmake-master.tar.gz",
                "morse-master.tar.gz", "morse")
            shutil.copytree(morse_src, morse, dirs_exist_ok=True)
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
            "-DMETIS_LIBRARIES=%s" % (self.prefix / "lib" / "libmetis.a"),
            "-DMETIS_INCLUDE_DIRS=%s" % (self.prefix / "include"),
            "-DCMAKE_PREFIX_PATH=%s;%s" % (self.prefix, self.core),
        ], "pastix6")
        self.cmake_build(build, "pastix6", install=True)

    # -- STRUMPACK ---------------------------------------------------------
    def strumpack(self) -> None:
        if (self.prefix / "lib" / "libstrumpack.a").exists():
            return
        src = self.source(
            "https://github.com/pghysels/STRUMPACK/archive/v7.1.0.tar.gz",
            "strumpack-7.1.0.tar.gz", "strumpack")
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
        src = self.source(
            "http://sources.easybuild.io/p/PaStiX/pastix_5.2.3.tar.bz2",
            "pastix-5.2.3.tar.bz2", "pastix5")
        stub = src / "sopalin" / "src" / "murge_fortran.c"
        if not stub.exists():
            write_text(stub, "int pastix_murge_dummy;\n")
        build = self.work / "build" / "pastix5"
        self.cmake_configure(src / "src" if (src / "src" / "CMakeLists.txt").exists() else src,
                             build, [
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
            "-DMETIS_LIBRARIES=%s" % (self.prefix / "lib" / "libmetis.a"),
            "-DMETIS_INCLUDE_DIRS=%s" % (self.prefix / "include"),
            "-DCMAKE_PREFIX_PATH=%s;%s" % (self.prefix, self.core),
        ], "pastix5")
        self.cmake_build(build, "pastix5", target="pastix", install=True)

    def build(self) -> None:
        self.gklib_metis()
        self.pastix6()
        self.strumpack()
        self.pastix5()
