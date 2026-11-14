# JOREK single-file suite builder

`build.py` builds the complete JOREK code base — every physics model, every
supported model-family extension, every compile-time feature combination, and
every diagnostic program — from source, links each executable against static
third-party libraries, and fuses the resulting executables into **one native
single-file program**.

The suite contains no interpreter, no package manager, no shared JOREK
libraries, and no installation tree.  On the target machine it only needs the
operating-system libraries that are unavoidable on that platform.

```
.
├── build.py              # the only entry point
├── src/
│   ├── common.py         # downloads, platform and process helpers
│   ├── deps.py           # dependency graph and source recipes
│   ├── jorek.py          # model/feature matrix and JOREK build backends
│   ├── suite.py          # single-file archive writer
│   ├── jorek_suite.c     # native launcher
│   ├── puff.c/.h         # self-contained DEFLATE decoder
│   └── ...
├── tests/
├── docs/
└── examples/
```

## Linux: one-command full build

```sh
python3 build.py full --root ../JOREK --work /big/jorek-build --jobs 32
```

If the source tree is absent, add `--fetch-source`; it clones into
`$WORK/src/JOREK`, the same source tree used for the other dependencies.
Without it, `build.py` searches `--root`, `JOREK_ROOT`, `$WORK/src/JOREK`,
`../JOREK`, the parent directory and the current directory, and fails if none
is a JOREK checkout.

This produces a single `jorek-suite` with MUMPS, PaStiX 5, PaStiX 6 and
STRUMPACK variants.  See [LINUX_FULL.md](docs/LINUX_FULL.md).

## Quick start

```sh
# 1. Inspect the host.  This never modifies anything.
python3 build.py doctor

# 2. Build a private, static dependency prefix (MPI, BLAS/LAPACK, FFTW,
#    parallel HDF5, MUMPS, Boost, ...).  Run this once per machine.
python3 build.py bootstrap --with-deps

# 3. Build one model and turn it into a single-file suite.
python3 build.py dist --model 303 --programs main

# 4. Inspect and run the suite.  No compiler, Python, or library is needed at
#    this point.
./build/dist/jorek-suite --list
# The suite contains its own Hydra mpiexec:
./build/dist/jorek-suite --mpi -np 4 --model 303 < namelist/model303/intear_square
```

To build every model and every supported model-family extension:

```sh
python3 build.py dist --matrix complete --programs main
```

`--matrix maximal` builds one variant per model with **every supported model
extension enabled simultaneously** (21 variants for the current tree).  The
complete family matrix (65 variants) is available with `--matrix complete`.

To enumerate the matrix without building it:

```sh
python3 build.py matrix --matrix complete --json > matrix.json
```

To select a variant at run time:

```sh
./build/dist/jorek-suite --model 303
./build/dist/jorek-suite --model 600 --params with_TiTe=1,with_vpar=1
./build/dist/jorek-suite --features hdf5,fftw,mumps --params n_tor=5,n_plane=8
./build/dist/jorek-suite --variant model303-n_tor5-n_plane8-mumps
```

## What "complete" means

The builder has three independent dimensions:

1. **Physics models**: all directories `models/modelNNN` in the JOREK
   checkout (001, 002, 003, 004, 180, 183, 199, 303, 305, 306, 307, 333, 401,
   500, 501, 502, 600, 710, 711, 712, 750 for the current tree).
2. **Model families**: 180 and 183 expose `with_TiTe`/`with_vpar`; 600 exposes
   `with_TiTe`, `with_vpar`, `with_rho`, `with_neutrals`, `with_impurities`;
   750 exposes `with_TiTe`, `with_neutrals`, `with_impurities`.  `--matrix
   complete` enumerates every supported combination.
3. **Compile-time features and hard-coded parameters**: solver backend,
   HDF5, FFTW, Boost versus the standard Bessel K implementation, GMRES versus
   BiCGSTAB, block/direct construction, quadtree/no-tree, complex
   preconditioner, 64-bit integers, task loops, JECCD/JEC2DIAG, DOMM versus an
   external field, IMAS, ParaView Catalyst, and the `n_tor`, `n_plane`,
   `n_order`, `n_period`, ... parameters.

