# Job schedulers and MPI launchers

The suite embeds MPICH/Hydra, so it does **not** need an `mpirun` installation
on the target.  A single generic launcher cannot be correct for every batch
system, however: each scheduler has its own allocation, host-list, and launch
protocol.  The launcher therefore provides adapters.

## Local and direct

| `--scheduler` | Mechanism |
| --- | --- |
| `fork` / `local` | Hydra fork launcher (default when no scheduler env is detected) |
| `ssh` | Hydra SSH launcher + `--hostfile`/`--hosts` |
| `rsh` | Hydra RSH launcher |
| `manual` | Hydra manual launcher (site-provided environment) |

## Batch systems

| System | Adapter | Notes |
| --- | --- | --- |
| Slurm | `--scheduler slurm` | uses `srun -n N` when available, otherwise Hydra `-launcher slurm` |
| PBS Professional | `--scheduler pbs` | uses `PBS_NODEFILE` to build a host file and Hydra SSH; site SSH/pbsdsh policy applies |
| OpenPBS | `--scheduler openpbs` | same as PBS |
| TORQUE | `--scheduler torque` | same as PBS |
| IBM Spectrum LSF | `--scheduler lsf` | Hydra `-launcher lsf` |
| Grid Engine / SGE | `--scheduler sge` | Hydra `-launcher sge` |
| Flux | `--scheduler flux` | native `flux run -n N` |
| IBM LoadLeveler | `--scheduler ll` | Hydra `-launcher ll` |
| Cobalt | `--scheduler cobalt` | Hydra `-launcher cobalt` |
| IBM Spectrum MPI / Summit | `--scheduler jsrun` | native `jsrun -n N` |
| Cray APRun | `--scheduler aprun` | native `aprun -n N` |
| IBM POE | `--scheduler poe` | native `poe -procs N` |
| HTCondor | — | **not automatically supported**; parallel-universe configuration is site-specific. Submit the suite through `condor_submit -interactive`/a site MPI wrapper, or use `--launcher ssh` with a host file. |
| Others | `--scheduler ssh` + `--hostfile` | works when the site provides an SSH-reachable host list and shared paths |

`--scheduler auto` (the default) detects `SLURM_JOB_ID`, `FLUX_URI`,
`PBS_JOBID`, `LSB_JOBID`, `SGE_JOB_ID`, and `CONDOR_CONFIG`.

## Shared paths

Every node must see:

* the suite at the same absolute path;
* the extracted helper/script directory at the same absolute path.

Use `--extract-helpers /shared/...` once and `--helper-dir /shared/...` on all
runs.  This is required for Hydra's remote `hydra_pmi_proxy` and for the
runtime scripts.

## What "all schedulers" would require

A universal single binary would have to embed every scheduler's client
library, protocol, and security model, and would still depend on site
configuration (SSH keys, PMI, allocation environment, accounting).  The
adapter design above covers the common systems explicitly; HTCondor and any
vendor-specific system need a site integration.  This is a deliberate,
documented boundary rather than a silent claim of universal support.
