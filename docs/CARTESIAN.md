# Building the full Cartesian matrix

The complete product of models × model-extension toggles × compile-time
features × solvers × parameter grids is **not** a different build mechanism.
It is a loop around the existing `build.py` options.  The hard part is size,
time, and invalid combinations, not a missing flag.

## What is already a flag

| Axis | Option |
| --- | --- |
| model | `--model 001` or `--matrix models` |
| model extension toggles | `--matrix complete` (all combinations) |
| solver | `--solver mumps`, `pastix6`, `pastix5`, `strumpack`, `all` |
| feature subset | `--features ...` / `--without ...` |
| numeric parameters | `--param KEY=VALUE`, `--toggles KEY=1` |
| programs | `--programs main`, `most`, `all`, `list` |

`build.py matrix --matrix complete --json` prints every toggle combination.
`build.py build` builds one product point; a shell loop over solvers, feature
subsets, and parameter grids builds the Cartesian product.

## Estimate

For a model with `T` extension toggles there are `2^T` toggle combinations
(model 600 has 5, model 750 has 3).  If `F` independent feature switches are
varied and `S` solvers are used, the number of JOREK binaries is:

```text
N = sum_over_models(2^T_model) × 2^F × S × P
```

where `P` is the number of numeric parameter-grid points.  Even the "small"
case `F=8, S=4, P=1` is 1024 binaries per model.  At roughly 25–50 MiB per
binary, the suite is hundreds of GiB before compression.  `F=15` is 32768
feature subsets and reaches TiB scale.

## Generator script

`tools/build_cartesian.sh` is a deliberately conservative generator.  It
prints the commands by default; `--execute` actually runs them:

```sh
tools/build_cartesian.sh --dry-run --features hdf5,fftw,block,direct,quadtree
tools/build_cartesian.sh --execute --features hdf5,fftw,block,direct \
  --solvers mumps,pastix6,strumpack --jobs 32 --work /big/jorek-cartesian
```

It does **not** pretend that every combination is valid.  Known invalid or
unsupported points are skipped or reported:

- PaStiX 5 and PaStiX 6 cannot be compiled into one binary; use separate
  solver variants (as the reference suite does).
- WSMP and HIPS require proprietary/user-supplied libraries.
- model 180/183 quadtree has an upstream API mismatch; the builder disables
  that combination.
- some feature combinations lack their optional headers/libraries (IMAS,
  Catalyst, etc.) and must be supplied explicitly.
- model-family combinations can hit the upstream defects documented in
  `VARIANTS.md`; those are source bugs, not option-plumbing differences.

## Disk

The reference build’s 21 models × two solver sets (447 programs) is
3.12 GiB compressed.  A full feature Cartesian product easily exceeds this by
orders of magnitude.  Use `--output` on a large filesystem, keep the per-variant
object trees (`--keep-source`) only if you need incremental rebuilds, and
expect the suite linker to need temporary space comparable to the uncompressed
payload.
