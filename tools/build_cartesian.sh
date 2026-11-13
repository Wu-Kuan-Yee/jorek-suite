#!/usr/bin/env bash
# Generate (and optionally execute) the outer Cartesian-product loop.
# This is a planner: it does not claim that every combination is valid.
set -u
features="hdf5,fftw,block,direct"
solvers="mumps,pastix6,strumpack"
models=""
jobs=12
work="${JOREK_CARTESIAN_WORK:-$PWD/build/cartesian}"
execute=0
while [ $# -gt 0 ]; do
  case "$1" in
    --features) features="$2"; shift 2 ;;
    --solvers) solvers="$2"; shift 2 ;;
    --models) models="$2"; shift 2 ;;
    --jobs) jobs="$2"; shift 2 ;;
    --work) work="$2"; shift 2 ;;
    --execute) execute=1; shift ;;
    --dry-run) execute=0; shift ;;
    -h|--help)
      echo "Usage: $0 [--features a,b] [--solvers a,b] [--models 303,600] [--jobs N] [--work DIR] [--execute]"
      exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
IFS=',' read -r -a feature_list <<< "$features"
IFS=',' read -r -a solver_list <<< "$solvers"
if [ -z "$models" ]; then
  models=$(cd "${JOREK_ROOT:-../JOREK}" && ls models | grep -E '^model[0-9]+$' | sed 's/^model//' | paste -sd, -)
fi
IFS=',' read -r -a model_list <<< "$models"
nfeat=${#feature_list[@]}
[ "$nfeat" -gt 20 ] && { echo "refusing more than 20 varying features" >&2; exit 2; }
masks=$((1 << nfeat))
total=0
for m in "${model_list[@]}"; do
  for s in "${solver_list[@]}"; do
    for ((mask=0; mask<masks; mask++)); do
      total=$((total + 1))
      fl=""
      for ((i=0; i<nfeat; i++)); do
        if (( mask & (1<<i) )); then fl="${fl:+$fl,}${feature_list[$i]}"; fi
      done
      cmd=(python3 build.py build --matrix complete --solver "$s" --features "$fl" \
           --programs main --jobs "$jobs" --work "$work" --root "${JOREK_ROOT:-../JOREK}")
      if (( execute )); then
        echo "+ ${cmd[*]}" >&2
        "${cmd[@]}" || echo "FAILED: model=$m solver=$s features=$fl" >&2
      else
        printf '%s\n' "${cmd[*]} --model $m"
      fi
    done
  done
done
echo "planned product points: $total" >&2
