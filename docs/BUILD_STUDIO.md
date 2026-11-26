# JOREK Build Studio

JOREK Build Studio is an optional, minimal Qt desktop frontend for the
existing JOREK suite builder. The command-line builder remains the source of
truth for model discovery, feature handling, dependency resolution, variant
planning, compilation, and packaging.

The GUI launches the selected build.py as a separate process using an argument
vector. It never constructs or executes a shell command.

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
standard input file, and arguments such as --mpi -np 8.

### Dependencies

List dependency recipes, inspect the dependency graph, fetch source archives,
build the selected dependency closure, or verify static library installation.
The optional dependency filter accepts comma-separated dependency names.

### Build

Preview the matrix, start a build, verify a generated suite, or cancel the
current builder process. Output is displayed in the window and written to a
persistent timestamped log file. The build driver's own logs remain available
in its work directory.

### History

Completed tasks are recorded in history.jsonl with start/end timestamps,
exit status, command arguments, and the associated log path. Double-click a
log-file cell to open that log with the operating system default viewer.

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
- Commands are launched with a program path and an argument list, not through
  a shell. Paths containing spaces remain single arguments.
- The GUI does not delete build directories automatically.
- A full solver build can be very large; inspect the matrix and available disk
  space first.
- Cancelling sends a terminate signal to the build driver and escalates to a
  kill if it does not stop. Depending on the operating system and the active
  compiler/MPI child process, verify that no orphaned compiler processes remain.
- A dry run is useful for checking arguments and variant plans, but it does
  not validate the resulting executable.
- The current frontend targets a local build host. Remote native builders,
  signed release installers, artifact SBOM generation, and exhaustive
  target-side portability certification are future work.

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
python -m unittest discover -s tests -p 'test_build_studio.py' -v
```

The GitHub Actions workflow additionally installs PySide6 and performs a
headless GUI construction smoke test on Linux, macOS, and Windows.
