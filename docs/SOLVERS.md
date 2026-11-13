# Sparse direct solvers

JOREK selects a direct solver at run time from the input namelist.  The
compile-time switches are:

| Backend | Compile switch | Typical namelist flags | Build status in this repository |
| --- | --- | --- | --- |
| MUMPS | `USE_MUMPS` | `use_mumps`, `use_mumps_eq`, `use_mumps_prj` | source recipe; **embedded in the reference suite** |
| PaStiX 6 | `USE_PASTIX6` | `use_pastix`, `use_pastix_eq`, `use_pastix_prj` | CMake recipe; needs PaStiX 6 + ordering libraries |
| PaStiX 5 | `USE_PASTIX` | same | legacy `pastix-conf` recipe; Murge optional |
| STRUMPACK | `USE_STRUMPACK` | `use_strumpack`, ... | CMake recipe; needs ScaLAPACK/METIS/ParMETIS/SCOTCH |
| WSMP | `USE_WSMP` | `use_wsmp` | proprietary IBM library; never downloaded |
| HIPS | `USE_HIPS` | historical switch | in-house legacy library; not publicly distributed |

The launcher exposes a solver selector:

```sh
jorek-suite --list
jorek-suite --model 303 --solver mumps < input
jorek-suite --model 303 --solver pastix6 < input
jorek-suite --model 303 --solver strumpack < input
```

`--solver` is a **variant selector**.  A suite can contain several variants of
the same model, each linked with a different backend; the launcher picks the
matching one.  A single binary can also contain several backends
(`build.py --solver all`), in which case the namelist's `use_*` flags choose
the backend at run time.

## Why the reference suite uses MUMPS

MUMPS was the only backend whose complete static dependency chain was
available and validated in the reference build (MPI, ScaLAPACK, OpenBLAS,
METIS, MUMPS/PORD).  PaStiX and STRUMPACK are supported by the build logic and
have source recipes, but their static builds have additional third-party
dependencies and were not part of that artifact.

The builder does not pretend that a proprietary backend exists.  WSMP and HIPS
require a user-supplied, licensed installation; their switches are preserved
and can be linked through a dependency prefix.

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
