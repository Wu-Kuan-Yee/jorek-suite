#!/usr/bin/env python3
"""Third-party dependency graph and source builders for the JOREK suite.

Everything here is intentionally data-driven.  A dependency is described by a
small record (version, URL, dependencies, install artefacts) plus a builder
name.  The generic builders cover the three build systems used by the JOREK
stack: Autoconf, CMake, and hand-written Makefiles.  A handful of packages
whose build systems are genuinely unusual (GCC, OpenBLAS, MUMPS, ScaLAPACK,
Boost, Dierckx) have dedicated routines.

The module does not import anything outside the Python standard library and
never assumes that a package manager exists.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from common import (
    BuildError,
    CommandError,
    Downloader,
    PlatformInfo,
    ensure_dir,
    extract_archive,
    human_bytes,
    output,
    read_text,
    run,
    sha256_file,
    which,
    write_text,
)


# ---------------------------------------------------------------------------
# Dependency records
# ---------------------------------------------------------------------------
@dataclass
class Dependency:
    name: str
    version: str
    url: str
    sha256: str = ""
    license: str = ""
    homepage: str = ""
    deps: Tuple[str, ...] = ()
    builder: str = "autotools"
    configure_args: Tuple[str, ...] = ()
    cmake_args: Tuple[str, ...] = ()
    make_args: Tuple[str, ...] = ()
    make_targets: Tuple[str, ...] = ()
    install: bool = True
    static_libs: Tuple[str, ...] = ()
    includes: Tuple[str, ...] = ()
    tools: Tuple[str, ...] = ()
    platforms: Tuple[str, ...] = ()
    optional: bool = False
    note: str = ""
    manual: bool = False
    archive: str = "auto"

    def supports(self, platform: PlatformInfo) -> bool:
        return not self.platforms or platform.system in self.platforms


def dep(name: str, version: str, url: str, **kwargs) -> Dependency:
    return Dependency(name=name, version=version, url=url, **kwargs)


DEPENDENCIES: Dict[str, Dependency] = {}


def register(*items: Dependency) -> None:
    for item in items:
        if item.name in DEPENDENCIES:
            raise RuntimeError("duplicate dependency record: %s" % item.name)
        DEPENDENCIES[item.name] = item


# --- bootstrap tools -------------------------------------------------------
register(
    dep("gmake", "4.4.1", "https://ftp.gnu.org/gnu/make/make-4.4.1.tar.gz",
        license="GPL-3.0-or-later", homepage="https://www.gnu.org/software/make/",
        tools=("gmake", "make"),
        configure_args=("--disable-nls",), builder="autotools",
        note="Only built when the host does not provide a POSIX make."),
    dep("m4", "1.4.19", "https://ftp.gnu.org/gnu/m4/m4-1.4.19.tar.xz",
        license="GPL-3.0-or-later", homepage="https://www.gnu.org/software/m4/",
        tools=("m4",), builder="autotools", configure_args=("--disable-nls",)),
    dep("perl", "5.40.0", "https://www.cpan.org/src/5.0/perl-5.40.0.tar.gz",
        license="Artistic-1.0-Perl OR GPL-1.0-or-later", homepage="https://www.perl.org/",
        tools=("perl",), builder="perl", note="Needed by OpenSSL, Automake, and CTest."),
    dep("gmp", "6.3.0", "https://ftp.gnu.org/gnu/gmp/gmp-6.3.0.tar.xz",
        license="LGPL-3.0-or-later OR GPL-2.0-or-later", homepage="https://gmplib.org/",
        static_libs=("libgmp.a",), includes=("gmp.h",), builder="autotools",
        configure_args=("--disable-shared", "--enable-static", "--with-pic")),
    dep("mpfr", "4.2.1", "https://ftp.gnu.org/gnu/mpfr/mpfr-4.2.1.tar.xz",
        license="LGPL-3.0-or-later", homepage="https://www.mpfr.org/",
        deps=("gmp",), static_libs=("libmpfr.a",), includes=("mpfr.h",), builder="autotools",
        configure_args=("--disable-shared", "--enable-static", "--with-pic")),
    dep("mpc", "1.3.1", "https://ftp.gnu.org/gnu/mpc/mpc-1.3.1.tar.gz",
        license="LGPL-3.0-or-later", homepage="https://www.multiprecision.org/mpc/",
        deps=("gmp", "mpfr"), static_libs=("libmpc.a",), includes=("mpc.h",), builder="autotools",
        configure_args=("--disable-shared", "--enable-static", "--with-pic")),
    dep("isl", "0.27", "https://libisl.sourceforge.io/isl-0.27.tar.xz",
        license="MIT", homepage="https://libisl.sourceforge.io/",
        deps=("gmp",), static_libs=("libisl.a",), includes=("isl/isl.h",), builder="autotools",
        configure_args=("--disable-shared", "--enable-static", "--with-pic")),
    dep("binutils", "2.43.1", "https://ftp.gnu.org/gnu/binutils/binutils-2.43.1.tar.xz",
        license="GPL-3.0-or-later", homepage="https://www.gnu.org/software/binutils/",
        deps=("gmp",), tools=("ar", "ranlib", "ld", "nm", "objcopy", "objdump", "strip"),
        configure_args=("--disable-nls", "--disable-shared", "--enable-static",
                        "--disable-werror", "--enable-targets=all"),
        builder="autotools", platforms=("linux", "bsd", "macos"),
        note="The host linker is used for ordinary builds; binutils is a fallback."),
    dep("gcc", "14.2.0", "https://ftp.gnu.org/gnu/gcc/gcc-14.2.0/gcc-14.2.0.tar.xz",
        license="GPL-3.0-or-later", homepage="https://gcc.gnu.org/",
        deps=("gmp", "mpfr", "mpc", "isl"),
        tools=("gcc", "g++", "gfortran", "cpp"), static_libs=("libgfortran.a", "libgcc.a", "libstdc++.a"),
        builder="gcc", platforms=("linux", "bsd", "macos"),
        note="Built from source when no suitable Fortran compiler is available."),
    dep("pkgconf", "2.3.0", "https://distfiles.ariadne.space/pkgconf/pkgconf-2.3.0.tar.xz",
        license="ISC", homepage="https://pkgconf.org/", tools=("pkgconf", "pkg-config"),
        configure_args=("--disable-shared", "--enable-static"), builder="autotools"),
    dep("cmake", "3.31.6", "https://github.com/Kitware/CMake/releases/download/v3.31.6/cmake-3.31.6.tar.gz",
        license="BSD-3-Clause", homepage="https://cmake.org/", tools=("cmake", "ctest", "cpack"),
        builder="cmake_bootstrap", platforms=("linux", "bsd", "macos"),
        note="Bootstrapped with the host C++ compiler; no binary wheel is used."),
    dep("bison", "3.8.2", "https://ftp.gnu.org/gnu/bison/bison-3.8.2.tar.xz",
        license="GPL-3.0-or-later", homepage="https://www.gnu.org/software/bison/",
        deps=("m4",), tools=("bison", "yacc"), configure_args=("--disable-nls",), builder="autotools"),
    dep("flex", "2.6.4", "https://github.com/westes/flex/releases/download/v2.6.4/flex-2.6.4.tar.gz",
        license="BSD-2-Clause", homepage="https://github.com/westes/flex",
        deps=("m4",), tools=("flex", "lex"), configure_args=("--disable-nls",), builder="autotools"),
    dep("gperf", "3.1", "https://ftp.gnu.org/gnu/gperf/gperf-3.1.tar.gz",
        license="GPL-3.0-or-later", homepage="https://www.gnu.org/software/gperf/",
        tools=("gperf",), configure_args=("--disable-nls",), builder="autotools"),
    dep("autoconf", "2.72", "https://ftp.gnu.org/gnu/autoconf/autoconf-2.72.tar.xz",
        license="GPL-3.0-or-later", homepage="https://www.gnu.org/software/autoconf/",
        deps=("m4", "perl"), tools=("autoconf", "autoheader", "autom4te"),
        builder="autotools", configure_args=("--disable-nls",)),
    dep("automake", "1.17", "https://ftp.gnu.org/gnu/automake/automake-1.17.tar.xz",
        license="GPL-3.0-or-later", homepage="https://www.gnu.org/software/automake/",
        deps=("autoconf", "perl"), tools=("automake", "aclocal"), builder="autotools",
        configure_args=("--disable-nls",)),
    dep("libtool", "2.5.4", "https://ftp.gnu.org/gnu/libtool/libtool-2.5.4.tar.xz",
        license="GPL-2.0-or-later", homepage="https://www.gnu.org/software/libtool/",
        deps=("m4", "perl"), tools=("libtool", "libtoolize"), builder="autotools",
        configure_args=("--disable-nls",)),
    dep("texinfo", "7.1", "https://ftp.gnu.org/gnu/texinfo/texinfo-7.1.tar.xz",
        license="GPL-3.0-or-later", homepage="https://www.gnu.org/software/texinfo/",
        deps=("perl",), tools=("makeinfo", "texi2any", "install-info"), builder="autotools",
        configure_args=("--disable-nls",)),
    dep("help2man", "1.49.3", "https://ftp.gnu.org/gnu/help2man/help2man-1.49.3.tar.xz",
        license="GPL-3.0-or-later", homepage="https://www.gnu.org/software/help2man/",
        deps=("perl",), tools=("help2man",), builder="autotools", configure_args=("--disable-nls",)),
    dep("zlib", "1.3.1", "https://zlib.net/fossils/zlib-1.3.1.tar.gz",
        license="Zlib", homepage="https://zlib.net/", static_libs=("libz.a",),
        includes=("zlib.h", "zconf.h"), builder="cmake",
        cmake_args=("-DBUILD_SHARED_LIBS=OFF", "-DZLIB_BUILD_SHARED=OFF",
                    "-DZLIB_BUILD_EXAMPLES=OFF")),
    dep("libaec", "1.1.3", "https://github.com/MathisRosenhauer/libaec/releases/download/v1.1.3/libaec-1.1.3.tar.gz",
        license="BSD-2-Clause", homepage="https://github.com/MathisRosenhauer/libaec",
        static_libs=("libaec.a", "libsz.a"), includes=("libaec.h", "szlib.h"), builder="cmake",
        cmake_args=("-DBUILD_SHARED_LIBS=OFF", "-DAEC_ENABLE_TESTS=OFF")),
    dep("zstd", "1.5.7", "https://github.com/facebook/zstd/releases/download/v1.5.7/zstd-1.5.7.tar.gz",
        license="BSD-3-Clause OR GPL-2.0-only", homepage="https://github.com/facebook/zstd",
        static_libs=("libzstd.a",), includes=("zstd.h",), builder="gnu_make",
        make_args=("lib-release",), install=False, tools=("zstd",)),
    dep("xz", "5.6.4", "https://tukaani.org/xz/xz-5.6.4.tar.gz",
        license="0BSD AND LGPL-2.1-or-later AND GPL-2.0-or-later", homepage="https://tukaani.org/xz/",
        static_libs=("liblzma.a",), includes=("lzma.h",), builder="autotools",
        configure_args=("--disable-shared", "--enable-static", "--disable-nls")),
    dep("bzip2", "1.0.8", "https://sourceware.org/pub/bzip2/bzip2-1.0.8.tar.gz",
        license="BSD-4-Clause", homepage="https://sourceware.org/bzip2/", builder="bzip2",
        static_libs=("libbz2.a",), includes=("bzlib.h",), tools=("bzip2", "bunzip2")),
    dep("openssl", "3.0.16", "https://www.openssl.org/source/openssl-3.0.16.tar.gz",
        license="Apache-2.0", homepage="https://www.openssl.org/", deps=("perl",),
        static_libs=("libssl.a", "libcrypto.a"), includes=("openssl/ssl.h",), builder="openssl",
        configure_args=("no-shared", "no-tests"), tools=("openssl",)),
    dep("sqlite", "3.47.2", "https://www.sqlite.org/2024/sqlite-autoconf-3470200.tar.gz",
        license="Public-Domain", homepage="https://sqlite.org/", static_libs=("libsqlite3.a",),
        includes=("sqlite3.h",), builder="autotools",
        configure_args=("--disable-shared", "--enable-static", "--disable-readline")),
    dep("ncurses", "6.5", "https://ftp.gnu.org/gnu/ncurses/ncurses-6.5.tar.gz",
        license="X11-style", homepage="https://invisible-island.net/ncurses/",
        static_libs=("libncurses.a", "libtinfo.a"), includes=("ncurses.h",), builder="autotools",
        configure_args=("--disable-shared", "--enable-static", "--with-normal", "--without-debug",
                        "--without-ada", "--without-tests", "--without-manpages")),
    dep("readline", "8.2", "https://ftp.gnu.org/gnu/readline/readline-8.2.tar.gz",
        license="GPL-3.0-or-later", homepage="https://tiswww.case.edu/php/chet/readline/rltop.html",
        deps=("ncurses",), static_libs=("libreadline.a",), includes=("readline/readline.h",),
        builder="autotools", configure_args=("--disable-shared", "--enable-static")),
    dep("libiconv", "1.18", "https://ftp.gnu.org/gnu/libiconv/libiconv-1.18.tar.gz",
        license="LGPL-2.1-or-later", homepage="https://www.gnu.org/software/libiconv/",
        static_libs=("libiconv.a",), includes=("iconv.h",), builder="autotools",
        configure_args=("--disable-shared", "--enable-static", "--disable-nls")),
    dep("gettext", "0.23.1", "https://ftp.gnu.org/gnu/gettext/gettext-0.23.1.tar.gz",
        license="GPL-3.0-or-later", homepage="https://www.gnu.org/software/gettext/",
        deps=("libiconv", "ncurses", "perl"), static_libs=("libintl.a", "libgettextlib.a"),
        includes=("libintl.h",), builder="autotools",
        configure_args=("--disable-shared", "--enable-static", "--disable-nls")),
    dep("libffi", "3.4.6", "https://github.com/libffi/libffi/releases/download/v3.4.6/libffi-3.4.6.tar.gz",
        license="MIT", homepage="https://github.com/libffi/libffi", static_libs=("libffi.a",),
        includes=("ffi.h",), builder="autotools",
        configure_args=("--disable-shared", "--enable-static", "--disable-docs")),
    dep("libxml2", "2.13.5", "https://download.gnome.org/sources/libxml2/2.13/libxml2-2.13.5.tar.xz",
        license="MIT", homepage="https://gitlab.gnome.org/GNOME/libxml2", deps=("zlib", "libiconv"),
        static_libs=("libxml2.a",), includes=("libxml/parser.h",), builder="cmake",
        cmake_args=("-DBUILD_SHARED_LIBS=OFF", "-DLIBXML2_WITH_PYTHON=OFF",
                    "-DLIBXML2_WITH_LZMA=OFF", "-DLIBXML2_WITH_ZLIB=ON", "-DLIBXML2_WITH_ICONV=ON")),
    dep("util-linux", "2.40.4", "https://www.kernel.org/pub/linux/utils/util-linux/v2.40/util-linux-2.40.4.tar.xz",
        license="GPL-2.0-or-later AND LGPL-2.1-or-later", homepage="https://util-linux.github.io/",
        static_libs=("libuuid.a",), includes=("uuid/uuid.h",), builder="autotools",
        configure_args=("--disable-shared", "--enable-static", "--disable-all-programs",
                        "--enable-libuuid", "--without-python", "--disable-nls"),
        platforms=("linux", "bsd")),
    dep("libunistring", "1.3", "https://ftp.gnu.org/gnu/libunistring/libunistring-1.3.tar.gz",
        license="LGPL-3.0-or-later", homepage="https://www.gnu.org/software/libunistring/",
        static_libs=("libunistring.a",), includes=("unistr.h",), builder="autotools",
        configure_args=("--disable-shared", "--enable-static", "--disable-nls")),
    dep("libatomic_ops", "7.8.2", "https://github.com/ivmai/libatomic_ops/releases/download/v7.8.2/libatomic_ops-7.8.2.tar.gz",
        license="GPL-2.0-or-later AND MIT", homepage="https://github.com/ivmai/libatomic_ops",
        static_libs=("libatomic_ops.a",), includes=("atomic_ops.h",), builder="autotools",
        configure_args=("--disable-shared", "--enable-static")),
    dep("bdw-gc", "8.2.8", "https://github.com/ivmai/bdwgc/releases/download/v8.2.8/gc-8.2.8.tar.gz",
        license="MIT", homepage="https://www.hboehm.info/gc/", deps=("libatomic_ops",),
        static_libs=("libgc.a",), includes=("gc.h",), builder="autotools",
        configure_args=("--disable-shared", "--enable-static", "--with-libatomic-ops=yes")),
    dep("guile", "3.0.10", "https://ftp.gnu.org/gnu/guile/guile-3.0.10.tar.gz",
        license="LGPL-3.0-or-later", homepage="https://www.gnu.org/software/guile/",
        deps=("gmp", "libunistring", "bdw-gc", "libffi", "libiconv", "readline"),
        static_libs=("libguile-3.0.a",), includes=("libguile.h",), builder="autotools",
        configure_args=("--disable-shared", "--enable-static", "--disable-nls")),
    dep("tcl", "8.6.16", "https://prdownloads.sourceforge.net/tcl/tcl8.6.16-src.tar.gz",
        license="BSD-style", homepage="https://www.tcl.tk/", static_libs=("libtcl8.6.a",),
        includes=("tcl.h",), builder="tcl", platforms=("linux", "bsd", "macos")),
    dep("expect", "5.45.4", "https://prdownloads.sourceforge.net/expect/expect5.45.4.tar.gz",
        license="Public-Domain", homepage="https://core.tcl-lang.org/expect/", deps=("tcl",),
        static_libs=("libexpect5.45.4.a",), builder="autotools", platforms=("linux", "bsd", "macos")),
    dep("dejagnu", "1.6.3", "https://ftp.gnu.org/gnu/dejagnu/dejagnu-1.6.3.tar.gz",
        license="GPL-3.0-or-later", homepage="https://www.gnu.org/software/dejagnu/",
        deps=("expect", "tcl"), builder="autotools", configure_args=("--disable-nls",)),
    dep("grep", "3.11", "https://ftp.gnu.org/gnu/grep/grep-3.11.tar.xz",
        license="GPL-3.0-or-later", homepage="https://www.gnu.org/software/grep/",
        tools=("grep",), builder="autotools", configure_args=("--disable-nls",)),
    dep("sed", "4.9", "https://ftp.gnu.org/gnu/sed/sed-4.9.tar.xz",
        license="GPL-3.0-or-later", homepage="https://www.gnu.org/software/sed/",
        tools=("sed",), builder="autotools", configure_args=("--disable-nls",)),
    dep("gawk", "5.3.1", "https://ftp.gnu.org/gnu/gawk/gawk-5.3.1.tar.xz",
        license="GPL-3.0-or-later", homepage="https://www.gnu.org/software/gawk/",
        deps=("mpfr", "gmp"), tools=("gawk", "awk"), builder="autotools",
        configure_args=("--disable-nls", "--disable-shared", "--enable-static")),
    dep("gdbm", "1.24", "https://ftp.gnu.org/gnu/gdbm/gdbm-1.24.tar.gz",
        license="GPL-3.0-or-later", homepage="https://www.gnu.org/software/gdbm/",
        static_libs=("libgdbm.a",), includes=("gdbm.h",), builder="autotools",
        configure_args=("--disable-shared", "--enable-static", "--disable-nls")),
)

# --- HPC stack -------------------------------------------------------------
register(
    dep("mpich", "4.3.0", "https://www.mpich.org/static/downloads/4.3.0/mpich-4.3.0.tar.gz",
        license="MPICH-license (BSD-like)", homepage="https://www.mpich.org/",
        static_libs=("libmpi.a", "libmpifort.a", "libmpicxx.a"),
        includes=("mpi.h", "mpif.h"), tools=("mpicc", "mpicxx", "mpifort", "mpif90", "mpirun", "mpiexec"),
        builder="mpich", platforms=("linux", "bsd", "macos", "windows")),
    dep("openmpi", "5.0.6", "https://download.open-mpi.org/release/open-mpi/v5.0/openmpi-5.0.6.tar.bz2",
        license="BSD-3-Clause", homepage="https://www.open-mpi.org/",
        deps=("hwloc",), static_libs=("libmpi.a", "libmpifort.a", "libmpi_cxx.a"),
        includes=("mpi.h", "mpif.h"), tools=("mpicc", "mpicxx", "mpifort", "mpirun"),
        builder="autotools", platforms=("linux", "bsd", "macos"),
        configure_args=("--disable-shared", "--enable-static", "--disable-dlopen",
                        "--disable-mca-dso", "--enable-mpi-fortran", "--without-slurm",
                        "--without-lsf", "--without-tm", "--without-sge", "--without-alps")),
    dep("hwloc", "2.11.2", "https://github.com/open-mpi/hwloc/releases/download/hwloc-2.11.2/hwloc-2.11.2.tar.gz",
        license="BSD-3-Clause", homepage="https://www.open-mpi.org/projects/hwloc/",
        static_libs=("libhwloc.a",), includes=("hwloc.h",), builder="autotools",
        configure_args=("--disable-shared", "--enable-static", "--disable-io", "--disable-libudev",
                        "--disable-cairo", "--disable-opencl", "--disable-pci")),
    dep("libpciaccess", "0.18.1", "https://xorg.freedesktop.org/releases/individual/lib/libpciaccess-0.18.1.tar.xz",
        license="MIT", homepage="https://gitlab.freedesktop.org/xorg/lib/libpciaccess",
        static_libs=("libpciaccess.a",), includes=("pciaccess.h",), builder="autotools",
        configure_args=("--disable-shared", "--enable-static")),
    dep("openblas", "0.3.30", "https://github.com/OpenMathLib/OpenBLAS/archive/refs/tags/v0.3.30.tar.gz",
        license="BSD-3-Clause", homepage="https://www.openblas.net/",
        static_libs=("libopenblas.a",), includes=("cblas.h", "lapacke.h"),
        builder="openblas", platforms=("linux", "bsd", "macos")),
    dep("lapack", "3.12.1", "https://github.com/Reference-LAPACK/lapack/archive/refs/tags/v3.12.1.tar.gz",
        license="BSD-3-Clause", homepage="https://www.netlib.org/lapack/",
        deps=("openblas",), static_libs=("liblapack.a", "libblas.a"),
        includes=("lapacke.h",), builder="cmake",
        cmake_args=("-DBUILD_SHARED_LIBS=OFF", "-DBUILD_TESTING=OFF",
                    "-DLAPACKE=ON", "-DCBLAS=ON", "-DBLAS_LIBRARIES=-lopenblas"),
        platforms=("linux", "bsd", "macos")),
    dep("scalapack", "2.2.0", "https://github.com/Reference-ScaLAPACK/scalapack/archive/refs/tags/v2.2.0.tar.gz",
        license="BSD-3-Clause", homepage="https://www.netlib.org/scalapack/",
        deps=("mpich", "openblas"), static_libs=("libscalapack.a",),
        builder="scalapack", platforms=("linux", "bsd", "macos")),
    dep("fftw", "3.3.10", "https://www.fftw.org/fftw-3.3.10.tar.gz",
        license="GPL-2.0-or-later", homepage="https://www.fftw.org/",
        static_libs=("libfftw3.a", "libfftw3_threads.a"),
        includes=("fftw3.h",), builder="autotools",
        configure_args=("--enable-shared=no", "--enable-static=yes", "--enable-threads",
                        "--disable-openmp", "--disable-fortran")),
    dep("hdf5", "1.14.6", "https://support.hdfgroup.org/releases/hdf5/v1_14/v1_14_6/downloads/hdf5-1.14.6.tar.gz",
        license="BSD-3-Clause", homepage="https://www.hdfgroup.org/solutions/hdf5/",
        deps=("mpich", "zlib", "libaec"), static_libs=("libhdf5.a", "libhdf5_fortran.a",
                                                       "libhdf5_hl.a", "libhdf5_hl_fortran.a"),
        includes=("hdf5.h", "H5FDmpi.h"), builder="hdf5",
        configure_args=("--enable-shared=no", "--enable-static=yes", "--enable-fortran",
                        "--enable-hl", "--enable-parallel", "--disable-tools", "--disable-tests",
                        "--disable-doxygen", "--disable-deprecated-symbols")),
    dep("metis", "5.2.1", "https://github.com/KarypisLab/METIS/archive/refs/tags/v5.2.1.tar.gz",
        license="Apache-2.0", homepage="https://github.com/KarypisLab/METIS",
        static_libs=("libmetis.a",), includes=("metis.h",), builder="metis"),
    dep("parmetis", "4.0.3", "https://github.com/KarypisLab/ParMETIS/archive/refs/tags/v4.0.3.tar.gz",
        license="ParMETIS-license (non-commercial)", homepage="https://github.com/KarypisLab/ParMETIS",
        deps=("metis", "mpich"), static_libs=("libparmetis.a",), includes=("parmetis.h",),
        builder="parmetis", note="The ParMETIS licence is not OSI-approved; review it before redistribution."),
    dep("scotch", "6.1.2", "https://gitlab.inria.fr/scotch/scotch/-/archive/v6.1.2/scotch-v6.1.2.tar.gz",
        license="CeCILL-C", homepage="https://www.labri.fr/perso/pelegrin/scotch/",
        deps=("mpich",), static_libs=("libscotch.a", "libscotcherr.a", "libesmumps.a",
                                         "libptscotch.a", "libptscotcherr.a", "libptesmumps.a"),
        includes=("scotch.h", "ptscotch.h"), builder="scotch"),
    dep("mumps", "5.7.3", "https://mumps-solver.org/MUMPS_5.7.3.tar.gz",
        license="CeCILL-C", homepage="http://mumps-solver.org/",
        deps=("scalapack",), static_libs=("libdmumps.a", "libzmumps.a",
                                                             "libmumps_common.a", "libpord.a"),
        includes=("dmumps_struc.h", "mumps_compat.h"), builder="mumps"),
    dep("pastix6", "6.3.2", "https://gitlab.inria.fr/solverstack/pastix/-/archive/v6.3.2/pastix-v6.3.2.tar.gz",
        license="CeCILL-C", homepage="https://solverstack.gitlabpages.inria.fr/pastix/",
        deps=("mpich", "openblas", "scalapack", "metis", "hwloc"),
        static_libs=("libpastix.a", "libspm.a", "libpastix_kernels.a"),
        includes=("pastix.h", "spm.h"),
        builder="pastix", platforms=("linux", "bsd", "macos")),
    dep("strumpack", "7.1.0", "https://github.com/pghysels/STRUMPACK/archive/v7.1.0.tar.gz",
        license="BSD-3-Clause", homepage="https://portal.nersc.gov/project/sparse/strumpack/",
        deps=("mpich", "openblas", "scalapack", "metis"),
        static_libs=("libstrumpack.a",), includes=("StrumpackConfig.h", "StrumpackSparseSolverMPIDist.hpp"),
        builder="strumpack", platforms=("linux", "bsd", "macos")),
    dep("boost", "1.87.0", "https://archives.boost.io/release/1.87.0/source/boost_1_87_0.tar.bz2",
        license="BSL-1.0", homepage="https://www.boost.org/",
        static_libs=("libboost_math_tr1.a",), includes=("boost/math/special_functions/bessel.hpp",),
        builder="boost"),
    dep("dierckx", "scipy-1.14.1", "https://github.com/scipy/scipy/archive/refs/tags/v1.14.1.tar.gz",
        license="BSD-3-Clause (Fitpack by P. Dierckx)", homepage="https://www.netlib.org/dierckx/",
        static_libs=("libdierckx.a",), builder="dierckx",
        note="Compiles the Fitpack sources shipped with SciPy; used by eqdsk2jorek."),
    dep("imas", "26.0.0", "https://github.com/iterorganization/IMAS-Core/archive/refs/tags/26.0.0.tar.gz",
        license="LGPL-3.0-or-later", homepage="https://github.com/iterorganization/IMAS-Core",
        deps=("mpich", "hdf5", "boost", "zeromq", "czmq", "libuuid", "sqlite", "libxml2",
              "openssl", "libffi", "python"),
        static_libs=("libimas.a", "libal.a", "libids.a"), includes=("imas.h", "ids_types.h"),
        builder="imas", optional=True,
        note="IMAS is a large optional data-dictionary layer; set JOREK_IMAS_ROOT to use a system build."),
    dep("catalyst", "5.13.2", "https://www.paraview.org/files/v5.13/ParaView-v5.13.2.tar.xz",
        license="BSD-3-Clause", homepage="https://www.paraview.org/",
        deps=("mpich", "hdf5", "boost", "fftw", "zlib", "libxml2"),
        static_libs=("libcatalyst.a",), includes=("catalyst/conduit_catalyst.h",),
        builder="catalyst", optional=True, platforms=("linux", "bsd", "macos"),
        note="ParaView Catalyst is huge; a system installation can be supplied with --catalyst-root."),
    dep("zeromq", "4.3.5", "https://github.com/zeromq/libzmq/releases/download/v4.3.5/zeromq-4.3.5.tar.gz",
        license="MPL-2.0", homepage="https://zeromq.org/", deps=("libsodium",),
        static_libs=("libzmq.a",), includes=("zmq.h",), builder="cmake",
        cmake_args=("-DBUILD_SHARED=OFF", "-DBUILD_STATIC=ON", "-DENABLE_DRAFTS=OFF",
                    "-DWITH_LIBSODIUM=ON")),
    dep("libsodium", "1.0.20", "https://github.com/jedisct1/libsodium/releases/download/1.0.20-RELEASE/libsodium-1.0.20.tar.gz",
        license="ISC", homepage="https://libsodium.org/", static_libs=("libsodium.a",),
        includes=("sodium.h",), builder="autotools",
        configure_args=("--disable-shared", "--enable-static")),
    dep("czmq", "4.2.1", "https://github.com/zeromq/czmq/releases/download/v4.2.1/czmq-4.2.1.tar.gz",
        license="MPL-2.0", homepage="https://github.com/zeromq/czmq", deps=("zeromq",),
        static_libs=("libczmq.a",), includes=("czmq.h",), builder="autotools",
        configure_args=("--disable-shared", "--enable-static")),
    dep("libuuid", "2.40.4", "https://www.kernel.org/pub/linux/utils/util-linux/v2.40/util-linux-2.40.4.tar.xz",
        license="BSD-3-Clause", homepage="https://github.com/util-linux/util-linux",
        static_libs=("libuuid.a",), includes=("uuid/uuid.h",), builder="autotools",
        configure_args=("--disable-shared", "--enable-static", "--disable-all-programs",
                        "--enable-libuuid", "--without-python", "--disable-nls"),
        platforms=("linux", "bsd", "macos")),
    dep("python", "3.12.8", "https://www.python.org/ftp/python/3.12.8/Python-3.12.8.tar.xz",
        license="PSF-2.0", homepage="https://www.python.org/", tools=("python3",),
        builder="autotools", configure_args=("--enable-shared=no", "--disable-test-modules",
                                             "--without-ensurepip", "--disable-optimizations")),
)


# ---------------------------------------------------------------------------
# Build context
# ---------------------------------------------------------------------------
@dataclass
class BuildContext:
    prefix: Path
    work: Path
    platform: PlatformInfo
    downloader: Downloader
    jobs: int = 1
    cc: str = "cc"
    cxx: str = "c++"
    fc: str = "gfortran"
    ar: str = "ar"
    ranlib: str = "ranlib"
    make: str = "make"
    cmake: Optional[str] = None
    perl: Optional[str] = None
    ld: Optional[str] = None
    verbose: bool = False
    force: bool = False
    offline: bool = False
    source_dir: Optional[Path] = None
    extra_path: Tuple[str, ...] = ()
    extra_env: Dict[str, str] = field(default_factory=dict)
    stamps: Dict[str, dict] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.prefix = Path(self.prefix)
        self.work = Path(self.work)
        ensure_dir(self.prefix / "bin")
        ensure_dir(self.prefix / "lib")
        ensure_dir(self.prefix / "include")
        ensure_dir(self.work / "src")
        ensure_dir(self.work / "build")
        ensure_dir(self.work / "logs")
        self.stamp_dir = self.work / "stamps"
        ensure_dir(self.stamp_dir)
        from common import read_json as _read_json
        for stamp in self.stamp_dir.glob("*.json"):
            data = _read_json(stamp, None)
            if isinstance(data, dict) and data.get("name"):
                self.stamps[data["name"]] = data
        # Prefer tools installed in our own prefix.
        standard = [p for p in ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", "/usr/bin", "/bin")
                    if os.path.isdir(p)]
        self.path = ([str(self.prefix / "bin")] + [str(p) for p in self.extra_path] + standard
                     + os.environ.get("PATH", "").split(os.pathsep))

    def refresh_tools(self) -> None:
        def find(name: str, fallback: str) -> str:
            candidate = which(name, self.path)
            return candidate or fallback
        self.cc = find("gcc", self.cc)
        self.cxx = find("g++", self.cxx)
        self.fc = find("gfortran", self.fc)
        self.ar = find("ar", self.ar)
        self.ranlib = find("ranlib", self.ranlib)
        self.make = find("gmake", self.make)
        if not which(self.make, self.path):
            self.make = find("make", self.make)
        self.cmake = which("cmake", self.path) or self.cmake
        self.perl = which("perl", self.path) or self.perl

    def build_env(self, dep: Optional[Dependency] = None, static: bool = True) -> Dict[str, str]:
        env = {
            "PATH": os.pathsep.join(self.path),
            "PKG_CONFIG_PATH": os.pathsep.join(
                [str(self.prefix / "lib" / "pkgconfig"), str(self.prefix / "share" / "pkgconfig")]
                + [p for p in os.environ.get("PKG_CONFIG_PATH", "").split(os.pathsep) if p]
            ),
            "CPPFLAGS": "-I%s" % (self.prefix / "include"),
            "CFLAGS": "-O2 -fPIC -I%s" % (self.prefix / "include"),
            "CXXFLAGS": "-O2 -fPIC -I%s" % (self.prefix / "include"),
            "FFLAGS": "-O2 -fPIC -I%s" % (self.prefix / "include"),
            "LDFLAGS": "-L%s" % (self.prefix / "lib"),
            "CC": self.cc,
            "CXX": self.cxx,
            "FC": self.fc,
            "F77": self.fc,
            "AR": self.ar,
            "RANLIB": self.ranlib,
            "MAKE": self.make,
        }
        if self.platform.is_macos:
            env["MACOSX_DEPLOYMENT_TARGET"] = os.environ.get("MACOSX_DEPLOYMENT_TARGET", "")
            if not env["MACOSX_DEPLOYMENT_TARGET"]:
                del env["MACOSX_DEPLOYMENT_TARGET"]
        env.update(self.extra_env)
        return env

    def log_path(self, name: str) -> Path:
        return self.work / "logs" / (name + ".log")

    def stamp_path(self, dep: Dependency) -> Path:
        return self.stamp_dir / (dep.name + ".json")

    def has_stamp(self, dep: Dependency) -> bool:
        if self.force:
            return False
        data = self.stamps.get(dep.name)
        if not data:
            return False
        if data.get("version") != dep.version or data.get("url") != dep.url:
            return False
        for lib in dep.static_libs:
            if lib and lib != "libgmake.a" and not _find_in_prefix(self.prefix, lib):
                return False
        return True

    def mark_built(self, dep: Dependency) -> None:
        value = {"name": dep.name, "version": dep.version, "url": dep.url, "time": _now()}
        self.stamps[dep.name] = value
        from common import write_json
        write_json(self.stamp_path(dep), value)


def _now() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _find_in_prefix(prefix: Path, name: str) -> Optional[Path]:
    for candidate in (prefix / "lib" / name, prefix / "lib64" / name, prefix / "bin" / name):
        if candidate.exists():
            return candidate
    hits = list((prefix / "lib").rglob(name)) if (prefix / "lib").exists() else []
    return hits[0] if hits else None


# ---------------------------------------------------------------------------
# Builder implementations
# ---------------------------------------------------------------------------
def _extract_root(ctx: BuildContext, dep: Dependency, archive: Path) -> Path:
    destination = ensure_dir(ctx.work / "src" / ("%s-%s" % (dep.name, dep.version)))
    # Always extract to a clean directory to keep patches deterministic.
    if destination.exists():
        shutil.rmtree(destination)
    ensure_dir(destination)
    return extract_archive(archive, destination)


def _configure(ctx: BuildContext, dep: Dependency, src: Path) -> None:
    configure = src / "configure"
    if not configure.exists():
        raise BuildError("%s does not contain a configure script" % dep.name)
    args = [str(configure), "--prefix=%s" % ctx.prefix]
    args.extend(dep.configure_args or ())
    args.extend(("--disable-shared", "--enable-static") if dep.builder == "autotools" and not any("shared" in a for a in dep.configure_args) else ())
    run(args, cwd=src, env=ctx.build_env(dep), log=ctx.log_path(dep.name))


def _make(ctx: BuildContext, dep: Dependency, src: Path, targets: Sequence[str] = ()) -> None:
    args = [ctx.make, "-j%d" % max(1, ctx.jobs)]
    args.extend(dep.make_args)
    args.extend(targets or dep.make_targets)
    run(args, cwd=src, env=ctx.build_env(dep), log=ctx.log_path(dep.name))
    if dep.install:
        run([ctx.make, "install"], cwd=src, env=ctx.build_env(dep), log=ctx.log_path(dep.name))


def build_autotools(ctx: BuildContext, dep: Dependency, src: Path) -> None:
    _configure(ctx, dep, src)
    _make(ctx, dep, src)
    _install_fallback(ctx, dep, src)


def _install_fallback(ctx: BuildContext, dep: Dependency, src: Path) -> None:
    # Some hand-written projects have no install target; copy the artefacts.
    if not dep.install:
        return
    marker = ctx.stamp_path(dep)
    if marker.exists():
        return
    for lib in dep.static_libs:
        found = _find_in_prefix(ctx.prefix, lib)
        if found:
            continue
        search = list(src.rglob(lib))
        if search:
            shutil.copy2(search[0], ctx.prefix / "lib" / lib)
    for header in dep.includes:
        if (ctx.prefix / "include" / header).exists():
            continue
        search = list(src.rglob(header))
        if search:
            target = ctx.prefix / "include" / header
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(search[0], target)


def build_cmake(ctx: BuildContext, dep: Dependency, src: Path) -> None:
    cmake = ctx.cmake or which("cmake", ctx.path)
    if not cmake:
        raise BuildError("cmake is required to build %s (run: build.py bootstrap --with cmake)" % dep.name)
    build_dir = ensure_dir(ctx.work / "build" / ("%s-%s" % (dep.name, dep.version)))
    args = [
        cmake, "-S", str(src), "-B", str(build_dir),
        "-DCMAKE_INSTALL_PREFIX=%s" % ctx.prefix,
        "-DCMAKE_BUILD_TYPE=Release",
        "-DCMAKE_POSITION_INDEPENDENT_CODE=ON",
        "-DBUILD_SHARED_LIBS=OFF",
    ]
    args.extend(dep.cmake_args)
    run(args, cwd=src, env=ctx.build_env(dep), log=ctx.log_path(dep.name))
    run([cmake, "--build", str(build_dir), "--parallel", str(max(1, ctx.jobs))],
        env=ctx.build_env(dep), log=ctx.log_path(dep.name))
    if dep.install:
        run([cmake, "--install", str(build_dir)], env=ctx.build_env(dep), log=ctx.log_path(dep.name))
    _install_fallback(ctx, dep, src)


def build_gnu_make(ctx: BuildContext, dep: Dependency, src: Path) -> None:
    _make(ctx, dep, src)


def build_perl(ctx: BuildContext, dep: Dependency, src: Path) -> None:
    env = ctx.build_env(dep)
    run(["./Configure", "-des", "-Dprefix=%s" % ctx.prefix, "-Dman1dir=none",
         "-Dman3dir=none", "-Dusethreads", "-Duse64bitint"],
        cwd=src, env=env, log=ctx.log_path(dep.name), input_text="\n")
    run([ctx.make, "-j%d" % max(1, ctx.jobs)], cwd=src, env=env, log=ctx.log_path(dep.name))
    run([ctx.make, "install"], cwd=src, env=env, log=ctx.log_path(dep.name))


def build_cmake_bootstrap(ctx: BuildContext, dep: Dependency, src: Path) -> None:
    env = ctx.build_env(dep)
    args = ["./bootstrap", "--prefix=%s" % ctx.prefix, "--parallel=%d" % max(1, ctx.jobs),
            "--", "-DCMAKE_USE_OPENSSL=OFF", "-DBUILD_TESTING=OFF", "-DBUILD_CursesDialog=OFF"]
    run(args, cwd=src, env=env, log=ctx.log_path(dep.name))
    run([ctx.make, "-j%d" % max(1, ctx.jobs)], cwd=src, env=env, log=ctx.log_path(dep.name))
    run([ctx.make, "install"], cwd=src, env=env, log=ctx.log_path(dep.name))


def build_bzip2(ctx: BuildContext, dep: Dependency, src: Path) -> None:
    env = ctx.build_env(dep)
    run([ctx.make, "-f", "Makefile-libbz2_so"], cwd=src, env=env, log=ctx.log_path(dep.name), check=False)
    run([ctx.make, "-j%d" % max(1, ctx.jobs), "bzip2", "bzip2recover", "libbz2.a"],
        cwd=src, env=env, log=ctx.log_path(dep.name))
    shutil.copy2(src / "libbz2.a", ctx.prefix / "lib" / "libbz2.a")
    for header in ("bzlib.h",):
        shutil.copy2(src / header, ctx.prefix / "include" / header)
    for tool in ("bzip2", "bunzip2", "bzcat"):
        path = src / tool
        if path.exists():
            shutil.copy2(path, ctx.prefix / "bin" / tool)


def build_openssl(ctx: BuildContext, dep: Dependency, src: Path) -> None:
    env = ctx.build_env(dep)
    target = "darwin64-arm64-cc" if ctx.platform.is_macos and ctx.platform.machine == "arm64" else (
        "darwin64-x86_64-cc" if ctx.platform.is_macos else "linux-x86_64")
    run(["./Configure", target, "--prefix=%s" % ctx.prefix, "--openssldir=%s" % (ctx.prefix / "ssl"),
         "no-shared", "no-tests"], cwd=src, env=env, log=ctx.log_path(dep.name))
    run([ctx.make, "-j%d" % max(1, ctx.jobs)], cwd=src, env=env, log=ctx.log_path(dep.name))
    run([ctx.make, "install_sw"], cwd=src, env=env, log=ctx.log_path(dep.name))


def build_tcl(ctx: BuildContext, dep: Dependency, src: Path) -> None:
    unix_dir = src / "unix"
    env = ctx.build_env(dep)
    run(["./configure", "--prefix=%s" % ctx.prefix, "--disable-shared", "--enable-static"],
        cwd=unix_dir, env=env, log=ctx.log_path(dep.name))
    run([ctx.make, "-j%d" % max(1, ctx.jobs)], cwd=unix_dir, env=env, log=ctx.log_path(dep.name))
    run([ctx.make, "install"], cwd=unix_dir, env=env, log=ctx.log_path(dep.name))


def build_gcc(ctx: BuildContext, dep: Dependency, src: Path) -> None:
    """Build GCC with C, C++, and Fortran front ends and static runtimes."""
    configured = src / "configure"
    build_dir = ensure_dir(ctx.work / "build" / "gcc-%s" % dep.version)
    env = ctx.build_env(dep)
    # GCC must not inherit CFLAGS from the dependency prefix for its bootstrap.
    for key in ("CFLAGS", "CXXFLAGS", "FFLAGS", "CPPFLAGS", "LDFLAGS"):
        env.pop(key, None)
    env["CFLAGS"] = "-O2"
    env["CXXFLAGS"] = "-O2"
    args = [
        str(configured),
        "--prefix=%s" % ctx.prefix,
        "--enable-languages=c,c++,fortran",
        "--disable-bootstrap",
        "--disable-multilib",
        "--disable-shared",
        "--enable-static",
        "--with-pic",
        "--disable-nls",
        "--disable-libsanitizer",
        "--disable-libssp",
        "--with-gmp=%s" % ctx.prefix,
        "--with-mpfr=%s" % ctx.prefix,
        "--with-mpc=%s" % ctx.prefix,
        "--with-isl=%s" % ctx.prefix,
    ]
    run(args, cwd=build_dir, env=env, log=ctx.log_path(dep.name))
    run([ctx.make, "-j%d" % max(1, ctx.jobs)], cwd=build_dir, env=env, log=ctx.log_path(dep.name))
    run([ctx.make, "install"], cwd=build_dir, env=env, log=ctx.log_path(dep.name))
    ctx.refresh_tools()
    if not (ctx.prefix / "bin" / "gfortran").exists():
        raise BuildError("GCC installation did not provide gfortran")


def build_mpich(ctx: BuildContext, dep: Dependency, src: Path) -> None:
    env = ctx.build_env(dep)
    device = "ch3:sock" if ctx.platform.is_macos else "ch3:nemesis"
    args = [
        str(src / "configure"),
        "--prefix=%s" % ctx.prefix,
        "--disable-shared",
        "--enable-static",
        "--enable-fortran=all",
        "--with-device=%s" % device,
        "--disable-cxx" if False else "--enable-cxx",
        "--disable-dependency-tracking",
        "--disable-fast-install",
    ]
    run(args, cwd=src, env=env, log=ctx.log_path(dep.name))
    run([ctx.make, "-j%d" % max(1, ctx.jobs)], cwd=src, env=env, log=ctx.log_path(dep.name))
    run([ctx.make, "install"], cwd=src, env=env, log=ctx.log_path(dep.name))


def build_openblas(ctx: BuildContext, dep: Dependency, src: Path) -> None:
    env = ctx.build_env(dep)
    args = [ctx.make, "-j%d" % max(1, ctx.jobs), "NO_SHARED=1", "USE_OPENMP=1",
            "NUM_THREADS=64", "NO_LAPACKE=0"]
    if ctx.platform.is_macos and ctx.platform.machine == "arm64":
        args.append("TARGET=ARMV8")
        args.append("DYNAMIC_ARCH=0")
    else:
        args.append("DYNAMIC_ARCH=1")
    run(args, cwd=src, env=env, log=ctx.log_path(dep.name))
    # OpenBLAS install honours the same build flags; without NO_SHARED the
    # install target tries to copy a shared object that was never built.
    install_args = [ctx.make, "PREFIX=%s" % ctx.prefix, "install",
                    "NO_SHARED=1", "USE_OPENMP=1", "NUM_THREADS=64", "NO_LAPACKE=0"]
    if ctx.platform.is_macos and ctx.platform.machine == "arm64":
        install_args += ["TARGET=ARMV8", "DYNAMIC_ARCH=0"]
    else:
        install_args += ["DYNAMIC_ARCH=1"]
    run(install_args, cwd=src, env=env, log=ctx.log_path(dep.name))


def build_hdf5(ctx: BuildContext, dep: Dependency, src: Path) -> None:
    env = ctx.build_env(dep)
    env["CC"] = which("mpicc", ctx.path) or ctx.cc
    env["FC"] = which("mpifort", ctx.path) or ctx.fc
    env["LIBS"] = "-ldl -lm"
    args = [
        str(src / "configure"),
        "--prefix=%s" % ctx.prefix,
        "--disable-shared",
        "--enable-static",
        "--enable-fortran",
        "--enable-hl",
        "--enable-parallel",
        "--disable-tools",
        "--disable-tests",
        "--disable-doxygen",
        "--disable-deprecated-symbols",
        "--with-zlib=%s" % ctx.prefix,
        "--with-szlib=%s" % ctx.prefix,
    ]
    run(args, cwd=src, env=env, log=ctx.log_path(dep.name))
    run([ctx.make, "-j%d" % max(1, ctx.jobs)], cwd=src, env=env, log=ctx.log_path(dep.name))
    run([ctx.make, "install"], cwd=src, env=env, log=ctx.log_path(dep.name))


def build_scalapack(ctx: BuildContext, dep: Dependency, src: Path) -> None:
    template = src / "SLmake.inc.example"
    if not template.exists():
        raise BuildError("ScaLAPACK source is missing SLmake.inc.example")
    text = read_text(template)
    blas = _lib_path(ctx, "libopenblas.a")
    text = re.sub(r"(?m)^BLASLIB\s*=.*$", "BLASLIB = %s" % blas, text)
    text = re.sub(r"(?m)^LAPACKLIB\s*=.*$", "LAPACKLIB = %s" % blas, text)
    text = re.sub(r"(?m)^FCFLAGS\s*=.*$", "FCFLAGS = -O2 -fPIC -fallow-argument-mismatch -std=legacy", text)
    text = re.sub(r"(?m)^CCFLAGS\s*=.*$", "CCFLAGS = -O2 -fPIC -Wno-implicit-function-declaration -Wno-implicit-int -fcommon", text)
    write_text(src / "SLmake.inc", text)
    env = ctx.build_env(dep)
    # A single make process prevents concurrent `ar` updates to libscalapack.a.
    run([ctx.make, "lib"], cwd=src, env=env, log=ctx.log_path(dep.name))
    shutil.copy2(src / "libscalapack.a", ctx.prefix / "lib" / "libscalapack.a")


def build_mumps(ctx: BuildContext, dep: Dependency, src: Path) -> None:
    env = ctx.build_env(dep)
    blas = _lib_path(ctx, "libopenblas.a")
    scalap = _lib_path(ctx, "libscalapack.a")
    libdir = src / "lib"
    ensure_dir(libdir)
    ordering = "-Dpord"
    ordering_libs = ""
    ordering_incs = ""
    if _find_in_prefix(ctx.prefix, "libmetis.a"):
        ordering += " -Dmetis"
        ordering_libs += " -L%s -lmetis" % (ctx.prefix / "lib")
        ordering_incs += " -I%s" % (ctx.prefix / "include")
    if _find_in_prefix(ctx.prefix, "libscotch.a"):
        ordering += " -Dscotch"
        ordering_libs += " -L%s -lesmumps -lscotch -lscotcherr" % (ctx.prefix / "lib")
        ordering_incs += " -I%s" % (ctx.prefix / "include")
    makeinc = """PLAT =
