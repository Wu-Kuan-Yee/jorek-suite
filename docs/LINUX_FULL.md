# One-command Linux/macOS full build

The entry point is a Python subcommand, not a Bash script:

```sh
python3 build.py full \
  --root ../JOREK \
  --work /big/jorek-build \
  --jobs 32
```

If JOREK is not present, let the same command clone it:

```sh
python3 build.py full --fetch-source --work /big/jorek-build --jobs 32
```

Without `--fetch-source` the builder only searches for an existing checkout;
it does not silently download source.  The default search order is
`JOREK_ROOT`, `../JOREK`, the parent directory, and the current directory.

`build.py` is the cross-platform implementation; the same command works on
Linux and macOS (and the POSIX backend generally).  It:

1. builds the static core stack (MPICH, OpenBLAS, ScaLAPACK, FFTW, parallel
   HDF5, METIS/GKlib, MUMPS, Boost); a missing Fortran compiler is bootstrapped
   from GCC source;
2. builds static PaStiX 6, STRUMPACK and PaStiX 5 with `src/solvers.py`;
3. builds 21 `solver=all` model variants with the `most` diagnostics;
4. builds 21 `pastix5` model variants;
5. merges everything into one native suite using
   `build.py`'s own suite merger (no external shell script).

Output:

```text
/big/jorek-build/dist/jorek-suite
```

Use it:

```sh
/big/jorek-build/dist/jorek-suite --info
/big/jorek-build/dist/jorek-suite --solver all --model 303 < input
/big/jorek-build/dist/jorek-suite --solver pastix5 --model 303 -- --version
/big/jorek-build/dist/jorek-suite --mpi -np 8 --scheduler slurm --solver all --model 750
```

## Existing static prefix

If the site already has a complete static prefix:

```sh
python3 build.py full \
  --root ../JOREK --work /big/jorek-build --jobs 32 \
  --dep-prefix /opt/jorek-static \
  --no-build-solvers \
  --solver-prefix /opt/jorek-static
```

The prefix must contain MUMPS, OpenBLAS, ScaLAPACK, FFTW, HDF5, PaStiX 5/6
and STRUMPACK.  The PaStiX 5 archive is expected at
`$solver_prefix/pastix5/lib/libpastix.a`.

## Requirements

* Linux x86-64/arm64 or macOS
* Python 3.8+
* a host C and C++ compiler; `gfortran` is bootstrapped if absent
* `cmake`, `make`, `curl`, `tar` (the builder can bootstrap CMake/make, but a
  base compiler is required)
* network access for the source archives
* disk: 30–60 GiB for the reference suite; much more for the full feature
  Cartesian product
* RAM: 8 GiB minimum, 32 GiB recommended for parallel linking

Debian/Ubuntu base packages:

```sh
sudo apt-get update
sudo apt-get install -y build-essential gfortran cmake curl tar python3 \
  bison flex perl m4 pkg-config
```

## Notes

* `--strict` is not needed here; `full` continues over model failures and
  reports them.  Use `--keep-going` explicitly only for lower-level commands.
* Logs are under `$WORK/logs/`; downloaded archives under
  `$WORK/downloads/`.
* PaStiX 5 and 6 are compile-time exclusive in JOREK, so they are separate
  variants in the same file rather than one binary.
* WSMP/HIPS are proprietary and are not fetched.  IMAS/Catalyst are optional
  external layers.
* The full feature Cartesian product is a separate planning problem:
  `python3 tools/build_cartesian.py ...` (see `CARTESIAN.md`).

## Manual phases

```sh
python3 build.py --root ../JOREK --work /big/jorek-build --jobs 32 \
  --deps source --profile full --solver mumps bootstrap --with-deps

python3 build.py --root ../JOREK --work /big/jorek-build --jobs 32 \
  --deps none --dep-prefix /big/jorek-build/solvers \
  --dep-prefix /big/jorek-build/deps \
  --mpi-prefix /big/jorek-build/deps \
  dist --matrix maximal --solver all --programs most \
  --output /big/jorek-build/dist/jorek-suite-solvers

python3 build.py --root ../JOREK --work /big/jorek-build --jobs 32 \
  --deps none --dep-prefix /big/jorek-build/solvers/pastix5 \
  --dep-prefix /big/jorek-build/solvers \
  --dep-prefix /big/jorek-build/deps \
  build --matrix maximal --solver pastix5 --programs main

python3 tools/merge_suites.py --root ../JOREK --work /big/jorek-build/merge \
  --variant-dir /big/jorek-build/variants \
  --output /big/jorek-build/dist/jorek-suite
```