`matrix --matrix complete` covers (1) and (2) exhaustively and gives each
combination the maximal feature set that the dependency prefix supports.
Feature alternatives are selected with `--enable/--disable` and combined with
`--param-grid`; the full Cartesian product is available, but it can be very
large.  The launcher stores the resulting metadata and selects the right
binary at run time.

A hard-coded parameter is **compiled** into a JOREK executable.  A suite can
therefore contain several executables for the same model with different
`n_tor`, `n_plane`, etc.; `--params n_tor=5,n_plane=8` picks the matching one.
If only one variant matches a request, `--model` or even no selector (when the
suite has one variant) is enough.

## Building from source only

`build.py` never downloads a binary artifact.  Every dependency is fetched as
source and built in a private prefix:

* bootstrap tools: GNU make, GCC (C/C++/Fortran), binutils, GMP, MPFR, MPC,
  ISL, Autoconf, Automake, Libtool, pkgconf, CMake, Perl, Bison, Flex, Gperf,
  Texinfo, and their own dependencies (`m4`, `ncurses`, `readline`, `libiconv`,
  `gettext`, `libffi`, `libxml2`, `libunistring`, Guile, BDW-GC, ...);
* HPC stack: MPICH, OpenBLAS, LAPACK, ScaLAPACK/BLACS, FFTW, parallel HDF5,
  zlib, libaec/SZIP, METIS, ParMETIS, SCOTCH, MUMPS, PaStiX 6, STRUMPACK,
  Boost, Fitpack/Dierckx;
* optional layers: ZeroMQ/CZMQ, libuuid, SQLite, OpenSSL, IMAS-Core, and
  ParaView Catalyst.

The dependency graph is recursive: if a requested package needs another
package, that package is resolved and built first.  Downloads are cached and
hashed; builds are stamped and resumable.

`python3 build.py deps graph` prints the resolved graph and
`python3 build.py deps build --profile complete` builds the whole HPC set.

## Static linking and verification

The generated `Makefile.inc` uses absolute paths to static archives, not
`-l` names that could resolve to a shared object.  The suite launcher and
every embedded executable are checked with `verify_static`:

* Linux/BSD: `readelf -d` / `ldd`; only the C library, `libm`, `libdl`,
  `libpthread`, `librt`, `libgcc_s`, and the dynamic loader are accepted;
* macOS: `otool -L`; only `/usr/lib`, `/System/Library`, and
  `/Library/Apple` are accepted (the system C/C++ libraries are not shipped
  as static archives by Apple);
* Windows: `objdump -p`; only known `KERNEL32`/`USER32`/MSVC/UCRT system DLLs
  are accepted.

Run `python3 build.py verify` on the final suite.  A violation is reported
with the exact offending library; it is never silently ignored.

## Platforms

| Platform | Status | Notes |
| --- | --- | --- |
| Linux (glibc/musl, x86-64/arm64) | Supported | Full static linking, including libc when a static libc is available. |
| FreeBSD/OpenBSD/NetBSD | Supported | POSIX make backend; static archives where the OS provides them. |
| macOS (arm64/x86-64) | Supported | Apple system libraries and frameworks remain dynamic; all third-party code is static. The suite is ad-hoc signed because appended Mach-O data invalidates the linker signature. |
| Windows (MSYS2/MinGW-w64) | Supported | Use the MSYS2 `MINGW64`/`UCRT64` toolchain; the launcher is a normal PE executable. Native MSVC is not a JOREK target because there is no MSVC Fortran compiler. |
| Cross-compilation | Partial | `--cc/--cxx/--fc` may point at cross tools; the build driver honours them, but the dependency recipes still run on the host. |

On a machine with only a C/C++ compiler and Python, run:

```sh
python3 build.py bootstrap
```

