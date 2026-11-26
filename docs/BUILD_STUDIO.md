# JOREK Build Studio

JOREK Build Studio is an optional, minimal Qt desktop frontend for the
existing JOREK suite builder. The command-line builder remains the source of
truth for model discovery, feature handling, dependency resolution, variant
planning, compilation, and packaging.

Local build commands are launched as a separate process using an argument
vector, not through a shell. The optional remote builder sends a fixed,
shell-quoted command sequence over OpenSSH; it does not source or execute
arbitrary imported shell scripts.

## Requirements

- Python 3.9 or later.
- PySide6 6.7 or later.
- A checkout of jorek-suite containing build.py and its src directory.
- A JOREK source checkout for model planning and actual builds.
- The native compiler/MPI/dependency requirements documented by the builder.

The CLI itself remains standard-library-only. PySide6 is an optional GUI
dependency.

## Install and launch from a checkout

From the root of the jorek-suite checkout:

```sh
python3 -m pip install -e '.[gui]'
python3 -m jorek_build_studio
```

The console entry point is also installed:

```sh
jorek-build-studio
```

For a temporary environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[gui]'
python -m jorek_build_studio
```

On Windows PowerShell, activate with:

```powershell
.venv\Scripts\Activate.ps1
```

## First launch

1. Open the Overview tab.
2. Confirm the build driver points to this checkout's build.py.
3. Select the JOREK source directory containing jorek2_main.f90 and models/.
4. Choose a dedicated work directory. Do not point it at the JOREK source tree.
5. Click Inspect checkout. The GUI reads model, feature, and dependency
   metadata from the selected checkout.
6. Click Check environment to run build.py doctor --json.
7. Open Configure and choose the action, model matrix, solver, dependency
   policy, programs, compiler overrides, and optional compile-time features.
8. Enter parameter-grid expressions only when required. Use one expression
   per line, for example n_tor=1,3,5.
9. Review Advanced for optional source overrides, toolchain prefixes, family
   extension settings, and run options. These settings are not required for a
   normal first build.
10. Click Preview matrix before starting an expensive build.
11. Click Start build and inspect the Build tab log. The full log is also
    written below the user's application configuration directory in logs/.

## Tabs

### Overview

Select build.py, the JOREK source checkout, and a separate work directory.
Inspect the builder's model, feature, and dependency catalogue. Check
compiler/tool availability with the builder's doctor command.

The legacy build.sh importer reads the file as text only. It extracts a
conservative set of literal assignments and reports lines it cannot map.
It does not source, evaluate, or execute the script.

### Configure

Choose one of three builder actions:

- Build executables only: invokes build.py build.
- Build and package: invokes build.py dist.
- Full solver suite: invokes build.py full, which builds the additional solver
  stacks and can consume substantial time and disk space.

Choose the model matrix:

- One model / one variant.
- One variant per model.
- Maximal extension set per model.
- Complete model-extension combination matrix.

The available feature switches and their default states are read from
src/jorek.py. The GUI emits only overrides that differ from those defaults.
For a selected model, family-extension controls are read from its model
settings file; the GUI emits --family-toggle overrides only when a value
differs from the source default. Hard-coded parameter grids and maxima are
passed to the existing CLI; they are not interpreted by a second planner.

### Advanced

Expose less frequently used CLI options: source cloning, private toolchain
prefixes, extracted dependency trees, primary/fallback mirrors, IMAS and
Catalyst prefixes, full-build solver prefixes, debug mode, source retention,
verification policy, and the Everything preset. The same tab can configure a
previously built suite run, including its model/variant/program selectors,
standard input file, and arguments such as --mpi -np 8. The suite verifier emits
a machine-readable portability report covering executable format, architecture,
non-system dynamic dependencies, macOS signature status, and a launcher info
smoke test.

### Dependencies

List dependency recipes, inspect the dependency graph, fetch source archives,
build the selected dependency closure, or verify static library installation.
The optional dependency filter accepts comma-separated dependency names.
Bootstrap toolchain builds the private dependency prefix and can also build the
full core dependency profile. It requires a working host C and C++ compiler;
without a host Fortran compiler, the bootstrap attempts to build GCC with
Fortran support.

### Build

Preview the matrix, start a build, verify a generated suite, run a suite, or
cancel the current builder process. Verification invokes build.py verify --json
and displays a structured report covering executable format, architecture,
non-system dynamic dependencies, macOS signature status, and the launcher info
smoke test. Output is displayed in the window and written to a persistent
timestamped log file. The build driver's own logs remain available in its work
directory.

### History

Completed tasks are recorded in history.jsonl with start/end timestamps,
exit status, command arguments, and the associated log path. Double-click a
log-file cell to open that log with the operating system default viewer.

### Remote

Create named SSH profiles for native builds on Linux, macOS, or BSD hosts.
The connection test reports the remote OS, CPU architecture, Python, and Git.
A build checks out a pinned full commit SHA, runs the selected packaged (dist)
or full-solver build, runs the remote portability verifier, and downloads the
artifact only after verification succeeds. Build-only mode is not offered here
because it does not produce the single-file suite that this workflow transfers. Local absolute compiler and dependency paths are
cleared before remote arguments are constructed. Enter machine-specific paths
as JSON overrides. The local machine needs OpenSSH ssh and scp; the remote
machine needs Git, Python 3, and a working native compiler/MPI/dependency
environment. Configure public-key or agent authentication in OpenSSH; the
builder uses batch mode and does not prompt for passwords. Strict host-key
checking is enabled by default; pre-populate known_hosts or deliberately
choose the accept-new policy for a first connection. SSH config aliases and
options such as ProxyJump, ForwardAgent, ControlMaster, and ControlPath can be
used for bastion and cluster-login workflows.

### Help

Help text is read directly from the selected build.py argparse interface.
This prevents the GUI's command reference from silently diverging from the
CLI parser.

## Configuration and logs

The GUI stores settings as JSON in the platform's standard per-user
configuration directory:

- Linux/BSD: $XDG_CONFIG_HOME/jorek-build-studio, or ~/.config/...
- macOS: ~/Library/Application Support/jorek-build-studio
- Windows: %APPDATA%/jorek-build-studio

Task logs are stored in the logs/ subdirectory. Configuration writes use a
temporary file followed by an atomic replace.

## Safety and operational notes

- The GUI never runs imported shell script content.
- Local commands are launched with a program path and an argument list.
  Paths containing spaces remain single arguments.
- Remote execution uses a fixed POSIX command sequence with shell-quoted
  profile values. It is native building on the remote host, not cross-compilation.
- The GUI does not delete build directories automatically.
- A full solver build can be very large; inspect the matrix and available disk
  space first.
- Cancelling sends a terminate signal to the build driver and escalates to a
  kill if it does not stop. Depending on the operating system and the active
  compiler/MPI child process, verify that no orphaned compiler processes remain.
- A dry run is useful for checking arguments and variant plans, but it does
  not validate the resulting executable.
- Remote cancellation terminates the local SSH client. If the connection
  drops during a remote build, inspect the remote process list before retrying.
- Signed release installers, artifact SBOM generation, and exhaustive
  target-side portability certification remain future work.

## Portability is target-specific

A JOREK executable is not universally portable across operating systems,
ABIs, libc implementations, or processor architectures. Static linking can
reduce runtime dependencies, but it does not erase those platform boundaries.
Build and validate a separate artifact for each supported target. The current
GUI is an interface to the existing builder; it does not claim support for
every platform in the proposed long-term target matrix.

## Tests

Run the standard-library unit tests:

```sh
python -m unittest discover -s tests -p 'test_*.py' -v
```

The GitHub Actions workflow installs PySide6 and performs headless GUI and
application-entry-point smoke tests on Linux, macOS, and Windows with Python
3.11. A separate CLI-only matrix runs the unit and integration suite on Python
3.9, 3.10, 3.12, and 3.13. The desktop jobs also build the source and wheel
distributions, install the wheel, and smoke-test its application entry point. A
separate integration job checks out a pinned upstream JOREK revision, runs the
full test suite without source-dependent skips, generates the real model matrix,
and dry-runs a model-303/MUMPS build plan. This is not a full Fortran compilation
or an HPC runtime test.
