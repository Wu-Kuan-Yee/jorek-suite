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

PaStiX 5.2.3 and PaStiX 6.3.2 are separate JOREK backends. PaStiX 5 uses
the legacy `USE_PASTIX` / optional `USE_PASTIX_MURGE` adapter and is installed
under a private `pastix5` prefix to avoid colliding with PaStiX 6's
`libpastix.a`. PaStiX 6 uses `USE_PASTIX6` and is fetched as the pinned
`v6.3.2` Git tag with recursive, shallow submodules. Its `spm` and
`morse_cmake` submodules are required by CMake and are checked before
configuration. Both bundled PaStiX backends currently use 32-bit solver
indices, matching JOREK's default `USE_INTSIZE64=0`; incompatible 64-bit
JOREK configurations are rejected during preflight. A normal source archive
is not sufficient for PaStiX 6. The remote host therefore needs Git in addition
to the compiler and CMake toolchain. SCOTCH's hand-written Makefile install
target receives the private toolchain prefix explicitly; it must not attempt
to write into system directories such as `/usr/local`.

### Build

Preview the matrix, start a build, verify a generated suite, run a suite, or
cancel the current builder process. The Build tab keeps a concise overview
separate from the Detailed logs view. The detailed view lists each dependency,
solver backend, JOREK model/variant, launcher, packaging, and verification
stage independently. The build driver publishes planned work before it starts,
so not-yet-started stages appear as disabled grey rows; active, completed,
failed, skipped, and not-run stages have distinct status symbols and colours.
Double-click a running or completed stage, or choose Open selected log, to open
its own full transcript. The live log window never forces the reader back to the
end: it follows new output only while the reader is already at the tail, and
otherwise preserves both vertical and horizontal scroll positions. Top, Bottom,
Left, and Right controls jump to each edge of the log. Detailed log windows also
provide visible-by-default line numbers, toggleable line-number gutters, PCRE
regular-expression search with next/previous navigation and match highlighting,
and live statistics for bytes, characters, lines, words, and maximum line length.
Diagnostic highlighting distinguishes fatal errors, warnings, expected failed
feature probes, ignored/non-fatal errors, and successful checks. Logs can be
copied or saved independently. Build Studio mode streams complete merged command
output to stage transcripts even when the underlying build command requests quiet
output. The progress indicator reports the current dependency/solver/variant
phase and its completed count.

Compiler and dependency commands stream their merged stdout/stderr while they
run. The full task transcript and each stage log are also persisted under the
application configuration directory. The build driver's own component logs
remain in its work directory. Verification invokes build.py verify --json and
displays a structured report covering executable format, architecture,
non-system dynamic dependencies, macOS signature status, and the launcher
info smoke test.

### History

Completed tasks are recorded in history.jsonl with start/end timestamps,
exit status, command arguments, the main transcript, and the list of per-stage
logs. Double-click the main log cell to open the complete transcript. Double-
click the stage-log count or choose View selected stage logs to browse the
individual component logs from an earlier task.

### Remote

Create named SSH profiles for native builds on Linux, macOS, or BSD hosts.
The connection test reports the remote OS, CPU architecture, Python, and Git.
Preview remote configuration shows the effective remote settings after local
compiler/dependency paths have been cleared and JSON overrides applied, along
with the pinned repository revision, final build arguments, remote command, and
artifact paths. The Build, verify and download action opens this preview for
confirmation before connecting.

A build checks out a pinned full commit SHA, runs a generic preflight for
the complete selected build plan, performs the packaged (dist) or full-solver
build, runs the remote portability verifier, and downloads the artifact only
after verification succeeds. The preflight validates the repository revision,
dependency and solver recipe registries, dependency graph, supplied sources,
cached sources in offline mode, solver artifact plans, JOREK source and variant
matrix, compiler toolchain, and writable storage. It is not tied to a particular
dependency such as PaStiX. Updating the local checkout does not silently change
a saved pin: use **Use local HEAD** and save the profile when you want the remote
host to build the updated checkout.

