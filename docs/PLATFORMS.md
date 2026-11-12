# Platform notes

## Linux

The intended fully-static path is:

```sh
python3 build.py bootstrap --with-deps
python3 build.py dist --matrix complete
```

`bootstrap` builds GNU make (if needed), then GMP/MPFR/MPC/ISL and GCC with
C/C++/Fortran.  It then builds MPICH, OpenBLAS, ScaLAPACK, FFTW, parallel
HDF5, METIS, SCOTCH, MUMPS, Boost, and the optional solvers.  The Makefile.inc
uses absolute `.a` paths.  A static glibc is used when available; musl systems
build musl from the system toolchain.  `verify` checks the final ELF.

If the host is already complete, `--deps none --dep-prefix DIR` consumes a
static installation and skips the download step.

## macOS

Apple does not ship static `libSystem`; dynamic links to `/usr/lib` and
`/System/Library` are the only allowed non-static dependencies.  Everything
else (MPI, BLAS, HDF5, FFTW, MUMPS, ...) is built from source as a static
archive.  Apple clang is enough for the C/C++ side; `bootstrap` builds GCC for
Fortran.  The suite is linked through a `__DATA,__jorek_suite` section because
appending bytes to a Mach-O breaks the code signature on Apple silicon.  The
builder re-signs the final image ad hoc.

Homebrew is useful for quick experiments, but a Homebrew MPI/gfortran build is
linked dynamically and will fail `verify`.  For a distributable suite use the
source-built prefix.

## FreeBSD / OpenBSD / NetBSD

The POSIX backend works with the base or ports compilers.  GNU make is built if
`gmake`/`make` is unavailable.  Static versions of system libraries are used
when the OS provides them; otherwise the same system-library exception as
macOS applies.  `ldd` is used for verification.

## Windows

JOREK's upstream build system is POSIX-make based.  The supported environment
is MSYS2 with the MinGW-w64 `gcc`/`gfortran` toolchain (UCRT64 or MINGW64).
From the MSYS2 shell:

```sh
python build.py doctor
python build.py bootstrap --with-deps
python build.py dist --model 303
```

The launcher is a normal PE executable and uses only Win32 APIs.  It runs
without MSYS2, Python, or a compiler on the target machine.  A native MSVC
build is not possible because there is no MSVC Fortran front end; the builder
does not pretend otherwise.

## Cross-compilation

`--cc`, `--cxx`, `--fc`, and `--mpi-prefix` can point to a cross toolchain.
Dependency recipes still execute on the build host, so build the dependency
prefix for the target first and pass it with `--deps none --dep-prefix`.
`verify` uses the host `otool`/`readelf`/`objdump`; pass the matching
cross-binutils on `PATH` for non-native targets.
