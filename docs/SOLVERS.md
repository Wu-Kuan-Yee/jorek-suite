# Sparse direct solvers

JOREK selects a direct solver at run time from the input namelist.  The
compile-time switches are:

| Backend | Compile switch | Typical namelist flags | Build status in this repository |
| --- | --- | --- | --- |
| MUMPS | `USE_MUMPS` | `use_mumps`, `use_mumps_eq`, `use_mumps_prj` | **statically embedded in the reference suite** |
| PaStiX 6 | `USE_PASTIX6` | `use_pastix`, `use_pastix_eq`, `use_pastix_prj` | **statically embedded in the reference suite** |
| PaStiX 5 | `USE_PASTIX` | same | **statically embedded as separate `pastix5` variants** |
| STRUMPACK | `USE_STRUMPACK` | `use_strumpack`, ... | **statically embedded in the reference suite** |
| WSMP | `USE_WSMP` | `use_wsmp` | proprietary IBM library; never downloaded |
| HIPS | `USE_HIPS` | historical switch | in-house legacy library; not publicly distributed |

The launcher exposes a solver selector:

```sh
jorek-suite --list
jorek-suite --solver all --model 303 < input
jorek-suite --solver pastix5 --model 303 -- --version
# or build separate solver variants and select them:
jorek-suite --model 303 --solver mumps < input
jorek-suite --model 303 --solver pastix6 < input
jorek-suite --model 303 --solver strumpack < input
```

`--solver` is a **variant selector**.  A suite can contain several variants of
the same model, each linked with a different backend; the launcher picks the
matching one.  A single binary can also contain several backends
(`build.py --solver all`), in which case the namelist's `use_*` flags choose
the backend at run time.  The reference suite uses the latter layout:

```sh
jorek-suite --solver all --model 303 < input
# then choose use_mumps / use_pastix / use_strumpack in the namelist
```

The all-solver binary is one executable per model; it does not contain the
full cross-product of every solver and every feature combination.  PaStiX 5
is a separate variant per model because JOREK's PaStiX 5 and PaStiX 6
interfaces are compile-time exclusive.  The reference suite therefore carries
42 variants: 21 `solver=all` binaries and 21 `pastix5` binaries.

## Why the reference suite uses MUMPS

The reference build has static MUMPS, PaStiX 6 and STRUMPACK libraries, with
PaStiX 5 carried as a separate variant. STRUMPACK is built with METIS,
ParMETIS, SCOTCH and PT-Scotch ordering when those dependencies are selected;
the standard all-solver dependency set now includes all four CPU ordering
backends. Its build verifies the generated feature macros and links a small
consumer against the installed CMake target, so missing static transitive
libraries surface during the build. All solver libraries are linked into the
model variants that support them.

The builder does not pretend that a proprietary backend exists. WSMP and HIPS
require a user-supplied, licensed installation; their switches are preserved
and can be linked through a dependency prefix. ParMETIS is fetched from its
upstream source and built locally; its upstream terms should be reviewed before
commercial use or redistribution.

## Building alternative variants

```sh
# MUMPS is the default
python3 build.py dist --model 303 --solver mumps

# PaStiX 6 (after building/installing PaStiX and an ordering library)
python3 build.py --deps none --dep-prefix /path/to/pastix-prefix \
  dist --model 303 --solver pastix6

# STRUMPACK
python3 build.py --deps none --dep-prefix /path/to/strumpack-prefix \
  dist --model 303 --solver strumpack
```

To embed multiple solver variants of the same model, run the build once per
solver with different output names and combine the variant manifests, or use
`--solver all` to link every available backend into one executable.
