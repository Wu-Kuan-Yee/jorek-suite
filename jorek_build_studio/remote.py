"""SSH-based native remote builder command construction.

Remote builders run on the target host and produce native artifacts. This first
implementation supports POSIX remote hosts and transfers the verified artifact
back with OpenSSH scp. It is not a cross-compiler.
"""

from __future__ import annotations

import json
import re
import hashlib
import shlex
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional

from .remote_supervisor import (
    DEFAULT_HEARTBEAT_TIMEOUT,
    remote_task_attach_command,
    remote_task_cancel_command,
    remote_task_start_command,
    remote_task_state_root,
    remote_task_status_command,
    validate_remote_task_id,
)


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
    policy = str(profile.get("host_key_policy", "yes")).strip() or "yes"
    if policy not in ("yes", "accept-new", "no"):
        errors.append("host key policy must be yes, accept-new, or no")
    return errors


def validate_profile(profile: Dict[str, Any]) -> List[str]:
    errors = validate_connection_profile(profile)
    if not str(profile.get("name", "")).strip():
        errors.append("profile name is required")
    if not str(profile.get("artifact_dir", "")).strip():
        errors.append("local artifact directory is required")
    for key in ("remote_root", "remote_work"):
        value = str(profile.get(key, "")).strip()
        if not value or not value.startswith("/"):
            errors.append("%s must be an absolute POSIX path" % key)
    if not str(profile.get("repo_url", "")).strip():
        errors.append("repository URL is required")
    branch = str(profile.get("branch", "")).strip()
    if not branch or branch.startswith("-"):
        errors.append("repository branch is required and must not start with '-'")
    commit = str(profile.get("commit", "")).strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", commit):
        errors.append("pin the remote build to a full 40- or 64-character commit SHA")
    try:
        parse_remote_overrides(str(profile.get("remote_overrides", "")))
    except RemoteProfileError as exc:
        errors.append(str(exc))
    timeout = profile.get("heartbeat_timeout", DEFAULT_HEARTBEAT_TIMEOUT)
    if (not isinstance(timeout, int) or isinstance(timeout, bool) or
            not 30 <= timeout <= 3600):
        errors.append("heartbeat_timeout must be an integer between 30 and 3600 seconds")
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


def remote_artifact_path(profile: Dict[str, Any]) -> str:
    identity = "\0".join((
        str(profile.get("destination", "")),
        str(profile.get("name", "")),
        str(profile.get("commit", "")),
    )).encode("utf-8")
    token = hashlib.sha256(identity).hexdigest()[:16]
    # A no-space staging path works with both legacy SCP and SFTP-mode clients.
    return "/tmp/jorek-suite-transfer-" + token


BUILD_EVENT_PREFIX = "@@JOREK_BUILD_EVENT@@"


def _remote_event_line(event: str, **payload) -> str:
    message = {"event": event}
    message.update(payload)
    return "printf '\\n%s\\n' " + shlex.quote(
        BUILD_EVENT_PREFIX + json.dumps(message, ensure_ascii=False, separators=(",", ":")))


