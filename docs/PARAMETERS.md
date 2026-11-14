# Hard-coded JOREK parameters

These parameters live in `models/mod_settings.f90`.  They are Fortran
`parameter` constants, so they are fixed when JOREK is compiled.  They are
**not** namelist inputs.  Use `--param-max`/`--param-grid` to embed several
compiled variants in one suite.

| Name | Meaning | Legal values / constraints |
| --- | --- | --- |
| `n_tor` | Number of toroidal harmonics (modes) | odd, `>= 1`; startup rejects even values |
| `n_coord_tor` | Number of toroidal harmonics in the `(R,Z)` coordinate mapping | odd, `>= 1`; `> 1` requires `rst_hdf5_version > 1`; GVEC import also requires it not exceed the imported mode count |
| `l_pol_domm` | Highest poloidal mode in the Dommaschk potentials | `>= 0`; used by Dommaschk/3-D equilibrium paths |
| `n_period` | Toroidal periodicity of the simulation | `>= 1`; mode numbers are multiples of this value |
| `n_coord_period` | Number of field periods of the coordinate mapping/device | `>= 1`; for GVEC it must equal the imported number of periods and be a multiple of `n_period` |
| `n_plane` | Number of toroidal planes/angles | `n_plane >= 2*(n_tor-1)`; without FFTW, if `n_tor >= n_tor_fft_thresh` it must be a power of two; non-power-of-two is only a performance warning when FFTW is enabled |
| `n_order` | Polynomial basis order | odd (`1,3,5,...`); `n_order > 3` is incompatible with `treat_axis=.true.` and `freeboundary=.true.`; `n_order >= 7` needs generated `mod_basisfunctions.f90` and `gauss.f90` (`n_gauss > 8`) |
| `n_nodes_max` | Maximum number of grid nodes | `>= 1`; capacity, actual grid must fit |
| `n_elements_max` | Maximum number of grid elements | `>= 1`; capacity |
| `n_boundary_max` | Maximum number of boundary elements | `>= 1`; capacity |
| `n_pieces_max` | Maximum number of line pieces per flux surface | `>= 1`; capacity |

Fixed internal constants that must not be treated as user knobs:

```text
n_dim        = 2
n_vertex_max = 4
nref_max     = 10   (refinement, not currently working)
n_ref_list   = 10
```

## `--param-max` expansion rules

* `n_tor`, `n_coord_tor`, `n_order`: odd values from 1 through the maximum.
* `l_pol_domm`: values from 0 through the maximum.
* `n_plane`: 1 through the maximum, then invalid
  `n_plane < 2*(n_tor-1)` combinations are skipped.
* `n_nodes_max`, `n_elements_max`, `n_boundary_max`, `n_pieces_max`: only the
  maximum value is built; smaller capacities would be redundant.
* `n_period`, `n_coord_period`: 1 through the maximum.

`--param-grid` can still be used for explicit value lists, including capacity
values.

## Not runtime-selectable

`--params` in the suite selects an already embedded compiled variant.  It does
not change these constants at execution time.  Changing one requires compiling
or selecting a different variant.
