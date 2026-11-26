from __future__ import annotations

import unittest

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

    def test_scp_remote_path_is_quoted(self):
        args = scp_arguments(
            PROFILE, "/scratch/jorek work/dist/jorek-suite",
            "/tmp/JOREK artifacts/suite")
        self.assertIn("-P", args)
        self.assertTrue(any("'/scratch/jorek work/dist/jorek-suite'" in item for item in args))
        self.assertEqual(args[-1], "/tmp/JOREK artifacts/suite")

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
