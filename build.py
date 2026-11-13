#!/usr/bin/env python3
"""build.py -- reproducible, from-source build system for JOREK suites.

The script is intentionally self-contained: it uses only the Python standard
library and the C/C++/Fortran toolchain that the host already provides.  Any
missing tool or library can be fetched and built from source into a private
prefix, then used to build every JOREK model/feature combination and pack the
resulting executables into one native single-file launcher.

Run ``python3 build.py --help`` for the full command reference and
``python3 build.py doctor`` for a diagnosis of the current machine.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
SRC = HERE / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import common
import deps
import jorek
import suite
from common import BuildError, Downloader, PlatformInfo, ensure_dir, human_bytes, read_json, run, which, write_json, write_text


VERSION = "1.0.0"
def _default_root() -> Path:
    env = os.environ.get("JOREK_ROOT")
    if env:
        return Path(env).expanduser()
    for candidate in (HERE.parent / "JOREK", HERE.parent, Path.cwd()):
        if (candidate / "jorek2_main.f90").exists() and (candidate / "models").is_dir():
            return candidate
    return HERE.parent / "JOREK"


DEFAULT_ROOT = _default_root()
WORK_DEFAULT = HERE / "build"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def banner(args) -> None:
    if getattr(args, "quiet", False):
        return
    print("")
    print("JOREK suite builder %s" % VERSION)
    print("  platform : %s" % PlatformInfo.detect().summary())
    print("  python   : %s" % sys.version.split()[0])


def _search_dirs() -> List[str]:
    dirs = []
    for candidate in ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", "/usr/bin", "/bin"):
        if os.path.isdir(candidate):
            dirs.append(candidate)
    dirs.extend(os.environ.get("PATH", "").split(os.pathsep))
    return dirs


def _host_tool(name: str) -> Optional[str]:
    return which(name, _search_dirs())


def _platform() -> PlatformInfo:
    return PlatformInfo.detect()


def _toolchain_prefix(args) -> List[Path]:
    values = [Path(p).expanduser() for p in (args.dep_prefix or [])]
    if getattr(args, "deps", "source") == "source":
        values.insert(0, Path(args.work) / "deps")
    return [p for p in values if str(p)]


def _find_compilers(args, platform: PlatformInfo) -> Tuple[str, str, str, str]:
    """Return (cc, cxx, fc, mpifc)."""
    search = [str(Path(args.work) / "deps" / "bin")] + [str(Path(p) / "bin") for p in (args.dep_prefix or [])]
    if args.toolchain_prefix:
        search.insert(0, str(Path(args.toolchain_prefix) / "bin"))
    for candidate_dir in ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", "/usr/bin", "/bin"):
        if os.path.isdir(candidate_dir):
            search.append(candidate_dir)
    search.extend(os.environ.get("PATH", "").split(os.pathsep))

    cc = args.cc or os.environ.get("CC") or which("gcc", search) or which("cc", search)
    cxx = args.cxx or os.environ.get("CXX") or which("g++", search) or which("c++", search)
    fc = args.fc or os.environ.get("FC") or which("gfortran", search)
    if not fc:
        for candidate in ("gfortran-14", "gfortran-13", "gfortran-12", "ifort", "ifx", "flang", "nvfortran"):
            fc = which(candidate, search)
            if fc:
                break
    mpifc = os.environ.get("MPIFC") or which("mpifort", search) or which("mpif90", search)
    if not fc:
        raise BuildError(
            "no Fortran compiler found.  Run 'python3 build.py bootstrap' to build GCC from "
            "source, or pass --fc /path/to/gfortran."
        )
    if not cc or not cxx:
        raise BuildError("a C and a C++ compiler are required (pass --cc and --cxx)")
    if not mpifc:
        # Link MPI manually later; the make builder adds the libraries.
        mpifc = fc
    return cc, cxx, fc, mpifc


def _write_toolchain_json(args, cc: str, cxx: str, fc: str, mpifc: str) -> None:
    path = Path(args.work) / "toolchain.json"
    write_json(path, {"cc": cc, "cxx": cxx, "fc": fc, "mpifc": mpifc, "time": time.time()})


def _load_toolchain(args) -> Optional[dict]:
    return read_json(Path(args.work) / "toolchain.json", None)


def _make_targets(args, model: str, programs: str) -> List[str]:
    main = "jorek_model%s" % model
    if programs == "main":
        return [main]
    if programs == "most":
        text = common.read_text(Path(args.root) / "Makefile")
        match = re.search(r"(?ms)^most:\s*(.*?)(?=^\S|\Z)", text)
        names = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", match.group(1).replace("\\\n", " ")) if match else []
        return [main] + [n for n in names if n not in ("jorek2_main", main)]
    if programs == "all":
        return ["all", main]
    if programs == "list":
        return []
    raise BuildError("unknown program set: %s" % programs)


def _apply_feature_args(features: jorek.FeatureSet, args) -> jorek.FeatureSet:
    for name in args.enable or []:
        features.enable(name, True)
    for name in args.disable or []:
        features.enable(name, False)
    if args.solver:
        features.solver = args.solver
    if args.debug:
        features.debug = True
    features.validate()
    return features


def _parse_assignments(values: Sequence[str], integer: bool = False) -> Dict[str, object]:
    result = {}
    for item in values or []:
        if "=" not in item:
            raise BuildError("expected NAME=VALUE, got %r" % item)
        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise BuildError("empty parameter name in %r" % item)
        if integer:
            try:
                result[key] = int(value)
            except ValueError:
                raise BuildError("parameter %s must be an integer, got %r" % (key, value))
        else:
            result[key] = value
    return result


def _variant_params(args, model_info: jorek.ModelInfo) -> Dict[str, int]:
    params = {k: v for k, v in model_info.params.items() if k in jorek.HARDCODED_PARAMS}
    params.update(_parse_assignments(args.set_param or [], integer=True))
    return params


def _param_grid(args, base: Dict[str, int]) -> List[Dict[str, int]]:
    if not args.param_grid:
        return [base]
    keys = []
    values = []
    for item in args.param_grid:
        if "=" not in item:
            raise BuildError("--param-grid expects NAME=VALUE[,VALUE...]")
        key, csv = item.split("=", 1)
        vals = []
        for value in csv.split(","):
            try:
                vals.append(int(value))
            except ValueError:
                raise BuildError("parameter grid value must be an integer: %r" % value)
        keys.append(key.strip())
        values.append(vals)
    import itertools
    result = []
    for combo in itertools.product(*values):
        item = dict(base)
        item.update(dict(zip(keys, combo)))
        result.append(item)
    return result


def _variant_name(spec: jorek.VariantSpec) -> str:
    name = spec.safe_name()
    # Keep names portable and short enough for filesystems.
    name = re.sub(r"[^A-Za-z0-9._+-]", "_", name)
    return name[:180] or ("model" + spec.model)


# ---------------------------------------------------------------------------
# Build orchestration
# ---------------------------------------------------------------------------
@dataclass
class BuiltVariant:
    spec: jorek.VariantSpec
    directory: Path
    programs: Dict[str, Path]
    config: Path
    log: Optional[Path]
    verified: bool = False
    offenders: Dict[str, List[str]] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "name": self.spec.name,
            "model": self.spec.model,
            "features": self.spec.features.feature_tokens(),
            "params": self.spec.param_tokens(),
            "programs": {name: str(path) for name, path in self.programs.items()},
            "verified": self.verified,
            "offenders": self.offenders,
        }


def _prepare_builder(args, spec: jorek.VariantSpec, platform: PlatformInfo, cc, cxx, fc, mpifc):
    prefixes = _toolchain_prefix(args)
    mpi_prefix = Path(args.mpi_prefix) if args.mpi_prefix else None
    layout = jorek.DepLayout(prefixes, platform, mpi_prefix=mpi_prefix,
                             imas_root=Path(args.imas_root) if args.imas_root else None,
                             catalyst_root=Path(args.catalyst_root) if args.catalyst_root else None)
    wrapper = _mpi_fortran_wrapper(args, fc, layout)
    extra_libs = _static_runtime_libs(fc)
    builder = jorek.MakeBuilder(
        jorek_root=Path(args.root),
        spec=spec,
        layout=layout,
        fc=wrapper,
        cc=cc,
        cxx=cxx,
        jobs=args.jobs,
        keep_source=args.keep_source,
        dry_run=args.dry_run,
        log_dir=Path(args.work) / "logs",
        extra_libs=extra_libs,
        full_static=(not getattr(args, "no_full_static", False)),
    )
    return builder, layout


def _mpi_fortran_wrapper(args, fc: str, layout: jorek.DepLayout) -> str:
    """Create a gfortran wrapper that adds MPI include paths.

    The MPICH/OpenMPI ``mpifort`` wrapper hard-codes dynamic ``-lmpi`` flags.
    JOREK must link the MPI archives statically, so the compiler driver is the
    underlying Fortran compiler and the MPI archive paths are added explicitly
    by MakeBuilder.  The wrapper only supplies the include path.
    """
    try:
        include = layout.mpi_include()
    except BuildError:
        return fc
    if "mpifort" not in os.path.basename(fc) and "mpif90" not in os.path.basename(fc):
        return fc
    real = _host_tool("gfortran") or fc
    directory = Path(args.work) / "toolchain"
    directory.mkdir(parents=True, exist_ok=True)
    wrapper = directory / ("mpifort-static" + (".bat" if os.name == "nt" else ""))
    if os.name == "nt":
        text = "@echo off\r\n\"%s\" -I\"%s\" %%*\r\n" % (real, include)
    else:
        text = "#!/bin/sh\nexec \"%s\" -I\"%s\" \"$@\"\n" % (real, include)
    wrapper.write_text(text, encoding="utf-8")
    os.chmod(wrapper, 0o755)
    return str(wrapper)


def _static_runtime_libs(fc: str) -> List[str]:
    """Ask the Fortran driver to embed its own runtime.

    Passing the archives by absolute path is tempting, but the gfortran driver
    still appends its default dynamic runtime, producing a mixture of static
    and shared copies of libgfortran.  The driver flags below are the supported
    way to select the static variants without breaking initialisation.
    """
    if not fc:
        return []
    try:
        proc = subprocess.run([fc, "--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        text = proc.stdout.decode("utf-8", "replace").lower()
    except OSError:
        return []
    if "gnu" not in text and "gcc" not in text:
        return []
    return ["-static-libgfortran", "-static-libgcc", "-static-libstdc++", "-static-libquadmath"]


def build_one(args, spec: jorek.VariantSpec, platform: PlatformInfo, toolchain, *, quiet=False) -> BuiltVariant:
    cc, cxx, fc, mpifc = toolchain
    builder, layout = _prepare_builder(args, spec, platform, cc, cxx, fc, mpifc)
    name = _variant_name(spec)
    source = Path(args.work) / "build" / name / "src"
    jorek.stage_source(Path(args.root), source, spec)
    builder.configure(source)
    targets = _make_targets(args, spec.model, args.programs)
    if args.programs == "list":
        text = common.read_text(Path(args.root) / "Makefile")
        match = re.search(r"(?ms)^most:\s*(.*?)(?=^\S|\Z)", text)
        names = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", match.group(1).replace("\\\n", " ")) if match else []
        print("Programs in the 'most' set:")
        for item in names:
            print("  " + item)
        return BuiltVariant(spec, source, {}, source / "Makefile.inc", None)
    if not quiet:
        print(">> building %s (model %s, solver %s, programs %s)" % (name, spec.model, spec.features.solver, args.programs))
    log = Path(args.work) / "logs" / (name + "-make.log")
    log.parent.mkdir(parents=True, exist_ok=True)
    if log.exists():
        log.unlink()
    bindirs = []
    for tool in (fc, cc, cxx, "make"):
        directory = os.path.dirname(tool) if os.path.sep in tool else None
        if directory and directory not in bindirs:
            bindirs.append(directory)
    for prefix in _toolchain_prefix(args):
        bindirs.append(str(prefix / "bin"))
    build_env = {"PATH": os.pathsep.join([d for d in bindirs if d] + [os.environ.get("PATH", "")])}
    proc = run(["make", "-j%d" % max(1, args.jobs)] + targets, cwd=source, env=build_env, log=log,
               dry_run=args.dry_run, check=False)
    programs: Dict[str, Path] = {}
    if args.dry_run:
        return BuiltVariant(spec, source, programs, source / "Makefile.inc", log)
    if proc.returncode != 0 and not args.keep_going:
        raise BuildError("JOREK build failed for %s (exit %d)\n  log: %s" % (name, proc.returncode, log))
    # Map the main program specially; diagnostics use their executable name.
    for target in targets:
        if target in ("all",):
            continue
        candidate = source / target
        if candidate.exists():
            programs["jorek2_main" if target.startswith("jorek_model") else target] = candidate
    # Some makefiles produce the diagnostics but not the requested main target
    # on the first pass; look for the conventional names.
    main_path = source / ("jorek_model%s" % spec.model)
    if main_path.exists():
        programs.setdefault("jorek2_main", main_path)
    if args.programs in ("most", "all"):
        dirs = jorek.parse_makefile_dirs(Path(args.root), spec.model)
        discovered = jorek.discover_programs(source, dirs)
        for name in discovered:
            candidate = source / name
            if candidate.exists() and candidate.is_file():
                programs.setdefault(name, candidate)
    if not programs and proc.returncode != 0:
        raise BuildError("no JOREK executables were produced; see %s" % log)
    if platform.is_macos:
        for path in programs.values():
            _ad_hoc_sign_path(path)
    result = BuiltVariant(spec, source, programs, source / "Makefile.inc", log)
    if not args.no_verify:
        for name_, path in programs.items():
            ok, offenders = jorek.verify_static(path, platform)
            result.offenders[name_] = offenders
            result.verified = result.verified or ok
            if not ok:
                message = "%s has non-system dynamic dependencies: %s" % (name_, ", ".join(offenders))
                if not getattr(args, "allow_shared", False):
                    raise BuildError(message + "\n  re-run with --allow-shared only for local experiments")
                print("  warning: " + message)
    return result


def _ad_hoc_sign_path(path: Path) -> None:
    if sys.platform != "darwin":
        return
    result = subprocess.run(["codesign", "--force", "--sign", "-", "--timestamp=none", str(path)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        raise BuildError("codesign failed for %s: %s" % (path, result.stderr.decode("utf-8", "replace")))


def _collect_variants(args, platform: PlatformInfo, toolchain) -> List[BuiltVariant]:
    models = jorek.discover_models(Path(args.root))
    if not models:
        raise BuildError("no JOREK models found under %s" % (Path(args.root) / "models"))
    features = jorek.FeatureSet()
    _apply_feature_args(features, args)
    if args.matrix == "one":
        models_to_build = [args.model or "303"]
    elif args.matrix in ("models", "maximal", "complete"):
        models_to_build = sorted(models)
    else:
        models_to_build = [args.model or "303"]
    if args.model and args.matrix in ("one", "models"):
        models_to_build = [args.model]
    specs: List[jorek.VariantSpec] = []
    for model in models_to_build:
        if model not in models:
            raise BuildError("unknown model %s (available: %s)" % (model, ", ".join(sorted(models))))
        info = models[model]
        base_params = _variant_params(args, info)
        grids = _param_grid(args, base_params)
        if args.matrix == "complete" and info.toggles:
            toggles = [t for t in info.toggles if t.lower() != "with_refluid"]
            import itertools
            combos = list(itertools.product((False, True), repeat=len(toggles)))
        elif args.matrix == "maximal" and info.toggles:
            toggles = [t for t in info.toggles if t.lower() != "with_refluid"]
            combos = [tuple(True for _ in toggles)]
            if model == "183":
                combos = [None]
        else:
            combos = [None]
        for params in grids:
            for combo in combos:
                toggles = dict(zip([t for t in info.toggles if t.lower() != "with_refluid"], combo)) if combo is not None else None
                overrides = {k: v for k, v in params.items() if k not in info.params or info.params.get(k) != v}
                spec = jorek.make_variant(model, models, features, params=params, toggles=toggles,
                                          programs=tuple(args._program_names))
                spec.name = _variant_name(spec)
                specs.append(spec)
    # De-duplicate by name.
    seen = set()
    unique = []
    for spec in specs:
        if spec.name in seen:
            continue
        seen.add(spec.name)
        unique.append(spec)
    if args.dry_run:
        print("Planned variants (%d):" % len(unique))
        for spec in unique:
            print("  %-58s model=%s solver=%s params=%s" % (spec.name, spec.model, spec.features.solver, ",".join(spec.param_tokens())))
        return []
    results = []
    for spec in unique:
        results.append(build_one(args, spec, platform, toolchain))
    return results


# ---------------------------------------------------------------------------
# Suite creation
# ---------------------------------------------------------------------------
def _compile_launcher(args, platform: PlatformInfo, work: Path) -> Tuple[Path, List[Path]]:
    cc = os.environ.get("CC") or _host_tool("cc") or _host_tool("clang") or _host_tool("gcc")
    if not cc:
        raise BuildError("a C compiler is required to build the suite launcher")
    objdir = ensure_dir(work / "launcher")
    flags = ["-O2", "-std=c99", "-Wall", "-Wextra"]
    if not platform.is_windows:
        flags.append("-D_POSIX_C_SOURCE=200809L")
    if platform.is_windows:
        flags.append("-D_CRT_SECURE_NO_WARNINGS")
    obj = objdir / ("jorek_suite" + platform.exe_suffix + ".o")
    puff_obj = objdir / ("puff" + platform.exe_suffix + ".o")
    include = HERE / "src"
    run([cc] + flags + ["-I", str(include), "-c", str(include / "jorek_suite.c"), "-o", str(obj)])
    run([cc] + flags + ["-I", str(include), "-c", str(include / "puff.c"), "-o", str(puff_obj)])
    if platform.is_macos:
        return obj, [obj, puff_obj]
    exe = objdir / ("jorek-suite" + platform.exe_suffix)
    run([cc, str(obj), str(puff_obj), "-o", str(exe)] + (["-mconsole"] if platform.is_windows else []))
    return exe, [obj, puff_obj]


def _compile_runtime_tool(args, platform: PlatformInfo, work: Path) -> Path:
    """Compile the native replacement for the common JOREK runtime scripts."""
    cxx = os.environ.get("CXX") or _host_tool("c++") or _host_tool("g++") or _host_tool("clang++")
    if not cxx:
        raise BuildError("a C++ compiler is required to build jorek-tool")
    source = HERE / "src" / "jorek_tool.cpp"
    output = Path(work) / "jorek-tool" / ("jorek-tool" + platform.exe_suffix)
    output.parent.mkdir(parents=True, exist_ok=True)
    flags = ["-O2", "-std=c++17", "-Wall", "-Wextra"]
    if platform.is_linux or platform.is_bsd:
        flags += ["-static-libstdc++", "-static-libgcc"]
    run([cxx] + flags + [str(source), "-o", str(output)])
    return output


def _runtime_payloads(root: Path) -> List[Tuple[str, Path]]:
    """Collect JOREK's user-facing runtime scripts and scheduler templates.

    Build-only helpers (config.sh, makedepend, obj_deps, compile/launch test
    drivers, equation code generation) are intentionally excluded: the suite
    is a runtime distribution, not the JOREK build tree.
    """
    root = Path(root)
    exclude_dirs = {
        "util/equation_codegen", "util/data_extractor", "util/__pycache__",
    }
    exclude_names = {
        "config.sh", "makedepend", "obj_deps", "compile_tests.sh",
        "compile_run_unit_tests.py", "launch_tests.sh",
        "generate_codes_for_norder_gt_7.py", "install.sh",
    }
    result: List[Tuple[str, Path]] = []
    for top in ("util", "run_jorek"):
        base = root / top
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if any(rel == d or rel.startswith(d + "/") for d in exclude_dirs):
                continue
            if path.name in exclude_names or path.suffix == ".pyc":
                continue
            if path.suffix.lower() in (".o", ".a", ".so", ".dylib", ".dll", ".exe"):
                continue
            result.append((rel, path))
    # Runtime scheduler examples and job templates.
    for pattern in ("namelist/**/*.job", "reg_tests/job_scripts/**/*.job",
                    "reg_tests/job_scripts/**/*.sh", "particles/**/*.sh"):
        for path in sorted(root.glob(pattern)):
            if path.is_file():
                result.append((path.relative_to(root).as_posix(), path))
    # Python post-processing helpers that live outside util/.
    for pattern in ("particles/postprocessors/utils/*.py", "particles/utils/*.py",
                    "diagnostics/catalyst/*.py", "communication/IMAS/*.py"):
        for path in sorted(root.glob(pattern)):
            if path.is_file():
                result.append((path.relative_to(root).as_posix(), path))
    # De-duplicate while preserving deterministic order.
    seen = set()
    unique = []
    for rel, path in result:
        if rel in seen:
            continue
        seen.add(rel)
        unique.append((rel, path))
    return unique


def _mpi_helper_payloads(args, platform: PlatformInfo) -> List[Tuple[str, Path]]:
    """Collect the Hydra launcher binaries and their private libraries.

    The suite embeds a complete local MPI launcher, not just the MPI library
    linked into JOREK.  On a cluster the same extracted helper directory can be
    placed on a shared filesystem and selected with --helper-dir.
    """
    prefix = None
    if getattr(args, "mpi_prefix", None):
        prefix = Path(args.mpi_prefix)
    if prefix is None:
        candidate = _host_tool("mpiexec") or _host_tool("mpirun")
        if candidate:
            prefix = Path(candidate).resolve().parent.parent
        else:
            for tool in ("mpifort", "mpif90", "mpicc"):
                candidate = _host_tool(tool)
                if candidate:
                    prefix = Path(candidate).resolve().parent.parent
                    break
    if prefix is None:
        return []
    bindir = prefix / "bin"
    if not bindir.is_dir():
        bindir = prefix
    names = ["mpiexec", "mpiexec.exe", "mpiexec.hydra", "mpirun",
             "hydra_pmi_proxy", "hydra_pmi_proxy.exe", "hydra_nameserver",
             "hydra_nameserver.exe", "hydra_persist", "hydra_persist.exe"]
    work = Path(args.work) / "mpi-helpers"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    selected: List[Path] = []
    for name in names:
        source = bindir / name
        if source.exists():
            selected.append(source)
    if not selected:
        return []
    # Copy binaries first, then their private shared libraries.
    copied: Dict[str, Path] = {}
    for source in selected:
        target = work / source.name
        shutil.copy2(source, target)
        os.chmod(target, 0o755)
        copied[source.name] = target
    if platform.is_macos:
        for target in list(copied.values()):
            _copy_macos_private_deps(target, work)
    elif platform.is_linux or platform.is_bsd:
        for target in list(copied.values()):
            _copy_elf_private_deps(target, work)
    elif platform.is_windows:
        for target in list(copied.values()):
            _copy_pe_private_deps(target, work)
    # Ensure the canonical names used by the launcher exist.
    if "mpiexec" not in copied and "mpiexec.hydra" in copied:
        shutil.copy2(copied["mpiexec.hydra"], work / "mpiexec")
        copied["mpiexec"] = work / "mpiexec"
    if platform.is_windows and "mpiexec" in copied and "mpiexec.exe" not in copied:
        shutil.copy2(copied["mpiexec"], work / "mpiexec.exe")
        copied["mpiexec.exe"] = work / "mpiexec.exe"
    # Return only real binaries, not duplicated aliases, but keep canonical
    # mpiexec and the Hydra proxy.
    result = []
    seen = set()
    for name in ("mpiexec", "mpiexec.exe", "mpiexec.hydra", "hydra_pmi_proxy",
                 "hydra_pmi_proxy.exe", "hydra_nameserver", "hydra_nameserver.exe",
                 "hydra_persist", "hydra_persist.exe"):
        path = work / name
        if path.exists() and name not in seen:
            seen.add(name)
            result.append((name, path))
    for path in sorted(work.iterdir()):
        if path.name in seen or not path.is_file():
            continue
        if path.suffix in (".dylib", ".so", ".dll") or ".so." in path.name:
            seen.add(path.name)
            result.append((path.name, path))
    return result


def _copy_macos_private_deps(binary: Path, directory: Path) -> None:
    proc = subprocess.run(["otool", "-L", str(binary)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    if proc.returncode != 0:
        return
    for line in proc.stdout.decode("utf-8", "replace").splitlines()[1:]:
        dep = line.strip().split(" (compatibility")[0].strip()
        if not dep or dep.startswith(("/usr/lib/", "/System/Library/", "/Library/Apple/")):
            continue
        source = Path(dep)
        if not source.is_file():
            continue
        target = directory / source.name
        if not target.exists():
            shutil.copy2(source, target)
        subprocess.run(["install_name_tool", "-change", dep, "@loader_path/" + source.name, str(binary)],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        # The newly copied library may itself have non-system dependencies; a
        # single level is sufficient for Hydra's hwloc dependency.
        subprocess.run(["codesign", "--force", "--sign", "-", "--timestamp=none", str(binary)],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        subprocess.run(["codesign", "--force", "--sign", "-", "--timestamp=none", str(target)],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


def _copy_elf_private_deps(binary: Path, directory: Path) -> None:
    proc = subprocess.run(["ldd", str(binary)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    if proc.returncode != 0:
        return
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        if "=>" not in line:
            continue
        dep = line.split("=>", 1)[1].strip().split(" ")[0]
        if not dep or dep.startswith(("/lib/", "/lib64/", "/usr/lib")):
            continue
        source = Path(dep)
        if source.is_file():
            target = directory / source.name
            if not target.exists():
                shutil.copy2(source, target)


def _copy_pe_private_deps(binary: Path, directory: Path) -> None:
    objdump = which("objdump") or which("x86_64-w64-mingw32-objdump")
    if not objdump:
        return
    proc = subprocess.run([objdump, "-p", str(binary)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    for dll in re.findall(r"DLL Name:\s*(\S+)", proc.stdout.decode("utf-8", "replace")):
        # System DLLs are not shipped; MinGW runtime DLLs are searched on PATH.
        continue


def build_suite(args, platform: PlatformInfo, built: Sequence[BuiltVariant]) -> Path:
    launcher, objects = _compile_launcher(args, platform, Path(args.work))
    output = Path(args.output) if args.output else (Path(args.work) / "dist" / ("jorek-suite" + platform.exe_suffix))
    remove = getattr(args, "remove_tools", False)
    builder = suite.SuiteBuilder(
        launcher=launcher,
        suite_version=VERSION,
        jorek_version=common.git_describe(Path(args.root)),
        compression_level=args.compression,
    )
    for result in built:
        variant = builder.add_variant(
            result.spec.name,
            result.spec.model,
            result.spec.features.feature_tokens(),
            result.spec.param_tokens(),
            make_default=(result.spec.model == (args.model or "303")),
        )
        for program_name, path in sorted(result.programs.items()):
            builder.add_program(variant, program_name, path)
    helpers = _mpi_helper_payloads(args, platform)
    for helper_name, helper_path in helpers:
        builder.add_helper(helper_name, helper_path)
    for rel, path in _runtime_payloads(Path(args.root)):
        builder.add_helper(rel, path)
    tool = _compile_runtime_tool(args, platform, Path(args.work))
    builder.add_helper("bin/jorek-tool" + platform.exe_suffix, tool)
    output.parent.mkdir(parents=True, exist_ok=True)
    if platform.is_macos:
        cc = os.environ.get("CC") or which("cc") or "cc"
        builder.build_macho_section(output, [cc], objects)
    else:
        builder.build(output)
    ok, offenders = jorek.verify_static(output, platform)
    if not ok and not getattr(args, "allow_shared", False):
        raise BuildError("suite has non-system dynamic dependencies: %s" % ", ".join(offenders))
    print("")
    print("Single-file JOREK suite: %s (%s)" % (output, human_bytes(output.stat().st_size)))
    return output


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_doctor(args) -> int:
    platform = _platform()
    if getattr(args, "json", False):
        tools = {}
        for tool in ("cc", "gcc", "c++", "g++", "gfortran", "mpicc", "mpifort", "mpirun",
                     "make", "gmake", "cmake", "ninja", "pkg-config", "pkgconf", "perl",
                     "ar", "ranlib", "ld", "nm", "objcopy", "strip", "git", "curl", "tar"):
            tools[tool] = _host_tool(tool)
        root = Path(args.root)
        print(json.dumps({
            "platform": {
                "system": platform.system, "machine": platform.machine,
                "libc": platform.libc, "python": platform.python,
                "python_version": sys.version.split()[0],
            },
            "tools": tools,
            "jorek": {
                "root": str(root.resolve()),
                "makefile": (root / "Makefile").exists(),
                "models": len(list((root / "models").glob("model*"))),
                "git": common.git_describe(root),
            },
            "work": str(Path(args.work).resolve()),
        }, indent=2, sort_keys=True))
        return 0
    print("Platform")
    print("  system        : %s" % platform.system)
    print("  machine       : %s" % platform.machine)
    print("  libc          : %s" % platform.libc)
    print("  python        : %s (%s)" % (platform.python, sys.version.split()[0]))
    print("  cwd           : %s" % Path.cwd())
    print("  jorek root    : %s" % Path(args.root).resolve())
    print("")
    print("Toolchain")
    tools = ["cc", "gcc", "c++", "g++", "gfortran", "mpicc", "mpifort", "mpirun",
             "make", "gmake", "cmake", "ninja", "pkg-config", "pkgconf", "perl",
             "ar", "ranlib", "ld", "nm", "objcopy", "strip", "git", "curl", "tar"]
    missing = []
    for tool in tools:
        path = _host_tool(tool)
        print("  %-12s %s" % (tool, path or "-- missing --"))
        if not path and tool in ("cc", "c++", "make", "ar", "ranlib"):
            missing.append(tool)
    print("")
    print("JOREK")
    root = Path(args.root)
    print("  Makefile      : %s" % (root / "Makefile").exists())
    print("  models        : %d" % len(list((root / "models").glob("model*"))))
    print("  git revision  : %s" % common.git_describe(root))
    print("")
    print("Disk")
    usage = shutil.disk_usage(Path(args.work) if Path(args.work).exists() else Path.cwd())
    print("  work          : %s" % Path(args.work).resolve())
    print("  free          : %s" % human_bytes(usage.free))
    print("")
    if missing:
        print("Missing mandatory tools: %s" % ", ".join(missing))
        return 1
    print("No mandatory tool is missing.")
    return 0


def cmd_deps(args) -> int:
    platform = _platform()
    ctx = deps.BuildContext(
        prefix=Path(args.work) / "deps",
        work=Path(args.work),
        platform=platform,
        downloader=Downloader(Path(args.work) / "downloads", offline=args.offline, verify=not args.no_checksum),
        jobs=args.jobs,
        cc=args.cc or _host_tool("gcc") or _host_tool("cc") or "cc",
        cxx=args.cxx or _host_tool("g++") or _host_tool("c++") or "c++",
        fc=args.fc or _host_tool("gfortran") or "gfortran",
        make=_host_tool("gmake") or _host_tool("make") or "make",
        cmake=which("cmake") or ("/opt/homebrew/bin/cmake" if os.path.exists("/opt/homebrew/bin/cmake") else None),
        force=args.force,
    )
    manager = deps.DepManager(ctx)
    if args.deps_action == "list":
        for name in sorted(deps.DEPENDENCIES):
            item = deps.DEPENDENCIES[name]
            print("%-18s %-12s %-28s %s" % (name, item.version, item.license, item.homepage))
        return 0
    if args.deps_action == "graph":
        order = manager.resolve(args.only or deps.core_dependencies(args.profile, args.solver))
        for item in order:
            print("%-18s -> %s" % (item.name, ", ".join(item.deps) or "-"))
        return 0
    if args.deps_action in ("build", "fetch"):
        names = args.only or deps.core_dependencies(args.profile, args.solver)
        if args.deps_action == "fetch":
            order = manager.resolve(names)
            for item in order:
                print("fetch", item.name)
                ctx.downloader.fetch(item.url, "%s-%s-%s" % (item.name, item.version, os.path.basename(item.url.split("?")[0])), item.sha256)
            return 0
        manager.build(names)
        return 0
    if args.deps_action == "verify":
        ok = True
        for name in args.only or sorted(deps.DEPENDENCIES):
            item = deps.DEPENDENCIES[name]
            missing = []
            for lib in item.static_libs:
                if lib and not _find_lib(Path(ctx.prefix), lib):
                    missing.append(lib)
            if missing:
                print("%-18s missing: %s" % (name, ", ".join(missing)))
                ok = False
            else:
                print("%-18s ok" % name)
        return 0 if ok else 1
    raise BuildError("unknown deps action")


def _find_lib(prefix: Path, name: str) -> Optional[Path]:
    for sub in ("lib", "lib64", "."):
        candidate = prefix / sub / name
        if candidate.exists():
            return candidate
    hits = list(prefix.rglob(name)) if prefix.exists() else []
    return hits[0] if hits else None


def cmd_bootstrap(args) -> int:
    platform = _platform()
    work = Path(args.work)
    prefix = Path(args.toolchain_prefix) if args.toolchain_prefix else work / "deps"
    ensure_dir(prefix)
    search = _search_dirs()
    host_cc = which("gcc", search) or which("cc", search)
    host_cxx = which("g++", search) or which("c++", search)
    if not host_cc or not host_cxx:
        raise BuildError("bootstrap needs a host C and C++ compiler (the user-provided baseline)")
    host_fc = which("gfortran", search) or which("gfortran-14", search) or which("gfortran-13", search)
    ctx = deps.BuildContext(
        prefix=prefix,
        work=work,
        platform=platform,
        downloader=Downloader(work / "downloads", offline=args.offline, verify=not args.no_checksum),
        jobs=args.jobs,
        cc=host_cc,
        cxx=host_cxx,
        fc=host_fc or host_cc,
        make=_host_tool("gmake") or _host_tool("make") or "make",
        force=args.force,
    )
    manager = deps.DepManager(ctx)
    if not _host_tool("make"):
        print("warning: no make found; GNU make itself cannot be bootstrapped without a make")
    if not host_fc:
        print("No Fortran compiler found; building GCC (C, C++, Fortran) from source.")
        manager.build(["gcc"])
    else:
        print("Using existing Fortran compiler: %s" % host_fc)
    if args.with_deps:
        manager.build(deps.core_dependencies("full", args.solver))
    # Install convenience symlinks/wrappers in the prefix.
    for name in ("gcc", "g++", "gfortran", "mpifort", "mpicc", "mpirun", "make", "cmake"):
        path = _find_tool_in_prefix(prefix, name)
        if path and not (prefix / "bin" / name).exists():
            try:
                os.symlink(os.path.relpath(path, prefix / "bin"), prefix / "bin" / name)
            except OSError:
                pass
    print("Toolchain prefix: %s" % prefix)
    return 0


def _find_tool_in_prefix(prefix: Path, name: str) -> Optional[Path]:
    candidate = prefix / "bin" / name
    if candidate.exists():
        return candidate
    for path in Path(prefix).rglob(name):
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def cmd_matrix(args) -> int:
    models = jorek.discover_models(Path(args.root))
    features = jorek.FeatureSet()
    _apply_feature_args(features, args)
    if args.matrix == "maximal":
        specs = []
        for model in sorted(models):
            info = models[model]
            # Model 183 hits an upstream algexpr2fort bounds bug when both
            # extensions are enabled; the R-tree/default variant is valid.
            toggles = {t: True for t in info.toggles if t.lower() != "with_refluid"}
            if model == "183":
                toggles = {}
            specs.append(jorek.make_variant(model, models, features, toggles=toggles,
                                            programs=tuple(args._program_names)))
    elif args.matrix == "complete":
        specs = jorek.complete_variants(models, features, programs=tuple(args._program_names))
    else:
        specs = [jorek.make_variant(m, models, features, programs=tuple(args._program_names)) for m in sorted(models)]
    if args.json:
        print(json.dumps([{
            "name": _variant_name(s),
            "model": s.model,
            "solver": s.features.solver,
            "features": s.features.feature_tokens(),
            "params": s.param_tokens(),
        } for s in specs], indent=2))
        return 0
    print("%-62s %-6s %-9s %s" % ("VARIANT", "MODEL", "SOLVER", "PROGRAMS"))
    for spec in specs:
        print("%-62s %-6s %-9s %s" % (_variant_name(spec), spec.model, spec.features.solver, ",".join(spec.programs)))
    print("")
    print("Total: %d variants" % len(specs))
    return 0


def cmd_build(args) -> int:
    banner(args)
    platform = _platform()
    if args.deps == "source" and not args.dry_run:
        _ensure_source_deps(args)
    cc, cxx, fc, mpifc = _find_compilers(args, platform)
    toolchain = (cc, cxx, fc, mpifc)
    _write_toolchain_json(args, *toolchain)
    built = _collect_variants(args, platform, toolchain)
    if built:
        out = Path(args.work) / "variants"
        ensure_dir(out)
        for result in built:
            write_json(out / (result.spec.name + ".json"), result.to_json())
        print("")
        print("Built %d variant(s) under %s" % (len(built), out))
    return 0


def _ensure_source_deps(args) -> None:
    platform = _platform()
    prefix = Path(args.work) / "deps"
    names = deps.core_dependencies(args.profile, args.solver,
                                   include_dierckx=(getattr(args, "programs", "main") == "all"))
    marker = prefix / ".complete"
    wanted = "\n".join(sorted(names)) + "\n"
    if marker.exists() and not args.force:
        try:
            if marker.read_text(encoding="utf-8") == wanted:
                return
        except OSError:
            pass
    ctx = deps.BuildContext(
        prefix=prefix,
        work=Path(args.work),
        platform=platform,
        downloader=Downloader(Path(args.work) / "downloads", offline=args.offline, verify=not args.no_checksum),
        jobs=args.jobs,
        cc=args.cc or _host_tool("gcc") or _host_tool("cc") or "cc",
        cxx=args.cxx or _host_tool("g++") or _host_tool("c++") or "c++",
        fc=args.fc or _host_tool("gfortran") or "gfortran",
        make=_host_tool("gmake") or _host_tool("make") or "make",
        force=args.force,
    )
    print("Ensuring source dependencies: %s" % ", ".join(names))
    deps.DepManager(ctx).build(names)
    marker.write_text(wanted, encoding="utf-8")


def cmd_dist(args) -> int:
    banner(args)
    platform = _platform()
    if args.deps == "source" and not args.dry_run:
        _ensure_source_deps(args)
    cc, cxx, fc, mpifc = _find_compilers(args, platform)
    toolchain = (cc, cxx, fc, mpifc)
    _write_toolchain_json(args, *toolchain)
    built = _collect_variants(args, platform, toolchain)
    if not built:
        raise BuildError("no variants were built")
    out = Path(args.work) / "variants"
    ensure_dir(out)
    for result in built:
        write_json(out / (result.spec.name + ".json"), result.to_json())
    suite_path = build_suite(args, platform, built)
    print("")
    print("Run it with:")
    print("  %s --list" % suite_path)
    print("  %s --model %s < namelist" % (suite_path, built[0].spec.model))
    return 0


def cmd_verify(args) -> int:
    platform = _platform()
    target = Path(args.target) if args.target else (Path(args.work) / "dist" / ("jorek-suite" + platform.exe_suffix))
    if not target.exists():
        raise BuildError("no suite at %s; run 'build.py dist' first" % target)
    print("Verifying %s" % target)
    ok, offenders = jorek.verify_static(target, platform)
    if ok:
        print("  static dependency check: ok (system libraries only)")
    else:
        print("  static dependency check: FAILED")
        for item in offenders:
            print("    " + item)
    if platform.is_macos:
        proc = subprocess.run(["codesign", "--verify", "--verbose=2", str(target)],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        print("  code signature: %s" % ("ok" if proc.returncode == 0 else "FAILED"))
        if proc.returncode != 0:
            ok = False
            print(proc.stdout.decode("utf-8", "replace"))
    proc = subprocess.run([str(target), "--info"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    print(proc.stdout.decode("utf-8", "replace").strip())
    return 0 if ok else 1


def cmd_run(args) -> int:
    platform = _platform()
    target = Path(args.suite) if args.suite else (Path(args.work) / "dist" / ("jorek-suite" + platform.exe_suffix))
    if not target.exists():
        raise BuildError("no suite at %s; run 'build.py dist' first" % target)
    argv = [str(target)] + list(args.args or [])
    if args.model:
        argv.extend(["--model", str(args.model)])
    if args.variant:
        argv.extend(["--variant", args.variant])
    if args.program:
        argv.extend(["--program", args.program])
    if args.stdin:
        argv.extend(["--"] + list(args.args or []))
        with open(args.stdin, "rb") as handle:
            return subprocess.run(argv, stdin=handle).returncode
    return subprocess.run(argv).returncode


def cmd_clean(args) -> int:
    work = Path(args.work).resolve()
    targets = []
    if args.all:
        if work.exists():
            for child in list(work.iterdir()):
                targets.append(child)
    else:
        if args.builds:
            targets.append(work / "build")
        if args.dist:
            targets.append(work / "dist")
        if args.downloads:
            targets.append(work / "downloads")
        if args.deps_flag:
            targets.append(work / "deps")
        if not targets:
            targets.extend([work / "build", work / "dist"])
    for target in targets:
        if target.exists():
            print("removing %s" % target)
            common.safe_rmtree(target, work)
    return 0


def cmd_env(args) -> int:
    data = _load_toolchain(args) or {}
    prefixes = [str(Path(args.work) / "deps")] + [str(p) for p in (args.dep_prefix or [])]
    print("export PATH=%s:$PATH" % os.pathsep.join([p + "/bin" for p in prefixes]))
    print("export PKG_CONFIG_PATH=%s" % os.pathsep.join([p + "/lib/pkgconfig" for p in prefixes]))
    print("export CC=%s" % data.get("cc", ""))
    print("export CXX=%s" % data.get("cxx", ""))
    print("export FC=%s" % data.get("mpifc", data.get("fc", "")))
    print("export LDFLAGS=-L%s" % os.pathsep.join([p + "/lib" for p in prefixes]))
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------
class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        raise SystemExit("error: %s" % message)


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build.py",
        description="Build the complete JOREK suite from source and fuse every model/feature variant into one native file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            QUICK START
              python3 build.py doctor
              python3 build.py bootstrap --with-deps
              python3 build.py dist --model 303 --programs main
              ./build/dist/jorek-suite --list

            COMPLETE MATRIX
              python3 build.py matrix --matrix complete --json > matrix.json
              python3 build.py dist --matrix complete --programs main

            DEPENDENCIES
              By default every third-party library is downloaded and built as a
              static archive under build/deps.  Use --deps none and --dep-prefix
              DIR to consume an existing static installation instead.  On
              Windows and macOS only operating-system libraries may remain
              dynamic; all other libraries are checked with verify.

            NOTES
              * Hard-coded parameters are compiled into each variant.  Use
                --set/--param-grid to generate parameter combinations; the
                launcher selects the matching variant at run time.
              * Model families (180, 183, 600, 750) are expanded to every
                supported extension combination by --matrix complete;
                --matrix maximal enables all supported extensions at once.
              * The suite embeds the MPICH/Hydra mpiexec and process manager.
                Use --mpi -np N for local runs and --hostfile/--launcher ssh
                for clusters with a shared filesystem.
              * Proprietary WSMP and the legacy HIPS backend are never
                downloaded; supply them explicitly if you are licensed to use
                them.  IMAS and ParaView Catalyst are large optional layers.
        """),
    )
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="JOREK source checkout (default: %(default)s)")
    parser.add_argument("--work", default=os.environ.get("JOREK_BUILD_DIR", str(WORK_DEFAULT)), help="build directory (default: %(default)s)")
    parser.add_argument("--jobs", "-j", type=int, default=common.cpu_count(), help="parallel build jobs (default: CPU count)")
    parser.add_argument("--verbose", "-v", action="store_true", help="show every command")
    parser.add_argument("--quiet", "-q", action="store_true", help="suppress informational output")
    parser.add_argument("--version", action="version", version="JOREK build.py %s" % VERSION)
    parser.add_argument("--dry-run", action="store_true", help="print commands and planned variants without executing")
    parser.add_argument("--offline", action="store_true", help="never access the network; use cached sources only")
    parser.add_argument("--force", action="store_true", help="rebuild dependencies even when stamps exist")
    parser.add_argument("--no-checksum", action="store_true", help="do not verify downloaded source digests")
    parser.add_argument("--no-verify", action="store_true", help="skip static dependency verification")
    parser.add_argument("--allow-shared", action="store_true", help="allow third-party shared libraries (not recommended)")
    parser.add_argument("--no-full-static", action="store_true", help="do not pass -static on Linux/BSD/Windows")
    parser.add_argument("--keep-going", action="store_true", help="continue after a JOREK target fails")
    parser.add_argument("--keep-source", action="store_true", help="keep staged source trees")
    parser.add_argument("--toolchain-prefix", default=os.environ.get("JOREK_TOOLCHAIN_PREFIX"), help="prefix containing bootstrap compilers")
    parser.add_argument("--cc", default=os.environ.get("JOREK_CC"), help="C compiler")
    parser.add_argument("--cxx", default=os.environ.get("JOREK_CXX"), help="C++ compiler")
    parser.add_argument("--fc", default=os.environ.get("JOREK_FC"), help="Fortran compiler")
    parser.add_argument("--mpi-prefix", default=os.environ.get("JOREK_MPI_PREFIX"), help="MPI installation prefix")
    parser.add_argument("--dep-prefix", action="append", default=[], help="additional static dependency prefix (repeatable)")
    parser.add_argument("--deps", choices=("source", "none"), default="source", help="build dependencies from source (default) or use --dep-prefix")
    parser.add_argument("--profile", choices=("minimal", "standard", "full", "complete"), default="full", help="dependency/solver profile")
    parser.add_argument("--solver", choices=("mumps", "pastix6", "pastix5", "strumpack", "wsmp", "hips", "all", "none"), default="mumps", help="direct solver backend")
    parser.add_argument("--ima-root", "--imas-root", dest="imas_root", default=os.environ.get("JOREK_IMAS_ROOT"), help="IMAS installation prefix")
    parser.add_argument("--catalyst-root", default=os.environ.get("JOREK_CATALYST_ROOT"), help="ParaView Catalyst installation prefix")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="diagnose the host toolchain and JOREK checkout")
    p.add_argument("--json", action="store_true", help="machine-readable output (not yet implemented)")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("deps", help="inspect, download, or build third-party dependencies")
    p.add_argument("action", choices=("list", "graph", "fetch", "build", "verify"))
    p.add_argument("--only", action="append", help="restrict to these dependency names (repeatable)")
    p.add_argument("--deps-action", dest="deps_action", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_deps, deps_action=None)

    p = sub.add_parser("bootstrap", help="build a missing Fortran compiler and foundational tools from source")
    p.add_argument("--with-deps", action="store_true", help="also build all JOREK third-party libraries")
    p.add_argument("--json", action="store_true", help="machine-readable summary")
    p.set_defaults(func=cmd_bootstrap)

    p = sub.add_parser("matrix", help="list the model/feature/parameter matrix")
    p.add_argument("--matrix", choices=("models", "complete", "maximal"), default="models")
    p.add_argument("--model", help="restrict to one model (models matrix only)")
    p.add_argument("--programs", choices=("main", "most", "all"), default="main")
    p.add_argument("--json", action="store_true")
    p.add_argument("--enable", action="append", default=[], help="enable feature (repeatable)")
    p.add_argument("--disable", action="append", default=[], help="disable feature (repeatable)")
    p.add_argument("--debug", action="store_true")
    p.set_defaults(func=cmd_matrix)

    p = sub.add_parser("build", help="build one or more JOREK variants")
    _add_build_options(p)
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("dist", help="build variants and pack them into one single-file suite")
    _add_build_options(p)
    p.set_defaults(func=cmd_dist)

    p = sub.add_parser("verify", help="verify static linkage and code signature of a suite")
    p.add_argument("target", nargs="?", help="suite path (default: build/dist/jorek-suite)")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("run", help="run a built suite")
    p.add_argument("--suite", default=None, help="suite path")
    p.add_argument("--model", default=None, help="model selector")
    p.add_argument("--variant", default=None, help="variant selector")
    p.add_argument("--program", default=None, help="program selector")
    p.add_argument("--stdin", default=None, help="JOREK namelist on stdin")
    p.add_argument("args", nargs=argparse.REMAINDER, help="arguments after -- are passed to JOREK")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("env", help="print shell exports for the built dependency prefix")
    p.set_defaults(func=cmd_env)

    p = sub.add_parser("clean", help="remove build outputs")
    p.add_argument("--all", action="store_true", help="remove the whole work directory")
    p.add_argument("--builds", action="store_true", help="remove staged sources")
    p.add_argument("--dist", action="store_true", help="remove distributions")
    p.add_argument("--downloads", action="store_true", help="remove the download cache")
    p.add_argument("--deps", dest="deps_flag", action="store_true", help="remove built dependencies")
    p.set_defaults(func=cmd_clean)

    p = sub.add_parser("version", help="print the builder version")
    p.set_defaults(func=lambda a: (print("JOREK build.py %s" % VERSION), 0)[1])

    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for subparser in action.choices.values():
                _add_global_to_sub(subparser)
    return parser


