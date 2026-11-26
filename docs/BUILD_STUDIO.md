# JOREK Build Studio

JOREK Build Studio is an optional Qt desktop frontend for the existing
\`build.py\` command-line builder. It does not replace or fork the JOREK build
logic: it discovers the CLI parser, dependency catalogue, and model metadata
from the selected checkout, then launches the existing driver with an argv
vector (never through a shell).

## Current scope

- Select the exact \`build.py\`, JOREK source tree, and isolated work directory.
- Run the existing \`doctor --json\` diagnostics.
- Discover model directories and the registered dependency catalogue.
- Configure build/dist/full commands, model matrices, programs, solvers,
  dependency profiles, feature flags, and hard-coded parameter grids.
- Preview the model matrix before starting an expensive build.
- Run dependency list/graph/fetch/build/verify actions.
- Capture merged process output in the GUI and a persistent log file.
- Cancel a running process; record task history as JSON Lines.
- Save and restore the GUI configuration atomically.
- Generate the Help tab from the selected \`build.py\` argparse parser so that
  CLI help remains the source of truth.
- Parse a legacy \`build.sh\` as text only. The importer never sources or runs it.

## Install and launch

The CLI remains standard-library-only. The GUI is optional:

\`\`\`sh
python -m pip install 'PySide6>=6.7'
python -m jorek_build_studio
\`\`\`

Alternatively, install the project metadata and GUI extra in an environment
that supports the selected Python version:

\`\`\`sh
python -m pip install -e '.[gui]'
jorek-build-studio
\`\`\`

When installed outside the repository, select the repository's \`build.py\`
from the Build Configuration tab. The selected file and its sibling \`src/\`
directory are used as the backend.

## Design constraints

- No shell command concatenation: \`QProcess\` receives the executable and
  argument list separately.
- No automatic deletion of build directories.
- No automatic change to the user's compiler or system environment.
- The GUI does not silently invent unsupported solver combinations.
- The existing CLI remains fully usable without Qt.
- Matrix previews and all build actions are executed by the same \`build.py\`
  used by command-line users.
- The GUI does not claim that a single-file suite is universally portable.
  A release must be built and validated separately for each target OS, ABI,
  libc baseline, and CPU architecture.

## Planned work

1. Add a complete legacy \`build.sh\` import report and explicit mapping review.
2. Add richer per-option contextual help and structured diagnostics.
3. Add persistent task browsing, resume helpers, and structured build stages.
4. Add artifact manifests, SBOM/license export, and target-platform test
   reports.
5. Add remote native builder profiles for targets that cannot be built on the
   current host.
6. Expand platform-specific packaging and target-side smoke tests.

The initial GUI release is an integration frontend, not a claim that every
listed target platform has already been validated.
