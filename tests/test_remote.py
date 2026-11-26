from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from jorek_build_studio.remote import (
    RemoteProfileError,
    build_remote_command,
    connection_test_command,
    parse_remote_overrides,
    remote_artifact_path,
    scp_arguments,
    ssh_arguments,
    validate_connection_profile,
    validate_profile,
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

    def test_remote_build_command_quotes_paths_and_pins_commit(self):
        command = build_remote_command(
            PROFILE,
            ["--work", "/scratch/jorek work", "dist", "--model", "502"],
            ["--work", "/scratch/jorek work", "verify", "--json"],
        )
        self.assertIn("git clone", command)
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

    def test_default_artifact_path_is_inside_remote_work_directory(self):
        self.assertEqual(
            remote_artifact_path(PROFILE),
            "/scratch/jorek work/dist/jorek-suite",
        )

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
                    suite.parent.mkdir(parents=True, exist_ok=True)
                    suite.write_text("native-suite", encoding="utf-8")
                elif args.action == "verify":
                    if not suite.is_file():
                        raise SystemExit(2)
                    print(json.dumps({"portable_on_build_host": True}))
                """)
            (source / "build.py").write_text(script, encoding="utf-8")
            subprocess.run(["git", "-C", str(source), "add", "build.py"], check=True)
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
            subprocess.run(["git", "-C", str(source), "add", "build.py"], check=True)
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
            remote_command = build_remote_command(
                profile,
                ["--work", profile["remote_work"], "dist"],
                ["--work", profile["remote_work"], "verify", "--json"],
            )
            # Make the disposable remote fixture removable by the local test user.
            remote_command += "; chmod -R a+rwX " + shlex.quote(profile["remote_root"])
            remote_command += " " + shlex.quote(profile["remote_work"])
            ssh = subprocess.run(
                ["ssh"] + ssh_arguments(profile, remote_command),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, check=False, timeout=60)
            self.assertEqual(ssh.returncode, 0, ssh.stdout)

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
                ["ssh"] + ssh_arguments(profile, "rm -f " + shlex.quote(transfer_path)),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, check=False, timeout=15)
            self.assertEqual(cleanup.returncode, 0, cleanup.stdout)
            self.assertIn('"portable_on_build_host": true', ssh.stdout)

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
