# MPI and multi-node execution

The suite contains **the MPI launcher as well as the MPI library**:

* the JOREK executables link MPICH (`libmpi.a`, `libmpifort.a`, `libpmpi.a`)
  statically;
* the launcher payload contains `mpiexec` (Hydra), `hydra_pmi_proxy`,
  `hydra_nameserver`, `hydra_persist`, and the private `hwloc` library used by
  Hydra.

No `mpirun`, MPICH installation, module load, compiler, or Python is required
on the target machine.

## Local multi-process run

```sh
jorek-suite --mpi -np 8 --model 303 < namelist/model303/intear_square
jorek-suite --mpi -np 4 --model 600 --params with_TiTe=1,with_vpar=1 < input
jorek-suite --mpi -np 2 --model 303 -- --version   # launcher smoke test
```

The suite extracts the helper directory into the user cache, runs the bundled
`mpiexec`, and lets Hydra start the same suite once per rank.  Each rank then
selects the same embedded JOREK variant.  The extraction cache is safe for
concurrent rank starts: entries are written to a temporary file and renamed
atomically after SHA-256 verification.

Use `--print-command` to inspect the exact command without running it:

```sh
jorek-suite --mpi -np 8 --hostfile hosts --launcher ssh --model 303 --print-command
```

## Shared-filesystem cluster

For a cluster the normal layout is a shared home or project directory:

```sh
# Once, from the login node:
jorek-suite --extract-helpers /shared/project/jorek-mpi-helpers

# Every node sees the same suite and helper directory:
mpiexec -np 512 -f hosts -launcher ssh \
  /shared/project/jorek-suite \
  --mpi-helper-dir /shared/project/jorek-mpi-helpers \
  --mpi -np 512 --model 303
```

Actually the bundled launcher can do the last step itself:

```sh
jorek-suite --mpi -np 512 --hostfile hosts --launcher ssh \
  --helper-dir /shared/project/jorek-mpi-helpers --model 303 < input
```

Requirements:

* the suite file is visible at the **same absolute path** on every node;
* the helper directory is visible at the same absolute path on every node (use
  `--extract-helpers` once on shared storage);
* passwordless `ssh` (or a supported scheduler launcher) is configured;
* all nodes have the same OS and CPU architecture;
* the embedded JOREK variant is built for that architecture.

If paths differ between nodes, use a shared filesystem or `--launcher ssh`
with a site-specific wrapper.  Hydra does not copy the suite to remote nodes;
that is a deliberate security choice.

## SLURM / PBS / LSF

Hydra can use a site launcher:

```sh
salloc -N 8 -n 512 jorek-suite --mpi -np 512 --launcher slurm --model 303 < input
# or:
mpiexec -np 512 --launcher slurm jorek-suite --model 303 < input
```

For sites that provide their own `mpiexec`, `--mpi` falls back to the system
launcher when the suite has no embedded helper.  Set `--helper-dir` to select
a shared copy explicitly.

## Threads and ranks

JOREK uses OpenMP inside each MPI rank.  Set `OMP_NUM_THREADS` as usual:

```sh
OMP_NUM_THREADS=4 jorek-suite --mpi -np 16 --model 303 < input
```

The MPI library is built with the same Fortran/C toolchain as the rest of the
suite.  `MPICH` is used by default; the dependency catalogue also contains an
OpenMPI recipe for sites that require it.

## Security

The helper files are verified by SHA-256 before extraction.  The launcher
only writes inside its cache or a directory explicitly passed with
`--extract-helpers`/`--helper-dir`.  Do not use helper directories writable by
other users on a shared cluster: they are executable payloads.