_REMOTE_BUILD_STREAMER = r'''
import json
import os
import re
import subprocess
import sys

PREFIX = "@@JOREK_BUILD_EVENT@@"
command = sys.argv[1:]
if not command:
    raise SystemExit("remote build streamer: missing build command")

child_env = os.environ.copy()
child_env["PYTHONIOENCODING"] = "utf-8"
child_env["PYTHONUNBUFFERED"] = "1"
child_env["JOREK_BUILD_STUDIO_EVENTS"] = "1"

# Detect native stage events independently: some pinned revisions may support
# dependency events but not JOREK-variant events (or vice versa).
native_dependency_events = False
native_variant_events = False
if len(command) >= 2:
    build_script = os.path.abspath(command[1])
    deps_script = os.path.join(os.path.dirname(build_script), "src", "deps.py")
    try:
        with open(deps_script, "r", encoding="utf-8", errors="replace") as handle:
            deps_source = handle.read()
        native_dependency_events = (
            "emit_build_event" in deps_source and '"dependency:"' in deps_source
        )
    except OSError:
        pass
    try:
        with open(build_script, "r", encoding="utf-8", errors="replace") as handle:
            build_source = handle.read()
        native_variant_events = (
            "emit_build_event" in build_source and '"variant:"' in build_source
        )
    except OSError:
        pass

work_dir = ""
for index, argument in enumerate(command):
    if argument == "--work" and index + 1 < len(command):
        work_dir = command[index + 1]
        break
    if argument.startswith("--work="):
        work_dir = argument.split("=", 1)[1]
        break


def write_bytes(data):
    stream = getattr(sys.stdout, "buffer", None)
    if stream is not None:
        stream.write(data)
        stream.flush()
    else:
        sys.stdout.write(data.decode("utf-8", "replace"))
        sys.stdout.flush()


def emit(event, **payload):
    message = {"event": event}
    message.update(payload)
    encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    write_bytes(("\n" + PREFIX + encoded + "\n").encode("utf-8"))


fallback_stage = None


def finish_fallback(status, error=""):
    global fallback_stage
    if fallback_stage is None:
        return
    payload = {"id": fallback_stage["id"], "status": status}
    if error:
        payload["error"] = error
    emit("stage_finish", **payload)
    fallback_stage = None


def consume_line(raw):
    global native_dependency_events, native_variant_events, fallback_stage
    line = raw.decode("utf-8", "replace").rstrip("\r\n")
    if line.startswith(PREFIX):
        try:
            event = json.loads(line[len(PREFIX):])
        except (TypeError, ValueError):
            event = {}
        if isinstance(event, dict) and event.get("event") == "stage_start":
            stage_id = str(event.get("id", ""))
            if stage_id.startswith("dependency:"):
                native_dependency_events = True
                finish_fallback("completed")
            elif stage_id.startswith("variant:"):
                native_variant_events = True
                finish_fallback("completed")
        write_bytes(raw)
        return

    dependency_match = re.match(
        r"^\s*>>\s+building\s+([A-Za-z0-9_.+-]+)\s+([^\s(][^\s]*)\s*$",
        line,
    )
    variant_match = re.match(
        r"^\s*>>\s+building\s+(\S+)\s+\(model\s+([^,]+),\s*solver\s+([^,]+),\s*programs\s+(.+)\)\s*$",
        line,
    )

    if variant_match and not native_variant_events:
        finish_fallback("completed")
        name, model, solver, programs = (part.strip() for part in variant_match.groups())
        stage_id = "remote-fallback:variant:" + name
        remote_log = (
            os.path.join(work_dir, "logs", name + "-make.log") if work_dir else ""
        )
        emit(
            "stage_start",
            id=stage_id,
            label="%s (model %s, solver %s, programs %s)" % (
                name, model, solver, programs),
            category="JOREK variant",
            phase="variants",
            log_path=remote_log,
        )
        fallback_stage = {"id": stage_id, "name": name}
    elif dependency_match and not native_dependency_events:
        finish_fallback("completed")
        name, version = dependency_match.groups()
        stage_id = "remote-fallback:dependency:" + name
        remote_log = (
            os.path.join(work_dir, "logs", name + ".log") if work_dir else ""
        )
        emit(
            "stage_start",
            id=stage_id,
            label="%s %s" % (name, version),
            category="Dependency",
            phase="dependencies",
            log_path=remote_log,
        )
        fallback_stage = {"id": stage_id, "name": name}

    write_bytes(raw)


process = subprocess.Popen(
    command,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    env=child_env,
    bufsize=0,
)
stream = process.stdout
buffer = bytearray()
reader = getattr(stream, "read1", stream.read)
while True:
    chunk = reader(65536)
    if not chunk:
        break
    buffer.extend(chunk)
    while True:
        newline = buffer.find(b"\n")
        if newline < 0:
            break
        raw = bytes(buffer[:newline + 1])
        del buffer[:newline + 1]
        consume_line(raw)
if buffer:
    consume_line(bytes(buffer))

return_code = process.wait()
if return_code == 0:
    finish_fallback("completed")
else:
    finish_fallback("failed", "remote build exited with status %d" % return_code)
raise SystemExit(return_code)
'''


def _remote_build_runner(python: str, repo: str, build_args: List[str]) -> str:
    """Stream a remote build and synthesize dependency events for older builders."""
    command = [python, repo + "/build.py"] + list(build_args)
    return shlex.join([python, "-u", "-c", _REMOTE_BUILD_STREAMER] + command)


