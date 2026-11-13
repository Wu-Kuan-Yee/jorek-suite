#!/usr/bin/env python3
"""Print or execute the Cartesian product around build.py.

This is a planner, not a promise that every combination is valid.
"""
import argparse
import shlex
import subprocess
import sys
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--features", default="hdf5,fftw,block,direct")
    p.add_argument("--solvers", default="mumps,pastix6,strumpack")
    p.add_argument("--models", default="")
    p.add_argument("--jobs", type=int, default=12)
    p.add_argument("--work", default="build/cartesian")
    p.add_argument("--root", default="../JOREK")
    p.add_argument("--build-py", default=str(Path(__file__).resolve().parents[1] / "build.py"))
    p.add_argument("--execute", action="store_true")
    args = p.parse_args()

    sys.path.insert(0, str(Path(args.build_py).resolve().parent / "src"))
    import jorek  # noqa: E402
    all_features = sorted(jorek.BOOLEAN_FEATURES)
    features = [x for x in args.features.split(",") if x]
    solvers = [x for x in args.solvers.split(",") if x]
    if args.models:
        models = [x for x in args.models.split(",") if x]
    else:
        model_root = Path(args.root) / "models"
        models = sorted(x.name[5:] for x in model_root.glob("model[0-9]*") if x.is_dir())
    if len(features) > 20:
        print("refusing more than 20 varying features", file=sys.stderr)
        return 2
    total = 0
    for model in models:
        for solver in solvers:
            for mask in range(1 << len(features)):
                selected = [f for i, f in enumerate(features) if mask & (1 << i)]
                cmd = [sys.executable, args.build_py, "build", "--root", args.root,
                       "--work", args.work, "--jobs", str(args.jobs), "--deps", "none",
                       "--solver", solver, "--matrix", "complete", "--programs", "main",
                       "--model", model]
                for feature in all_features:
                    if feature not in selected:
                        cmd += ["--disable", feature]
                for feature in selected:
                    cmd += ["--enable", feature]
                total += 1
                if args.execute:
                    print("+ " + " ".join(shlex.quote(x) for x in cmd), file=sys.stderr)
                    rc = subprocess.run(cmd).returncode
                    if rc:
                        print("FAILED model=%s solver=%s features=%s" % (
                            model, solver, ",".join(selected)), file=sys.stderr)
                else:
                    print(" ".join(shlex.quote(x) for x in cmd))
    print("planned product points: %d" % total, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