It builds GNU make if needed, then GCC with C/C++/Fortran front ends, then
uses the new compiler for every subsequent dependency.  Building GCC takes a
while (typically 20–60 minutes); progress is logged under `build/logs/`.
On macOS the Apple toolchain supplies C/C++; GCC is only needed for Fortran.
On Windows, install the MSYS2 base system and the `mingw-w64` toolchain; the
builder uses those tools and never requires Visual Studio.

## Optional and proprietary components

JOREK itself guards some capabilities behind libraries that are not freely
redistributable:

* **WSMP** is proprietary IBM software.  It is never downloaded.  If you have
  a licensed installation, pass its paths through `--solver wsmp` and a
  dependency prefix containing `libpwsmp64`.
* **HIPS** is a legacy in-house solver.  The switch is preserved, but the
  source is not public; supply it explicitly if available.
* **IMAS-Core** is large and has its own data-dictionary build.  Use
  `--ima-root DIR` to consume an existing static build; the compile-time
  `USE_IMAS` path is otherwise fully wired.
* **ParaView Catalyst** is built from ParaView, which is a separate
  large project.  Use `--catalyst-root DIR` for an existing installation.

No compile-time JOREK switch is silently removed: every option in
`defaults.mk` has a feature name, and every solver backend has a recipe or an
explicit manual hook.

## Reproducibility and offline builds

Downloads are stored under `build/downloads/` with a digest lock file.  Once
the sources are cached:

```sh
python3 build.py --offline dist --matrix complete
```

works without network access.  `--force` rebuilds dependencies; otherwise
stamp files under `build/stamps/` make repeated runs incremental.

## Reference result

A locally verified macOS arm64 build is recorded in
[BUILD_RESULTS.md](docs/BUILD_RESULTS.md): 21 model variants, 42 solver variants, 447 packed
programs, a 3.12 GiB single file with a valid code signature, static system-only
dependencies, and a working embedded two-rank Hydra launch.

## Tests

```sh
./tests/run.sh
```

The suite tests compile tiny native payloads, build a real single-file suite,
and exercise selection, feature matching, caching, extraction, and
model inference from a JOREK namelist.

## Documentation

* [Design](docs/DESIGN.md)
* [Dependency catalogue](docs/DEPENDENCIES.md)
* [Variant and feature matrix](docs/VARIANTS.md)
* [Platform notes](docs/PLATFORMS.md)
* [MPI and clusters](docs/MPI.md)
* [Job scheduler adapters](docs/SCHEDULERS.md)
* [Solver backends](docs/SOLVERS.md)
* [Source acquisition policy](docs/SOURCES.md)
* [Complete matrix shorthand](#complete-matrix-shorthand)
* [Building the full Cartesian matrix](docs/CARTESIAN.md)
* [Embedded runtime scripts and native jorek-tool](docs/RUNTIME_SCRIPTS.md)
* [Single-file format](docs/SUITE_FORMAT.md)
* [Security](docs/SECURITY.md)

## Licence

The build orchestration and launcher are LGPL-3.0-or-later, matching JOREK.
See `LICENSE` and `NOTICE`.  Third-party components keep their own licences;
`docs/DEPENDENCIES.md` lists them.

## Complete matrix shorthand

`--everything` expands to the largest useful build:

```sh
python3 build.py --everything full
```

It sets:

```text
--matrix complete
--solver all
--programs all
--profile complete
--deps auto
```

This expands every model-family toggle (for example `with_TiTe`,
`with_vpar`, `with_rho`, `with_neutrals`, `with_impurities`).  Numeric
hard-coded parameters still need an explicit grid, because there is no finite
universal range for `n_tor`, `n_order`, grid capacities, and similar values:

```sh
python3 build.py --everything full \
  --param-grid n_tor=1,3,5 \
  --param-grid n_order=3,4
```

Use the planner before committing to a very large build:

```sh
python3 build.py matrix --everything --details
python3 build.py --everything --dry-run build --deps none --solver all
```

`full --everything` also builds the PaStiX 5 pass.  The resulting suite can be
much larger than the default 42-variant suite.

