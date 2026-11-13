# Reference build result (macOS arm64)

This page records a concrete build made with this repository.  It is a test
record, not a platform-independent promise.

## Environment

* macOS arm64, Apple silicon
* Python 3.9.6
* Homebrew GCC 16.2.0 and gfortran, MPICH 5.0.2
* Static test prefix: HDF5 1.14.6 (parallel, Fortran/HL), FFTW 3.3.11,
  OpenBLAS, METIS, MUMPS 5.7.3 (PORD), ScaLAPACK 2.2.0, Boost.Math, zlib,
  libaec/SZIP
* JOREK `v2.26.09.0-95-g8c8e9c3d21db`

## Command

```sh
python3 build.py \
  --deps none --dep-prefix ... --mpi-prefix /opt/homebrew/opt/mpich \
  --keep-source --jobs 12 \
  dist --matrix maximal --programs most \
  --output build/dist/jorek-suite-all
```

`--matrix maximal` builds one variant per model with every supported model
extension enabled:
180 (`with_TiTe`, `with_vpar`), 600 (all five), 750 (all three).  Model 183 is
built with its default extension set because `algexpr2fort` has an upstream
bounds defect when both 183 extensions are enabled.  `--programs most` adds the
curated diagnostic set to every model.

## Result

| Property | Value |
| --- | --- |
| Models / variants | 21 |
| Packed programs | 496 |
| Uncompressed payload | 10447.98 MiB |
| Single-file size | 2.53 GiB |
| Runtime script/helper files | 168 + 6 MPI + jorek-tool (175 total) |
| Static dependency check | passed (system libraries only) |
| macOS code signature | valid ad-hoc signature |
| Embedded MPI helpers | `mpiexec`, `hydra_pmi_proxy`, `hydra_nameserver`, `hydra_persist`, `mpiexec.hydra`, `libhwloc` |
| Embedded runtime scripts | `util/`, `run_jorek/`, scheduler templates, particle post-processing |
| Local MPI smoke test | `--mpi -np 2 --model 600 -- --version` reports 2 MPI ranks |

Programs per model include `jorek2_main`, `jorek2vtk`, `jorek2_postproc`,
`jorek2_poincare`, `jorek2_four`, `jorek2_diagno`, connection/strike tools,
HDF5 restart converters, and the rest of the upstream `most` target.

## Compatibility fixes applied by the builder

These are narrow source fixes for defects that a current GNU Fortran rejects
or that prevent linking.  They are applied only to a staged copy and are
documented here so the transformation is auditable.

| Model | File | Fix |
| --- | --- | --- |
| 001 | `models/model001/mod_elt_matrix.f90` | remove duplicate `aux_nodes` declaration |
| 180/183 | generated `defaults.mk` | strip Intel-only `-heap-arrays` for GNU |
| 180/183 | feature selection | use R-tree; the quadtree path calls the wrong API |
| 305/306 | `initial_conditions.f90` | declare `Omega` derivatives |
| 306 | `matrix/construct_matrix_mod.f90` | match the model-specific element-matrix interfaces |
| 333 | `construct_variables.f90` | rename dummy `VAR_t` colliding with parameter `var_T` |
| 401/502/711/712 | diagnostic integral modules | pass the third source argument required by those models |
| 712 | `initialise_parameters.f90` | replace invalid logical `==` with `.eqv.` |

The complete family matrix (`--matrix complete`, 65 variants) remains available
for users who need each extension combination separately; some combinations
may hit the upstream defects above and are reported rather than silently
skipped.

## Running the reference suite

```sh
build/dist/jorek-suite-all --info
build/dist/jorek-suite-all --model 303 --program jorek2vtk < input
build/dist/jorek-suite-all --mpi -np 8 --model 303 < namelist
```

For a cluster, see [MPI.md](MPI.md).  The helper directory must be on shared
storage and the suite must be at the same absolute path on all nodes.
