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
import copy
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
import portability
import solvers
import suite
from common import BuildError, Downloader, PlatformInfo, ensure_dir, human_bytes, read_json, run, which, write_json, write_text


VERSION = "1.0.0"
def _default_root() -> Path:
    """Best-effort checkout for display and legacy callers."""
    env = os.environ.get("JOREK_ROOT")
    if env:
        return Path(env).expanduser()
    for candidate in (HERE.parent / "JOREK", HERE.parent, Path.cwd()):
        if (candidate / "jorek2_main.f90").exists() and (candidate / "models").is_dir():
            return candidate
    return HERE.parent / "JOREK"


def _find_jorek_root(work: Optional[Path] = None,
                     explicit: Optional[Path] = None) -> Optional[Path]:
    """Find an existing checkout in the conventional source locations."""
    candidates = []
    if explicit:
        # An explicit --root is authoritative; do not silently substitute a
        # different checkout.
        candidates.append(explicit)
    else:
        env = os.environ.get("JOREK_ROOT")
        if env:
            candidates.append(Path(env).expanduser())
        if work:
            # Same layout as dependency sources: $WORK/src/<package>.
            candidates.append(Path(work).expanduser() / "src" / "JOREK")
        candidates.extend([HERE.parent / "JOREK", HERE.parent, Path.cwd()])
    for candidate in candidates:
        if (candidate / "jorek2_main.f90").exists() and (candidate / "models").is_dir():
            return candidate
    return None


DEFAULT_ROOT = _default_root()
WORK_DEFAULT = HERE / "build"
JOREK_GIT_URL = "https://github.com/iterorganization/JOREK.git"


def _tracked_stage(stage_id: str, label: str, category: str, callback,
                   log_path: Optional[Path] = None, phase: str = ""):
    """Run one visible build stage and report its terminal state to the GUI."""
    common.emit_build_event(
        "stage_start", id=stage_id, label=label, category=category,
        phase=phase, log_path=str(log_path) if log_path else "")
    try:
        result = callback()
    except BaseException as exc:
        common.emit_build_event(
            "stage_finish", id=stage_id, status="failed", error=str(exc))
        raise
    common.emit_build_event("stage_finish", id=stage_id, status="completed")
    return result