def _add_build_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--matrix", choices=("one", "models", "complete", "maximal"), default="one", help="variant set (default one)")
    parser.add_argument("--model", default=None, help="JOREK physics model (default 303)")
    parser.add_argument("--set", dest="set_param", action="append", default=[], metavar="NAME=VALUE", help="override a hard-coded parameter")
    parser.add_argument("--param-grid", action="append", default=[], metavar="NAME=V1,V2", help="build a Cartesian parameter grid")
    parser.add_argument("--programs", choices=("main", "most", "all", "list"), default="main", help="program set per variant")
    parser.add_argument("--enable", action="append", default=[], help="enable a compile-time feature (repeatable)")
    parser.add_argument("--disable", action="append", default=[], help="disable a compile-time feature (repeatable)")
    parser.add_argument("--debug", action="store_true", help="build with runtime checks and debug symbols")
    parser.add_argument("--output", default=None, help="suite output path")
    parser.add_argument("--compression", type=int, default=9, help="DEFLATE level (0..9)")



def _add_global_to_sub(parser: argparse.ArgumentParser) -> None:
    """Let users place global options after the subcommand as well."""
    common_opts = [
        ("--root", dict(default=argparse.SUPPRESS)),
        ("--work", dict(default=argparse.SUPPRESS)),
        ("--jobs", dict(type=int, default=argparse.SUPPRESS)),
        ("--verbose", dict(action="store_true", default=argparse.SUPPRESS)),
        ("--quiet", dict(action="store_true", default=argparse.SUPPRESS)),
        ("--dry-run", dict(action="store_true", default=argparse.SUPPRESS)),
        ("--offline", dict(action="store_true", default=argparse.SUPPRESS)),
        ("--force", dict(action="store_true", default=argparse.SUPPRESS)),
        ("--no-checksum", dict(action="store_true", default=argparse.SUPPRESS)),
        ("--no-verify", dict(action="store_true", default=argparse.SUPPRESS)),
        ("--allow-shared", dict(action="store_true", default=argparse.SUPPRESS)),
        ("--no-full-static", dict(action="store_true", default=argparse.SUPPRESS)),
        ("--keep-going", dict(action="store_true", default=argparse.SUPPRESS)),
        ("--keep-source", dict(action="store_true", default=argparse.SUPPRESS)),
        ("--toolchain-prefix", dict(default=argparse.SUPPRESS)),
        ("--cc", dict(default=argparse.SUPPRESS)),
        ("--cxx", dict(default=argparse.SUPPRESS)),
        ("--fc", dict(default=argparse.SUPPRESS)),
        ("--mpi-prefix", dict(default=argparse.SUPPRESS)),
        ("--dep-prefix", dict(action="append", default=argparse.SUPPRESS)),
        ("--deps", dict(choices=("source", "none"), default=argparse.SUPPRESS)),
        ("--profile", dict(choices=("minimal", "standard", "full", "complete"), default=argparse.SUPPRESS)),
        ("--solver", dict(choices=("mumps", "pastix6", "pastix5", "strumpack", "wsmp", "hips", "all", "none"), default=argparse.SUPPRESS)),
        ("--ima-root", "--imas-root", dict(dest="imas_root", default=argparse.SUPPRESS)),
        ("--catalyst-root", dict(default=argparse.SUPPRESS)),
    ]
    for item in common_opts:
        if len(item) == 3:
            option_strings, kwargs = (item[0], item[1]), item[2]
        else:
            names, kwargs = item
            option_strings = names if isinstance(names, tuple) else (names,)
        if any(opt in parser._option_string_actions for opt in option_strings):
            continue
        parser.add_argument(*option_strings, **kwargs)


def _postprocess_args(args) -> None:
    # ``deps`` uses a positional action; store it for the command.
    if args.command == "deps":
        args.deps_action = args.action
    # JOREK program names are resolved lazily by cmd_dist/build.
    if args.command in ("build", "dist", "matrix"):
        if args.command == "matrix" and not hasattr(args, "set_param"):
            args.set_param = []
        args._program_names = ["jorek2_main"]
        if getattr(args, "programs", "main") == "main":
            args._program_names = ["jorek2_main"]
        elif getattr(args, "programs", "main") == "most":
            args._program_names = ["jorek2_main"]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    # Shared options may be specified before or after the subcommand.  Merge
    # the subparser defaults with the global values where appropriate.
    try:
        _postprocess_args(args)
        if getattr(args, "verbose", False):
            os.environ["JOREK_QUIET"] = ""
        if getattr(args, "quiet", False):
            os.environ["JOREK_QUIET"] = "1"
        return int(args.func(args) or 0)
    except BuildError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
