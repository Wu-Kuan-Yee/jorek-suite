"""Detached remote build supervision and SSH attach protocol.

The supervisor is embedded in an SSH command so the target needs only Python 3
and a POSIX process model; it does not need a preinstalled Build Studio package.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import PurePosixPath
from typing import Any, Dict

DEFAULT_HEARTBEAT_TIMEOUT = 120
HEARTBEAT_INTERVAL = 15
REMOTE_DATA_PREFIX = "@@JOREK_REMOTE_DATA@@"
REMOTE_TASK_PREFIX = "@@JOREK_REMOTE_TASK@@"
REMOTE_STARTED_PREFIX = "@@JOREK_REMOTE_STARTED@@"


REMOTE_SUPERVISOR_SCRIPT = r'''
import base64
import json
import os
import re
import select
import signal
import subprocess
import sys
import time
import traceback

DATA_PREFIX = "@@JOREK_REMOTE_DATA@@"
TASK_PREFIX = "@@JOREK_REMOTE_TASK@@"
STARTED_PREFIX = "@@JOREK_REMOTE_STARTED@@"
TASK_ID_RE = re.compile(r"^[0-9a-f]{32}$")
TERMINAL = ("completed", "failed", "cancelled")


def task_directory(root, task_id):
    if not TASK_ID_RE.fullmatch(task_id):
        raise ValueError("invalid remote task ID")
    return os.path.join(os.path.abspath(root), task_id)


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def atomic_json(path, payload):
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    temporary = path + ".tmp.%d" % os.getpid()
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def touch(path):
    flags = os.O_WRONLY | os.O_CREAT
    fd = os.open(path, flags, 0o600)
    try:
        os.utime(path, None)
    finally:
        os.close(fd)


def update_state(directory, **changes):
    path = os.path.join(directory, "state.json")
    state = read_json(path, {}) or {}
    state.update(changes)
    atomic_json(path, state)
    return state


def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def drain_output(stream, log_handle, timeout=0.0):
    if stream is None:
        return False
    try:
        ready, _, _ = select.select([stream.fileno()], [], [], timeout)
    except (OSError, ValueError):
        return False
    if not ready:
        return False
    try:
        chunk = os.read(stream.fileno(), 65536)
    except OSError:
        return False
    if not chunk:
        return None
    log_handle.write(chunk)
    return True


def terminate_group(pgid, child, stream, log_handle):
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    deadline = time.monotonic() + 5.0
    stream_eof = False
    while time.monotonic() < deadline:
        if stream is not None and not stream_eof:
            result = drain_output(stream, log_handle, 0.15)
            if result is None:
                stream_eof = True
        else:
            time.sleep(0.15)
        if not group_alive(pgid):
            break
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            child.kill()
        except OSError:
            pass
        child.wait()


def run_daemon(root, task_id):
    directory = task_directory(root, task_id)
    request = read_json(os.path.join(directory, "request.json"))
    if not isinstance(request, dict):
        raise RuntimeError("remote task request is missing or invalid")
    timeout = max(10, int(request.get("heartbeat_timeout", 120)))
    command = str(request.get("command", ""))
    cwd = str(request.get("cwd", ""))
    if not command or not os.path.isdir(cwd):
        raise RuntimeError("remote task command or working directory is invalid")

    child = None
    stream = None
    log_handle = None
    pgid = None
    reason = ""
    exit_code = 1
    started = time.time()
    try:
        update_state(
            directory, task_id=task_id, status="starting",
            created_at=request.get("created_at", started),
            started_at=started, heartbeat_timeout=timeout,
            supervisor_pid=os.getpid(), reason="",
        )
        if os.path.exists(os.path.join(directory, "cancel")):
            update_state(
                directory, status="cancelled", exit_code=-signal.SIGTERM,
                finished_at=time.time(), reason="cancel_requested_before_start",
            )
            return 0

        child = subprocess.Popen(
            ["/bin/sh", "-c", command],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            close_fds=True,
            start_new_session=True,
        )
        stream = child.stdout
        pgid = child.pid
        log_handle = open(os.path.join(directory, "output.log"), "ab", buffering=0)
        update_state(
            directory, status="running", pid=child.pid, pgid=pgid,
            started_at=time.time(),
        )

        stream_eof = False
        leftovers_checked = False
        last_state_write = 0.0
        while True:
            now = time.time()
            heartbeat_path = os.path.join(directory, "heartbeat")
            try:
                heartbeat_age = max(0.0, now - os.stat(heartbeat_path).st_mtime)
            except OSError:
                heartbeat_age = now - started

            if not reason and os.path.exists(os.path.join(directory, "cancel")):
                reason = "user_cancel"
                update_state(directory, status="stopping", reason=reason)
                terminate_group(pgid, child, stream, log_handle)
            elif not reason and heartbeat_age > timeout:
                reason = "heartbeat_timeout"
                update_state(
                    directory, status="disconnected", reason=reason,
                    heartbeat_age_seconds=heartbeat_age,
                )
                terminate_group(pgid, child, stream, log_handle)

            if not stream_eof:
                result = drain_output(stream, log_handle, 0.5)
                if result is None:
                    stream_eof = True
            else:
                time.sleep(0.1)

            child_code = child.poll()
            if child_code is not None and not leftovers_checked:
                leftovers_checked = True
                if group_alive(pgid):
                    # The shell exited but a descendant still holds the pipe or
                    # remains in the task process group. Do not leave it orphaned.
                    terminate_group(pgid, child, stream, log_handle)

            if now - last_state_write >= 5.0 and not reason:
                update_state(
                    directory, heartbeat_age_seconds=heartbeat_age,
                    last_heartbeat=now - heartbeat_age,
                )
                last_state_write = now

            if child_code is not None and stream_eof:
                exit_code = int(child_code)
                break

        if reason:
            status = "cancelled"
        else:
            status = "completed" if exit_code == 0 else "failed"
        update_state(
            directory, status=status, exit_code=exit_code,
            finished_at=time.time(), reason=reason,
            pid=child.pid, pgid=pgid,
        )
        return 0
    except BaseException as exc:
        if child is not None and pgid is not None:
            try:
                terminate_group(pgid, child, stream, log_handle)
            except BaseException:
                pass
        update_state(
            directory, status="failed", exit_code=1,
            finished_at=time.time(), reason="supervisor_error: %s" % exc,
            traceback=traceback.format_exc()[-12000:],
        )
        return 1
    finally:
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
        if log_handle is not None:
            log_handle.close()


def start_task(root, task_id, timeout, payload):
    os.umask(0o077)
    root = os.path.abspath(root)
    os.makedirs(root, mode=0o700, exist_ok=True)
    directory = task_directory(root, task_id)
    os.mkdir(directory, 0o700)
    request = {
        "task_id": task_id,
        "heartbeat_timeout": max(10, int(timeout)),
        "command": str(payload.get("command", "")),
        "cwd": str(payload.get("cwd", "")),
        "created_at": time.time(),
    }
    atomic_json(os.path.join(directory, "request.json"), request)
    atomic_json(os.path.join(directory, "state.json"), {
        "task_id": task_id, "status": "starting",
        "created_at": request["created_at"],
        "heartbeat_timeout": request["heartbeat_timeout"],
        "exit_code": None, "reason": "",
    })
    open(os.path.join(directory, "output.log"), "ab").close()
    touch(os.path.join(directory, "heartbeat"))
    supervisor_log = os.path.join(directory, "supervisor.log")
    pid = os.fork()
    if pid:
        print(STARTED_PREFIX + json.dumps({
            "task_id": task_id, "status": "starting",
            "supervisor_pid": pid,
            "heartbeat_timeout": request["heartbeat_timeout"],
        }, separators=(",", ":")), flush=True)
        return

    # The child is detached from the SSH session before doing any long-running
    # work. Ignore SIGHUP as an additional guard for PAM/sshd session teardown.
    try:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        os.setsid()
        null_fd = os.open(os.devnull, os.O_RDONLY)
        os.dup2(null_fd, 0)
        if null_fd > 2:
            os.close(null_fd)
        log_fd = os.open(supervisor_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
        if log_fd > 2:
            os.close(log_fd)
        result = run_daemon(root, task_id)
        os._exit(int(result))
    except BaseException:
        os._exit(1)


def attach_task(root, task_id, offset):
    directory = task_directory(root, task_id)
    state_path = os.path.join(directory, "state.json")
    output_path = os.path.join(directory, "output.log")
    heartbeat_path = os.path.join(directory, "heartbeat")
    cancel_path = os.path.join(directory, "cancel")
    if not os.path.isfile(state_path):
        print(TASK_PREFIX + json.dumps({
            "task_id": task_id, "status": "missing", "exit_code": 1,
            "reason": "remote task was not found",
        }, separators=(",", ":")), flush=True)
        return 0

    offset = max(0, int(offset))
    touch(heartbeat_path)
    input_fd = sys.stdin.fileno()
    input_open = True
    while True:
        try:
            if os.path.isfile(output_path):
                size = os.path.getsize(output_path)
                if offset > size:
                    offset = size
                if size > offset:
                    with open(output_path, "rb") as handle:
                        handle.seek(offset)
                        data = handle.read(min(65536, size - offset))
                    if data:
                        encoded = base64.b64encode(data).decode("ascii")
                        print(DATA_PREFIX + "%d:%s" % (offset, encoded), flush=True)
                        offset += len(data)
        except OSError:
            pass

        state = read_json(state_path, {}) or {}
        if state.get("status") in TERMINAL:
            try:
                final_size = os.path.getsize(output_path)
            except OSError:
                final_size = 0
            if offset >= final_size:
                print(TASK_PREFIX + json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                      flush=True)
                return 0

        if input_open:
            try:
                readable, _, _ = select.select([input_fd], [], [], 0.25)
            except (OSError, ValueError):
                readable = []
            if readable:
                try:
                    incoming = os.read(input_fd, 4096)
                except OSError:
                    incoming = b""
                if not incoming:
                    return 0
                if b"CANCEL" in incoming.upper():
                    open(cancel_path, "ab").close()
                touch(heartbeat_path)
        else:
            time.sleep(0.25)


def cancel_task(root, task_id):
    directory = task_directory(root, task_id)
    if not os.path.isdir(directory):
        print(json.dumps({"task_id": task_id, "status": "missing"}), flush=True)
        return 1
    open(os.path.join(directory, "cancel"), "ab").close()
    print(json.dumps({"task_id": task_id, "cancel_requested": True}), flush=True)
    return 0


def status_task(root, task_id):
    directory = task_directory(root, task_id)
    state = read_json(os.path.join(directory, "state.json"))
    if state is None:
        state = {"task_id": task_id, "status": "missing"}
    print(json.dumps(state, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0


def main():
    if len(sys.argv) < 4:
        raise SystemExit("usage: supervisor.py COMMAND STATE_ROOT TASK_ID ...")
    command, root, task_id = sys.argv[1:4]
    if command == "start":
        if len(sys.argv) != 6:
            raise SystemExit("start requires HEARTBEAT_TIMEOUT and JSON payload")
        start_task(root, task_id, int(sys.argv[4]), json.loads(sys.argv[5]))
        return 0
    if command == "daemon":
        return run_daemon(root, task_id)
    if command == "attach":
        if len(sys.argv) != 5:
            raise SystemExit("attach requires LOG_OFFSET")
        return attach_task(root, task_id, int(sys.argv[4]))
    if command == "cancel":
        return cancel_task(root, task_id)
    if command == "status":
        return status_task(root, task_id)
    raise SystemExit("unknown supervisor command: " + command)


if __name__ == "__main__":
    raise SystemExit(main())
'''


def validate_remote_task_id(task_id: str) -> str:
    value = str(task_id or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("remote task ID must be a 32-character hexadecimal UUID")
    return value


def remote_task_state_root(profile: Dict[str, Any]) -> str:
    root = str(profile.get("remote_root", "")).strip()
    if not root.startswith("/"):
        raise ValueError("remote_root must be an absolute POSIX path")
    return str(PurePosixPath(root) / ".jorek-build-studio" / "tasks")


def _supervisor_argv(profile: Dict[str, Any], action: str, task_id: str, *extra: str):
    python = str(profile.get("python", "python3")).strip() or "python3"
    return [
        python, "-u", "-c", REMOTE_SUPERVISOR_SCRIPT,
        action, remote_task_state_root(profile), validate_remote_task_id(task_id),
    ] + [str(item) for item in extra]


def remote_task_start_command(
    profile: Dict[str, Any], task_id: str, workload_command: str,
    heartbeat_timeout: int = DEFAULT_HEARTBEAT_TIMEOUT,
) -> str:
    timeout = int(heartbeat_timeout)
    if timeout < 10:
        raise ValueError("heartbeat timeout must be at least 10 seconds")
    payload = json.dumps({
        "command": str(workload_command),
        # The remote_root may not exist until the workload creates it.
        "cwd": "/",
    }, ensure_ascii=False, separators=(",", ":"))
    return shlex.join(_supervisor_argv(
        profile, "start", task_id, str(timeout), payload))


def remote_task_attach_command(
    profile: Dict[str, Any], task_id: str, offset: int = 0,
) -> str:
    if int(offset) < 0:
        raise ValueError("remote log offset cannot be negative")
    return shlex.join(_supervisor_argv(profile, "attach", task_id, str(int(offset))))


def remote_task_cancel_command(profile: Dict[str, Any], task_id: str) -> str:
    return shlex.join(_supervisor_argv(profile, "cancel", task_id))


def remote_task_status_command(profile: Dict[str, Any], task_id: str) -> str:
    return shlex.join(_supervisor_argv(profile, "status", task_id))
