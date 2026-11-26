from __future__ import annotations

import os
import shlex
import json
import subprocess
import sys
import tempfile
import textwrap
import time
import threading
import base64
import unittest
from pathlib import Path

from jorek_build_studio.remote import (
    RemoteProfileError,
    build_remote_command,
    build_remote_task_command,
    connection_test_command,
    parse_remote_overrides,
    remote_artifact_path,
    scp_arguments,
    ssh_arguments,
    validate_connection_profile,
    validate_profile,
    _REMOTE_BUILD_STREAMER,
)
from jorek_build_studio.remote_supervisor import (
    DEFAULT_HEARTBEAT_TIMEOUT,
    REMOTE_SUPERVISOR_SCRIPT,
    remote_task_attach_command,
    remote_task_cancel_command,
    remote_task_start_command,
    remote_task_state_root,
    validate_remote_task_id,
)


PROFILE = {
    "name": "Linux x86_64",
    "destination": "builder@example.invalid",
    "port": 2222,
    "identity_file": "/home/user/.ssh/build key",
    "remote_root": "/srv/build studio",
    "remote_work": "/scratch/jorek work",
    "artifact_dir": "/tmp/JOREK artifacts",
    "repo_url": "https://github.com/Wu-Kuan-Yee/jorek-suite.git",
    "branch": "feature/build-studio-gui",
    "commit": "0123456789abcdef0123456789abcdef01234567",
    "python": "python3",
    "host_key_policy": "yes",
    "ssh_options": ["Compression=yes"],
}


