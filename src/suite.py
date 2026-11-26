#!/usr/bin/env python3
"""Create and inspect JOREK single-file suites.

This module is deliberately dependency-free (Python standard library only) so
that build.py can use it on a bare machine.  The on-disk format is shared with
src/jorek_suite.c; keep the two implementations in sync.

Layout of a suite file::

    [native launcher][group 0][group 1]...[manifest][128-byte footer]

Each group is a concatenation of one or more programs and is compressed as a
single raw DEFLATE stream.  The uncompressed bytes carry no container header:
the manifest stores each program's offset and size inside its group.  This
keeps the C reader tiny and makes solid compression across similar binaries
possible.

The footer is little-endian::

    0   16  magic b"JOREKSUITE0001\\0"
    16   4  format version (1)
    20   4  footer size (128)
    24   8  manifest offset
    32   8  manifest size
    40   8  archive offset (end of launcher image)
    48   8  archive size
    56  32  SHA-256 of manifest
    88  32  reserved (zero)
    120  4  CRC-32 of bytes 0..123
    124  4  reserved (zero)
"""

from __future__ import annotations

import hashlib
import os
import shutil
import shlex
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

MAGIC = b"JOREKSUITE0001\x00\x00"
FORMAT_VERSION = 1
FOOTER_SIZE = 128
ARCHIVE_METHOD_DEFLATE = 1


class SuiteError(RuntimeError):
    pass


def _u32(value: int) -> bytes:
    return struct.pack("<I", value & 0xFFFFFFFF)


def _u64(value: int) -> bytes:
    return struct.pack("<Q", value & 0xFFFFFFFFFFFFFFFF)


def _safe_name(name: str) -> bool:
    if not name or name in (".", ".."):
        return False
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-+=")
    return all(ch in allowed for ch in name)