def _ensure_jorek_source(args) -> Path:
    """Validate --root; with --fetch-source clone into the unified source tree."""
    work = Path(args.work).expanduser()
    explicit = Path(args.root).expanduser() if getattr(args, "root", None) else None
    found = _find_jorek_root(work, explicit)
    if found:
        args.root = str(found)
        return found
    target = explicit or (work / "src" / "JOREK")
    if not getattr(args, "fetch_source", False):
        raise BuildError(
            "JOREK source not found.  Searched --root/JOREK_ROOT, %s, %s and %s.\n"
            "Pass --root /path/to/JOREK, set JOREK_ROOT, or use --fetch-source "
            "to clone %s into %s." % (
                work / "src" / "JOREK", HERE.parent / "JOREK", Path.cwd(),
                JOREK_GIT_URL, target)
        )
    git = which("git") or "git"
    if target.exists() and any(target.iterdir()):
        raise BuildError(
            "cannot clone JOREK into %s: directory is not empty and does not "
            "look like a JOREK checkout" % target
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    url = getattr(args, "jorek_url", None) or JOREK_GIT_URL
    print("JOREK source not found; cloning %s into %s" % (url, target))
    def clone_source():
        run([git, "clone", "--depth", "1", url, str(target)],
            log=work / "logs" / "jorek-source.log")
        if not (target / "jorek2_main.f90").exists():
            raise BuildError("cloned source at %s does not look like JOREK" % target)
        return target
    _tracked_stage(
        "source:jorek", "Fetch JOREK source", "Source",
        clone_source, work / "logs" / "jorek-source.log")
    args.root = str(target)
    return target


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


def _source_options(args):
    source_dirs = {}
    for item in getattr(args, "source", []) or []:
        if "=" not in item:
            raise BuildError("--source expects NAME=DIR, got %r" % item)
        name, value = item.split("=", 1)
        source_dirs[name.strip()] = Path(value).expanduser()
    urls = {}
    for option in ("url", "mirror"):
        for item in getattr(args, option, []) or []:
            if "=" not in item:
                raise BuildError("--%s expects NAME=URL, got %r" % (option, item))
            name, value = item.split("=", 1)
            urls.setdefault(name.strip(), []).append(value.strip())
    return source_dirs, urls


def _system_static_prefixes(args) -> List[Path]:
    prefixes = [Path(p).expanduser() for p in (args.dep_prefix or [])]
    built = Path(args.work).expanduser() / "deps"
    if built.exists() and built not in prefixes:
        prefixes.insert(0, built)
    for candidate in ("/usr/local", "/usr"):
        path = Path(candidate)
        if path.exists() and path not in prefixes:
            prefixes.append(path)
    return prefixes


def _static_available(prefixes: Sequence[Path], lib: str) -> bool:
    for prefix in prefixes:
        for sub in ("lib", "lib64", "lib/x86_64-linux-gnu", "lib/aarch64-linux-gnu"):
            if (prefix / sub / lib).exists():
                return True
    return False


def _source_context(args) -> dict:
    source_dirs, urls = _source_options(args)
    return {"source_dirs": source_dirs, "url_overrides": urls,
            "source_dir": Path(args.source_dir).expanduser() if getattr(args, "source_dir", None) else None}


def _static_libc_available(args) -> bool:
    cc = args.cc or _host_tool("gcc") or _host_tool("cc")
    if not cc:
        return False
    for lib in ("libc.a", "libm.a", "libmvec.a"):
        try:
            proc = subprocess.run([cc, "-print-file-name=%s" % lib],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  text=True, check=False)
        except OSError:
            return False
        path = Path(proc.stdout.strip())
        if not path.is_absolute() or not path.exists():
            return False
    return True


def _resolve_static_libc(args, platform: PlatformInfo) -> None:
    if (not platform.is_linux or args.no_full_static
            or getattr(args, "require_full_static", False)):
        return
    if not _static_libc_available(args):
        print("WARNING: this Linux host does not provide a complete static glibc")
        print("         (libc.a, libm.a and libmvec.a).  Falling back to dynamic")
        print("         glibc; all third-party libraries remain static.")
        print("         Use --require-full-static to make this an error.")
        args.no_full_static = True


def _wait_for_deps_policy(args) -> None:
    """Resolve --deps auto/system before building."""
    if args.deps in ("source", "none"):
        return
    names = deps.core_dependencies(args.profile, args.solver)
    required = []
    for name in names:
        item = deps.DEPENDENCIES.get(name)
        if item:
            required.extend(item.static_libs)
    prefixes = _system_static_prefixes(args)
    missing = [lib for lib in required if not _static_available(prefixes, lib)]
    if args.deps == "system":
        if missing:
            raise BuildError("--deps system is missing static libraries: %s" % ", ".join(missing))
        args.dep_prefix = [str(p) for p in prefixes]
        return
    # auto
    if not missing:
        print("Using system static dependencies under %s" % ", ".join(str(p) for p in prefixes))
        args.deps = "system"
        args.dep_prefix = [str(p) for p in prefixes]
    else:
        print("System static dependencies incomplete (%s); building from source" % ", ".join(missing[:4]))
        args.deps = "source"


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


def _parse_family_toggles(values: Sequence[str]) -> Dict[str, bool]:
    result: Dict[str, bool] = {}
    for item in values or []:
        if "=" not in item:
            raise BuildError("--family-toggle expects NAME=0|1, got %r" % item)
        name, raw = item.split("=", 1)
        name = name.strip()
        value = raw.strip().lower()
        if not name:
            raise BuildError("--family-toggle has an empty name: %r" % item)
        if name in result:
            raise BuildError("--family-toggle specifies %s more than once" % name)
        if value in ("1", "true", "yes", "on"):
            result[name] = True
        elif value in ("0", "false", "no", "off"):
            result[name] = False
        else:
            raise BuildError("--family-toggle value must be 0 or 1: %r" % item)
    return result


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


CAPACITY_PARAMS = ("n_nodes_max", "n_elements_max", "n_boundary_max", "n_pieces_max")


def _sorted_param_values(name: str, maximum: int) -> List[int]:
    if maximum < 0 or (maximum == 0 and name != "l_pol_domm"):
        raise BuildError("--param-max %s=%d must be positive" % (name, maximum))
    if name in ("n_tor", "n_coord_tor", "n_order"):
        # JOREK validates all three as odd integers.
        return list(range(1, maximum + 1, 2))
    if name == "l_pol_domm":
        # The Dommaschk expansion starts at poloidal mode 0.
        return list(range(0, maximum + 1))
    if name in CAPACITY_PARAMS:
        # A capacity is an upper bound: one build at the maximum covers all
        # smaller actual problem sizes.  Building 1..maximum would create
        # thousands of pointless binaries.
        return [maximum]
    return list(range(1, maximum + 1))


def _param_max_specs(args) -> List[tuple]:
    specs = []
    for item in getattr(args, "param_max", []) or []:
        if "=" not in item:
            raise BuildError("--param-max expects NAME=MAX, got %r" % item)
        name, value = item.split("=", 1)
        name = name.strip()
        if name not in jorek.HARDCODED_PARAMS:
            raise BuildError("unknown hard-coded parameter %s (known: %s)" % (
                name, ", ".join(jorek.HARDCODED_PARAMS)))
        try:
            maximum = int(value)
        except ValueError:
            raise BuildError("--param-max value must be an integer: %r" % item)
        specs.append((name, maximum))
    return specs


def _variant_params(args, model_info: jorek.ModelInfo) -> Dict[str, int]:
    params = {k: v for k, v in model_info.params.items() if k in jorek.HARDCODED_PARAMS}
    params.update(_parse_assignments(args.set_param or [], integer=True))
    return params


def _param_grid(args, base: Dict[str, int]) -> List[Dict[str, int]]:
    specs: List[tuple] = []
    for item in getattr(args, "param_grid", []) or []:
        if "=" not in item:
            raise BuildError("--param-grid expects NAME=VALUE[,VALUE...]")
        key, csv = item.split("=", 1)
        key = key.strip()
        vals = []
        for value in csv.split(","):
            try:
                vals.append(int(value))
            except ValueError:
                raise BuildError("parameter grid value must be an integer: %r" % value)
        specs.append((key, vals))
    for key, maximum in _param_max_specs(args):
        if any(existing == key for existing, _ in specs):
            raise BuildError("%s specified by both --param-grid and --param-max" % key)
        specs.append((key, _sorted_param_values(key, maximum)))
    if not specs:
        return [base]
    import itertools
    result = []
    for combo in itertools.product(*[vals for _, vals in specs]):
        item = dict(base)
        item.update(dict(zip([key for key, _ in specs], combo)))
        # Skip combinations JOREK itself rejects: n_plane must be at least
        # 2*(n_tor-1) to avoid toroidal aliasing.
        if "n_tor" in item and "n_plane" in item:
            if item["n_plane"] < 2 * (item["n_tor"] - 1):
                continue
        result.append(item)
    if not result:
        raise BuildError("parameter grid contains no valid combinations")
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
        tail = ""
        try:
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = "\n".join(lines[-30:])
        except OSError:
            pass
        raise BuildError("no JOREK executables were produced; see %s\n%s" % (log, tail))
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
    if args.model and args.matrix in ("one", "models", "maximal", "complete"):
        models_to_build = [args.model]
    specs: List[jorek.VariantSpec] = []
    family_overrides = _parse_family_toggles(getattr(args, "family_toggle", []))
    matched_family_overrides = set()
    for model in models_to_build:
        if model not in models:
            raise BuildError("unknown model %s (available: %s)" % (model, ", ".join(sorted(models))))
        info = models[model]
        model_toggles = [t for t in info.toggles if t.lower() != "with_refluid"]
        model_overrides = {name: value for name, value in family_overrides.items()
                           if name in model_toggles}
        matched_family_overrides.update(model_overrides)
        unsupported = set(family_overrides) - set(model_toggles)
        if getattr(args, "model", None) and unsupported:
            raise BuildError("model %s does not support family extension(s): %s" %
                             (model, ", ".join(sorted(unsupported))))
        base_params = _variant_params(args, info)
        grids = _param_grid(args, base_params)
        if args.matrix == "complete" and model_toggles:
            import itertools
            combos = list(itertools.product((False, True), repeat=len(model_toggles)))
            combos = [combo for combo in combos
                      if all(dict(zip(model_toggles, combo)).get(name) == value
                             for name, value in model_overrides.items())]
        elif args.matrix == "maximal" and model_toggles:
            combo = [True for _ in model_toggles]
            combos = [tuple(model_overrides.get(name, value)
                            for name, value in zip(model_toggles, combo))]
            if model == "183":
                combos = [None]
        else:
            combos = [None]
        for params in grids:
            for combo in combos:
                if combo is None:
                    toggles = None
                    if model_overrides:
                        toggles = {key: bool(info.defaults.get(key.lower(), False))
                                   for key in info.toggles}
                        toggles.update(model_overrides)
                else:
                    toggles = dict(zip(model_toggles, combo))
                    toggles.update(model_overrides)
                spec = jorek.make_variant(model, models, features, params=params, toggles=toggles,
                                          programs=tuple(args._program_names))
                spec.name = _variant_name(spec)
                specs.append(spec)
    if family_overrides and not matched_family_overrides:
        raise BuildError("none of the selected models supports family extension(s): %s" %
                         ", ".join(sorted(family_overrides)))
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
    common.emit_build_event(
        "phase_start", phase="variants", label="JOREK variants",
        total=len(unique))
    common.emit_build_event(
        "stage_plan", phase="variants",
        stages=[{
            "id": "variant:" + spec.name,
            "label": spec.name,
            "category": "JOREK variant",
            "log_path": str(Path(args.work) / "logs" / (spec.name + "-make.log")),
        } for spec in unique])
    results = []
    completed = 0
    for spec in unique:
        log_path = Path(args.work) / "logs" / (spec.name + "-make.log")
        try:
            result = _tracked_stage(
                "variant:" + spec.name, spec.name, "JOREK variant",
                lambda spec=spec: build_one(args, spec, platform, toolchain),
                log_path, phase="variants")
            results.append(result)
        finally:
            completed += 1
            common.emit_build_event(
                "phase_progress", phase="variants",
                completed=completed, total=len(unique))
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
    if platform.is_linux or platform.is_bsd or platform.is_windows:
        flags.append("-static")
    obj = objdir / ("jorek_suite" + platform.exe_suffix + ".o")
    puff_obj = objdir / ("puff" + platform.exe_suffix + ".o")
    include = HERE / "src"
    log = work / "logs" / "suite-launcher.log"
    run([cc] + flags + ["-I", str(include), "-c", str(include / "jorek_suite.c"), "-o", str(obj)], log=log)
    run([cc] + flags + ["-I", str(include), "-c", str(include / "puff.c"), "-o", str(puff_obj)], log=log)
    if platform.is_macos:
        return obj, [obj, puff_obj]
    exe = objdir / ("jorek-suite" + platform.exe_suffix)
    link_flags: List[str] = []
    if platform.is_linux or platform.is_bsd or platform.is_windows:
        link_flags.append("-static")
    run([cc] + link_flags + [str(obj), str(puff_obj), "-o", str(exe)] +
        (["-mconsole"] if platform.is_windows else []),
        log=work / "logs" / "suite-launcher.log")
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
    if platform.is_linux or platform.is_bsd or platform.is_windows:
        flags.append("-static")
    run([cxx] + flags + [str(source), "-o", str(output)],
        log=Path(args.work) / "logs" / "jorek-tool.log")
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


def build_suite(args, platform: PlatformInfo, built: Sequence[BuiltVariant],
                stage_prefix: str = "suite") -> Path:
    work = Path(args.work)
    common.emit_build_event(
        "stage_plan", phase="packaging",
        stages=[
            {
                "id": stage_prefix + ":launcher",
                "label": "Compile suite launcher",
                "category": "Packaging",
                "log_path": str(work / "logs" / "suite-launcher.log"),
            },
            {
                "id": stage_prefix + ":runtime-tool",
                "label": "Compile JOREK runtime helper",
                "category": "Packaging",
                "log_path": str(work / "logs" / "jorek-tool.log"),
            },
            {
                "id": stage_prefix + ":archive",
                "label": "Assemble single-file suite",
                "category": "Packaging",
                "log_path": str(work / "logs" / "suite-archive.log"),
            },
            {
                "id": stage_prefix + ":verify",
                "label": "Verify packaged suite",
                "category": "Verification",
                "log_path": str(work / "logs" / "suite-verify.log"),
            },
        ])
    launcher, objects = _tracked_stage(
        stage_prefix + ":launcher", "Compile suite launcher", "Packaging",
        lambda: _compile_launcher(args, platform, work),
        work / "logs" / "suite-launcher.log")
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
    tool = _tracked_stage(
        stage_prefix + ":runtime-tool", "Compile JOREK runtime helper", "Packaging",
        lambda: _compile_runtime_tool(args, platform, work),
        work / "logs" / "jorek-tool.log")
    builder.add_helper("bin/jorek-tool" + platform.exe_suffix, tool)
    output.parent.mkdir(parents=True, exist_ok=True)
    def package_suite():
        if platform.is_macos:
            cc = os.environ.get("CC") or which("cc") or "cc"
            builder.build_macho_section(output, [cc], objects)
        else:
            builder.build(output)
        return output
    _tracked_stage(
        stage_prefix + ":archive", "Assemble single-file suite", "Packaging",
        package_suite, work / "logs" / "suite-archive.log")
    def verify_packaged_suite():
        ok, offenders = jorek.verify_static(output, platform)
        if not ok and not getattr(args, "allow_shared", False):
            raise BuildError(
                "suite has non-system dynamic dependencies: %s" % ", ".join(offenders))
        return ok, offenders
    ok, offenders = _tracked_stage(
        stage_prefix + ":verify", "Verify packaged suite", "Verification",
        verify_packaged_suite, work / "logs" / "suite-verify.log")
    if not ok:
        print("warning: packaged suite has non-system dependencies: %s" % ", ".join(offenders))
    print("")
    print("Single-file JOREK suite: %s (%s)" % (output, human_bytes(output.stat().st_size)))
    return output


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_doctor(args) -> int:
    platform = _platform()
    # doctor must work before a JOREK checkout exists; report the detected
    # root (or cwd) instead of dereferencing a None root.
    root = Path(args.root) if getattr(args, "root", None) else (_find_jorek_root(Path(args.work)) or Path.cwd()) if getattr(args, "root", None) else (_find_jorek_root(Path(args.work)) or Path.cwd())
    if getattr(args, "json", False):
        tools = {}
        for tool in ("cc", "gcc", "c++", "g++", "gfortran", "mpicc", "mpifort", "mpirun",
                     "make", "gmake", "cmake", "ninja", "pkg-config", "pkgconf", "perl",
                     "ar", "ranlib", "ld", "nm", "objcopy", "strip", "git", "curl", "tar"):
            tools[tool] = _host_tool(tool)
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
    print("  jorek root    : %s" % root.resolve())
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
    root = Path(args.root) if getattr(args, "root", None) else (_find_jorek_root(Path(args.work)) or Path.cwd())
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
        **_source_context(args),
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
        **_source_context(args),
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
    _ensure_jorek_source(args)
    models = jorek.discover_models(Path(args.root))
    features = jorek.FeatureSet()
    _apply_feature_args(features, args)
    specs = []
    family_overrides = _parse_family_toggles(getattr(args, "family_toggle", []))
    matched_family_overrides = set()
    selected_models = [args.model] if getattr(args, "model", None) else sorted(models)
    for model in selected_models:
        if model not in models:
            raise BuildError("unknown model %s (available: %s)" %
                             (model, ", ".join(sorted(models))))
        info = models[model]
        toggles_list = [t for t in info.toggles if t.lower() != "with_refluid"]
        model_overrides = {name: value for name, value in family_overrides.items()
                           if name in toggles_list}
        matched_family_overrides.update(model_overrides)
        unsupported = set(family_overrides) - set(toggles_list)
        if getattr(args, "model", None) and unsupported:
            raise BuildError("model %s does not support family extension(s): %s" %
                             (model, ", ".join(sorted(unsupported))))
        base = _variant_params(args, info)
        grids = _param_grid(args, base)
        if args.matrix == "complete" and toggles_list:
            import itertools
            combos = list(itertools.product((False, True), repeat=len(toggles_list)))
            combos = [combo for combo in combos
                      if all(dict(zip(toggles_list, combo)).get(name) == value
                             for name, value in model_overrides.items())]
        elif args.matrix == "maximal" and toggles_list:
            combos = [tuple(model_overrides.get(name, True) for name in toggles_list)]
            if model == "183":
                combos = [None]
        else:
            combos = [None]
        for params in grids:
            for combo in combos:
                if combo is None:
                    toggles = None
                    if model_overrides:
                        toggles = {key: bool(info.defaults.get(key.lower(), False))
                                   for key in info.toggles}
                        toggles.update(model_overrides)
                else:
                    toggles = dict(zip(toggles_list, combo))
                    toggles.update(model_overrides)
                spec = jorek.make_variant(
                    model, models, features, params=params, toggles=toggles,
                    programs=tuple(args._program_names))
                specs.append(spec)
    if family_overrides and not matched_family_overrides:
        raise BuildError("none of the selected models supports family extension(s): %s" %
                         ", ".join(sorted(family_overrides)))
    if args.json:
        print(json.dumps([{
            "name": _variant_name(s),
            "model": s.model,
            "solver": s.features.solver,
            "features": s.features.feature_tokens(),
            "params": s.param_tokens(),
        } for s in specs], indent=2))
        return 0
    if args.details:
        print("%-54s %-6s %-10s %-9s %s" % ("VARIANT", "MODEL", "SOLVER", "PROGRAMS", "FEATURES / PARAMETERS"))
        for spec in specs:
            detail = ",".join(spec.features.feature_tokens()) + " | " + ",".join(spec.param_tokens())
            print("%-54s %-6s %-10s %-9s %s" % (
                _variant_name(spec), spec.model, spec.features.solver,
                getattr(args, "programs", "main"), detail))
    else:
        print("%-62s %-6s %-9s %s" % ("VARIANT", "MODEL", "SOLVER", "PROGRAMS"))
        for spec in specs:
            print("%-62s %-6s %-9s %s" % (_variant_name(spec), spec.model, spec.features.solver, getattr(args, "programs", "main")))
    print("")
    print("Total: %d variants" % len(specs))
    return 0


def cmd_build(args) -> int:
    banner(args)
    _ensure_jorek_source(args)
    platform = _platform()
    _resolve_static_libc(args, platform)
    common.emit_build_event(
        "stage_plan", phase="preparation",
        stages=[
            {
                "id": "dependencies:policy",
                "label": "Resolve dependency policy",
                "category": "Dependencies",
                "log_path": str(Path(args.work) / "logs" / "dependency-policy.log"),
            },
            {
                "id": "toolchain:detect",
                "label": "Detect native compiler toolchain",
                "category": "Toolchain",
                "log_path": str(Path(args.work) / "logs" / "toolchain-detection.log"),
            },
        ])
    _tracked_stage(
        "dependencies:policy", "Resolve dependency policy", "Dependencies",
        lambda: _wait_for_deps_policy(args),
        Path(args.work) / "logs" / "dependency-policy.log")
    if args.deps == "source" and not args.dry_run:
        _ensure_source_deps(args)
    toolchain = _tracked_stage(
        "toolchain:detect", "Detect native compiler toolchain", "Toolchain",
        lambda: _find_compilers(args, platform),
        Path(args.work) / "logs" / "toolchain-detection.log")
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
        **_source_context(args),
    )
    print("Ensuring source dependencies: %s" % ", ".join(names))
    deps.DepManager(ctx).build(names)
    marker.write_text(wanted, encoding="utf-8")


