"""SSH-based native remote builder command construction.

Remote builders run on the target host and produce native artifacts. This first
implementation supports POSIX remote hosts and transfers the verified artifact
back with OpenSSH scp. It is not a cross-compiler.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import PurePosixPath
from typing import Any, Dict, List


class RemoteProfileError(ValueError):
    pass


REMOTE_OVERRIDE_KEYS = frozenset({
    "cc", "cxx", "fc", "mpi_prefix", "toolchain_prefix", "source_dir",
    "jorek_url", "imas_root", "catalyst_root", "solver_prefix",
    "dep_prefixes", "sources", "urls", "mirrors", "deps_policy", "profile",
    "solver", "compression", "jobs", "no_build_solvers", "offline", "force",
    "no_checksum", "no_verify", "keep_source", "allow_shared",
    "require_full_static", "no_full_static", "keep_going", "verbose",
})


def parse_remote_overrides(value: str) -> Dict[str, Any]:
    """Parse an optional JSON object of remote-host-specific build settings."""
    if not str(value or "").strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RemoteProfileError("remote overrides must be valid JSON: %s" % exc) from exc
    if not isinstance(parsed, dict):
        raise RemoteProfileError("remote overrides must be a JSON object")
    unknown = sorted(set(parsed) - REMOTE_OVERRIDE_KEYS)
    if unknown:
        raise RemoteProfileError("unsupported remote override key(s): %s" % ", ".join(unknown))
    string_keys = REMOTE_OVERRIDE_KEYS - {
        "dep_prefixes", "sources", "urls", "mirrors", "compression", "jobs",
        "no_build_solvers", "offline", "force", "no_checksum", "no_verify",
        "keep_source", "allow_shared", "require_full_static", "no_full_static",
        "keep_going", "verbose",
    }
    for key in string_keys:
        if key in parsed and not isinstance(parsed[key], str):
            raise RemoteProfileError("%s must be a string" % key)
    for key in ("sources", "urls", "mirrors"):
        if key in parsed and not isinstance(parsed[key], (str, list)):
            raise RemoteProfileError("%s must be a string or list of NAME=VALUE entries" % key)
    if "dep_prefixes" in parsed:
        values = parsed["dep_prefixes"]
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise RemoteProfileError("dep_prefixes must be a list of strings")
    for key in ("compression", "jobs"):
        if key in parsed and (not isinstance(parsed[key], int) or isinstance(parsed[key], bool)):
            raise RemoteProfileError("%s must be an integer" % key)
    if "compression" in parsed and parsed["compression"] not in range(10):
        raise RemoteProfileError("compression must be between 0 and 9")
    if "jobs" in parsed and parsed["jobs"] < 1:
        raise RemoteProfileError("jobs must be at least 1")
    for key in (
        "no_build_solvers", "offline", "force", "no_checksum", "no_verify",
        "keep_source", "allow_shared", "require_full_static", "no_full_static",
        "keep_going", "verbose",
    ):
        if key in parsed and not isinstance(parsed[key], bool):
            raise RemoteProfileError("%s must be a boolean" % key)
    return parsed


def validate_connection_profile(profile: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    destination = str(profile.get("destination", "")).strip()
    if not destination or destination.startswith("-") or any(ch.isspace() for ch in destination):
        errors.append("SSH destination must be a non-empty user@host or SSH alias without whitespace")
    try:
        port = int(profile.get("port", 22))
        if not 1 <= port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("SSH port must be an integer between 1 and 65535")
    return errors


def validate_profile(profile: Dict[str, Any]) -> List[str]:
    errors = validate_connection_profile(profile)
    for key in ("remote_root", "remote_work"):
        value = str(profile.get(key, "")).strip()
        if not value or not value.startswith("/"):
            errors.append("%s must be an absolute POSIX path" % key)
    if not str(profile.get("repo_url", "")).strip():
        errors.append("repository URL is required")
    if not str(profile.get("branch", "")).strip():
        errors.append("repository branch is required")
    commit = str(profile.get("commit", "")).strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", commit):
        errors.append("pin the remote build to a full 40- or 64-character commit SHA")
    try:
        parse_remote_overrides(str(profile.get("remote_overrides", "")))
    except RemoteProfileError as exc:
        errors.append(str(exc))
    return errors


def _ssh_common(profile: Dict[str, Any], scp: bool = False) -> List[str]:
    port = str(int(profile.get("port", 22)))
    args = ["-P" if scp else "-p", port]
    args.extend(["-o", "BatchMode=yes"])
    args.extend(["-o", "ConnectTimeout=20"])
    args.extend(["-o", "ServerAliveInterval=30"])
    args.extend(["-o", "ServerAliveCountMax=3"])
    policy = str(profile.get("host_key_policy", "yes")).strip() or "yes"
    if policy not in ("yes", "accept-new", "no"):
        raise RemoteProfileError("host key policy must be yes, accept-new, or no")
    args.extend(["-o", "StrictHostKeyChecking=" + policy])
    key = str(profile.get("identity_file", "")).strip()
    if key:
        args.extend(["-i", key])
    for option in profile.get("ssh_options", []) or []:
        option = str(option).strip()
        if option:
            args.extend(["-o", option])
    return args


def ssh_arguments(profile: Dict[str, Any], remote_command: str) -> List[str]:
    errors = validate_connection_profile(profile)
    if errors:
        raise RemoteProfileError("; ".join(errors))
    return _ssh_common(profile) + [str(profile["destination"]).strip(), remote_command]


def connection_test_command(profile: Dict[str, Any]) -> str:
    python = str(profile.get("python", "python3")).strip() or "python3"
    commands = [
        "set -eu",
        "printf 'system='; uname -s",
        "printf 'machine='; uname -m",
        "command -v " + shlex.quote(python),
        shlex.join([python, "--version"]),
        "git --version",
    ]
    return "; ".join(commands)


def remote_repo_path(profile: Dict[str, Any]) -> str:
    return str(PurePosixPath(str(profile["remote_root"])) / "jorek-suite")


def remote_artifact_path(profile: Dict[str, Any], output: str = "") -> str:
    if output:
        return output
    return str(PurePosixPath(str(profile["remote_work"])) / "dist" / "jorek-suite")


def build_remote_command(
    profile: Dict[str, Any],
    build_args: List[str],
    verify_args: List[str],
) -> str:
    errors = validate_profile(profile)
    if errors:
        raise RemoteProfileError("; ".join(errors))
    repo = remote_repo_path(profile)
    root = str(profile["remote_root"])
    repo_url = str(profile["repo_url"])
    branch = str(profile["branch"])
    commit = str(profile["commit"]).lower()
    python = str(profile.get("python", "python3")).strip() or "python3"
    qrepo = shlex.quote(repo)
    qroot = shlex.quote(root)
    qurl = shlex.quote(repo_url)
    qbranch = shlex.quote(branch)
    qcommit = shlex.quote(commit)
    build_command = shlex.join([python, repo + "/build.py"] + list(build_args))
    verify_command = shlex.join([python, repo + "/build.py"] + list(verify_args))
    commands = [
        "set -eu",
        "mkdir -p " + qroot,
        "if [ ! -d " + shlex.quote(repo + "/.git") + " ]; then "
        "git clone --filter=blob:none --no-checkout -- " + qurl + " " + qrepo + "; fi",
        "git -C " + qrepo + " remote set-url origin " + qurl,
        "git -C " + qrepo + " fetch --prune origin " + qbranch,
        "git -C " + qrepo + " checkout --force --detach " + qcommit,
        "test \"$(git -C " + qrepo + " rev-parse HEAD)\" = " + qcommit,
        build_command,
        verify_command,
    ]
    return "; ".join(commands)


def scp_arguments(profile: Dict[str, Any], remote_path: str, local_path: str) -> List[str]:
    errors = validate_connection_profile(profile)
    if errors:
        raise RemoteProfileError("; ".join(errors))
    remote_spec = "%s:%s" % (
        str(profile["destination"]).strip(), shlex.quote(remote_path))
    return _ssh_common(profile, scp=True) + [remote_spec, local_path]


def ssh_executable() -> str:
    return "ssh"


def scp_executable() -> str:
    return "scp"