def _remote_stage(stage_id: str, label: str, category: str, command: str) -> str:
    """Wrap a remote shell command in start/success/failure progress events."""
    start = _remote_event_line(
        "stage_start", id=stage_id, label=label, category=category)
    success = _remote_event_line(
        "stage_finish", id=stage_id, status="completed")
    failure = _remote_event_line(
        "stage_finish", id=stage_id, status="failed", error="remote command failed")
    return (
        start + "; if " + command + "; then " + success +
        "; else rc=$?; " + failure + "; exit \"$rc\"; fi"
    )


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
    build_command = _remote_build_runner(python, repo, build_args)
    verify_command = "PYTHONIOENCODING=utf-8 PYTHONUNBUFFERED=1 JOREK_BUILD_STUDIO_EVENTS=1 " + shlex.join(
        [python, repo + "/build.py"] + list(verify_args))
    suite_source = str(PurePosixPath(str(profile["remote_work"])) / "dist" / "jorek-suite")
    transfer_path = remote_artifact_path(profile)
    source_path = str(PurePosixPath(repo) / "src")
    audit_script = (
        "import deps; item=deps.DEPENDENCIES.get('pastix6'); "
        "source_kind=getattr(item, 'source_kind', 'archive') if item else 'missing'; "
        "git_ref=getattr(item, 'git_ref', '') if item else ''; "
        "submodules=bool(getattr(item, 'git_submodules', False)) if item else False; "
        "print('PaStiX 6.3.2 recipe: source_kind=%s, git_ref=%s, recursive_submodules=%s' "
        "% (source_kind, git_ref, submodules)); "
        "assert item is not None and source_kind == 'git' and git_ref == 'v6.3.2' "
        "and submodules, "
        "'Pinned commit does not contain the recursive PaStiX 6.3.2 source fix. "
        "Update the pinned repository commit to a revision containing that fix.'"
    )
    audit_command = (
        "PYTHONPATH=" + shlex.quote(source_path) + " " +
        shlex.join([python, "-c", audit_script])
    )
    repository_command = (
        "if [ ! -d " + shlex.quote(repo + "/.git") + " ]; then "
        + "git clone --filter=blob:none --no-checkout -- " + qurl + " " + qrepo + "; fi"
        + " && git -C " + qrepo + " remote set-url origin " + qurl
        + " && git -C " + qrepo + " fetch --prune origin " + qbranch
        + " && git -C " + qrepo + " checkout --force --detach " + qcommit
        + " && test \"$(git -C " + qrepo + " rev-parse HEAD)\" = " + qcommit
        + " && printf 'Pinned jorek-suite commit: '"
        + " && git -C " + qrepo + " rev-parse HEAD"
    )
    remote_plan = _remote_event_line(
        "stage_plan", phase="remote",
        stages=[
            {
                "id": "remote:repository",
                "label": "Fetch and pin jorek-suite repository",
                "category": "Remote preparation",
            },
            {
                "id": "remote:source-audit",
                "label": "Verify pinned PaStiX build recipe",
                "category": "Remote preparation",
            },
            {
                "id": "remote:build",
                "label": "Build JOREK suite on remote host",
                "category": "Remote build",
            },
            {
                "id": "remote:verify",
                "label": "Verify remote build artifact",
                "category": "Verification",
            },
            {
                "id": "remote:transfer-stage",
                "label": "Prepare artifact for download",
                "category": "Artifact",
            },
        ])
    commands = [
        "set -eu",
        "mkdir -p " + qroot,
        remote_plan,
        _remote_stage(
            "remote:repository", "Fetch and pin jorek-suite repository",
            "Remote preparation", repository_command),
        _remote_stage(
            "remote:source-audit", "Verify pinned PaStiX build recipe",
            "Remote preparation", audit_command),
        _remote_stage(
            "remote:build", "Build JOREK suite on remote host",
            "Remote build", build_command),
        _remote_stage(
            "remote:verify", "Verify remote build artifact",
            "Verification", verify_command),
        _remote_stage(
            "remote:transfer-stage", "Prepare artifact for download",
            "Artifact", "cp " + shlex.quote(suite_source) + " " +
            shlex.quote(transfer_path) + " && chmod 700 " + shlex.quote(transfer_path)),
    ]
    return "; ".join(commands)


def build_remote_task_command(
    profile: Dict[str, Any],
    build_args: List[str],
    verify_args: List[str],
    task_id: str,
    heartbeat_timeout: Optional[int] = None,
) -> str:
    """Return the short SSH command that starts a detached supervised build."""
    workload = build_remote_command(profile, build_args, verify_args)
    timeout = (
        profile.get("heartbeat_timeout", DEFAULT_HEARTBEAT_TIMEOUT)
        if heartbeat_timeout is None else heartbeat_timeout)
    return remote_task_start_command(
        profile, task_id, workload, heartbeat_timeout=timeout)


def scp_arguments(profile: Dict[str, Any], remote_path: str, local_path: str) -> List[str]:
    errors = validate_connection_profile(profile)
    if errors:
        raise RemoteProfileError("; ".join(errors))
    if any(character.isspace() for character in remote_path):
        raise RemoteProfileError(
            "remote SCP transfer path must not contain whitespace; use remote_artifact_path()")
    # The no-space staging path also works with older legacy-SCP clients.
    remote_spec = "%s:%s" % (
        str(profile["destination"]).strip(), remote_path)
    return _ssh_common(profile, scp=True) + [remote_spec, local_path]


def ssh_executable() -> str:
    return "ssh"


def scp_executable() -> str:
    return "scp"