def cmd_dist(args) -> int:
    banner(args)
    _ensure_jorek_source(args)
    platform = _platform()
    _resolve_static_libc(args, platform)
    common.emit_build_event(
        "stage_plan", phase="preparation",
        stages=[
            {
                "id": "dependencies:policy",
                "label": "Resolve dependency policy",
                "category": "Dependencies",
                "log_path": str(Path(args.work) / "logs" / "dependency-policy.log"),
            },
            {
                "id": "toolchain:detect",
                "label": "Detect native compiler toolchain",
                "category": "Toolchain",
                "log_path": str(Path(args.work) / "logs" / "toolchain-detection.log"),
            },
        ])
    _tracked_stage(
        "dependencies:policy", "Resolve dependency policy", "Dependencies",
        lambda: _wait_for_deps_policy(args),
        Path(args.work) / "logs" / "dependency-policy.log")
    if args.deps == "source" and not args.dry_run:
        _ensure_source_deps(args)
    toolchain = _tracked_stage(
        "toolchain:detect", "Detect native compiler toolchain", "Toolchain",
        lambda: _find_compilers(args, platform),
        Path(args.work) / "logs" / "toolchain-detection.log")
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


def _merge_suite_manifests(args, platform: PlatformInfo, variant_dirs: Sequence[Path],
                          output: Path) -> Path:
    """Merge build.py variant JSON manifests into one native suite."""
    work = Path(args.work)
    launcher, objects = _tracked_stage(
        "suite:final-merge:launcher", "Compile final suite launcher", "Packaging",
        lambda: _compile_launcher(args, platform, work),
        work / "logs" / "suite-launcher.log")
    builder = suite.SuiteBuilder(
        launcher=launcher, suite_version=VERSION,
        jorek_version=common.git_describe(Path(args.root)),
        compression_level=args.compression,
    )
    first = True
    total_programs = 0
    for directory in variant_dirs:
        for manifest in sorted(Path(directory).glob("*.json")):
            data = read_json(manifest, None)
            if not data:
                continue
            variant = builder.add_variant(
                data["name"], data["model"], data["features"], data["params"],
                make_default=first)
            first = False
            for name, path in sorted(data.get("programs", {}).items()):
                candidate = Path(path)
                if candidate.is_file():
                    builder.add_program(variant, name, candidate)
                    total_programs += 1
    for helper_name, helper_path in _mpi_helper_payloads(args, platform):
        builder.add_helper(helper_name, helper_path)
    for rel, path in _runtime_payloads(Path(args.root)):
        builder.add_helper(rel, path)
    tool = _tracked_stage(
        "suite:final-merge:runtime-tool", "Compile final JOREK runtime helper", "Packaging",
        lambda: _compile_runtime_tool(args, platform, work),
        work / "logs" / "jorek-tool.log")
    builder.add_helper("bin/jorek-tool" + platform.exe_suffix, tool)
    output.parent.mkdir(parents=True, exist_ok=True)
    def package_suite():
        if platform.is_macos:
            cc = os.environ.get("CC") or which("cc") or "cc"
            builder.build_macho_section(output, [cc], objects)
        else:
            builder.build(output)
        return output
    _tracked_stage(
        "suite:final-merge:archive", "Assemble final single-file suite", "Packaging",
        package_suite, work / "logs" / "suite-archive.log")
    def verify_suite():
        ok, offenders = jorek.verify_static(output, platform)
        if not ok and not getattr(args, "allow_shared", False):
            raise BuildError(
                "suite has non-system dynamic dependencies: %s" % ", ".join(offenders))
        return ok, offenders
    ok, offenders = _tracked_stage(
        "suite:final-merge:verify", "Verify final single-file suite", "Verification",
        verify_suite, work / "logs" / "suite-verify.log")
    if not ok:
        print("warning: final suite has non-system dependencies: %s" % ", ".join(offenders))
    print("Merged %d variants, %d programs into %s (%s)" % (
        len(builder.variants), total_programs, output, human_bytes(output.stat().st_size)))
    return output