The preflight is a reusable verification framework, not a PaStiX-specific
condition. It checks the exact repository revision and clean working tree,
build-option consistency, the complete dependency registry and prerequisite
graph, solver source recipes, supplied source directories and mirrors, selected
dependency artifacts, JOREK source/model discovery, the requested variant
matrix, compiler/tool versions, and writable work/output locations. The same
preflight runs before local build, dist, and full commands; the remote workflow
runs it once immediately after checking out the pinned builder and again inside
the build command after any requested JOREK source checkout.

Every check has its own result, log, severity, and machine-readable entry in
WORK/logs/preflight/preflight-report.json. Errors stop compilation; warnings
are reported without silently turning a warning into a failure. Dependency
and solver builders also verify their declared installed libraries and headers
after building. Source-recipe metadata is shared between build paths wherever
possible; the full-solver PaStiX 6 builder uses the same recursive Git source
recipe as the ordinary dependency builder. The build runs under a detached
remote supervisor, not as a foreground child of the SSH connection. The
supervisor stores a task ID, state, heartbeat, complete output transcript, and
process-group ID below:

```
<remote_root>/.jorek-build-studio/tasks/<task-id>/
```

The GUI sends a heartbeat every 15 seconds. The Remote tab's Disconnect
grace period setting defaults to 120 seconds and can be configured per SSH
profile from 30 seconds to one hour. If no heartbeat is received for the
configured period, the supervisor treats the GUI as disconnected, sends SIGTERM to
the complete build process group, waits up to five seconds, and then sends
SIGKILL to any remaining members. The same process-group termination is used
for an explicit Cancel request. The SSH log stream can disconnect and reconnect
without restarting the build; the GUI resumes from the last completely
received log frame. If the GUI itself exits or the computer loses power, the
remote lease still expires and stops the build. When Build Studio is opened
again, it offers to resume monitoring a task recorded in its local recovery
metadata. A completed task's remote transcript and state are retained so that
a later reconnect can recover the final output and status.

Remote stage events and compiler output are streamed into the same per-stage
log interface used by local builds. The remote supervisor also retains the
complete raw transcript independently of the GUI. Remote task directories are
not automatically deleted; remove old task directories from the path above
when their logs are no longer needed. Build-only mode is not offered here
because it does not produce the single-file suite that this workflow transfers.
Enter machine-specific paths as JSON overrides. The local machine needs
OpenSSH ssh and scp; the remote machine needs Git, Python 3, and a POSIX
process model, in addition to a working native compiler/MPI/dependency
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

Task transcripts and per-stage log files are stored in the logs/
subdirectory. Each task gets a separate stage-log directory, and task history
retains the paths so that completed component logs can be reopened later.
Configuration writes use a temporary file followed by an atomic replace.

## Safety and operational notes

- The GUI never runs imported shell script content.
- Local commands are launched with a program path and an argument list.
  Paths containing spaces remain single arguments.
- Remote execution uses a fixed POSIX command sequence with shell-quoted
  profile values. It is native building on the remote host, not cross-compilation.
- The GUI does not delete build directories automatically.
- A full solver build can be very large; inspect the matrix and available disk
  space first.
- Local cancellation sends a terminate signal to the build driver and
  escalates to a kill if it does not stop.
- Remote cancellation is delivered to the detached supervisor, which
  terminates the build process group rather than merely killing the local SSH
  client. If the network is unavailable, the 120-second heartbeat lease is the
  fallback cleanup mechanism.
- The supervisor controls the build's process group. A build script that
  deliberately daemonizes descendants into a different session can escape
  process-group cleanup; ordinary compiler, linker, make, and dependency
  subprocesses remain in the managed group.
- A dry run is useful for checking arguments and variant plans, but it does
  not validate the resulting executable.
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
