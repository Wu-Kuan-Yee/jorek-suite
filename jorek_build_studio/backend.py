"""Read-only inspection helpers for a selected JOREK suite checkout."""

from __future__ import annotations

import ast
import json
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


class BackendError(RuntimeError):
    pass


def external_exec_runner() -> str:
    """Return a POSIX QProcess trampoline that execs argv[0] with the full argv."""
    return "import os,sys; getattr(os,'setsid',lambda:None)(); os.execvp(sys.argv[0], sys.argv)"


def default_build_script() -> Path:
    return Path(__file__).resolve().parent.parent / "build.py"


def resolve_build_script(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file() or path.name != "build.py":
        raise BackendError("Select an existing build.py file; received: %s" % path)
    return path


def _assignments(tree: ast.AST) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for node in ast.walk(tree):
        targets = []
        value = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        if value is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                try:
                    result[target.id] = ast.literal_eval(value)
                except (ValueError, TypeError, SyntaxError):
                    pass
    return result


def inspect_checkout(build_script: str, jorek_root: str = "") -> Dict[str, Any]:
    script = resolve_build_script(build_script)
    source_dir = script.parent / "src"
    jorek_file = source_dir / "jorek.py"
    deps_file = source_dir / "deps.py"
    if not jorek_file.is_file() or not deps_file.is_file():
        raise BackendError("Expected src/jorek.py and src/deps.py beside %s" % script)

    tree = ast.parse(jorek_file.read_text(encoding="utf-8"), filename=str(jorek_file))
    values = _assignments(tree)
    feature_defaults = values.get("DEFAULT_FEATURES", {})
    boolean_features = values.get("BOOLEAN_FEATURES", {})
    family_toggles = values.get("FAMILY_TOGGLES", {})
    features = sorted(str(name) for name in boolean_features)
    features = [name for name in features if name in feature_defaults]

    root = Path(jorek_root).expanduser() if jorek_root else None
    models = []
    toggles_by_model = {}
    toggle_defaults = {}
    if root and (root / "models").is_dir():
        for directory in sorted((root / "models").glob("model*")):
            match = re.fullmatch(r"model(\d{3})", directory.name)
            if match and directory.is_dir():
                model = match.group(1)
                models.append(model)
                toggles_by_model[model] = list(family_toggles.get(model, ()))
                settings = directory / "mod_model_settings.f90"
                try:
                    settings_text = settings.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    settings_text = ""
                toggle_defaults[model] = {}
                for toggle in toggles_by_model[model]:
                    match_toggle = re.search(
                        r"(?im)^\s*logical\s*,\s*parameter\s*::\s*" +
                        re.escape(toggle) + r"\s*=\s*\.(true|false)\.", settings_text)
                    if match_toggle:
                        toggle_defaults[model][toggle] = match_toggle.group(1).lower() == "true"
    if not models:
        models = sorted(str(name) for name in values.get("MODELS", ()))
        toggles_by_model = {model: list(family_toggles.get(model, ())) for model in models}
        toggle_defaults = {model: {} for model in models}

    dep_tree = ast.parse(deps_file.read_text(encoding="utf-8"), filename=str(deps_file))
    dependencies = []
    for node in ast.walk(dep_tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Name) or func.id != "dep" or len(node.args) < 3:
            continue
        try:
            name, version, url = (ast.literal_eval(node.args[i]) for i in range(3))
        except (ValueError, TypeError, SyntaxError):
            continue
        kwargs = {}
        for keyword in node.keywords:
            if keyword.arg:
                try:
                    kwargs[keyword.arg] = ast.literal_eval(keyword.value)
                except (ValueError, TypeError, SyntaxError):
                    pass
        dependencies.append({
            "name": str(name), "version": str(version), "url": str(url),
            "license": str(kwargs.get("license", "")),
            "builder": str(kwargs.get("builder", "autotools")),
            "optional": bool(kwargs.get("optional", False)),
            "platforms": list(kwargs.get("platforms", ())),
            "note": str(kwargs.get("note", "")),
        })
    dependencies.sort(key=lambda item: item["name"])
    return {
        "build_script": str(script),
        "source_dir": str(source_dir),
        "features": features,
        "feature_defaults": {name: bool(feature_defaults.get(name, False)) for name in features},
        "models": models,
        "model_toggles": toggles_by_model,
        "model_toggle_defaults": toggle_defaults,
        "dependencies": dependencies,
        "platform": {
            "system": platform.system(), "release": platform.release(),
            "machine": platform.machine(), "python": sys.version.split()[0],
        },
    }


def run_readonly(build_script: str, args: List[str], cwd: Optional[str] = None,
                 timeout: int = 30) -> subprocess.CompletedProcess:
    script = resolve_build_script(build_script)
    command = [sys.executable, str(script)] + list(args)
    try:
        return subprocess.run(command, cwd=cwd or str(script.parent),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise BackendError("Command timed out after %s seconds: %s" % (timeout, " ".join(command))) from exc


def doctor_arguments(config: Dict[str, Any]) -> List[str]:
    args = ["--work", str(config.get("work_dir") or Path.home() / "jorek-build-work")]
    if config.get("jorek_root"):
        args.extend(["--root", str(config["jorek_root"])])
    return args + ["doctor", "--json"]


def parse_doctor_output(output: str) -> Dict[str, Any]:
    start = output.find("{")
    end = output.rfind("}")
    if start < 0 or end < start:
        raise BackendError("The build driver did not return a JSON doctor report.")
    try:
        data = json.loads(output[start:end + 1])
    except json.JSONDecodeError as exc:
        raise BackendError("Invalid JSON from doctor: %s" % exc) from exc
    if not isinstance(data, dict):
        raise BackendError("Doctor report must be a JSON object.")
    return data


def cli_help(build_script: str, command: str = "") -> str:
    args = ["--help"] if not command else [command, "--help"]
    result = run_readonly(build_script, args, timeout=20)
    return result.stdout or "(No help text was returned.)"


def validate_jorek_root(root: str) -> Optional[str]:
    if not root:
        return "JOREK source directory is not selected."
    path = Path(root).expanduser()
    if not path.is_dir():
        return "JOREK source directory does not exist: %s" % path
    if not (path / "jorek2_main.f90").is_file():
        return "jorek2_main.f90 is missing from: %s" % path
    if not (path / "models").is_dir():
        return "models/ directory is missing from: %s" % path
    return None