def cmd_full(args) -> int:
    """Build the complete reference suite: all-solver + PaStiX5 variants."""
    banner(args)
    _ensure_jorek_source(args)
    platform = _platform()
    _resolve_static_libc(args, platform)
    _tracked_stage(
        "dependencies:policy", "Resolve dependency policy", "Dependencies",
        lambda: _wait_for_deps_policy(args),
        Path(args.work) / "logs" / "dependency-policy.log")
    if not (platform.is_linux or platform.is_macos or platform.is_bsd):
        raise BuildError("the full solver builder is currently supported on Linux, macOS and the BSDs")
    if args.dry_run:
        print("Would build the full suite with core deps and PaStiX 5/6 + STRUMPACK.")
        print("  work    : %s" % args.work)
        print("  root    : %s" % args.root)
        print("  output  : %s" % (args.output or (Path(args.work) / "dist" / "jorek-suite")))
        return 0
    work = Path(args.work)
    ensure_dir(work)
    core_prefix = work / "deps"
    solver_prefix = Path(args.solver_prefix) if args.solver_prefix else work / "solvers"

    if args.dep_prefix:
        core_prefix = Path(args.dep_prefix[0])
    elif args.deps == "system":
        core_prefix = Path(args.dep_prefix[0]) if args.dep_prefix else Path("/usr")
    else:
        # Build the core static stack with MUMPS.  The solver stack is built
        # by solvers.py so its upstream quirks stay auditable in one place.
        core_args = copy.copy(args)
        core_args.solver = "mumps"
        core_args.profile = getattr(args, "profile", "full")
        core_args.deps = "source"
        _ensure_source_deps(core_args)

    if not args.no_build_solvers and not args.dep_prefix:
        cc, cxx, fc, mpifc = _tracked_stage(
            "toolchain:solver-detection", "Detect compiler toolchain for solvers",
            "Toolchain", lambda: _find_compilers(args, platform),
            work / "logs" / "solver-toolchain-detection.log")
        downloader = Downloader(work / "downloads", offline=args.offline,
                                verify=not args.no_checksum)
        source_dirs, url_overrides = _source_options(args)
        sb = solvers.SolverBuild(
            prefix=solver_prefix, core_prefix=core_prefix, work=work / "solver-build",
            jobs=args.jobs, platform=platform, downloader=downloader,
            cc=cc, cxx=cxx, fc=fc,
            mpicc=which("mpicc", [str(core_prefix / "bin")]) or cc,
            mpicxx=which("mpicxx", [str(core_prefix / "bin")]) or cxx,
            mpifc=mpifc, log_dir=work / "logs",
            source_dirs=source_dirs, url_overrides=url_overrides,
        )
        sb.build()

    prefixes = [solver_prefix, core_prefix]
    for item in (args.dep_prefix or []):
        path = Path(item)
        if path not in prefixes:
            prefixes.append(path)
    mpi_prefix = Path(args.mpi_prefix) if args.mpi_prefix else None
    if not mpi_prefix and (core_prefix / "bin" / "mpifort").exists():
        mpi_prefix = core_prefix

    all_args = copy.copy(args)
    if not hasattr(all_args, "model"):
        all_args.model = None
    all_args.deps = "none"
    all_args.solver = "all"
    all_args.model = args.model
    preset_matrix = getattr(args, "matrix", None)
    all_args.matrix = preset_matrix or ("one" if args.model else "maximal")
    all_args.programs = getattr(args, "programs", None) or "most"
    all_args.dep_prefix = [str(p) for p in prefixes]
    all_args.mpi_prefix = str(mpi_prefix) if mpi_prefix else None
    all_args.output = str(work / "dist" / "jorek-suite-solvers")
    all_args.keep_going = True
    toolchain = _tracked_stage(
        "toolchain:variant-detection", "Detect compiler toolchain for JOREK variants",
        "Toolchain", lambda: _find_compilers(all_args, platform),
        work / "logs" / "variant-toolchain-detection.log")
    built = _collect_variants(all_args, platform, toolchain)
    variants_dir = work / "variants"
    ensure_dir(variants_dir)
    for result in built:
        write_json(variants_dir / (result.spec.name + ".json"), result.to_json())
    build_suite(all_args, platform, built, stage_prefix="suite:solver-subset")

    p5_args = copy.copy(all_args)
    p5_args.solver = "pastix5"
    p5_args.model = args.model
    p5_args.matrix = all_args.matrix
    p5_args.programs = getattr(args, "programs", None) or "main"
    p5_prefix = solver_prefix / "pastix5"
    p5_args.dep_prefix = [str(p5_prefix)] + [str(p) for p in prefixes]
    p5_args.output = str(work / "dist" / "jorek-suite-pastix5")
    p5_built = _collect_variants(p5_args, platform, toolchain)
    for result in p5_built:
        write_json(variants_dir / (result.spec.name + ".json"), result.to_json())

    final = Path(args.output) if args.output else work / "dist" / "jorek-suite"
    _merge_suite_manifests(args, platform, [variants_dir], final)
    print("")
    print("Full single-file suite: %s" % final)
    return 0


