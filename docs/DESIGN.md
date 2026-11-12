# Design

## Goals

1. A clean machine with Python and a C/C++ compiler can obtain every other
   build input from source and produce a complete JOREK binary.
2. All third-party code is statically linked into the JOREK executables.
   Only operating-system libraries may remain dynamic on platforms where the
   vendor does not ship static archives.
3. Every JOREK model, model-family extension, and compile-time feature can be
   built.  The resulting executables are packed into one native file that
   selects the right variant when it starts.
4. The build is observable, resumable, and auditable.  No downloaded binary
   blobs, no silent fallback to shared third-party libraries, no hidden
   package-manager dependency.

## Modules

| Module | Responsibility |
| --- | --- |
| `build.py` | CLI, orchestration, static verification, suite production |
| `src/common.py` | platform detection, subprocess/logging, download cache, archive extraction |
| `src/deps.py` | dependency records, recursive graph, Autoconf/CMake/Make builders, special recipes |
| `src/jorek.py` | model/feature matrix, Fortran settings rewriting, `Makefile.inc` generation, Make backend, static checks |
| `src/suite.py` | manifest, solid-DEFLATE groups, footer, Mach-O section embedding |
| `src/jorek_suite.c` | dependency-free native launcher |

The Python packages use only the standard library.  The C launcher uses only
POSIX or Win32 APIs and the vendored `puff` decoder.  It does not require MPI,
HDF5, zlib, or a C++ runtime.

## Build pipeline

```
doctor
  └─ detect OS/arch/libc/toolchain

bootstrap (optional)
  └─ gmake → gmp/mpfr/mpc/isl → gcc(c,c++,fortran)
      └─ binutils, pkgconf, cmake, perl, autotools, ... (on demand)

deps
  └─ download → extract → configure → make/cmake → install(stamp)
      └─ repeat recursively for every declared dependency

variant
  ├─ copy JOREK source (hard links where safe)
  ├─ rewrite models/mod_settings.f90
  ├─ rewrite models/modelNNN/mod_model_settings.f90
  ├─ write Makefile.inc with absolute static archives
  └─ make -j N <main and diagnostics>

suite
  ├─ compress each variant's executables as one solid DEFLATE group
  ├─ write manifest (variant, features, parameters, hashes)
  ├─ build launcher
  ├─ append group(s)+manifest+footer, or place them in a Mach-O section
  └─ ad-hoc sign on macOS
```

### Why the make backend?

JOREK's own build system already understands every source-level switch and
diagnostic target.  Re-implementing its dependency graph would duplicate a
large amount of fragile logic and would silently diverge from upstream.  The
builder therefore stages a private copy and uses the upstream Makefile, but
generates the configuration itself and supplies absolute static archive paths.
A future native backend can be added without changing the variant or suite
layers; the manifest is independent of the build backend.

### Dependency graph

Dependencies are resolved with a depth-first post-order walk.  Cycles are
detected and reported.  Every record declares:

* version, URL, licence, and optional digest;
* prerequisites (which are resolved recursively);
* a builder (`autotools`, `cmake`, `gnu_make`, or a named special case);
* the static archives and headers that must exist after installation.

A package is considered built only if all declared archives exist.  This
prevents a partially installed dependency from poisoning later builds.

The download cache is content-addressed by a lock file.  If the upstream
project supplies a digest, it is enforced.  If not, the first download is
recorded and subsequent builds verify against the recorded digest (trust on
first use).  `--no-checksum` disables this for constrained environments.

### Static linkage

The builder never passes a bare `-lhdf5` that could resolve to `libhdf5.dylib`
or `libhdf5.so`.  Instead it resolves each archive under the private prefix and
passes its absolute path.  GCC runtime archives are resolved with
`gfortran -print-file-name=...`.  MPI is linked from `libmpi.a`,
`libmpifort.a`, and `libpmpi.a`.

On Windows and macOS, the platform's own system libraries remain dynamic:
Apple does not distribute static archives for `libSystem`, and Windows
applications necessarily import `KERNEL32` and friends.  Everything else is
static.

`verify_static()` inspects the produced executable.  On Linux it reads the
dynamic section; on macOS it uses `otool -L`; on Windows it reads PE imports
with `objdump -p`; on BSD it uses `ldd`.  The verification is opt-out only with
`--no-verify` and is part of the default path.

### Suite format

The suite is an ordinary native executable followed by payload and metadata.
On macOS, appending to a Mach-O invalidates the code signature, so the payload
is placed in a `__DATA,__jorek_suite` section and the final image is signed by
the linker.  See [SUITE_FORMAT.md](SUITE_FORMAT.md).

### Failure model

* Missing mandatory host tool: a clear `BuildError` and a suggested bootstrap
  command; no partially built suite is emitted.
* Dependency build failure: the dependency log path is included; the stamp is
  not written; a retry resumes at the failed package.
* JOREK make failure: the build log is preserved and the staged source is kept
  for inspection (`--keep-source`).
* Static verification failure: the exact offending libraries are printed.
  `--no-verify` only skips the check; it never changes the link line.

## Extending

Add a dependency record in `src/deps.py`, register a builder if it is unusual,
and add it to `core_dependencies()` for the relevant profile.  Add a feature
name in `src/jorek.py` and a line in `Makefile.inc`.  Add a variant dimension
in `_collect_variants()`.  The suite manifest is generic: no launcher change is
needed for new models, features, or parameters.
