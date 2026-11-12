# Variants, features, and hard-coded parameters

## Models

The current JOREK tree contains these models:

```
001 002 003 004 180 183 199 303 305 306 307 333
401 500 501 502 600 710 711 712 750
```

`build.py` enumerates `models/modelNNN` directories and is not hard-coded to
this list; new models are discovered automatically.  The model number is
compiled into the binary as `JOREK_MODEL` and selected by the executable name
(`jorek_model303`).

## Model families

For families, the extension flags are Fortran `parameter` logicals in
`models/modelNNN/mod_model_settings.f90`.  The complete matrix toggles:

| Model | Extensions |
| --- | --- |
| 180, 183 | `with_TiTe`, `with_vpar` |
| 600 | `with_TiTe`, `with_vpar`, `with_rho`, `with_neutrals`, `with_impurities` |
| 750 | `with_TiTe`, `with_neutrals`, `with_impurities` |

`with_refluid` is declared by 600 but explicitly marked unsupported in the
source and is therefore not enumerated.  It can still be forced with
`--enable`-style parameter editing if the code is updated.

## Feature names

| Name | Makefile switch | Notes |
| --- | --- | --- |
| `hdf5` | `USE_HDF5` | parallel HDF5, Fortran and HL interfaces |
| `fftw` | `USE_FFTW` | required for the FFT element-matrix path |
| `boost` | `USE_BOOST` | Boost.Math Bessel K |
| `std_besselk` | `USE_STD_BESSELK` | alternative, mutually exclusive with `boost` |
| `block` | `USE_BLOCK` | block matrix assembly |
| `direct` | `USE_DIRECT_CONSTRUCTION` | direct matrix construction |
| `quadtree` | `USE_QUADTREE` | quadtree neighbour search |
| `no_tree` | `USE_NO_TREE` | linear search; mutually exclusive with `quadtree` |
| `complex_precond` | `USE_COMPLEX_PRECOND` | complex preconditioner |
| `int64` | `USE_INTSIZE64` | 64-bit integer indices |
| `taskloop` | `USE_TASKLOOP` | OpenMP task loops in particle post-processing |
| `domm` | `USE_DOMM` | Dommaschk potentials |
| `ext_field` | `USE_EXT_FIELD` | external field; mutually exclusive with `domm` |
| `bicgstab` | `USE_BICGSTAB` | Krylov method; otherwise GMRES |
| `jec` | `JECCD` | ECCD current |
| `jec2diag` | `JEC2DIAG` | ECCD diagnostics |
| `imas` | `USE_IMAS` | requires `--ima-root` |
| `catalyst` | `USE_CATALYST` | requires `--catalyst-root` |
| `debug` | `DEBUG=1` | bounds checks, backtraces, `-O0` |

Solver backends are selected separately with `--solver`:

```
mumps  pastix6  pastix5  strumpack  wsmp  hips  all  none
```

PaStiX 5 and PaStiX 6 cannot be combined.  MUMPS, PaStiX 6, and STRUMPACK can
coexist in one executable; JOREK selects the backend from the input namelist
(`use_mumps`, `use_pastix`, `use_strumpack`).

## Hard-coded parameters

The following parameters are rewritten in `models/mod_settings.f90`:

```
n_tor n_coord_tor l_pol_domm n_period n_coord_period n_plane n_order
n_nodes_max n_elements_max n_boundary_max n_pieces_max
```

Use `--set NAME=VALUE` for a single value and `--param-grid NAME=V1,V2` for a
Cartesian grid across several parameters.  Validation enforces:

* `n_order` odd;
* `n_plane >= 2 * (n_tor - 1)`.

A variant with non-default parameters gets a suffix such as
`model303-n_tor5-n_plane8-mumps`.  At run time, `--params n_tor=5,n_plane=8`
selects it.  The manifest records the complete parameter set, not only the
diff.

## Program sets

* `main` — the model executable (`jorek_modelNNN`) only;
* `most` — the curated `most` list from JOREK's Makefile plus the main program;
* `all` — the upstream `all` target plus the main program (may be large and
  includes `eqdsk2jorek`, which needs Dierckx);
* `list` — print the `most` set without building.

Each executable is a separate program in the suite.  `--program jorek2vtk`
selects a diagnostic; `jorek2_main` is the default alias for the model main
program.
