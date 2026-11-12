#!/usr/bin/env python3
"""Integration tests for the single-file suite format and launcher."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from suite import SuiteBuilder, SuiteError, read_footer  # noqa: E402


PAYLOAD_C = r"""
#include <stdio.h>
#include <stdlib.h>
int main(int argc, char **argv) {
    const char *variant = getenv("JOREK_SUITE_VARIANT");
    const char *model = getenv("JOREK_SUITE_MODEL");
    const char *program = getenv("JOREK_SUITE_PROGRAM");
    printf("variant=%s model=%s program=%s\n",
           variant ? variant : "?", model ? model : "?", program ? program : "?");
    for (int i = 1; i < argc; ++i) printf("arg%d=%s\n", i, argv[i]);
    return 7;
}
"""


def cc() -> str:
    for candidate in (os.environ.get("CC"), "cc", "clang", "gcc"):
        if candidate and shutil.which(candidate):
            return candidate
    raise unittest.SkipTest("no C compiler available")


class SuiteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="jorek-suite-test-"))
        self.cc = cc()
        self.payload_a = self.tmp / "payload-a"
        self.payload_b = self.tmp / "payload-b"
        source = self.tmp / "payload.c"
        source.write_text(PAYLOAD_C, encoding="utf-8")
        subprocess.run([self.cc, "-O2", str(source), "-o", str(self.payload_a)], check=True)
        subprocess.run([self.cc, "-O2", str(source), "-o", str(self.payload_b)], check=True)
        self.launcher_obj = self.tmp / "launcher.o"
        self.puff_obj = self.tmp / "puff.o"
        subprocess.run([self.cc, "-O2", "-std=c99", "-D_POSIX_C_SOURCE=200809L",
                        "-I", str(SRC), "-c", str(SRC / "jorek_suite.c"), "-o", str(self.launcher_obj)], check=True)
        subprocess.run([self.cc, "-O2", "-I", str(SRC), "-c", str(SRC / "puff.c"), "-o", str(self.puff_obj)], check=True)
        self.suite = self.tmp / "suite"
        builder = SuiteBuilder(self.tmp / "unused-launcher", jorek_version="test")
        v1 = builder.add_variant("model303-full", "303", ("hdf5", "mumps"), ("n_tor=3",), make_default=True)
        builder.add_program(v1, "jorek2_main", self.payload_a)
        v2 = builder.add_variant("model199-full", "199", ("hdf5",), ("n_tor=1",))
        builder.add_program(v2, "jorek2_main", self.payload_b)
        builder.add_program(v2, "jorek2vtk", self.payload_b)
        if sys.platform == "darwin":
            builder.build_macho_section(self.suite, [self.cc], [self.launcher_obj, self.puff_obj])
        else:
            # Append mode requires a launcher executable, not an object file.
            exe = self.tmp / "launcher"
            subprocess.run([self.cc, str(self.launcher_obj), str(self.puff_obj), "-o", str(exe)], check=True)
            builder.launcher = exe
            builder.build(self.suite)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_suite(self, *args: str, stdin: str | None = None, cache: Path | None = None):
        env = os.environ.copy()
        env["JOREK_SUITE_CACHE"] = str(cache or (self.tmp / "cache"))
        return subprocess.run([str(self.suite), *args], input=stdin, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, check=False)

    def test_list(self) -> None:
        proc = self.run_suite("--list")
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("model303-full", proc.stdout)
        self.assertIn("model199-full", proc.stdout)

    def test_select_and_run(self) -> None:
        proc = self.run_suite("--model", "303", "--", "hello", "world")
        self.assertEqual(proc.returncode, 7, proc.stdout)
        self.assertIn("variant=model303-full", proc.stdout)
        self.assertIn("arg1=hello", proc.stdout)

    def test_features(self) -> None:
        proc = self.run_suite("--features", "hdf5", "--params", "n_tor=1")
        self.assertEqual(proc.returncode, 7, proc.stdout)
        self.assertIn("model=199", proc.stdout)

    def test_unknown(self) -> None:
        proc = self.run_suite("--model", "999")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("no variant matches", proc.stdout)

    def test_extract_and_cache(self) -> None:
        proc = self.run_suite("--model", "303", "--extract-only")
        self.assertEqual(proc.returncode, 0, proc.stdout)
        path = Path(proc.stdout.strip().splitlines()[-1])
        self.assertTrue(path.exists())
        self.assertTrue(os.access(path, os.X_OK))
        first = path
        proc = self.run_suite("--model", "303", "--extract-only")
        second = Path(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(first, second)
        self.run_suite("--model", "303")

    def test_auto_model_from_namelist(self) -> None:
        proc = self.run_suite(stdin="! @jorek_model: 199\n&in1\n/\n")
        self.assertEqual(proc.returncode, 7, proc.stdout)
        self.assertIn("model=199", proc.stdout)

    def test_append_footer(self) -> None:
        if sys.platform == "darwin":
            self.skipTest("append mode is not used on macOS")
        footer = read_footer(self.suite)
        self.assertTrue(footer["crc_ok"])
        self.assertEqual(footer["format"], 1)


if __name__ == "__main__":
    unittest.main()
