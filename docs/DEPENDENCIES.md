# Dependency catalogue

All versions are pinned in `src/deps.py`.  A row is a build input, not a
promise that the project is always available at that URL; the downloader
reports a clear error and can use a local mirror through `file://` URLs or a
pre-populated cache.

## Bootstrap and tools

| Package | Version | Licence | Why |
| --- | --- | --- | --- |
| GNU make | 4.4.1 | GPL-3.0-or-later | portable build driver |
| GCC | 14.2.0 | GPL-3.0-or-later | C/C++/Fortran when none is installed |
| binutils | 2.43.1 | GPL-3.0-or-later | fallback assembler/linker/archiver |
| GMP / MPFR / MPC / ISL | 6.3.0 / 4.2.1 / 1.3.1 / 0.27 | GNU LGPL/GPL | GCC prerequisites |
| m4 | 1.4.19 | GPL-3.0-or-later | Autoconf |
| Autoconf / Automake / Libtool | 2.72 / 1.17 / 2.5.4 | GPL | configure scripts |
| pkgconf | 2.3.0 | ISC | pkg-config replacement |
| CMake | 3.31.6 | BSD-3-Clause | CMake-based dependencies |
| Perl | 5.40.0 | Artistic/GPL | OpenSSL, build scripts |
| Bison / Flex / Gperf | 3.8.2 / 2.6.4 / 3.1 | GPL/BSD | parsers and generators |
| Texinfo / help2man | 7.1 / 1.49.3 | GPL | documentation targets |
| zlib / libaec / zstd / xz / bzip2 | 1.3.1 / 1.1.3 / 1.5.7 / 5.6.4 / 1.0.8 | permissive | compression |
| OpenSSL / SQLite / ncurses / readline | 3.0.16 / 3.47.2 / 6.5 / 8.2 | Apache/PD/X11/GPL | optional IMAS and tools |
| libxml2 / libiconv / gettext / libffi | 2.13.5 / 1.18 / 0.23.1 / 3.4.6 | permissive/LGPL | optional toolchain and IMAS |
| libunistring / Guile / BDW-GC / libatomic_ops | 1.3 / 3.0.10 / 8.2.8 / 7.8.2 | LGPL/MIT/GPL | autogen/Guile-style builds |
| Tcl / Expect / DejaGnu | 8.6.16 / 5.45.4 / 1.6.3 | BSD/PD/GPL | test drivers |
| grep / sed / gawk / gdbm | 3.11 / 4.9 / 5.3.1 / 1.24 | GPL | POSIX tool replacements |

## HPC stack

| Package | Version | Licence | Why |
| --- | --- | --- | --- |
| MPICH | 4.3.0 | BSD-like | MPI |
| OpenBLAS | 0.3.30 | BSD-3-Clause | BLAS/LAPACK |
| LAPACK | 3.12.1 | BSD-3-Clause | reference fallback |
| ScaLAPACK | 2.2.0 | BSD-3-Clause | distributed dense algebra; BLACS is bundled in `libscalapack.a` |
| FFTW | 3.3.10 | GPL-2.0-or-later | toroidal FFTs |
| HDF5 | 1.14.6 | BSD-3-Clause | parallel restart/output |
| METIS | 5.2.1 | Apache-2.0 | fill-reducing ordering |
| GKlib | master | Apache-2.0 | METIS support library |
| ParMETIS | 4.0.3 | ParMETIS terms | distributed ordering |
| SCOTCH | 6.1.2 | CeCILL-C | ordering/partitioning |
| MUMPS | 5.7.3 | CeCILL-C | sparse direct solver |
| PaStiX 5 | 5.2.3 | CeCILL-C | legacy JOREK `USE_PASTIX` / optional MURGE adapter; isolated prefix |
| PaStiX 6 | 6.3.2 | CeCILL-C | modern JOREK `USE_PASTIX6` adapter; METIS ordering, 32-bit indices |
| STRUMPACK | 7.1.0 | BSD-3-Clause | sparse direct solver |
| Boost | 1.87.0 | BSL-1.0 | Boost.Math Bessel K |
| Fitpack/Dierckx | SciPy 1.14.1 sources | BSD-3-Clause | `eqdsk2jorek` regridding |

## Optional layers

| Package | Version | Licence | Note |
| --- | --- | --- | --- |
| IMAS-Core | 26.0.0 | LGPL-3.0-or-later | data dictionary; very large |
| ParaView Catalyst | 5.13.2 | BSD-3-Clause | in-situ visualisation; very large |
| ZeroMQ / CZMQ | 4.3.5 / 4.2.1 | MPL-2.0 | IMAS transport |
| libsodium | 1.0.20 | ISC | ZeroMQ crypto |
| libuuid | 2.40.4 | BSD-3-Clause | IMAS identifiers |

## Deliberately not downloaded

| Component | Reason | How to use |
| --- | --- | --- |
| WSMP | proprietary IBM licence | `--solver wsmp` + prefix with `libpwsmp64` |
| HIPS | legacy in-house code | a prefix containing `libhips` |
| MKL | proprietary Intel licence | system prefix with MKL archives |
| CUDA | proprietary NVIDIA runtime | not required by the CPU JOREK build |

The feature switches remain in the generated configuration; they are not
silently deleted.  The build simply refuses to invent a library it cannot
legally fetch.


### BLACS

ScaLAPACK 2.0 and later incorporate BLACS into the ScaLAPACK library. This
project builds ScaLAPACK 2.2.0, so separate legacy `mpiblacs` and
`mpiblacs-patch` packages are not downloaded. The MUMPS link configuration
uses `libscalapack.a` as its BLACS provider. A separate BLACS build should
only be added for a deliberately selected legacy ScaLAPACK/BLACS toolchain.

### PaStiX integer ABI

The bundled PaStiX 5 and PaStiX 6 builds currently use 32-bit solver indices,
matching JOREK's default `USE_INTSIZE64=0`. PaStiX 5 is installed below a
private `pastix5/` prefix because both major versions export
`libpastix.a`; this prevents one version from overwriting the other. The
JOREK makefile adapter selects the matching include files, libraries and
feature switches for each version. Enabling JOREK's `int64` feature with a
bundled PaStiX backend is rejected during preflight rather than allowing an
integer-ABI mismatch to reach the compiler or linker.