def _safe_relpath(name: str) -> bool:
    """Allow a relative path for a runtime script/helper, never traversal."""
    if not name or name.startswith(("/", "\\")):
        return False
    parts = name.replace("\\", "/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        return False
    return all(_safe_name(part) for part in parts)


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _hex(digest: bytes) -> str:
    return digest.hex()


@dataclass
class Variant:
    name: str
    model: str
    features: Tuple[str, ...] = ()
    params: Tuple[str, ...] = ()
    programs: List["Program"] = field(default_factory=list)

    def feature_csv(self) -> str:
        return ",".join(self.features)

    def param_csv(self) -> str:
        return ",".join(self.params)


@dataclass
class Program:
    variant: str
    name: str
    path: Path
    mode: int = 0o755
    data: Optional[bytes] = None
    offset: int = 0
    size: int = 0
    sha: bytes = b""
    group: int = 0


@dataclass
class Group:
    ident: int
    compressed: bytes
    programs: List[Program]
    sha: bytes = b""
    usize: int = 0


class SuiteBuilder:
    """Assemble a single-file suite from a launcher and native executables."""

    def __init__(
        self,
        launcher: Path,
        suite_version: str = "1.0.0",
        jorek_version: str = "unknown",
        compression_level: int = 9,
    ) -> None:
        self.launcher = Path(launcher)
        self.suite_version = suite_version
        self.jorek_version = jorek_version
        self.compression_level = max(0, min(9, compression_level))
        self.variants: List[Variant] = []
        self.helpers: List[Program] = []
        self.build_id = ""
        self.default_variant = ""
        self._by_name: Dict[str, Variant] = {}

    def add_variant(
        self,
        name: str,
        model: str,
        features: Sequence[str] = (),
        params: Sequence[str] = (),
        make_default: bool = False,
    ) -> Variant:
        if not _safe_name(name) or not _safe_name(model):
            raise SuiteError("unsafe variant or model name: %r/%r" % (name, model))
        if name in self._by_name:
            raise SuiteError("duplicate variant name: %s" % name)
        feature_list = []
        for item in features:
            if not _safe_name(item):
                raise SuiteError("unsafe feature name: %r" % (item,))
            feature_list.append(item)
        param_list = []
        for item in params:
            if not _safe_name(item):
                raise SuiteError("unsafe parameter token: %r" % (item,))
            param_list.append(item)
        variant = Variant(name, model, tuple(feature_list), tuple(param_list))
        self.variants.append(variant)
        self._by_name[name] = variant
        if make_default or not self.default_variant:
            self.default_variant = name
        return variant

    def add_program(
        self,
        variant: Variant,
        name: str,
        path: Path,
        mode: int = 0o755,
    ) -> Program:
        if not _safe_name(name):
            raise SuiteError("unsafe program name: %r" % (name,))
        path = Path(path)
        if not path.is_file():
            raise SuiteError("program payload does not exist: %s" % path)
        if path.stat().st_size == 0:
            raise SuiteError("program payload is empty: %s" % path)
        program = Program(variant=variant.name, name=name, path=path, mode=mode)
        variant.programs.append(program)
        return program

    def add_helper(self, name: str, path: Path, mode: int = 0o755) -> Program:
        """Embed an auxiliary executable (for example mpiexec.hydra)."""
        if not _safe_relpath(name):
            raise SuiteError("unsafe helper path: %r" % (name,))
        path = Path(path)
        if not path.is_file():
            raise SuiteError("helper payload is missing: %s" % path)
        program = Program(variant="__helpers__", name=name, path=path, mode=mode)
        self.helpers.append(program)
        return program

    def _build_groups(self) -> List[Group]:
        groups: List[Group] = []
        for index, variant in enumerate(self.variants):
            if not variant.programs:
                raise SuiteError("variant %s has no programs" % variant.name)
            chunks: List[bytes] = []
            offset = 0
            seen: Dict[bytes, Tuple[int, int, bytes]] = {}
            for program in variant.programs:
                data = program.data if program.data is not None else program.path.read_bytes()
                digest = _sha256(data)
                if digest in seen:
                    start, size, _ = seen[digest]
                else:
                    start = offset
                    size = len(data)
                    chunks.append(data)
                    offset += size
                    seen[digest] = (start, size, digest)
                program.offset = start
                program.size = size
                program.sha = digest
                program.group = index
            raw = b"".join(chunks)
            group_sha = _sha256(raw)
            compressor = zlib.compressobj(
                self.compression_level, zlib.DEFLATED, -15, 9, zlib.Z_DEFAULT_STRATEGY
            )
            compressed = compressor.compress(raw) + compressor.flush()
            del raw
            groups.append(Group(index, compressed, list(variant.programs), group_sha, offset))
        if self.helpers:
            index = len(groups)
            chunks = []
            offset = 0
            seen = {}
            for program in self.helpers:
                data = program.data if program.data is not None else program.path.read_bytes()
                digest = _sha256(data)
                if digest in seen:
                    start, size, _ = seen[digest]
                else:
                    start = offset
                    size = len(data)
                    chunks.append(data)
                    offset += size
                    seen[digest] = (start, size, digest)
                program.offset = start
                program.size = size
                program.sha = digest
                program.group = index
            raw = b"".join(chunks)
            group_sha = _sha256(raw)
            compressor = zlib.compressobj(self.compression_level, zlib.DEFLATED, -15, 9, zlib.Z_DEFAULT_STRATEGY)
            compressed = compressor.compress(raw) + compressor.flush()
            del raw
            groups.append(Group(index, compressed, list(self.helpers), group_sha, offset))
        return groups

    def _manifest_text(self, groups: Sequence[Group]) -> bytes:
        # A build id derived from payload digests (not from the manifest
        # itself, which would be circular).
        h = hashlib.sha256()
        h.update(b"jorek-suite\0")
        h.update(self.suite_version.encode("utf-8", "replace"))
        for variant in self.variants:
            h.update(variant.name.encode("ascii"))
            h.update(b"\0")
            h.update(variant.model.encode("ascii"))
            h.update(b"\0")
            for program in variant.programs:
                h.update(program.name.encode("ascii"))
                h.update(b"\0")
                h.update(program.sha)
        self.build_id = h.hexdigest()[:32]
        lines = [
            "format %d" % FORMAT_VERSION,
            "suite-version %s" % self.suite_version,
            "jorek-version %s" % (self.jorek_version or "unknown").replace(" ", "_"),
            "build-id %s" % self.build_id,
            "default-variant %s" % (self.default_variant or self.variants[0].name),
        ]
        for group in groups:
            lines.append(
                "group %d %d %d %d %s"
                % (
                    group.ident,
                    self._group_offsets[group.ident][0],
                    len(group.compressed),
                    group.usize,
                    _hex(group.sha),
                )
            )
        for variant in self.variants:
            group_index = self.variants.index(variant)
            lines.append(
                "variant %s %s %s %s %d"
                % (
                    variant.name,
                    variant.model,
                    variant.feature_csv() or "-",
                    variant.param_csv() or "-",
                    group_index,
                )
            )
        for variant in self.variants:
            for program in variant.programs:
                lines.append(
                    "program %s %s %d %d %d %o %s"
                    % (
                        variant.name,
                        program.name,
                        program.group,
                        program.offset,
                        program.size,
                        program.mode,
                        _hex(program.sha),
                    )
                )
        for program in self.helpers:
            lines.append(
                "helper %s %d %d %d %o %s"
                % (program.name, program.group, program.offset, program.size,
                   program.mode, _hex(program.sha))
            )
        return ("\n".join(lines) + "\n").encode("ascii")

    def _pack(self, groups: Sequence[Group], archive_off: int) -> Tuple[bytes, bytes, bytes, bytes]:
        """Return (archive, manifest, footer, blob-with-footer)."""
        self._group_offsets = {}
        cursor = 0
        for group in groups:
            self._group_offsets[group.ident] = (cursor, len(group.compressed), group.usize)
            cursor += len(group.compressed)
        archive = b"".join(group.compressed for group in groups)
        manifest = self._manifest_text(groups)
        manifest_sha = _sha256(manifest)
        manifest_off = archive_off + len(archive)
        footer = bytearray(FOOTER_SIZE)
        footer[0:16] = MAGIC
        struct.pack_into("<II", footer, 16, FORMAT_VERSION, FOOTER_SIZE)
        struct.pack_into("<QQQQ", footer, 24, manifest_off, len(manifest), archive_off, len(archive))
        footer[56:88] = manifest_sha
        crc = zlib.crc32(bytes(footer[:120])) & 0xFFFFFFFF
        struct.pack_into("<I", footer, 120, crc)
        return archive, manifest, bytes(footer), archive + manifest + bytes(footer)

    def build(self, output: Path, sign: bool = True, strip_launcher: bool = False) -> Path:
        """Build by appending the payload to an existing launcher.

        This is the portable mode used on Linux, the BSDs, and Windows.  It is
        not used on macOS, where code signing requires the payload to live in a
        Mach-O section (see build_macho_section).
        """
        output = Path(output)
        launcher_data = self.launcher.read_bytes()
        if not launcher_data:
            raise SuiteError("launcher is empty: %s" % self.launcher)
        groups = self._build_groups()
        archive, manifest, footer, _blob = self._pack(groups, len(launcher_data))
        output.parent.mkdir(parents=True, exist_ok=True)
        tmp = output.with_name(output.name + ".tmp-%d" % os.getpid())
        with open(tmp, "wb") as handle:
            handle.write(launcher_data)
            handle.write(archive)
            handle.write(manifest)
            handle.write(footer)
        os.replace(tmp, output)
        try:
            os.chmod(output, 0o755)
        except OSError:
            pass
        if sign and sys.platform == "darwin":
            _ad_hoc_sign(output)
        return output

    def build_macho_section(
        self,
        output: Path,
        cc: Sequence[str],
        objects: Sequence[Path],
        extra_args: Sequence[str] = (),
        arch: Optional[str] = None,
    ) -> Path:
        """Build a signed Mach-O suite with the blob in __DATA,__jorek_suite.

        Appending bytes to a Mach-O image invalidates its code signature on
        Apple silicon.  Putting the archive in a section keeps the file a
        well-formed signed executable that the kernel accepts.
        """
        if sys.platform != "darwin":
            raise SuiteError("Mach-O section embedding is only available on macOS")
        output = Path(output)
        groups = self._build_groups()
        archive, manifest, footer, blob = self._pack(groups, 0)
        work = Path(tempfile.mkdtemp(prefix="jorek-suite-macho-"))
        try:
            blob_path = work / "payload.bin"
            blob_path.write_bytes(blob)
            dummy_c = work / "dummy.c"
            dummy_c.write_text("int jorek_suite_dummy_symbol;\n", encoding="ascii")
            dummy_o = work / "dummy.o"
            section_o = work / "payload_section.o"
            _run([*cc, "-c", str(dummy_c), "-o", str(dummy_o)])
            ld = shutil.which("ld") or "/usr/bin/ld"
            arch_name = arch
            if not arch_name:
                machine = os.uname().machine
                arch_name = "arm64" if machine == "arm64" else "x86_64"
            _run([
                ld, "-r", "-arch", arch_name,
                "-sectcreate", "__DATA", "__jorek_suite",
                str(blob_path), "-o", str(section_o), str(dummy_o),
            ])
            output.parent.mkdir(parents=True, exist_ok=True)
            tmp = output.with_name(output.name + ".tmp-%d" % os.getpid())
            _run([*cc, *map(str, objects), str(section_o), "-o", str(tmp), *extra_args])
            os.replace(tmp, output)
            os.chmod(output, 0o755)
            _ad_hoc_sign(output)
            return output
        finally:
            shutil.rmtree(work, ignore_errors=True)


def _run(argv: Sequence[str]) -> None:
    command = list(map(str, argv))
    if os.environ.get("JOREK_BUILD_STUDIO_EVENTS") == "1":
        print("  + " + shlex.join(command), flush=True)
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
        chunks = []
        stream = process.stdout
        binary_stdout = getattr(sys.stdout, "buffer", None)
        while stream is not None:
            reader = getattr(stream, "read1", stream.read)
            chunk = reader(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if binary_stdout is not None:
                binary_stdout.write(chunk)
                binary_stdout.flush()
            else:
                sys.stdout.write(chunk.decode("utf-8", "replace"))
                sys.stdout.flush()
        returncode = process.wait()
        output = b"".join(chunks)
        if returncode != 0:
            raise SuiteError(
                "command failed (%d): %s\n%s"
                % (returncode, " ".join(command), output.decode("utf-8", "replace"))
            )
        return

    result = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        raise SuiteError(
            "command failed (%d): %s\n%s"
            % (result.returncode, " ".join(command), result.stderr.decode("utf-8", "replace"))
        )


def _ad_hoc_sign(path: Path) -> None:
    """Re-sign a mutated Mach-O image ad hoc.

    Appending bytes invalidates the linker-generated ad-hoc signature on Apple
    silicon.  codesign(1) is part of the base system, so this does not add a
    build dependency.
    """

    codesign = shutil.which("codesign") or "/usr/bin/codesign"
    if not os.path.exists(codesign):
        return
    if os.environ.get("JOREK_BUILD_STUDIO_EVENTS") == "1":
        _run([codesign, "--force", "--sign", "-", "--timestamp=none", str(path)])
        return
    result = subprocess.run(
        [codesign, "--force", "--sign", "-", "--timestamp=none", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise SuiteError(
            "codesign failed for %s: %s" % (path, result.stderr.decode("utf-8", "replace").strip())
        )


def read_footer(path: Path) -> dict:
    data = Path(path).read_bytes()
    if len(data) < FOOTER_SIZE:
        raise SuiteError("file too short")
    footer = data[-FOOTER_SIZE:]
    if footer[:16] != MAGIC:
        raise SuiteError("bad magic")
    fmt, size = struct.unpack_from("<II", footer, 16)
    manifest_off, manifest_size, archive_off, archive_size = struct.unpack_from("<QQQQ", footer, 24)
    manifest_sha = footer[56:88]
    crc = struct.unpack_from("<I", footer, 120)[0]
    return {
        "format": fmt,
        "footer_size": size,
        "manifest_offset": manifest_off,
        "manifest_size": manifest_size,
        "archive_offset": archive_off,
        "archive_size": archive_size,
        "manifest_sha256": manifest_sha.hex(),
        "crc_ok": (zlib.crc32(footer[:120]) & 0xFFFFFFFF) == crc,
        "file_size": len(data),
    }


def format_bytes(value: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return "%.1f %s" % (value, unit) if unit != "B" else "%d B" % value
        value /= 1024.0
    return "%d B" % value