def cmd_verify(args) -> int:
    platform = _platform()
    target = Path(args.target) if args.target else (Path(args.work) / "dist" / ("jorek-suite" + platform.exe_suffix))
    if not target.is_file():
        raise BuildError("no suite at %s; run 'build.py dist' first" % target)

    try:
        binary = portability.inspect_binary(target)
    except (OSError, ValueError) as exc:
        raise BuildError("cannot inspect suite binary %s: %s" % (target, exc)) from exc
    host_match = portability.host_compatibility(binary, platform.system, platform.machine)
    static_ok, offenders = jorek.verify_static(target, platform)

    signature = {"status": "not_applicable", "details": ""}
    signature_ok = True
    if platform.is_macos:
        codesign = which("codesign")
        if not codesign:
            signature = {"status": "unavailable", "details": "codesign is unavailable"}
            signature_ok = False
        else:
            try:
                proc = subprocess.run(
                    [codesign, "--verify", "--verbose=2", str(target)],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    check=False, timeout=20)
                details = proc.stdout.decode("utf-8", "replace").strip()
                signature_ok = proc.returncode == 0
                signature = {"status": "valid" if signature_ok else "invalid",
                             "details": details}
            except (OSError, subprocess.TimeoutExpired) as exc:
                signature_ok = False
                signature = {"status": "error", "details": str(exc)}

    suite_info = {"status": "not_run", "data": None, "details": ""}
    if host_match["compatible"]:
        try:
            proc = subprocess.run(
                [str(target), "--json", "--info"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                check=False, timeout=20)
            output = proc.stdout.decode("utf-8", "replace").strip()
            if proc.returncode == 0:
                try:
                    suite_info = {"status": "passed", "data": json.loads(output), "details": ""}
                except json.JSONDecodeError:
                    suite_info = {"status": "passed_legacy_text", "data": None,
                                  "details": output[:4000]}
            else:
                suite_info = {"status": "failed", "data": None, "details": output[:4000]}
        except (OSError, subprocess.TimeoutExpired) as exc:
            suite_info = {"status": "error", "data": None, "details": str(exc)}
    else:
        suite_info["details"] = "not executed because the artifact format/architecture does not match this host"

    info_ok = suite_info["status"] in ("passed", "passed_legacy_text")
    portable = bool(host_match["compatible"] and static_ok and signature_ok and info_ok)
    report = {
        "schema_version": 1,
        "artifact": {
            "path": str(target.resolve()),
            "size_bytes": target.stat().st_size,
            "sha256": common.sha256_file(target),
        },
        "build_host": {
            "system": platform.system,
            "release": __import__("platform").release(),
            "architecture": platform.machine,
            "libc": platform.libc,
            "target_triple": platform.triple,
        },
        "binary": binary,
        "host_compatibility": host_match,
        "static_dependency_check": {
            "status": "passed" if static_ok else "failed",
            "non_system_dependencies_or_errors": offenders,
        },
        "code_signature": signature,
        "suite_info_smoke_test": suite_info,
        "cpu_baseline": {
            "status": "not_encoded",
            "details": "The executable does not reliably encode the compiler's minimum CPU baseline. "
                       "The builder does not add -march=native/-mcpu=native by default, but custom "
                       "compiler wrappers may add target-specific flags.",
        },
        "kernel_compatibility": {
            "status": "not_certified",
            "details": "Binary metadata cannot prove compatibility with every kernel release. "
                       "Validate on the oldest supported kernel for this target.",
        },
        "portable_on_build_host": portable,
        "limitations": [
            "This report checks the current build host, not every machine with the same architecture.",
            "The target needs a writable temporary/cache directory for single-file extraction.",
            "Multi-node MPI execution still depends on cluster launcher, network, and scheduler configuration.",
        ],
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("Artifact: %s" % target)
        print("  format: %s" % binary.get("format"))
        print("  architecture(s): %s" % ", ".join(binary.get("architectures", [])))
        print("  host compatibility: %s" % ("PASS" if host_match["compatible"] else "FAIL"))
        print("  static dependency check: %s" % ("PASS" if static_ok else "FAIL"))
        for item in offenders:
            print("    " + item)
        print("  code signature: %s" % signature["status"])
        print("  suite info smoke test: %s" % suite_info["status"])
        if binary.get("minimum_glibc_version"):
            print("  minimum GLIBC symbol version: %s" % binary["minimum_glibc_version"])
        if binary.get("minimum_os_version"):
            print("  minimum OS version: %s" % binary["minimum_os_version"])
        print("  portable on this build host: %s" % ("PASS" if portable else "FAIL"))
        print("  CPU baseline: not encoded; see report limitations")
        if suite_info.get("details"):
            print(suite_info["details"])
    return 0 if portable else 1


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


def _add_subparser(sub, name: str, summary: str, description: str, epilog: str):
    return sub.add_parser(
        name,
        help=summary,
        description=description,
        epilog=textwrap.dedent(epilog).strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build.py",
        description=(
            "Build the complete JOREK suite from source and fuse every "
            "model/feature/parameter variant into one native single-file launcher."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            OPTION PLACEMENT
              Global options may appear before the command:
                build.py --work /big/build --jobs 32 dist ...
              Build-matrix options belong after the command:
                build.py dist --model 600 --param-grid n_tor=33 ...
              Do not use --param-grid/--param-max before the command; they
              are subcommand options.

            PARAMETER GRIDS
              Hard-coded JOREK parameters are compiled into each variant.
              The launcher selects a variant by --params at run time.

              --set NAME=VALUE
                Override one hard-coded parameter for every planned variant.

              --param-grid NAME=V1,V2,...
                Build this explicit Cartesian set of values.

              --param-max NAME=MAX
                Build every value up to MAX.  n_tor, n_coord_tor and n_order
                use only odd values 1,3,5,...; n_plane and normal integer
                parameters use 1..MAX.  The capacity parameters
                n_nodes_max, n_elements_max, n_boundary_max and n_pieces_max
                are upper bounds, so --param-max builds only the MAX variant
                of each capacity.  l_pol_domm starts at 0.

              Multiple parameter options form their Cartesian product.
              Invalid JOREK combinations are skipped automatically; in
              particular n_plane must be at least 2*(n_tor-1).

              --param-grid and --param-max cannot both name the same
              parameter.  --everything does NOT generate numeric values;
              add --param-grid or --param-max explicitly.

            COMMANDS
              doctor    host/toolchain/JOREK diagnostic
              deps      inspect, fetch or build dependency sources
              bootstrap build missing compiler/toolchain pieces
              matrix    plan variants without building
              build     build variants, without packing a suite
              dist      build variants and pack one suite
              full      build all solver backends and merge their variants
              verify    check static linkage/signature of a suite
              run       launch a suite
              env       print shell exports for the work prefix
              clean     remove selected build outputs
              version   print the builder version

            EXAMPLES
              python3 build.py doctor
              python3 build.py matrix --model 600 --param-max n_tor=33 \\
                  --param-max n_plane=128
              python3 build.py --deps source dist --model 600 \\
                  --matrix maximal --programs main \\
                  --param-max n_tor=33 --param-max n_plane=128
              python3 build.py --deps source full --model 600 \\
                  --matrix maximal --programs main \\
                  --param-grid n_tor=33 --param-grid n_plane=128
              ./build/dist/jorek-suite-model600-all-solvers --list

            NOTES
              * Model families (180, 183, 600, 750) are expanded to every
                supported extension combination by --matrix complete;
                --matrix maximal enables all supported extensions at once.
              * Proprietary WSMP and the legacy HIPS backend are never
                downloaded.  Supply their libraries explicitly only if
                licensed; see the --solver help and README.
              * The suite embeds the MPICH/Hydra mpiexec and process manager.
                Use --mpi -np N for local runs and --hostfile/--launcher ssh
                for clusters with a shared filesystem.
        """),
    )
    parser.add_argument("--root", default=None, help="JOREK source checkout; default auto-detect, or WORK/src/JOREK with --fetch-source")
    parser.add_argument("--fetch-source", action="store_true", help="clone the JOREK git repository into --root if it is missing")
    parser.add_argument("--jorek-url", default=JOREK_GIT_URL, help="JOREK git URL used with --fetch-source (default: %(default)s)")
    parser.add_argument("--work", default=os.environ.get("JOREK_BUILD_DIR", str(WORK_DEFAULT)), help="build/work directory (default: %(default)s)")
    parser.add_argument("--jobs", "-j", type=int, default=common.cpu_count(), help="parallel build jobs (default: number of CPUs, %(default)s)")
    parser.add_argument("--verbose", "-v", action="store_true", help="show every executed command")
    parser.add_argument("--quiet", "-q", action="store_true", help="suppress informational output")
    parser.add_argument("--version", action="version", version="JOREK build.py %s" % VERSION, help="print builder version and exit")
    parser.add_argument("--dry-run", action="store_true", help="print the planned commands/variants without executing them")
    parser.add_argument("--offline", action="store_true", help="never access the network; use only cached source archives")
    parser.add_argument("--force", action="store_true", help="rebuild dependencies even when their stamp files exist")
    parser.add_argument("--no-checksum", action="store_true", help="do not verify downloaded source archive digests")
    parser.add_argument("--no-verify", action="store_true", help="skip static dependency verification")
    parser.add_argument("--allow-shared", action="store_true", help="permit non-system shared libraries instead of failing verification")
    parser.add_argument("--no-full-static", action="store_true", help="do not pass -static on Linux/BSD/Windows")
    parser.add_argument("--require-full-static", action="store_true", help="fail instead of falling back when the platform lacks a static libc")
    parser.add_argument("--keep-going", action="store_true", help="continue after a JOREK target or variant fails")
    parser.add_argument("--keep-source", action="store_true", help="keep staged per-variant source trees after building")
    parser.add_argument("--toolchain-prefix", default=os.environ.get("JOREK_TOOLCHAIN_PREFIX"), help="prefix containing a bootstrapped compiler toolchain")
    parser.add_argument("--cc", default=os.environ.get("JOREK_CC"), help="C compiler to use (default: auto-detect)")
    parser.add_argument("--cxx", default=os.environ.get("JOREK_CXX"), help="C++ compiler to use (default: auto-detect)")
    parser.add_argument("--fc", default=os.environ.get("JOREK_FC"), help="Fortran compiler to use (default: auto-detect)")
    parser.add_argument("--mpi-prefix", default=os.environ.get("JOREK_MPI_PREFIX"), help="MPI installation prefix to use for headers/libraries")
    parser.add_argument("--dep-prefix", action="append", default=[], metavar="DIR", help="additional static dependency prefix; repeatable and searched before built deps")
    parser.add_argument("--source", action="append", default=[], metavar="NAME=DIR", help="use an already-extracted source tree for the named dependency")
    parser.add_argument("--source-dir", default=None, metavar="DIR", help="directory containing extracted <name>-<version> dependency trees")
    parser.add_argument("--url", action="append", default=[], metavar="NAME=URL", help="override the primary source URL for a dependency")
    parser.add_argument("--mirror", action="append", default=[], metavar="NAME=URL", help="additional fallback source URL for a dependency; repeatable")
    parser.add_argument("--deps", choices=("source", "auto", "system", "none"), default="auto",
                        help="dependency policy: source=build all needed static libraries; auto=use complete static prefix else build from source; system=require preinstalled static libraries; none=do not build/resolve deps and use --dep-prefix")
    parser.add_argument("--profile", choices=("minimal", "standard", "full", "complete"), default="full",
                        help="dependency/solver profile: minimal/standard=core stack; full=also Boost; complete=also optional solver/SCOTCH dependencies")
    parser.add_argument("--everything", action="store_true",
                        help="preset for the largest useful build: --matrix complete, --solver all, --programs all, --profile complete, --deps auto; numeric grids still need --param-grid or --param-max")
    parser.add_argument("--solver", choices=("mumps", "pastix6", "pastix5", "strumpack", "wsmp", "hips", "all", "none"), default="mumps",
                        help="direct solver backend: mumps, pastix6, pastix5, strumpack, all (MUMPS+PaStiX6+STRUMPACK), or none; wsmp/hips require externally supplied proprietary libraries")
    parser.add_argument("--ima-root", "--imas-root", dest="imas_root", default=os.environ.get("JOREK_IMAS_ROOT"), help="IMAS installation prefix (only needed for the IMAS feature)")
    parser.add_argument("--catalyst-root", default=os.environ.get("JOREK_CATALYST_ROOT"), help="ParaView Catalyst installation prefix (only needed for the Catalyst feature)")

    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = _add_subparser(sub, "doctor",
        "diagnose host, toolchain, JOREK checkout and disk",
        "Inspect the host platform, compiler/tool availability, JOREK checkout and disk space. No files are modified.",
        """
        EXAMPLES
          build.py doctor
          build.py doctor --json
        """)
    p.add_argument("--json", action="store_true", help="emit the diagnostic report as machine-readable JSON")
    p.set_defaults(func=cmd_doctor)

    p = _add_subparser(sub, "deps",
        "inspect, download or build third-party dependencies",
        "Inspect, resolve, download, build or verify the static third-party dependency set.",
        """
        ACTIONS
          list     print the dependency table (name, version, license, homepage)
          graph    print the resolved dependency order and direct children
          fetch    download the source archives, without building
          build    build the selected dependencies and their prerequisites
          verify   check that each dependency's static libraries are installed

        --only restricts graph/fetch/build/verify to the named dependencies;
        by default the profile/solver dependency closure is used.
        """)
    p.add_argument("action", choices=("list", "graph", "fetch", "build", "verify"), metavar="ACTION",
                   help="operation to perform: list, graph, fetch, build, or verify")
    p.add_argument("--only", action="append", metavar="NAME", help="restrict to this dependency name; repeatable")
    p.add_argument("--deps-action", dest="deps_action", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_deps, deps_action=None)

    p = _add_subparser(sub, "bootstrap",
        "build missing compiler and foundational tools",
        "Build a missing GNU make/GCC/Fortran toolchain and optionally the JOREK third-party stack.",
        """
        EXAMPLES
          build.py bootstrap
          build.py bootstrap --with-deps
        """)
    p.add_argument("--with-deps", action="store_true", help="also build the complete JOREK static dependency set")
    p.set_defaults(func=cmd_bootstrap)

    p = _add_subparser(sub, "matrix",
        "list the model/feature/parameter variant matrix",
        "Plan and print variants without compiling them. Useful before a large --param-grid/--param-max build.",
        """
        MATRIX MODES
          models    one variant per model (default)
          maximal   each model with all supported model extensions enabled
          complete  every supported model-extension combination

        --model filters the matrix to one model.  Parameter options form a
        Cartesian product; --param-max and --param-grid may be used here
        without building anything.
        """)
    p.add_argument("--matrix", choices=("models", "complete", "maximal"), default="models",
                   help="variant set to plan (default: models)")
    p.add_argument("--family-toggle", action="append", default=[], metavar="NAME=0|1",
                   help="override one model-family extension; repeatable, e.g. with_TiTe=1")
    p.add_argument("--model", help="restrict the planned matrix to one model number, e.g. 303 or 600")
    p.add_argument("--programs", choices=("main", "most", "all"), default="main",
                   help="program set metadata to attach to planned variants (default: main)")
    p.add_argument("--json", action="store_true", help="emit the planned variants as JSON instead of a table")
    p.add_argument("--details", action="store_true", help="include feature tokens and hard-coded parameters in the table")
    p.add_argument("--enable", action="append", default=[], metavar="FEATURE", help="enable a compile-time feature; repeatable")
    p.add_argument("--disable", action="append", default=[], metavar="FEATURE", help="disable a compile-time feature; repeatable")
    p.add_argument("--debug", action="store_true", help="plan debug builds (runtime checks and debug symbols)")
    p.set_defaults(func=cmd_matrix)

    p = _add_subparser(sub, "build",
        "build one or more JOREK variants",
        "Compile the planned JOREK variants but do not pack them into a suite.",
        """
        Examples
          build.py build --model 303 --matrix one --programs main
          build.py build --model 600 --matrix maximal --param-grid n_tor=33 --param-grid n_plane=128
        """)
    _add_build_options(p, suite_io=False)
    p.set_defaults(func=cmd_build)

    p = _add_subparser(sub, "dist",
        "build variants and pack one single-file suite",
        "Build the planned variants and write one native single-file JOREK suite.",
        """
        Examples
          build.py dist --model 303 --programs main
          build.py dist --model 600 --matrix maximal --programs all \\
              --param-max n_tor=33 --param-max n_plane=128 --output suite
        """)
    _add_build_options(p, suite_io=True)
    p.set_defaults(func=cmd_dist)

    p = _add_subparser(sub, "full",
        "build all solvers and merge one full suite",
        "Build the MUMPS core plus the PaStiX6/STRUMPACK solver stack and PaStiX5 variants, then merge all variants into one suite.",
        """
        This is the only command that builds multiple direct-solver variants:
          all variant     MUMPS + PaStiX6 + STRUMPACK in one executable
          pastix5 variant PaStiX5 in a separate executable
          mumps variant   MUMPS in a separate executable (when selected)

        WSMP and HIPS are never downloaded; they require externally supplied
        licensed libraries and are not part of this full suite by default.
        Use --deps source in a clean work directory so PaStiX/STRUMPACK are
        actually built (auto may reuse an incomplete system prefix).
        --solver is accepted globally but this command always builds the
        all-solver variant plus the PaStiX5 variant; use --solver-prefix or
        --no-build-solvers to control where the solver stack comes from.
        """)
    p.add_argument("--model", default=None, help="build only this model; default: all models (or one if --matrix one)")
    p.add_argument("--solver", choices=("mumps", "pastix6", "pastix5", "strumpack", "wsmp", "hips", "all", "none"), default=None,
                   help="accepted for compatibility; full always builds the all-solver and PaStiX5 variants, independent of this value")
    p.add_argument("--solver-prefix", default=None, metavar="DIR", help="location for the PaStiX/STRUMPACK solver stack (default: WORK/solvers)")
    p.add_argument("--no-build-solvers", action="store_true", help="use an existing solver prefix instead of building PaStiX/STRUMPACK")
    p.add_argument("--output", default=None, metavar="FILE", help="final single-file suite path (default: WORK/dist/jorek-suite)")
    p.add_argument("--compression", type=int, default=9, metavar="LEVEL", help="DEFLATE compression level for the suite payload, 0..9 (default: 9)")
    p.set_defaults(func=cmd_full)

    p = _add_subparser(sub, "verify",
        "verify static linkage and code signature",
        "Verify that a built suite has only allowed system dynamic dependencies and, on macOS, a valid code signature.",
        """
        EXAMPLES
          build.py verify build/dist/jorek-suite
          build.py verify --work /big/build build/dist/jorek-suite
        """)
    p.add_argument("target", nargs="?", metavar="SUITE", help="suite path to verify (default: WORK/dist/jorek-suite)")
    p.add_argument("--json", action="store_true", help="emit a machine-readable portability report")
    p.set_defaults(func=cmd_verify)

    p = _add_subparser(sub, "run",
        "run a built suite",
        "Launch a built suite, optionally through the embedded MPI launcher.",
        """
        EXAMPLES
          build.py run --suite build/dist/jorek-suite --model 303 --stdin namelist
          build.py run --suite suite --variant NAME -- --mpi -np 8
        """)
    p.add_argument("--suite", default=None, metavar="FILE", help="suite executable to run (default: WORK/dist/jorek-suite)")
    p.add_argument("--model", default=None, help="model selector passed to the suite")
    p.add_argument("--variant", default=None, metavar="NAME", help="exact variant name passed to the suite")
    p.add_argument("--program", default=None, metavar="NAME", help="embedded program name to run (default: jorek2_main)")
    p.add_argument("--stdin", default=None, metavar="FILE", help="JOREK namelist file to feed on standard input")
    p.add_argument("args", nargs=argparse.REMAINDER, metavar="JOREK-ARGS", help="arguments after -- are passed to JOREK")
    p.set_defaults(func=cmd_run)

    p = _add_subparser(sub, "env",
        "print shell exports for the work prefix",
        "Print shell export statements for PATH, PKG_CONFIG_PATH, compilers and linker flags for the build prefix.",
        """
        Usage
          eval "$(build.py env)"
        """)
    p.set_defaults(func=cmd_env)

    p = _add_subparser(sub, "clean",
        "remove selected build outputs",
        "Remove build outputs from the work directory. With no flags, removes WORK/build and WORK/dist.",
        """
        Selection
          --all        remove every child of WORK
          --builds     remove staged source trees and compiled variants
          --dist       remove built suite distributions
          --downloads  remove the download cache
          --deps       remove the built dependency prefix

        --all takes precedence over the individual flags.  Work-directory
        safety checks reject paths outside WORK.
        """)
    p.add_argument("--all", action="store_true", help="remove every child of the work directory")
    p.add_argument("--builds", action="store_true", help="remove staged JOREK sources and variant build trees")
    p.add_argument("--dist", action="store_true", help="remove suite distributions")
    p.add_argument("--downloads", action="store_true", help="remove the downloaded source archive cache")
    p.add_argument("--deps", dest="deps_flag", action="store_true", help="remove the built static dependency prefix")
    p.set_defaults(func=cmd_clean)

    p = _add_subparser(sub, "version",
        "print the builder version",
        "Print the JOREK suite builder version and exit.",
        "\nVersion information for the JOREK single-file suite builder.\n")
    p.set_defaults(func=lambda a: (print("JOREK build.py %s" % VERSION), 0)[1])

    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, subparser in action.choices.items():
                _add_global_to_sub(subparser, include_build_options=name in ("matrix", "build", "dist", "full"))
    return parser


def _add_build_options(parser: argparse.ArgumentParser, *, suite_io: bool) -> None:
    parser.add_argument("--matrix", choices=("one", "models", "complete", "maximal"), default="one",
                        help="variant set: one=one model, models=one variant per model, complete=every model-extension combination, maximal=all extensions enabled per model (default: one)")
    parser.add_argument("--model", default=None, help="JOREK model number to build; with --matrix one/models it is the only model, with complete/maximal it filters the expansion (default: 303)")
    parser.add_argument("--set", dest="set_param", action="append", default=[], metavar="NAME=VALUE",
                        help="override a hard-coded integer parameter for every planned variant; repeatable; use matrix --details to inspect names")
    parser.add_argument("--family-toggle", action="append", default=[], metavar="NAME=0|1",
                        help="override a model-family extension; repeatable, e.g. with_TiTe=1")
    parser.add_argument("--param-grid", action="append", default=[], metavar="NAME=V1,V2,...",
                        help="build an explicit Cartesian grid of values for a hard-coded parameter; repeatable; different parameter options are multiplied together; may not name the same parameter as --param-max")
    parser.add_argument("--param-max", action="append", default=[], metavar="NAME=MAX",
                        help="build every value up to MAX for one hard-coded parameter: n_tor/n_coord_tor/n_order use odd 1..MAX; n_plane and other integers use 1..MAX; capacities build only MAX; repeated options form a Cartesian product; n_plane below 2*(n_tor-1) is skipped; --param-max and --param-grid cannot name the same parameter")
    parser.add_argument("--programs", choices=("main", "most", "all", "list"), default="main",
                        help="program set per variant: main=jorek2_main, most=main+most diagnostics, all=all diagnostics, list=print the 'most' program names without building")
    parser.add_argument("--enable", action="append", default=[], metavar="FEATURE", help="enable a compile-time feature; repeatable; use matrix --details to inspect available tokens")
    parser.add_argument("--disable", action="append", default=[], metavar="FEATURE", help="disable a compile-time feature; repeatable; mutually exclusive alternatives are rejected")
    parser.add_argument("--debug", action="store_true", help="build with runtime checks and debug symbols")
    if suite_io:
        parser.add_argument("--output", default=None, metavar="FILE", help="suite output path (default: WORK/dist/jorek-suite)")
        parser.add_argument("--compression", type=int, default=9, metavar="LEVEL", help="DEFLATE compression level for the suite payload, 0..9 (default: 9)")


def _add_global_to_sub(parser: argparse.ArgumentParser, *, include_build_options: bool) -> None:
    """Let users place global options after the subcommand as well."""
    common_opts = [
        ("--root", dict(default=argparse.SUPPRESS, help="JOREK source checkout; default auto-detect")),
        ("--fetch-source", dict(action="store_true", default=argparse.SUPPRESS, help="clone JOREK into --root if missing")),
        ("--jorek-url", dict(default=argparse.SUPPRESS, help="JOREK git URL used with --fetch-source")),
        ("--work", dict(default=argparse.SUPPRESS, help="build/work directory")),
        ("--jobs", dict(type=int, default=argparse.SUPPRESS, help="parallel build jobs")),
        ("--verbose", dict(action="store_true", default=argparse.SUPPRESS, help="show every executed command")),
        ("--quiet", dict(action="store_true", default=argparse.SUPPRESS, help="suppress informational output")),
        ("--dry-run", dict(action="store_true", default=argparse.SUPPRESS, help="print planned commands/variants only")),
        ("--offline", dict(action="store_true", default=argparse.SUPPRESS, help="never access the network")),
        ("--force", dict(action="store_true", default=argparse.SUPPRESS, help="rebuild dependencies even if stamped")),
        ("--no-checksum", dict(action="store_true", default=argparse.SUPPRESS, help="do not verify source archive digests")),
        ("--no-verify", dict(action="store_true", default=argparse.SUPPRESS, help="skip static dependency verification")),
        ("--allow-shared", dict(action="store_true", default=argparse.SUPPRESS, help="allow non-system shared libraries")),
        ("--no-full-static", dict(action="store_true", default=argparse.SUPPRESS, help="do not pass -static on Linux/BSD/Windows")),
        ("--require-full-static", dict(action="store_true", default=argparse.SUPPRESS, help="fail if a static libc is unavailable")),
        ("--keep-going", dict(action="store_true", default=argparse.SUPPRESS, help="continue after a target/variant fails")),
        ("--keep-source", dict(action="store_true", default=argparse.SUPPRESS, help="keep staged per-variant source trees")),
        ("--toolchain-prefix", dict(default=argparse.SUPPRESS, help="prefix containing a bootstrapped toolchain")),
        ("--cc", dict(default=argparse.SUPPRESS, help="C compiler")),
        ("--cxx", dict(default=argparse.SUPPRESS, help="C++ compiler")),
        ("--fc", dict(default=argparse.SUPPRESS, help="Fortran compiler")),
        ("--mpi-prefix", dict(default=argparse.SUPPRESS, help="MPI installation prefix")),
        ("--dep-prefix", dict(action="append", default=argparse.SUPPRESS, metavar="DIR", help="additional static dependency prefix")),
        ("--deps", dict(choices=("source", "auto", "system", "none"), default=argparse.SUPPRESS, help="dependency policy: source, auto, system, or none")),
        ("--source", dict(action="append", default=argparse.SUPPRESS, metavar="NAME=DIR", help="use an extracted source tree for a dependency")),
        ("--source-dir", dict(default=argparse.SUPPRESS, metavar="DIR", help="directory of extracted dependency sources")),
        ("--url", dict(action="append", default=argparse.SUPPRESS, metavar="NAME=URL", help="override a primary source URL")),
        ("--mirror", dict(action="append", default=argparse.SUPPRESS, metavar="NAME=URL", help="additional fallback source URL")),
        ("--profile", dict(choices=("minimal", "standard", "full", "complete"), default=argparse.SUPPRESS, help="dependency/solver profile")),
        ("--solver", dict(choices=("mumps", "pastix6", "pastix5", "strumpack", "wsmp", "hips", "all", "none"), default=argparse.SUPPRESS, help="direct solver backend (all=MUMPS+PaStiX6+STRUMPACK)")),
        ("--ima-root", "--imas-root", dict(dest="imas_root", default=argparse.SUPPRESS, help="IMAS installation prefix")),
        ("--catalyst-root", dict(default=argparse.SUPPRESS, help="ParaView Catalyst installation prefix")),
    ]
    if include_build_options:
        common_opts.append(("--everything", dict(action="store_true", default=argparse.SUPPRESS, help="preset: complete matrix, all solvers, all programs, complete deps; numeric grids still need --param-grid/--param-max")))
        common_opts += [
            ("--param-max", dict(action="append", default=argparse.SUPPRESS, metavar="NAME=MAX", help="build every value up to MAX for one hard-coded parameter: n_tor/n_coord_tor/n_order odd 1..MAX, n_plane and other integers 1..MAX, capacities only MAX; repeated options form a Cartesian product; n_plane below 2*(n_tor-1) is skipped; cannot name the same parameter as --param-grid")),
            ("--param-grid", dict(action="append", default=argparse.SUPPRESS, metavar="NAME=V1,V2,...", help="build an explicit Cartesian grid for a hard-coded parameter; repeatable and combined across parameters; cannot combine with --param-max for the same name")),
            ("--set", dict(dest="set_param", action="append", default=argparse.SUPPRESS, metavar="NAME=VALUE", help="override a hard-coded parameter for every planned variant")),
            ("--family-toggle", dict(action="append", default=argparse.SUPPRESS, metavar="NAME=0|1", help="override a model-family extension")),
            ("--matrix", dict(choices=("one", "models", "complete", "maximal"), default=argparse.SUPPRESS, help="variant set: one, models, complete, or maximal")),
            ("--programs", dict(choices=("main", "most", "all", "list"), default=argparse.SUPPRESS, help="program set: main, most, all, or list")),
            ("--enable", dict(action="append", default=argparse.SUPPRESS, metavar="FEATURE", help="enable a compile-time feature")),
            ("--disable", dict(action="append", default=argparse.SUPPRESS, metavar="FEATURE", help="disable a compile-time feature")),
            ("--debug", dict(action="store_true", default=argparse.SUPPRESS, help="build with runtime checks and debug symbols")),
        ]
    for item in common_opts:
        if len(item) == 2:
            option_strings, kwargs = item
        else:
            # Three-element form: (primary option, alias, kwargs).
            option_strings, kwargs = item[:2], item[2]
        if isinstance(option_strings, str):
            option_strings = (option_strings,)
        if any(opt in parser._option_string_actions for opt in option_strings):
            continue
        parser.add_argument(*option_strings, **kwargs)


def _apply_everything(args) -> None:
    if not getattr(args, "everything", False):
        return
    # A single switch for the largest useful build.  Numeric parameter
    # combinations still need an explicit --param-grid: there is no finite
    # universal range for n_tor, n_order, grid capacities, etc.
    args.matrix = "complete"
    args.solver = "all"
    args.programs = "all"
    args.profile = "complete"
    args.deps = "auto"


def _postprocess_args(args) -> None:
    # ``deps`` uses a positional action; store it for the command.
    if args.command == "deps":
        args.deps_action = args.action
    # JOREK program names are resolved lazily by cmd_dist/build.
    if args.command in ("build", "dist", "matrix", "full"):
        if args.command == "matrix" and not hasattr(args, "set_param"):
            args.set_param = []
        args._program_names = ["jorek2_main"]
        if getattr(args, "programs", "main") == "main":
            args._program_names = ["jorek2_main"]
        elif getattr(args, "programs", "main") == "most":
            args._program_names = ["jorek2_main"]
        if args.command == "full":
            for name, value in (("enable", []), ("disable", []), ("debug", False),
                                ("set_param", []), ("param_grid", []), ("param_max", []),
                                ("family_toggle", [])):
                if not hasattr(args, name):
                    setattr(args, name, value)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    # Shared options may be specified before or after the subcommand.  Merge
    # the subparser defaults with the global values where appropriate.
    try:
        _apply_everything(args)
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
