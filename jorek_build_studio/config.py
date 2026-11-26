"""Configuration and command-line construction for JOREK Build Studio.

This module deliberately has no Qt dependency so its validation and argv
construction can be tested on machines without a graphical environment.
"""

from __future__ import annotations

import json
import os
import shlex
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_CONFIG: Dict[str, Any] = {
    "build_script": "",
    "jorek_root": "",
    "work_dir": "",
    "jobs": max(1, os.cpu_count() or 1),
    "cc": "",
    "cxx": "",
    "fc": "",
    "mpi_prefix": "",
    "toolchain_prefix": "",
    "source_dir": "",
    "sources": "",
    "urls": "",
    "mirrors": "",
    "jorek_url": "",
    "imas_root": "",
    "catalyst_root": "",
    "solver_prefix": "",
    "suite_path": "",
    "run_model": "",
    "run_variant": "",
    "run_program": "",
    "stdin_file": "",
    "run_args": "",
    "dep_prefixes": [],
    "command": "dist",
    "matrix": "one",
    "model": "303",
    "programs": "main",
    "solver": "mumps",
    "deps_policy": "auto",
    "profile": "full",
    "param_grid": "",
    "param_max": "",
    "set_params": "",
    "output": "",
    "compression": 9,
    "feature_states": {},
    "feature_defaults": {},
    "family_toggle_states": {},
    "family_toggle_defaults": {},
    "fetch_source": False,
    "everything": False,
    "no_verify": False,
    "keep_source": False,
    "debug": False,
    "no_build_solvers": False,
    "offline": False,
    "force": False,
    "no_checksum": False,
    "allow_shared": False,
    "require_full_static": False,
    "no_full_static": False,
    "keep_going": False,
    "verbose": False,
    "dry_run": False,
    "dep_action": "list",
    "bootstrap_with_deps": True,
    "dep_only": "",
    "remote_profiles": [],
    "active_remote_profile": "",
}


def default_config_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home()))
    elif os.name == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "jorek-build-studio" / "config.json"


