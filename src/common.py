#!/usr/bin/env python3
"""Small, dependency-free utilities shared by build.py.

The module intentionally supports Python 3.7+ and the three major platforms
(Linux, the BSDs, macOS, and Windows/MSYS2).  It contains no JOREK-specific
knowledge.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import posixpath
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
IS_BSD = any(sys.platform.startswith(x) for x in ("freebsd", "openbsd", "netbsd", "dragonfly"))
EXE_SUFFIX = ".exe" if IS_WINDOWS else ""


class BuildError(RuntimeError):
    """A user-facing build failure with an actionable message."""


class CommandError(BuildError):
    def __init__(self, argv: Sequence[str], returncode: int, log: Optional[Path] = None):
        self.argv = list(argv)
        self.returncode = returncode
        self.log = log
        msg = "command failed with exit code %d: %s" % (returncode, _display_argv(argv))
        if log:
            msg += "\n  log: %s" % log
        super().__init__(msg)


def _display_argv(argv: Sequence[str]) -> str:
    out = []
    for item in argv:
        item = str(item)
        if not item or any(c in item for c in " \t\"'\\$"):
            out.append("'" + item.replace("'", "'\\''") + "'")
        else:
            out.append(item)
    return " ".join(out)


def which(name: str, extra_path: Optional[Sequence[str]] = None) -> Optional[str]:
    if not name:
        return None
    if os.path.isabs(name):
        return name if os.path.isfile(name) and os.access(name, os.X_OK) else None
    paths = []
    if extra_path:
        paths.extend(str(x) for x in extra_path)
    paths.extend(os.environ.get("PATH", "").split(os.pathsep))
    for directory in paths:
        if not directory:
            continue
        candidate = os.path.join(directory, name)
        if IS_WINDOWS and not candidate.lower().endswith(".exe"):
            for suffix in (".exe", ".cmd", ".bat"):
                if os.path.isfile(candidate + suffix):
                    return candidate + suffix
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def which_all(name: str, extra_path: Optional[Sequence[str]] = None) -> List[str]:
    paths = []
    if extra_path:
        paths.extend(str(x) for x in extra_path)
    paths.extend(os.environ.get("PATH", "").split(os.pathsep))
    seen = set()
    result = []
    for directory in paths:
        if not directory:
            continue
        candidate = os.path.join(directory, name)
        if IS_WINDOWS and not candidate.lower().endswith(".exe"):
            candidates = [candidate + s for s in (".exe", ".cmd", ".bat")]
        else:
            candidates = [candidate]
        for c in candidates:
            if c not in seen and os.path.isfile(c) and os.access(c, os.X_OK):
                seen.add(c)
                result.append(c)
    return result


def run(
    argv: Sequence[str],
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    log: Optional[Path] = None,
    dry_run: bool = False,
    echo: bool = True,
    check: bool = True,
    input_text: Optional[str] = None,
) -> subprocess.CompletedProcess:
    """Run a command with a merged environment and optional combined log."""
    argv = [str(x) for x in argv]
    if dry_run:
        print("  + " + _display_argv(argv))
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    env = env or {}
    full_env = os.environ.copy()
    if env:
        full_env.update({str(k): str(v) for k, v in env.items()})
    if echo and not env.get("JOREK_QUIET"):
        print("  + " + _display_argv(argv))
    t0 = time.time()
    proc = subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        env=full_env,
        input=input_text.encode() if input_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    elapsed = time.time() - t0
    if log:
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "ab") as handle:
            handle.write(("\n$ " + _display_argv(argv) + "\n").encode())
            handle.write(proc.stdout)
            handle.write(("\n[exit %d, %.1fs]\n" % (proc.returncode, elapsed)).encode())
    if echo and proc.stdout and not env.get("JOREK_QUIET"):
        text = proc.stdout.decode("utf-8", "replace")
        if len(text) > 20000:
            text = text[:10000] + "\n... [output truncated, see log] ...\n" + text[-8000:]
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
    if check and proc.returncode != 0:
        raise CommandError(argv, proc.returncode, log)
    return proc


def output(argv: Sequence[str], cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None) -> str:
    proc = subprocess.run(
        [str(x) for x in argv],
        cwd=str(cwd) if cwd else None,
        env={**os.environ, **(env or {})},
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return proc.stdout.decode("utf-8", "replace").strip()


def read_text(path: Path, default: str = "") -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return default


def write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-%d" % os.getpid())
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, value) -> None:
    write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def safe_rmtree(path: Path, root: Path) -> None:
    """Remove *path* only if it is strictly below *root*.

    This guards against accidental ``--work /`` or symlink surprises in clean
    commands.  Symlinks are removed as links, never followed.
    """
    try:
        resolved_root = Path(root).resolve()
        resolved = Path(path).resolve()
        resolved.relative_to(resolved_root)
    except (ValueError, OSError):
        raise BuildError("refusing to remove path outside the work directory: %s" % path)
    if resolved == resolved_root:
        raise BuildError("refusing to remove the work directory itself: %s" % path)
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def ensure_dir(path: Path) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def normalize_sha256(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = value.strip().lower()
    if value.startswith("sha256:"):
        value = value[7:]
    if re.fullmatch(r"[0-9a-f]{64}", value):
        return value
    return None


def human_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0 or unit == "TiB":
            if unit == "B":
                return "%d B" % int(value)
            return "%.2f %s" % (value, unit)
        value /= 1024.0
    return "%d B" % n


def hostname() -> str:
    try:
        return os.uname().nodename  # type: ignore[attr-defined]
    except AttributeError:
        return os.environ.get("COMPUTERNAME", "unknown")


@dataclass
class PlatformInfo:
    system: str
    machine: str
    libc: str
    is_windows: bool
    is_macos: bool
    is_linux: bool
    is_bsd: bool
    exe_suffix: str
    shared_suffix: str
    static_suffix: str
    triple: str
    python: str

    @classmethod
    def detect(cls) -> "PlatformInfo":
        system = "windows" if IS_WINDOWS else ("macos" if IS_MACOS else ("linux" if IS_LINUX else ("bsd" if IS_BSD else sys.platform)))
        machine = platform.machine().lower()
        if machine in ("aarch64", "arm64"):
            machine = "arm64"
        elif machine in ("x86_64", "amd64"):
            machine = "x86_64"
        elif machine.startswith("armv7") or machine.startswith("armv8"):
            machine = "arm"
        libc = "msvcrt"
        if IS_LINUX:
            libc = "musl" if _is_musl() else "glibc"
        elif IS_MACOS:
            libc = "libSystem"
        elif IS_BSD:
            libc = "bsd-libc"
        triple = "%s-%s" % (machine, system)
        return cls(
            system=system,
            machine=machine,
            libc=libc,
            is_windows=IS_WINDOWS,
            is_macos=IS_MACOS,
            is_linux=IS_LINUX,
            is_bsd=IS_BSD,
            exe_suffix=".exe" if IS_WINDOWS else "",
            shared_suffix=".dll" if IS_WINDOWS else (".dylib" if IS_MACOS else ".so"),
            static_suffix=".lib" if IS_WINDOWS else ".a",
            triple=triple,
            python=sys.executable,
        )

    def summary(self) -> str:
        return "%s %s (%s, python %s)" % (
            self.system,
            self.machine,
            self.libc,
            platform.python_version(),
        )


def _is_musl() -> bool:
    try:
        with open("/usr/bin/ldd", "rb") as handle:
            data = handle.read(4096)
        return b"musl" in data
    except OSError:
        pass
    for path in ("/lib/ld-musl-x86_64.so.1", "/lib/ld-musl-aarch64.so.1"):
        if os.path.exists(path):
            return True
    return False


# ---------------------------------------------------------------------------
# Downloads and archives
# ---------------------------------------------------------------------------
class Downloader:
    def __init__(
        self,
        cache_dir: Path,
        offline: bool = False,
        retries: int = 3,
        verify: bool = True,
        lock_path: Optional[Path] = None,
    ) -> None:
        self.cache_dir = ensure_dir(cache_dir)
        self.offline = offline
        self.retries = max(1, retries)
        self.verify = verify
        self.lock_path = lock_path or (cache_dir / "downloads.lock.json")
        self.lock = read_json(self.lock_path, {}) or {}

    def _record(self, name: str, sha: str) -> None:
        if self.lock.get(name) != sha:
            self.lock[name] = sha
            write_json(self.lock_path, self.lock)

    def fetch(self, url: str, filename: str, sha256: Optional[str] = None) -> Path:
        filename = os.path.basename(filename) or "download"
        destination = self.cache_dir / filename
        expected = normalize_sha256(sha256)
        if destination.exists():
            actual = sha256_file(destination)
            if expected and self.verify and actual != expected:
                print("  cached file has wrong digest, redownloading: %s" % destination)
                destination.unlink()
            elif self.verify:
                recorded = self.lock.get(filename)
                if recorded and actual != recorded:
                    print("  warning: %s does not match the previously recorded digest" % destination)
                self._record(filename, actual)
                return destination
        if self.offline:
            raise BuildError("offline mode: missing cached download %s" % destination)
        last_error: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            try:
                print("  downloading %s" % url)
                self._download(url, destination)
                actual = sha256_file(destination)
                if expected and self.verify and actual != expected:
                    try:
                        destination.unlink()
                    except OSError:
                        pass
                    raise BuildError(
                        "SHA-256 mismatch for %s\n  expected %s\n  actual   %s"
                        % (url, expected, actual)
                    )
                if self.verify:
                    self._record(filename, actual)
                return destination
            except Exception as exc:  # noqa: BLE001 - retry all network errors
                last_error = exc
                print("  download attempt %d/%d failed: %s" % (attempt, self.retries, exc))
                if attempt < self.retries:
                    time.sleep(min(2 ** attempt, 10))
        raise BuildError("could not download %s: %s" % (url, last_error))

    def _download(self, url: str, destination: Path) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme == "file":
            shutil.copyfile(parsed.path, destination)
            return
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "jorek-build/1.0 (+https://github.com/iterorganization/JOREK)"},
        )
        tmp = destination.with_name(destination.name + ".part")
        try:
            with urllib.request.urlopen(request, timeout=60) as response, open(tmp, "wb") as out:
                total = response.headers.get("Content-Length")
                total_i = int(total) if total and total.isdigit() else 0
                done = 0
                last_print = 0.0
                while True:
                    block = response.read(1024 * 256)
                    if not block:
                        break
                    out.write(block)
                    done += len(block)
                    now = time.time()
                    if total_i and now - last_print > 1.0:
                        print("    %.1f%% (%s / %s)" % (100.0 * done / total_i, human_bytes(done), human_bytes(total_i)))
                        last_print = now
            os.replace(tmp, destination)
        except Exception as primary:  # noqa: BLE001 - fall back to curl
            curl = shutil.which("curl")
            if curl:
                print("  urllib failed (%s); retrying with curl --http1.1" % primary)
                try:
                    subprocess.run(
                        [curl, "-L", "--http1.1", "--retry", "5", "--retry-delay", "2",
                         "-o", str(tmp), url],
                        check=True,
                    )
                    if tmp.exists() and tmp.stat().st_size > 0:
                        os.replace(tmp, destination)
                        return
                except (OSError, subprocess.CalledProcessError) as fallback:
                    print("  curl fallback failed: %s" % fallback)
            raise
        finally:
            if tmp.exists():
                tmp.unlink()


def extract_archive(archive: Path, destination: Path, strip_components: int = 0) -> Path:
    """Extract .tar.{gz,xz,bz2,zst} or .zip and return the single source root."""
    destination = ensure_dir(destination)
    archive = Path(archive)
    name = archive.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(destination)
    else:
        mode = "r:*"
        try:
            with tarfile.open(archive, mode) as tf:
                members = []
                for member in tf.getmembers():
                    name = member.name.replace("\\", "/")
                    parts = Path(name).parts
                    if name.startswith("/") or ".." in parts:
                        raise BuildError("unsafe path in archive %s: %s" % (archive, member.name))
                    if member.issym() or member.islnk():
                        target = member.linkname.replace("\\", "/")
                        # A relative link is safe when it resolves inside the
                        # extraction root.  MPICH ships links such as
                        # psm3/shared -> ../../src, which are legitimate.
                        base = posixpath.dirname(name) if member.issym() else ""
                        resolved = posixpath.normpath(posixpath.join(base, target))
                        if (target.startswith("/") or resolved == ".." or
                                resolved.startswith("../") or resolved.startswith("/")):
                            raise BuildError("unsafe link in archive %s: %s -> %s" % (
                                archive, member.name, member.linkname))
                    members.append(member)
                if strip_components:
                    members = [_strip_member(m, strip_components) for m in members]
                    members = [m for m in members if m is not None]
                tf.extractall(destination, members=members)
        except tarfile.TarError as exc:
            raise BuildError("cannot extract %s: %s" % (archive, exc))
    entries = [p for p in destination.iterdir() if p.name not in (".", "..")]
    dirs = [p for p in entries if p.is_dir()]
    if len(dirs) == 1 and not any(p.is_file() for p in entries):
        return dirs[0]
    return destination


def _strip_member(member: tarfile.TarInfo, count: int) -> Optional[tarfile.TarInfo]:
    parts = Path(member.name).parts
    if len(parts) <= count:
        return None
    member.name = str(Path(*parts[count:]))
    return member


def copy_tree(src: Path, dst: Path, hardlink: bool = True) -> None:
    """Copy a tree, optionally hard-linking regular files (fallback to copy).

    Mutable configuration files are copied normally by the caller after this
    function returns.
    """
    src = Path(src)
    dst = Path(dst)
    ignore_names = {
        ".git", ".hg", ".svn", ".obj", ".mod", ".dep", "__pycache__",
        ".pytest_cache", ".mypy_cache", ".idea", ".vscode",
    }
    ignore_globs = [
        "*.o", "*.mod", "*.dep", "*.pyc", "*.pyo", "*.so", "*.dylib", "*.dll",
        "*.a", "*.exe", "jorek_model*", "Makefile.inc", "mpiversion.mk",
    ]
    for root, dirs, files in os.walk(src):
        rel = Path(root).relative_to(src)
        dirs[:] = sorted(d for d in dirs if d not in ignore_names)
        target_dir = dst / rel
        target_dir.mkdir(parents=True, exist_ok=True)
        for filename in files:
            if any(Path(filename).match(g) for g in ignore_globs):
                continue
            source = Path(root) / filename
            target = target_dir / filename
            if target.exists():
                continue
            if hardlink:
                try:
                    os.link(source, target)
                    continue
                except OSError:
                    pass
            shutil.copy2(source, target)


def git_describe(root: Path) -> str:
    if not (Path(root) / ".git").exists():
        return "unknown"
    proc = subprocess.run(
        ["git", "-C", str(root), "describe", "--always", "--dirty", "--abbrev=12"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if proc.returncode == 0:
        return proc.stdout.decode("utf-8", "replace").strip()
    proc = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--short=12", "HEAD"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return proc.stdout.decode("utf-8", "replace").strip() if proc.returncode == 0 else "unknown"


def cpu_count(default: int = 1) -> int:
    try:
        n = os.cpu_count() or default
    except Exception:  # noqa: BLE001
        n = default
    return max(1, n)


def log_section(title: str) -> None:
    print("")
    print("== %s ==" % title)
