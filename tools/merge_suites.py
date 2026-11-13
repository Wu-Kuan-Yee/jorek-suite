#!/usr/bin/env python3
"""Merge variant JSON manifests into one single-file suite.

Example:
  tools/merge_suites.py --root ../JOREK --work /tmp/merge \
    --variant-dir /tmp/solver-full/variants \
    --variant-dir /tmp/pastix5-full/variants \
    --output build/dist/jorek-suite
"""
import argparse
import json
import sys
import types
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "src"))
import build  # noqa: E402
from common import PlatformInfo  # noqa: E402
from suite import SuiteBuilder  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="JOREK source checkout")
    parser.add_argument("--work", required=True, help="scratch directory")
    parser.add_argument("--variant-dir", action="append", required=True,
                        help="directory containing build.py variant JSON files")
    parser.add_argument("--output", required=True)
    parser.add_argument("--mpi-prefix", default=None)
    parser.add_argument("--jorek-version", default="unknown")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    work = Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    platform = PlatformInfo.detect()
    simple = types.SimpleNamespace(
        root=str(root), work=str(work), mpi_prefix=args.mpi_prefix,
        dep_prefix=[], toolchain_prefix=[],
    )
    launcher, objects = build._compile_launcher(simple, platform, work)
    builder = SuiteBuilder(launcher, jorek_version=args.jorek_version)
    first = True
    for directory in args.variant_dir:
        for manifest in sorted(Path(directory).glob("*.json")):
            data = json.loads(manifest.read_text(encoding="utf-8"))
            variant = builder.add_variant(
                data["name"], data["model"], data["features"], data["params"],
                make_default=first,
            )
            first = False
            for name, path in sorted(data["programs"].items()):
                candidate = Path(path)
                if candidate.is_file():
                    builder.add_program(variant, name, candidate)
    for name, path in build._mpi_helper_payloads(simple, platform):
        builder.add_helper(name, path)
    for rel, path in build._runtime_payloads(root):
        builder.add_helper(rel, path)
    builder.add_helper("bin/jorek-tool", build._compile_runtime_tool(simple, platform, work))
    output = Path(args.output)
    if platform.is_macos:
        builder.build_macho_section(output, ["cc"], objects)
    else:
        builder.build(output)
    print("variants: %d  helpers: %d  output: %s" % (
        len(builder.variants), len(builder.helpers), output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