LIBEXT = .a
OUTC = -o
OUTF = -o
RM = /bin/rm -f
CC = {cc}
FC = {fc}
FL = {fc}
AR = ar vr 
RANLIB = {ranlib}
LAPACK = {blas}
SCALAP = {scalap}
INCPAR = {incpar}
LIBPAR = {libpar}
LIBBLAS = {blas}
LIBBLACS = {scalap}
LIBOTHERS =
CDEFS = -DAdd_
OPTF = -O2 -fPIC -fallow-argument-mismatch -fallow-invalid-boz -std=legacy
OPTC = -O2 -fPIC -fcommon
OPTL = -O2
ORDERINGSF = {ordering}
ORDERINGSC = {ordering}
LORDERINGS = {ordering_libs}
IORDERINGSF = {ordering_incs} -I../PORD/include
IORDERINGSC = {ordering_incs} -I../PORD/include
LPORDDIR = $(topdir)/PORD/lib/
LPORD = -L$(LPORDDIR) -lpord$(PLAT)
""".format(
        cc=which("mpicc", ctx.path) or ctx.cc,
        fc=which("mpifort", ctx.path) or ctx.fc,
        ranlib=ctx.ranlib,
        blas=blas,
        scalap=scalap,
        incpar="-I%s" % _mpi_include(ctx),
        libpar="-L%s -lmpi -lmpifort" % _mpi_libdir(ctx),
        ordering=ordering,
        ordering_libs=ordering_libs,
        ordering_incs=ordering_incs,
    )
    write_text(src / "Makefile.inc", makeinc)
    run([ctx.make, "prerequisites"], cwd=src, env=env, log=ctx.log_path(dep.name))
    run([ctx.make, "-C", "src", "-j%d" % max(1, ctx.jobs), "d", "z"], cwd=src, env=env, log=ctx.log_path(dep.name))
    ensure_dir(ctx.prefix / "lib")
    ensure_dir(ctx.prefix / "include")
    for lib in ("libdmumps.a", "libzmumps.a", "libmumps_common.a", "libpord.a"):
        path = libdir / lib
        if not path.exists():
            raise BuildError("MUMPS did not produce %s" % lib)
        shutil.copy2(path, ctx.prefix / "lib" / lib)
    inc = src / "include"
    if inc.exists():
        for header in inc.iterdir():
            if header.is_file():
                shutil.copy2(header, ctx.prefix / "include" / header.name)


def build_metis(ctx: BuildContext, dep: Dependency, src: Path) -> None:
    env = ctx.build_env(dep)
    run([ctx.make, "config", "shared=0", "prefix=%s" % ctx.prefix], cwd=src, env=env,
        log=ctx.log_path(dep.name))
    run([ctx.make, "-j%d" % max(1, ctx.jobs)], cwd=src, env=env, log=ctx.log_path(dep.name))
    run([ctx.make, "install"], cwd=src, env=env, log=ctx.log_path(dep.name))


def build_parmetis(ctx: BuildContext, dep: Dependency, src: Path) -> None:
    env = ctx.build_env(dep)
    run([ctx.make, "config", "shared=0", "prefix=%s" % ctx.prefix], cwd=src, env=env,
        log=ctx.log_path(dep.name))
    run([ctx.make, "-j%d" % max(1, ctx.jobs)], cwd=src, env=env, log=ctx.log_path(dep.name))
    run([ctx.make, "install"], cwd=src, env=env, log=ctx.log_path(dep.name))


def build_scotch(ctx: BuildContext, dep: Dependency, src: Path) -> None:
    make_inc_dir = src / "src" / "Make.inc"
    candidates = sorted(make_inc_dir.glob("Makefile.inc.*"))
    if not candidates:
        raise BuildError("SCOTCH source has no Make.inc examples")
    chosen = None
    for candidate in candidates:
        name = candidate.name.lower()
        if ctx.platform.is_macos and ("mac" in name or "i686" in name):
            chosen = candidate
            break
        if ctx.platform.machine == "x86_64" and "x86-64" in name:
            chosen = candidate
            break
    chosen = chosen or candidates[0]
    text = read_text(chosen)
    text = re.sub(r"(?m)^prefix\s*=.*$", "prefix = %s" % ctx.prefix, text)
    text = re.sub(r"(?m)^CCS\s*=.*$", "CCS = %s" % (which("mpicc", ctx.path) or ctx.cc), text)
    text = re.sub(r"(?m)^CCP\s*=.*$", "CCP = %s" % (which("mpicc", ctx.path) or ctx.cc), text)
    text = re.sub(r"(?m)^CFLAGS\s*=.*$", "CFLAGS = -O2 -fPIC", text)
    write_text(src / "src" / "Makefile.inc", text)
    env = ctx.build_env(dep)
    run([ctx.make, "-C", "src", "-j%d" % max(1, ctx.jobs), "esmumps", "ptesmumps"],
        cwd=src, env=env, log=ctx.log_path(dep.name))
    run([ctx.make, "-C", "src", "install"], cwd=src, env=env, log=ctx.log_path(dep.name), check=False)
    _install_fallback(ctx, dep, src)


def build_boost(ctx: BuildContext, dep: Dependency, src: Path) -> None:
    env = ctx.build_env(dep)
    run(["./bootstrap.sh", "--prefix=%s" % ctx.prefix, "--with-libraries=math",
         "--with-toolset=gcc"], cwd=src, env=env, log=ctx.log_path(dep.name))
    b2 = src / "b2"
    if b2.exists():
        os.chmod(b2, 0o755)
    run(["./b2", "-j%d" % max(1, ctx.jobs), "link=static", "runtime-link=static",
         "threading=multi", "variant=release", "install"], cwd=src, env=env,
        log=ctx.log_path(dep.name))


def build_dierckx(ctx: BuildContext, dep: Dependency, src: Path) -> None:
    fitpack = src / "scipy" / "interpolate" / "fitpack"
    if not fitpack.exists():
        raise BuildError("SciPy source does not contain scipy/interpolate/fitpack")
    objdir = ensure_dir(ctx.work / "build" / "dierckx" / "obj")
    env = ctx.build_env(dep)
    objects = []
    for source in sorted(fitpack.glob("*.f")):
        obj = objdir / (source.stem + ".o")
        run([ctx.fc, "-O2", "-fPIC", "-fallow-argument-mismatch", "-std=legacy", "-c",
             str(source), "-o", str(obj)], cwd=objdir, env=env, log=ctx.log_path(dep.name))
        objects.append(str(obj))
    archive = ctx.prefix / "lib" / "libdierckx.a"
    run([ctx.ar, "rcs", str(archive)] + objects, env=env, log=ctx.log_path(dep.name))


def build_pastix(ctx: BuildContext, dep: Dependency, src: Path) -> None:
    build_cmake(ctx, dep, src)
    # PaStiX installs a CMake package; pastix-conf is not produced by 6.x.
    conf = ctx.prefix / "bin" / "pastix-conf"
    if not conf.exists():
        write_text(conf, "#!/bin/sh\n# PaStiX 6 compatibility shim for JOREK\nexit 1\n")
        os.chmod(conf, 0o755)


def build_strumpack(ctx: BuildContext, dep: Dependency, src: Path) -> None:
    cmake = ctx.cmake or which("cmake", ctx.path)
    if not cmake:
        raise BuildError("cmake is required to build STRUMPACK")
    build_dir = ensure_dir(ctx.work / "build" / ("%s-%s" % (dep.name, dep.version)))
    args = [
        cmake, "-S", str(src), "-B", str(build_dir),
        "-DCMAKE_INSTALL_PREFIX=%s" % ctx.prefix,
        "-DCMAKE_BUILD_TYPE=Release",
        "-DBUILD_SHARED_LIBS=OFF",
        "-DSTRUMPACK_USE_MPI=ON",
        "-DSTRUMPACK_USE_OPENMP=ON",
        "-DSTRUMPACK_USE_CUDA=OFF",
        "-DSTRUMPACK_USE_HIP=OFF",
        "-DSTRUMPACK_BUILD_TESTS=OFF",
    ]
    run(args, cwd=src, env=ctx.build_env(dep), log=ctx.log_path(dep.name))
    run([cmake, "--build", str(build_dir), "--parallel", str(max(1, ctx.jobs))],
        env=ctx.build_env(dep), log=ctx.log_path(dep.name))
    run([cmake, "--install", str(build_dir)], env=ctx.build_env(dep), log=ctx.log_path(dep.name))


def build_imas(ctx: BuildContext, dep: Dependency, src: Path) -> None:
    raise BuildError(
        "automatic IMAS-Core builds are not stable across platforms; install IMAS-Core "
        "and pass --imas-root DIR, or use --without imas.  The dependency graph and "
        "compile-time switches are otherwise complete."
    )


def build_catalyst(ctx: BuildContext, dep: Dependency, src: Path) -> None:
    raise BuildError(
        "ParaView Catalyst must be supplied as a system installation (--catalyst-root DIR); "
        "building ParaView from source is outside the scope of a JOREK build.  Use "
        "--without catalyst to omit the in-situ adaptor."
    )


BUILDERS = {
    "autotools": build_autotools,
    "cmake": build_cmake,
    "gnu_make": build_gnu_make,
    "perl": build_perl,
    "cmake_bootstrap": build_cmake_bootstrap,
    "bzip2": build_bzip2,
    "openssl": build_openssl,
    "tcl": build_tcl,
    "gcc": build_gcc,
    "mpich": build_mpich,
    "openblas": build_openblas,
    "hdf5": build_hdf5,
    "scalapack": build_scalapack,
    "mumps": build_mumps,
    "metis": build_metis,
    "parmetis": build_parmetis,
    "scotch": build_scotch,
    "boost": build_boost,
    "dierckx": build_dierckx,
    "pastix": build_pastix,
    "strumpack": build_strumpack,
    "imas": build_imas,
    "catalyst": build_catalyst,
}


def _lib_path(ctx: BuildContext, name: str) -> str:
    path = _find_in_prefix(ctx.prefix, name)
    if path:
        return str(path)
    # Common host locations, used only when a system dependency was requested.
    for root in ("/usr/lib", "/usr/lib64", "/usr/local/lib", "/opt/homebrew/lib"):
        candidate = Path(root) / name
        if candidate.exists():
            return str(candidate)
    raise BuildError("required static library %s was not found in %s" % (name, ctx.prefix))


def _mpi_include(ctx: BuildContext) -> str:
    return str(ctx.prefix / "include")


def _mpi_libdir(ctx: BuildContext) -> str:
    return str(ctx.prefix / "lib")


# ---------------------------------------------------------------------------
# Dependency manager
# ---------------------------------------------------------------------------
class DepManager:
    def __init__(self, ctx: BuildContext, optional: bool = False) -> None:
        self.ctx = ctx
        self.optional = optional

    def resolve(self, names: Iterable[str]) -> List[Dependency]:
        seen: Dict[str, int] = {}
        order: List[Dependency] = []

        def visit(name: str, stack: Tuple[str, ...]) -> None:
            if name in seen:
                if seen[name] == 1:
                    raise BuildError("cyclic dependency: %s" % " -> ".join(stack + (name,)))
                return
            if name not in DEPENDENCIES:
                raise BuildError("unknown dependency: %s" % name)
            seen[name] = 1
            item = DEPENDENCIES[name]
            for child in item.deps:
                visit(child, stack + (name,))
            seen[name] = 2
            order.append(item)

        for name in names:
            visit(name, ())
        return order

    def build(self, names: Iterable[str], include_optional: bool = True) -> None:
        order = self.resolve(names)
        for item in order:
            if not item.supports(self.ctx.platform):
                if item.optional or self.optional:
                    print("  skipping %s (unsupported on %s)" % (item.name, self.ctx.platform.system))
                    continue
                raise BuildError("%s does not support %s" % (item.name, self.ctx.platform.system))
            if item.manual:
                print("  skipping manual dependency %s: %s" % (item.name, item.note))
                continue
            if item.name == "imas" and item.name not in names and not include_optional:
                continue
            self._build_one(item)

    def _build_one(self, item: Dependency) -> None:
        ctx = self.ctx
        if item.name in ("cmake", "pkgconf", "perl", "m4", "gmake") and not ctx.force:
            found = [which(tool, ctx.path) for tool in item.tools]
            if item.tools and all(found):
                print("  %-18s using host tool" % item.name)
                ctx.mark_built(item)
                return
        if ctx.has_stamp(item):
            print("  %-18s already built" % item.name)
            return
        if not item.supports(ctx.platform):
            if item.optional:
                print("  %-18s skipped (platform)" % item.name)
                return
            raise BuildError("%s is not supported on %s" % (item.name, ctx.platform.system))
        print("")
        print(">> building %s %s" % (item.name, item.version))
        builder = BUILDERS.get(item.builder)
        if not builder:
            raise BuildError("no builder registered for %s (%s)" % (item.name, item.builder))
        archive = ctx.downloader.fetch(item.url, "%s-%s-%s" % (item.name, item.version, os.path.basename(item.url.split("?")[0])), item.sha256)
        src = _extract_root(ctx, item, archive)
        try:
            builder(ctx, item, src)
        except BuildError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BuildError("failed to build %s: %s" % (item.name, exc)) from exc
        ctx.mark_built(item)
        ctx.refresh_tools()
        if not item.static_libs:
            return
        missing = [lib for lib in item.static_libs if not _find_in_prefix(ctx.prefix, lib)]
        if missing:
            raise BuildError(
                "%s built, but these static libraries were not installed: %s"
                % (item.name, ", ".join(missing))
            )


def core_dependencies(profile: str = "standard", solver: str = "mumps",
                      include_dierckx: bool = False) -> List[str]:
    """Return the dependency names needed for a JOREK configuration."""
    names = ["cmake", "mpich", "openblas", "fftw", "hdf5", "zlib", "libaec"]
    if include_dierckx:
        names.append("dierckx")
    if solver in ("mumps", "all"):
        names.extend(["scalapack", "metis", "mumps"])
    if solver in ("pastix6", "all"):
        names.extend(["scalapack", "metis", "scotch", "hwloc", "pastix6"])
    if solver in ("strumpack", "all"):
        names.extend(["scalapack", "metis", "parmetis", "strumpack"])
    if profile in ("full", "complete"):
        names.extend(["boost"])
    if profile == "complete":
        names.extend(["scotch"])
    return names
