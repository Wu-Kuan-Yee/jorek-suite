#!/usr/bin/env python3
"""Archive extraction must allow internal links but reject traversal."""

from __future__ import annotations

import io
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))

import common  # noqa: E402


class ArchiveTest(unittest.TestCase):
    def _tar(self, links):
        handle = io.BytesIO()
        with tarfile.open(fileobj=handle, mode="w") as tf:
            data = b"x"
            info = tarfile.TarInfo("pkg/src/file.c")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
            for name, target in links:
                link = tarfile.TarInfo(name)
                link.type = tarfile.SYMTYPE
                link.linkname = target
                tf.addfile(link)
        handle.seek(0)
        return handle

    def test_internal_relative_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "pkg.tar"
            archive.write_bytes(self._tar([
                ("pkg/prov/psm3/shared", "../../src"),
            ]).read())
            root = common.extract_archive(archive, Path(tmp) / "out")
            self.assertTrue((root / "prov/psm3/shared").is_symlink())

    def test_escaping_link_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "pkg.tar"
            archive.write_bytes(self._tar([
                ("pkg/escape", "../../../etc/passwd"),
            ]).read())
            with self.assertRaises(common.BuildError):
                common.extract_archive(archive, Path(tmp) / "out")


if __name__ == "__main__":
    unittest.main()