def normalize_config(data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    result = dict(DEFAULT_CONFIG)
    if data:
        result.update(data)
    result["jobs"] = max(1, int(result.get("jobs") or 1))
    result["compression"] = min(9, max(0, int(result.get("compression", 9))))
    value = result.get("dep_prefixes", [])
    if isinstance(value, str):
        value = [line.strip() for line in value.splitlines() if line.strip()]
    result["dep_prefixes"] = list(value or [])
    result["feature_states"] = dict(result.get("feature_states") or {})
    result["feature_defaults"] = dict(result.get("feature_defaults") or {})
    result["family_toggle_states"] = dict(result.get("family_toggle_states") or {})
    result["family_toggle_defaults"] = dict(result.get("family_toggle_defaults") or {})
    result["remote_profiles"] = [dict(item) for item in (result.get("remote_profiles") or [])
                                 if isinstance(item, dict)]
    result["active_remote_profile"] = str(result.get("active_remote_profile") or "")
    for key in ("offline", "force", "no_checksum", "allow_shared",
                "require_full_static", "no_full_static", "keep_going",
                "verbose", "dry_run", "fetch_source", "everything",
                "no_verify", "keep_source", "debug", "no_build_solvers", "bootstrap_with_deps"):
        result[key] = bool(result.get(key, False))
    for key in DEFAULT_CONFIG:
        if isinstance(DEFAULT_CONFIG[key], str) and result.get(key) is None:
            result[key] = ""
    return result


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    target = Path(path or default_config_path())
    if not target.exists():
        return normalize_config()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Cannot read configuration %s: %s" % (target, exc)) from exc
    if not isinstance(payload, dict):
        raise ValueError("Configuration root must be a JSON object: %s" % target)
    return normalize_config(payload)


def save_config(data: Dict[str, Any], path: Optional[Path] = None) -> Path:
    target = Path(path or default_config_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(normalize_config(data), ensure_ascii=False, indent=2) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp",
                                     dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return target


def _raw_lines(value: Any) -> List[str]:
    if not value:
        return []
    source = value if isinstance(value, (list, tuple)) else str(value).splitlines()
    return [str(item).strip() for item in source if str(item).strip()]


def _lines(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        source = value
    else:
        source = str(value).replace(";", "\n").splitlines()
    return [str(item).strip() for item in source if str(item).strip()]


def _add_pairs(argv: List[str], option: str, value: Any) -> None:
    for item in _lines(value):
        if "=" not in item:
            raise ValueError("%s expects NAME=VALUE, got %r" % (option, item))
        name, raw = item.split("=", 1)
        if not name.strip() or not raw.strip():
            raise ValueError("%s expects a non-empty NAME and VALUE, got %r" % (option, item))
        argv.extend([option, item])


def global_arguments(config: Dict[str, Any]) -> List[str]:
    cfg = normalize_config(config)
    argv: List[str] = ["--work", str(cfg["work_dir"]), "--jobs", str(cfg["jobs"])]
    if cfg["jorek_root"]:
        argv.extend(["--root", str(cfg["jorek_root"])])
    for key, option in (("cc", "--cc"), ("cxx", "--cxx"), ("fc", "--fc"),
                        ("mpi_prefix", "--mpi-prefix"),
                        ("toolchain_prefix", "--toolchain-prefix"),
                        ("source_dir", "--source-dir"),
                        ("jorek_url", "--jorek-url"),
                        ("imas_root", "--imas-root"),
                        ("catalyst_root", "--catalyst-root")):
        if cfg.get(key):
            argv.extend([option, str(cfg[key])])
    for prefix in cfg["dep_prefixes"]:
        argv.extend(["--dep-prefix", str(prefix)])
    for key, option in (("sources", "--source"), ("urls", "--url"), ("mirrors", "--mirror")):
        for item in _raw_lines(cfg.get(key)):
            if "=" not in item:
                raise ValueError("%s expects NAME=VALUE, got %r" % (option, item))
            argv.extend([option, item])
    argv.extend(["--deps", cfg["deps_policy"], "--profile", cfg["profile"],
                 "--solver", cfg["solver"]])
    for key, option in (("fetch_source", "--fetch-source"), ("everything", "--everything"),
                        ("offline", "--offline"), ("force", "--force"),
                        ("no_checksum", "--no-checksum"), ("no_verify", "--no-verify"),
                        ("keep_source", "--keep-source"),
                        ("allow_shared", "--allow-shared"),
                        ("require_full_static", "--require-full-static"),
                        ("no_full_static", "--no-full-static"),
                        ("keep_going", "--keep-going"), ("verbose", "--verbose"),
                        ("dry_run", "--dry-run")):
        if cfg.get(key):
            argv.append(option)
    return argv


def build_arguments(config: Dict[str, Any], command: Optional[str] = None) -> List[str]:
    """Return argv after the build.py path; never invoke a shell."""
    cfg = normalize_config(config)
    action = command or cfg["command"]
    argv = global_arguments(cfg)
    if action == "doctor":
        return argv + ["doctor", "--json"]
    if action == "bootstrap":
        argv.append("bootstrap")
        if cfg.get("bootstrap_with_deps", True):
            argv.append("--with-deps")
        return argv
    if action == "deps":
        argv.extend(["deps", cfg["dep_action"]])
        for name in _lines(cfg.get("dep_only")):
            for dependency in [part.strip() for part in name.split(",") if part.strip()]:
                argv.extend(["--only", dependency])
        return argv
    if action == "verify":
        argv.append("verify")
        if cfg.get("output"):
            argv.append(str(cfg["output"]))
        argv.append("--json")
        return argv
    if action == "run":
        argv.append("run")
        if cfg.get("suite_path"):
            argv.extend(["--suite", str(cfg["suite_path"])])
        if cfg.get("run_model"):
            argv.extend(["--model", str(cfg["run_model"])])
        if cfg.get("run_variant"):
            argv.extend(["--variant", str(cfg["run_variant"])])
        if cfg.get("run_program"):
            argv.extend(["--program", str(cfg["run_program"])])
        if cfg.get("stdin_file"):
            argv.extend(["--stdin", str(cfg["stdin_file"])])
        if cfg.get("run_args"):
            argv.append("--")
            argv.extend(shlex.split(str(cfg["run_args"]), posix=(os.name != "nt")))
        return argv
    if action == "matrix":
        matrix = cfg["matrix"] if cfg["matrix"] != "one" else "models"
        argv.extend(["matrix", "--matrix", matrix, "--json"])
        if cfg.get("model") and cfg["model"] != "all":
            argv.extend(["--model", str(cfg["model"])])
        argv.extend(["--programs", cfg["programs"]])
        _append_feature_overrides(argv, cfg)
        _append_family_overrides(argv, cfg)
        if cfg.get("debug"):
            argv.append("--debug")
        _add_pairs(argv, "--param-grid", cfg.get("param_grid"))
        _add_pairs(argv, "--param-max", cfg.get("param_max"))
        _add_pairs(argv, "--set", cfg.get("set_params"))
        return argv
    if action not in ("build", "dist", "full"):
        raise ValueError("Unsupported build command: %s" % action)

    argv.append(action)
    if cfg.get("model") and cfg["model"] != "all":
        argv.extend(["--model", str(cfg["model"])])
    if action in ("build", "dist"):
        matrix = cfg["matrix"]
        if cfg.get("model") == "all" and matrix == "one":
            matrix = "models"
        argv.extend(["--matrix", matrix, "--programs", cfg["programs"]])
        _append_feature_overrides(argv, cfg)
        _append_family_overrides(argv, cfg)
        if cfg.get("debug"):
            argv.append("--debug")
        _add_pairs(argv, "--param-grid", cfg.get("param_grid"))
        _add_pairs(argv, "--param-max", cfg.get("param_max"))
        _add_pairs(argv, "--set", cfg.get("set_params"))
    else:
        matrix = cfg["matrix"]
        if cfg.get("model") == "all" and matrix == "one":
            matrix = "models"
        if matrix:
            argv.extend(["--matrix", matrix])
        if cfg.get("programs"):
            argv.extend(["--programs", cfg["programs"]])
        _append_feature_overrides(argv, cfg)
        _append_family_overrides(argv, cfg)
        if cfg.get("debug"):
            argv.append("--debug")
        _add_pairs(argv, "--param-grid", cfg.get("param_grid"))
        _add_pairs(argv, "--param-max", cfg.get("param_max"))
        _add_pairs(argv, "--set", cfg.get("set_params"))
    if action == "full":
        if cfg.get("solver_prefix"):
            argv.extend(["--solver-prefix", str(cfg["solver_prefix"])])
        if cfg.get("no_build_solvers"):
            argv.append("--no-build-solvers")
    if action in ("dist", "full") and cfg.get("output"):
        argv.extend(["--output", str(cfg["output"])])
    if action in ("dist", "full"):
        argv.extend(["--compression", str(cfg["compression"])])
    return argv


def _append_feature_overrides(argv: List[str], config: Dict[str, Any]) -> None:
    defaults = config.get("feature_defaults") or {}
    states = config.get("feature_states") or {}
    for name in sorted(states):
        state = bool(states[name])
        if name not in defaults or state != bool(defaults[name]):
            argv.extend(["--enable" if state else "--disable", name])



def _append_family_overrides(argv: List[str], config: Dict[str, Any]) -> None:
    defaults = config.get("family_toggle_defaults") or {}
    states = config.get("family_toggle_states") or {}
    for name in sorted(states):
        state = bool(states[name])
        if name not in defaults or state != bool(defaults[name]):
            argv.extend(["--family-toggle", "%s=%d" % (name, 1 if state else 0)])

def validate_config(config: Dict[str, Any], require_source: bool = False) -> List[str]:
    cfg = normalize_config(config)
    errors: List[str] = []
    if not cfg["build_script"]:
        errors.append("Select the build.py file.")
    elif not Path(cfg["build_script"]).expanduser().is_file():
        errors.append("build.py does not exist: %s" % cfg["build_script"])
    if not cfg["work_dir"]:
        errors.append("Select a work directory.")
    if cfg["jobs"] < 1:
        errors.append("Parallel job count must be at least 1.")
    if cfg["compression"] not in range(10):
        errors.append("Compression level must be between 0 and 9.")
    if cfg["deps_policy"] not in ("source", "auto", "system", "none"):
        errors.append("Invalid dependency policy.")
    if cfg["profile"] not in ("minimal", "standard", "full", "complete"):
        errors.append("Invalid dependency profile.")
    if cfg["solver"] not in ("mumps", "pastix6", "pastix5", "strumpack",
                              "wsmp", "hips", "all", "none"):
        errors.append("Invalid solver selection.")
    if require_source and cfg["jorek_root"]:
        root = Path(cfg["jorek_root"]).expanduser()
        if not (root / "jorek2_main.f90").is_file() or not (root / "models").is_dir():
            errors.append("The selected directory does not look like a JOREK checkout: %s" % root)
    grid_names = set()
    max_names = set()
    for item in _lines(cfg.get("param_grid")):
        if "=" not in item:
            errors.append("Parameter grid expects NAME=V1,V2,...: %s" % item)
            continue
        name, values = item.split("=", 1)
        if not name.strip() or not values.strip():
            errors.append("Parameter grid has an empty name or value: %s" % item)
        if name in grid_names:
            errors.append("Parameter appears more than once in the grid: %s" % name)
        grid_names.add(name)
    for item in _lines(cfg.get("param_max")):
        if "=" not in item:
            errors.append("Parameter maximum expects NAME=MAX: %s" % item)
            continue
        name, value = item.split("=", 1)
        if not name.strip() or not value.strip():
            errors.append("Parameter maximum has an empty name or value: %s" % item)
        if name in max_names:
            errors.append("Parameter maximum appears more than once: %s" % name)
        max_names.add(name)
    overlap = grid_names & max_names
    if overlap:
        errors.append("A parameter cannot use both grid and maximum: %s" % ", ".join(sorted(overlap)))
    for item in _lines(cfg.get("set_params")):
        if "=" not in item:
            errors.append("Parameter override expects NAME=VALUE: %s" % item)
    return errors