@unittest.skipIf(os.name == "nt", "remote supervisor requires POSIX process groups")
class RemoteSupervisorTests(unittest.TestCase):
    def _start(self, root: Path, task_id: str, command: str, timeout: int = 10):
        state_root = root / "tasks"
        payload = json.dumps({"command": command, "cwd": str(root)})
        result = subprocess.run(
            [sys.executable, "-u", "-c", REMOTE_SUPERVISOR_SCRIPT,
             "start", str(state_root), task_id, str(timeout), payload],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, check=False, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("@@JOREK_REMOTE_STARTED@@", result.stdout)
        return state_root

    def _wait_for_terminal(self, state_path: Path, timeout: float = 18.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                state = {}
            if state.get("status") in ("completed", "failed", "cancelled"):
                return state
            time.sleep(0.2)
        self.fail("remote supervisor did not reach a terminal state")

    def test_detached_supervisor_kills_process_group_after_heartbeat_timeout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_id = "a" * 32
            command = shlex.join([
                sys.executable, "-u", "-c",
                "import time; print('long-task-started', flush=True); time.sleep(60)",
            ])
            state_root = self._start(root, task_id, command, timeout=10)
            state_path = state_root / task_id / "state.json"
            state = self._wait_for_terminal(state_path, timeout=18)
            self.assertEqual(state.get("status"), "cancelled", state)
            self.assertEqual(state.get("reason"), "heartbeat_timeout", state)
            pgid = int(state.get("pgid") or 0)
            self.assertGreater(pgid, 0)
            with self.assertRaises(ProcessLookupError):
                os.killpg(pgid, 0)

    def test_attach_can_cancel_and_stream_the_remote_log(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_id = "b" * 32
            command = shlex.join([
                sys.executable, "-u", "-c",
                "import time; print('remote-task-ready', flush=True); time.sleep(60)",
            ])
            state_root = self._start(root, task_id, command, timeout=10)
            state_path = state_root / task_id / "state.json"
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                if state.get("status") == "running":
                    break
                time.sleep(0.1)
            else:
                self.fail("remote task did not start")

            attach = subprocess.Popen(
                [sys.executable, "-u", "-c", REMOTE_SUPERVISOR_SCRIPT,
                 "attach", str(state_root), task_id, "0"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self.assertIsNotNone(attach.stdin)
            self.assertIsNotNone(attach.stdout)
            for _ in range(5):
                attach.stdin.write(b"\n")
                attach.stdin.flush()
                time.sleep(0.1)
            attach.stdin.write(b"CANCEL\n")
            attach.stdin.flush()
            state = self._wait_for_terminal(state_path, timeout=8)
            self.assertEqual(state.get("status"), "cancelled", state)
            # The attach loop polls the task state and exits once the final
            # log bytes and terminal marker have been sent.
            output = attach.stdout.read()
            attach.wait(timeout=5)
            attach.stdin.close()
            self.assertIn(b"@@JOREK_REMOTE_TASK@@", output)
            frames = [
                line for line in output.splitlines()
                if line.startswith(b"@@JOREK_REMOTE_DATA@@")
            ]
            decoded = b""
            for frame in frames:
                _offset, encoded = frame.split(b":", 1)
                decoded += __import__("base64").b64decode(encoded)
            self.assertIn(b"remote-task-ready", decoded)

    def test_supervisor_script_is_valid_python(self):
        compile(REMOTE_SUPERVISOR_SCRIPT, "<remote-supervisor>", "exec")


class RemoteBuildStreamerTests(unittest.TestCase):
    def test_legacy_dependency_failure_emits_failed_stage_finish(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            build_script = root / "build.py"
            build_script.write_text(
                "print('>> building fixture_dep 1.2.3', flush=True)\n"
                "print('CMake configuration failed', flush=True)\n"
                "raise SystemExit(7)\n",
                encoding="utf-8",
            )
            command = [
                sys.executable, "-u", "-c", _REMOTE_BUILD_STREAMER,
                sys.executable, str(build_script), "--work", str(root / "work"), "dist",
            ]
            result = subprocess.run(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, check=False, timeout=15)
            self.assertEqual(result.returncode, 7, result.stdout)
            self.assertIn('"event":"stage_finish"', result.stdout)
            self.assertIn('"status":"failed"', result.stdout)
            self.assertIn("remote build exited with status 7", result.stdout)

    def test_native_dependency_events_are_not_duplicated_by_legacy_parser(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            (root / "src" / "deps.py").write_text(
                'emit_build_event("stage_start", id="dependency:" + name)\n',
                encoding="utf-8",
            )
            build_script = root / "build.py"
            build_script.write_text(
                '# common.emit_build_event("stage_start", id="variant:" + name)\n'
                "import json\n"
                "prefix = '@@JOREK_BUILD_EVENT@@'\n"
                "start = {'event': 'stage_start', 'id': 'dependency:fixture_dep'}\n"
                "finish = {'event': 'stage_finish', 'id': 'dependency:fixture_dep', 'status': 'completed'}\n"
                "variant_start = {'event': 'stage_start', 'id': 'variant:fixture_variant'}\n"
                "variant_finish = {'event': 'stage_finish', 'id': 'variant:fixture_variant', 'status': 'completed'}\n"
                "print(prefix + json.dumps(start), flush=True)\n"
                "print('>> building fixture_dep 1.2.3', flush=True)\n"
                "print(prefix + json.dumps(finish), flush=True)\n"
                "print(prefix + json.dumps(variant_start), flush=True)\n"
                "print('>> building fixture_variant (model 600, solver mumps, programs main)', flush=True)\n"
                "print(prefix + json.dumps(variant_finish), flush=True)\n",
                encoding="utf-8",
            )
            command = [
                sys.executable, "-u", "-c", _REMOTE_BUILD_STREAMER,
                sys.executable, str(build_script), "--work", str(root / "work"), "dist",
            ]
            result = subprocess.run(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, check=False, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("dependency:fixture_dep", result.stdout)
            self.assertNotIn("remote-fallback:dependency:fixture_dep", result.stdout)
            self.assertIn("variant:fixture_variant", result.stdout)
            self.assertNotIn("remote-fallback:variant:fixture_variant", result.stdout)


class RemoteCommandTests(unittest.TestCase):
    def test_profile_requires_a_pinned_full_commit_and_absolute_paths(self):
        profile = dict(PROFILE)
        profile["commit"] = "main"
        self.assertTrue(any("full 40- or 64-character" in item for item in validate_profile(profile)))
        profile["commit"] = PROFILE["commit"]
        profile["remote_work"] = "relative/work"
        self.assertTrue(any("remote_work" in item for item in validate_profile(profile)))

    def test_profile_requires_local_artifact_directory_and_valid_host_key_policy(self):
        profile = dict(PROFILE)
        profile["artifact_dir"] = ""
        self.assertTrue(any("local artifact directory" in item for item in validate_profile(profile)))
        profile["artifact_dir"] = PROFILE["artifact_dir"]
        profile["host_key_policy"] = "accept-any"
        self.assertTrue(any("host key policy" in item for item in validate_profile(profile)))

    def test_connection_test_does_not_require_build_metadata(self):
        profile = {"destination": "builder@example.invalid", "port": 22}
        self.assertEqual(validate_connection_profile(profile), [])
        args = ssh_arguments(profile, "uname -s")
        self.assertEqual(args[-2:], ["builder@example.invalid", "uname -s"])

    def test_ssh_arguments_use_batch_mode_and_preserve_identity_path(self):
        args = ssh_arguments(PROFILE, "uname -s")
        self.assertIn("BatchMode=yes", args)
        self.assertIn("2222", args)
        self.assertIn("/home/user/.ssh/build key", args)
        profile = dict(PROFILE)
        profile["ssh_options"] = ["Compression=yes", "ProxyJump=jump@example.invalid"]
        jump_args = ssh_arguments(profile, "uname -s")
        self.assertIn("ProxyJump=jump@example.invalid", jump_args)
        self.assertEqual(args[-2:], ["builder@example.invalid", "uname -s"])

    def test_supervised_remote_command_uses_task_id_and_120_second_lease(self):
        task_id = "c" * 32
        command = build_remote_task_command(
            PROFILE, ["--work", PROFILE["remote_work"], "dist"],
            ["--work", PROFILE["remote_work"], "verify", "--json"], task_id)
        self.assertIn(task_id, command)
        self.assertIn(str(DEFAULT_HEARTBEAT_TIMEOUT), command)
        self.assertIn(".jorek-build-studio/tasks", remote_task_state_root(PROFILE))
        self.assertIn("@@JOREK_REMOTE_STARTED@@", command)
        self.assertIn("attach", remote_task_attach_command(PROFILE, task_id, 123))
        self.assertIn("cancel", remote_task_cancel_command(PROFILE, task_id))
        extended = build_remote_task_command(
            dict(PROFILE, heartbeat_timeout=240), ["dist"], ["verify"], task_id)
        self.assertIn("240", extended)
        with self.assertRaises(ValueError):
            build_remote_task_command(
                PROFILE, ["dist"], ["verify"], task_id, heartbeat_timeout=5)
        invalid_profile = dict(PROFILE, heartbeat_timeout=5)
        self.assertTrue(any(
            "heartbeat_timeout" in item for item in validate_profile(invalid_profile)))
        with self.assertRaises(ValueError):
            validate_remote_task_id("../other-task")

    def test_remote_build_command_quotes_paths_and_pins_commit(self):
        command = build_remote_command(
            PROFILE,
            ["--work", "/scratch/jorek work", "dist", "--model", "502"],
            ["--work", "/scratch/jorek work", "verify", "--json"],
        )
        self.assertIn("git clone", command)
        self.assertIn("@@JOREK_BUILD_EVENT@@", command)
        self.assertIn("remote:repository", command)
        self.assertIn("remote:build", command)
        self.assertIn("remote:verify", command)
        self.assertIn('child_env["JOREK_BUILD_STUDIO_EVENTS"] = "1"', command)
        self.assertIn("0123456789abcdef0123456789abcdef01234567", command)
        self.assertIn("'/srv/build studio/jorek-suite/build.py'", command)
        self.assertIn("'/scratch/jorek work'", command)
        self.assertIn("verify --json", command)
        self.assertIn("rev-parse HEAD", command)

    def test_scp_uses_a_space_free_remote_staging_path(self):
        transfer = remote_artifact_path(PROFILE)
        self.assertFalse(any(character.isspace() for character in transfer))
        args = scp_arguments(PROFILE, transfer, "/tmp/JOREK artifacts/suite")
        self.assertIn("-P", args)
        self.assertIn("builder@example.invalid:" + transfer, args)
        self.assertEqual(args[-1], "/tmp/JOREK artifacts/suite")
        with self.assertRaises(RemoteProfileError):
            scp_arguments(PROFILE, "/tmp/remote path with spaces", "/tmp/local")

    def test_connection_test_reports_native_host_information(self):
        command = connection_test_command(PROFILE)
        self.assertIn("uname -m", command)
        self.assertIn("python3 --version", command)
        self.assertIn("git --version", command)

    def test_invalid_host_key_policy_is_rejected(self):
        profile = dict(PROFILE)
        profile["host_key_policy"] = "accept-any"
        with self.assertRaises(RemoteProfileError):
            ssh_arguments(profile, "uname -s")

    def test_default_artifact_path_is_a_safe_remote_staging_path(self):
        path = remote_artifact_path(PROFILE)
        self.assertTrue(path.startswith("/tmp/jorek-suite-transfer-"))
        self.assertEqual(len(path.rsplit("-", 1)[-1]), 16)
        self.assertFalse(any(character.isspace() for character in path))
        self.assertEqual(path, remote_artifact_path(dict(PROFILE)))
        changed = dict(PROFILE, commit="b" * 40)
        self.assertNotEqual(path, remote_artifact_path(changed))

    @unittest.skipIf(os.name == "nt", "remote command uses a POSIX shell")
    def test_remote_build_sequence_clones_pinned_commit_builds_and_verifies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source repo"
            source.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(source)],
                           check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(["git", "-C", str(source), "config", "user.name", "Build Test"],
                           check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.email", "build@example.invalid"],
                           check=True)
            script = textwrap.dedent("""\
                import argparse
                import json
                from pathlib import Path
                parser = argparse.ArgumentParser()
                parser.add_argument("--work", required=True)
                parser.add_argument("action")
                parser.add_argument("--json", action="store_true")
                args = parser.parse_args()
                suite = Path(args.work) / "dist" / "jorek-suite"
                if args.action == "dist":
                    print(">> building fixture_dep 1.2.3", flush=True)
                    print("fixture dependency compiler output", flush=True)
                    print(">> building fixture_variant (model 600, solver pastix6, programs main)", flush=True)
                    print("fixture JOREK variant compiler output", flush=True)
                    suite.parent.mkdir(parents=True, exist_ok=True)
                    suite.write_text("native-suite", encoding="utf-8")
                elif args.action == "verify":
                    if not suite.is_file():
                        raise SystemExit(2)
                    print(json.dumps({"portable_on_build_host": True}))
                """)
            (source / "build.py").write_text(script, encoding="utf-8")
            (source / "src").mkdir(parents=True, exist_ok=True)
            (source / "src" / "deps.py").write_text(
                "from types import SimpleNamespace\n"
                "DEPENDENCIES = {'pastix6': SimpleNamespace("
                "source_kind='git', git_ref='v6.3.2', git_submodules=True)}\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", str(source), "add", "build.py", "src/deps.py"],
                check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-m", "fixture"],
                           check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            commit = subprocess.run(
                ["git", "-C", str(source), "rev-parse", "HEAD"],
                check=True, stdout=subprocess.PIPE, text=True).stdout.strip()
            profile = dict(PROFILE)
            profile.update({
                "remote_root": str(root / "remote root"),
                "remote_work": str(root / "remote work"),
                "repo_url": str(source),
                "branch": "main",
                "commit": commit,
            })
            command = build_remote_command(
                profile,
                ["--work", profile["remote_work"], "dist"],
                ["--work", profile["remote_work"], "verify", "--json"],
            )
            result = subprocess.run(
                ["/bin/sh", "-c", command], stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, check=False, timeout=45)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("@@JOREK_BUILD_EVENT@@", result.stdout)
            self.assertIn('"id":"remote:repository"', result.stdout)
            self.assertIn('"id":"remote:source-audit"', result.stdout)
            self.assertIn('"id":"remote:build"', result.stdout)
            self.assertIn("Pinned jorek-suite commit: " + commit, result.stdout)
            self.assertIn(
                "PaStiX 6.3.2 recipe: source_kind=git, git_ref=v6.3.2, "
                "recursive_submodules=True", result.stdout)
            self.assertIn('"id":"remote:verify"', result.stdout)
            event_prefix = "@@JOREK_BUILD_EVENT@@"
            events = [
                json.loads(line[len(event_prefix):])
                for line in result.stdout.splitlines()
                if line.startswith(event_prefix)
            ]
            fallback_events = [
                event for event in events
                if event.get("id") == "remote-fallback:dependency:fixture_dep"
            ]
            self.assertEqual(
                [event.get("event") for event in fallback_events],
                ["stage_start", "stage_finish"],
                result.stdout,
            )
            self.assertEqual(
                fallback_events[0].get("log_path"),
                str(Path(profile["remote_work"]) / "logs" / "fixture_dep.log"),
            )
            self.assertIn("fixture dependency compiler output", result.stdout)
            variant_events = [
                event for event in events
                if event.get("id") == "remote-fallback:variant:fixture_variant"
            ]
            self.assertEqual(
                [event.get("event") for event in variant_events],
                ["stage_start", "stage_finish"],
                result.stdout,
            )
            self.assertEqual(
                variant_events[0].get("log_path"),
                str(Path(profile["remote_work"]) / "logs" / "fixture_variant-make.log"),
            )
            self.assertIn("fixture JOREK variant compiler output", result.stdout)
            artifact = Path(profile["remote_work"]) / "dist" / "jorek-suite"
            self.assertEqual(artifact.read_text(encoding="utf-8"), "native-suite")
            transfer = Path(remote_artifact_path(profile))
            self.assertEqual(transfer.read_text(encoding="utf-8"), "native-suite")
            transfer.unlink(missing_ok=True)
            checkout = Path(profile["remote_root"]) / "jorek-suite"
            actual = subprocess.run(
                ["git", "-C", str(checkout), "rev-parse", "HEAD"],
                check=True, stdout=subprocess.PIPE, text=True).stdout.strip()
            self.assertEqual(actual, commit)
            self.assertIn('"portable_on_build_host": true', result.stdout)

    @unittest.skipUnless(
        os.environ.get("JOREK_SSH_TEST_KEY"),
        "local OpenSSH integration endpoint is not configured",
    )
    def test_native_remote_build_and_artifact_download_over_ssh_and_scp(self):
        with tempfile.TemporaryDirectory(prefix="jorek-ssh-integration-") as temporary:
            root = Path(temporary)
            root.chmod(0o777)  # The isolated remote SSH account must write below this fixture directory.
            source = root / "source repo"
            source.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(source)],
                           check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(["git", "-C", str(source), "config", "user.name", "Build Test"],
                           check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.email", "build@example.invalid"],
                           check=True)
            script = textwrap.dedent("""\
                import argparse
                import json
                from pathlib import Path
                parser = argparse.ArgumentParser()
                parser.add_argument("--work", required=True)
                parser.add_argument("action")
                parser.add_argument("--json", action="store_true")
                args = parser.parse_args()
                suite = Path(args.work) / "dist" / "jorek-suite"
                if args.action == "dist":
                    suite.parent.mkdir(parents=True, exist_ok=True)
                    suite.write_text("ssh-native-suite", encoding="utf-8")
                elif args.action == "verify":
                    if not suite.is_file():
                        raise SystemExit(2)
                    print(json.dumps({"portable_on_build_host": True}))
                """)
            (source / "build.py").write_text(script, encoding="utf-8")
            (source / "src").mkdir(parents=True, exist_ok=True)
            (source / "src" / "deps.py").write_text(
                "from types import SimpleNamespace\n"
                "DEPENDENCIES = {'pastix6': SimpleNamespace("
                "source_kind='git', git_ref='v6.3.2', git_submodules=True)}\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", str(source), "add", "build.py", "src/deps.py"],
                check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-m", "fixture"],
                           check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            commit = subprocess.run(
                ["git", "-C", str(source), "rev-parse", "HEAD"],
                check=True, stdout=subprocess.PIPE, text=True).stdout.strip()

            profile = dict(PROFILE)
            profile.update({
                "name": "Local SSH integration",
                "destination": os.environ.get("JOREK_SSH_TEST_HOST", "runner@127.0.0.1"),
                "port": int(os.environ.get("JOREK_SSH_TEST_PORT", "2222")),
                "identity_file": os.environ["JOREK_SSH_TEST_KEY"],
                "remote_root": str(root / "remote root"),
                "remote_work": str(root / "remote work"),
                "artifact_dir": str(root / "downloaded artifacts"),
                "repo_url": str(source),
                "branch": "main",
                "commit": commit,
                "host_key_policy": "yes",
                "ssh_options": ["IdentitiesOnly=yes"],
            })
            task_id = "d" * 32
            remote_command = build_remote_command(
                profile,
                ["--work", profile["remote_work"], "dist"],
                ["--work", profile["remote_work"], "verify", "--json"],
            )
            # Make the disposable remote fixture removable by the local test user.
            remote_command += "; chmod -R a+rwX " + shlex.quote(profile["remote_root"])
            remote_command += " " + shlex.quote(profile["remote_work"])
            start_command = remote_task_start_command(profile, task_id, remote_command)
            start = subprocess.run(
                ["ssh"] + ssh_arguments(profile, start_command),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, check=False, timeout=15)
            self.assertEqual(start.returncode, 0, start.stdout)
            self.assertIn("@@JOREK_REMOTE_STARTED@@", start.stdout)

            attach = subprocess.Popen(
                ["ssh"] + ssh_arguments(
                    profile, remote_task_attach_command(profile, task_id, 0)),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            stop_heartbeats = threading.Event()

            def send_heartbeats():
                while not stop_heartbeats.is_set():
                    try:
                        attach.stdin.write(b"\n")
                        attach.stdin.flush()
                    except (BrokenPipeError, OSError, AttributeError):
                        return
                    stop_heartbeats.wait(0.5)

            heartbeat_thread = threading.Thread(target=send_heartbeats, daemon=True)
            heartbeat_thread.start()
            attach_output = attach.stdout.read()
            stop_heartbeats.set()
            heartbeat_thread.join(timeout=2)
            attach.wait(timeout=15)
            attach.stdin.close()
            self.assertEqual(attach.returncode, 0, attach_output.decode("utf-8", "replace"))
            self.assertIn(b"@@JOREK_REMOTE_TASK@@", attach_output)
            remote_log = b""
            task_state = {}
            for line in attach_output.splitlines():
                if line.startswith(b"@@JOREK_REMOTE_DATA@@"):
                    _offset, encoded = line.split(b":", 1)
                    remote_log += base64.b64decode(encoded)
                elif line.startswith(b"@@JOREK_REMOTE_TASK@@"):
                    task_state = json.loads(line[len(b"@@JOREK_REMOTE_TASK@@"):])
            self.assertEqual(task_state.get("status"), "completed", task_state)
            self.assertIn(b'"portable_on_build_host": true', remote_log)

            local_dir = Path(profile["artifact_dir"])
            local_dir.mkdir(parents=True, exist_ok=True)
            local_artifact = local_dir / "jorek-suite"
            transfer_path = remote_artifact_path(profile)
            scp = subprocess.run(
                ["scp"] + scp_arguments(
                    profile, transfer_path, str(local_artifact)),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, check=False, timeout=30)
            self.assertEqual(scp.returncode, 0, scp.stdout)
            self.assertEqual(local_artifact.read_text(encoding="utf-8"), "ssh-native-suite")
            cleanup = subprocess.run(
                ["ssh"] + ssh_arguments(
                    profile, "rm -f " + shlex.quote(transfer_path) +
                    " && rm -rf " + shlex.quote(profile["remote_root"]) +
                    " " + shlex.quote(profile["remote_work"])),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, check=False, timeout=15)
            self.assertEqual(cleanup.returncode, 0, cleanup.stdout)

    def test_remote_overrides_are_typed_and_restricted(self):
        overrides = parse_remote_overrides(
            '{"toolchain_prefix": "/opt/gcc", "dep_prefixes": ["/opt/fftw"], "jobs": 8}')
        self.assertEqual(overrides["toolchain_prefix"], "/opt/gcc")
        self.assertEqual(overrides["dep_prefixes"], ["/opt/fftw"])
        self.assertEqual(overrides["jobs"], 8)
        with self.assertRaises(RemoteProfileError):
            parse_remote_overrides('{"build_script": "/tmp/build.py"}')
        with self.assertRaises(RemoteProfileError):
            parse_remote_overrides('{"jobs": "eight"}')


if __name__ == "__main__":
    unittest.main()
