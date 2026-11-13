# One-command Linux build

The supported entry point is:

```sh
./scripts/build-full-linux.sh \
  --root ../JOREK \
  --work /big/jorek-build \
  --jobs 32 \
  --from-source
```

This builds:

1. a static core dependency prefix (GCC from source only if no Fortran
   compiler is available; MPICH, OpenBLAS, ScaLAPACK, FFTW, parallel HDF5,
   METIS/GKlib, MUMPS, Boost);
2. static PaStiX 5, PaStiX 6 and STRUMPACK;
3. 21 `solver=all` model variants (MUMPS + PaStiX6 + STRUMPACK) with the
   `most` diagnostic set;
4. 21 `pastix5` model variants;
5. one merged file:

```text
/big/jorek-build/dist/jorek-suite
```

Run it with:

```sh
/big/jorek-build/dist/jorek-suite --info
/big/jorek-build/dist/jorek-suite --solver all --model 303 < input
/big/jorek-build/dist/jorek-suite --solver pastix5 --model 303 -- --version
/big/jorek-build/dist/jorek-suite --mpi -np 8 --scheduler slurm --solver all --model 750
```

## Requirements

* Linux x86-64 or arm64
* Python 3.8+
* a host C and C++ compiler (GCC or Clang); the script bootstraps GCC/Fortran
  if `gfortran` is absent
* `cmake`, `make`, `curl`, `tar`; `build.py bootstrap` builds CMake/make if
  they are missing from the host, but the initial bootstrap itself needs a
  working C/C++ compiler
* network access to GitHub, GitLab and the GNU/Boost/HDF5 mirrors
* disk: plan for 30–60 GiB; a full `--programs all` Cartesian build needs much
  more
* RAM: 8 GiB minimum; 32 GiB recommended for parallel linking

If the host is a minimal container, install the C/C++ toolchain first.  For
Debian/Ubuntu:

```sh
sudo apt-get update
sudo apt-get install -y build-essential gfortran cmake curl tar python3 \
  bison flex perl m4 pkg-config
```

The builder can bootstrap missing pieces, but a base compiler is the stated
contract.

## Using an existing static prefix

If your site already provides a static prefix containing MPICH, OpenBLAS,
ScaLAPACK, FFTW, HDF5, MUMPS, PaStiX 5/6 and STRUMPACK:

```sh
./scripts/build-full-linux.sh \
  --root ../JOREK --work /big/jorek-build --jobs 32 \
  --prefix /opt/jorek-static
```

The prefix must contain `libpastix.a` (PaStiX 6) and
`$prefix/pastix5/lib/libpastix.a` for the separate PaStiX 5 variants.  Use
`--strict` to stop at the first model failure instead of continuing.

## What can fail

This is a real source build, not a prebuilt binary:

* solver source archives and their submodules change; the script pins the
  versions used by the reference build, but upstream removals can break it;
* PaStiX 5 is old C code and needs `-Wno-implicit-function-declaration` on
  modern compilers; the script applies that;
* the Linux static libc (`libc.a`) must be installed for full `-static`;
  otherwise use `--no-full-static` (not a fully static distribution);
* proprietary WSMP/HIPS are not fetched;
* IMAS and ParaView Catalyst are not part of this one-command build.

Logs are under `$WORK/logs/`; dependency sources and logs are under
`$WORK/downloads/` and `$WORK/solver-src/`.

## Manual equivalent

The phases are ordinary commands, so they can be run separately:

```sh
python3 build.py --root ../JOREK --work /big/jorek-build --jobs 32 \
  --deps source --profile full --solver mumps bootstrap --with-deps

PREFIX=/big/jorek-build/solvers \
CORE_PREFIX=/big/jorek-build/deps \
WORK=/big/jorek-build/solver-src JOBS=32 \
  ./scripts/build_solvers_linux.sh

python3 build.py --root ../JOREK --work /big/jorek-build --jobs 32 \
  --deps none --dep-prefix /big/jorek-build/solvers \
  --dep-prefix /big/jorek-build/deps --mpi-prefix /big/jorek-build/deps \
  dist --matrix maximal --solver all --programs most \
  --output /big/jorek-build/dist/jorek-suite-solvers

# then the pastix5 build and tools/merge_suites.py as in the script
```

For the full feature/solver Cartesian product, use
[tools/build_cartesian.sh](../tools/build_cartesian.sh); a clean one-command
build of **all** feature combinations is a storage/time decision, not a
different build mechanism.
