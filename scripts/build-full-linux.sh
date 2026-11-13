#!/usr/bin/env bash
# One-command Linux build of the complete JOREK suite.
#
#   ./scripts/build-full-linux.sh --root ../JOREK --work /big/jorek-build --jobs 32
#
# With --from-source this also bootstraps a static dependency prefix (GCC only
# if the host lacks a Fortran compiler), builds PaStiX 5/6 and STRUMPACK, then
# builds the 21-model all-solver suite plus the 21 PaStiX5 variants and merges
# them into one file.  Without --from-source, --prefix must point at a prefix
# that already contains MUMPS, OpenBLAS, ScaLAPACK, HDF5, FFTW, PaStiX 5/6 and
# STRUMPACK.
set -euo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
ROOT=${JOREK_ROOT:-$HERE/../JOREK}
WORK=${JOREK_BUILD_DIR:-$HERE/build-linux}
JOBS=${JOBS:-$(nproc 2>/dev/null || echo 4)}
PREFIX=${JOREK_DEPS_PREFIX:-}
FROM_SOURCE=0
KEEP_GOING=--keep-going
while [ $# -gt 0 ]; do
  case "$1" in
    --root) ROOT=$2; shift 2 ;;
    --work) WORK=$2; shift 2 ;;
    --jobs) JOBS=$2; shift 2 ;;
    --prefix|--deps-prefix) PREFIX=$2; shift 2 ;;
    --from-source) FROM_SOURCE=1; shift ;;
    --strict) KEEP_GOING=""; shift ;;
    -h|--help)
      echo "Usage: $0 [--root DIR] [--work DIR] [--jobs N] [--prefix STATIC_PREFIX] [--from-source] [--strict]"
      exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
mkdir -p "$WORK"

if ! command -v python3 >/dev/null; then echo "python3 is required" >&2; exit 1; fi
if ! command -v cmake >/dev/null; then echo "cmake is required (or run build.py deps build --only cmake)" >&2; exit 1; fi

if [ "$FROM_SOURCE" = 1 ] && [ -z "$PREFIX" ]; then
  echo "== bootstrapping core static dependencies (GCC/MPI/OpenBLAS/HDF5/FFTW/MUMPS)"
  python3 "$HERE/build.py" --root "$ROOT" --work "$WORK" --jobs "$JOBS" \
    --deps source --profile full --solver mumps \
    bootstrap --with-deps
  CORE="$WORK/deps"
  SOLVERS="$WORK/solvers"
  if [ -d "$CORE/bin" ]; then export PATH="$CORE/bin:$PATH"; fi
  echo "== building PaStiX 5/6 and STRUMPACK into $SOLVERS"
  PREFIX="$SOLVERS" CORE_PREFIX="$CORE" WORK="$WORK/solver-src" JOBS="$JOBS" \
    "$HERE/scripts/build_solvers_linux.sh"
  PREFIX="$SOLVERS"
elif [ -z "$PREFIX" ]; then
  echo "No --prefix given.  Use --from-source for a clean-machine build," >&2
  echo "or pass --prefix DIR pointing at an existing static dependency prefix." >&2
  exit 2
fi

if [ -n "${CORE:-}" ]; then DEPS=("$PREFIX" "$CORE"); else DEPS=("$PREFIX"); fi
COMMON=(--root "$ROOT" --work "$WORK" --jobs "$JOBS" --deps none
        --dep-prefix "${DEPS[0]}")
for d in "${DEPS[@]:1}"; do COMMON+=(--dep-prefix "$d"); done
if [ -f "${CORE:-$PREFIX}/bin/mpifort" ]; then COMMON+=(--mpi-prefix "${CORE:-$PREFIX}"); fi
COMMON+=(--keep-source "$KEEP_GOING")

mkdir -p "$WORK/dist"
echo "== building 21 models with MUMPS + PaStiX6 + STRUMPACK"
python3 "$HERE/build.py" "${COMMON[@]}" dist \
  --matrix maximal --solver all --programs most \
  --output "$WORK/dist/jorek-suite-solvers"

echo "== building 21 PaStiX5 variants"
P5=("$PREFIX/pastix5")
if [ -n "${CORE:-}" ]; then P5+=("$PREFIX" "$CORE"); else P5+=("$PREFIX"); fi
P5ARGS=(--root "$ROOT" --work "$WORK" --jobs "$JOBS" --deps none
        --dep-prefix "${P5[0]}")
for d in "${P5[@]:1}"; do P5ARGS+=(--dep-prefix "$d"); done
if [ -f "${CORE:-$PREFIX}/bin/mpifort" ]; then P5ARGS+=(--mpi-prefix "${CORE:-$PREFIX}"); fi
P5ARGS+=(--keep-source "$KEEP_GOING")
python3 "$HERE/build.py" "${P5ARGS[@]}" build \
  --matrix maximal --solver pastix5 --programs main

echo "== merging solver variants"
python3 "$HERE/tools/merge_suites.py" \
  --root "$ROOT" --work "$WORK/merge" \
  --variant-dir "$WORK/variants" \
  --output "$WORK/dist/jorek-suite"

echo "== done: $WORK/dist/jorek-suite"
"$WORK/dist/jorek-suite" --info | head -12
