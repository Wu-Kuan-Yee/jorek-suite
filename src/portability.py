"""Dependency-free binary metadata inspection for suite portability reports.

The report distinguishes facts read from the executable from host assumptions.
It does not claim to certify arbitrary kernel versions or CPU micro-architectures.
"""

from __future__ import annotations

import platform as host_platform
import re
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Any, Dict, List


_ELF_MACHINES = {
    3: "i386", 8: "mips", 20: "ppc", 21: "ppc64", 40: "arm",
    62: "x86_64", 183: "aarch64", 243: "riscv64", 258: "loongarch64",
}
_MACHO_CPUS = {
    7: "i386", 0x01000007: "x86_64", 12: "arm", 0x0100000C: "arm64",
    18: "ppc", 0x01000012: "ppc64",
}
_PE_MACHINES = {
    0x014C: "i386", 0x8664: "x86_64", 0x01C0: "arm",
    0xAA64: "arm64", 0x5064: "riscv64",
}
_ELF_OSABI = {
    0: "System V", 1: "HP-UX", 2: "NetBSD", 3: "Linux",
    6: "Solaris", 9: "FreeBSD", 12: "OpenBSD",
}


def _run(tool: str, args: List[str]) -> str:
    executable = shutil.which(tool)
    if not executable:
        return ""
    try:
        result = subprocess.run(
            [executable] + args, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, check=False,
        )
    except OSError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.decode("utf-8", "replace")


def _parse_elf(data: bytes) -> Dict[str, Any]:
    if len(data) < 20:
        raise ValueError("truncated ELF header")
    if data[5] == 1:
        endian = "<"
        endian_name = "little"
    elif data[5] == 2:
        endian = ">"
        endian_name = "big"
    else:
        raise ValueError("invalid ELF byte-order marker")
    bits = {1: 32, 2: 64}.get(data[4], 0)
    if not bits:
        raise ValueError("invalid ELF class")
    machine_id = struct.unpack_from(endian + "H", data, 18)[0]
    return {
        "format": "ELF",
        "bits": bits,
        "endianness": endian_name,
        "architectures": [_ELF_MACHINES.get(machine_id, "unknown-%d" % machine_id)],
        "os_abi": _ELF_OSABI.get(data[7], "unknown-%d" % data[7]),
        "file_type": struct.unpack_from(endian + "H", data, 16)[0],
    }


def _macho_cpu_name(cpu: int) -> str:
    return _MACHO_CPUS.get(cpu, "unknown-0x%x" % cpu)


def _parse_macho(data: bytes) -> Dict[str, Any]:
    magic = data[:4]
    fat = {
        b"\xca\xfe\xba\xbe": (">", False),
        b"\xbe\xba\xfe\xca": ("<", False),
        b"\xca\xfe\xba\xbf": (">", True),
        b"\xbf\xba\xfe\xca": ("<", True),
    }
    if magic in fat:
        endian, is_64 = fat[magic]
        if len(data) < 8:
            raise ValueError("truncated universal Mach-O header")
        count = struct.unpack_from(endian + "I", data, 4)[0]
        entry_size = 32 if is_64 else 20
        if count > 128 or 8 + count * entry_size > len(data):
            raise ValueError("invalid universal Mach-O architecture table")
        architectures = []
        for index in range(count):
            offset = 8 + index * entry_size
            cpu = struct.unpack_from(endian + "I", data, offset)[0]
            name = _macho_cpu_name(cpu)
            if name not in architectures:
                architectures.append(name)
        return {
            "format": "Mach-O universal",
            "bits": None,
            "endianness": "fat-header",
            "architectures": architectures,
            "os_abi": "Darwin",
        }

    thin = {
        b"\xfe\xed\xfa\xce": (">", 32),
        b"\xce\xfa\xed\xfe": ("<", 32),
        b"\xfe\xed\xfa\xcf": (">", 64),
        b"\xcf\xfa\xed\xfe": ("<", 64),
    }
    if magic not in thin:
        raise ValueError("unknown Mach-O magic")
    endian, bits = thin[magic]
    if len(data) < 8:
        raise ValueError("truncated Mach-O header")
    cpu = struct.unpack_from(endian + "I", data, 4)[0]
    return {
        "format": "Mach-O",
        "bits": bits,
        "endianness": "little" if endian == "<" else "big",
        "architectures": [_macho_cpu_name(cpu)],
        "os_abi": "Darwin",
    }


def _parse_pe(data: bytes) -> Dict[str, Any]:
    if len(data) < 64:
        raise ValueError("truncated DOS header")
    offset = struct.unpack_from("<I", data, 0x3C)[0]
    if offset + 6 > len(data) or data[offset:offset + 4] != b"PE\x00\x00":
        raise ValueError("invalid PE signature")
    machine = struct.unpack_from("<H", data, offset + 4)[0]
    return {
        "format": "PE",
        "bits": 64 if machine in (0x8664, 0xAA64, 0x5064) else 32,
        "endianness": "little",
        "architectures": [_PE_MACHINES.get(machine, "unknown-0x%x" % machine)],
        "os_abi": "Windows",
        "machine_id": machine,
    }


def inspect_binary(path: Path) -> Dict[str, Any]:
    target = Path(path)
    with target.open("rb") as handle:
        data = handle.read(4096)
    if data.startswith(b"\x7fELF"):
        info = _parse_elf(data)
    elif data[:4] in (
        b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
        b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
        b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",
    ):
        info = _parse_macho(data)
    elif data.startswith(b"MZ"):
        info = _parse_pe(data)
    else:
        info = {
            "format": "unknown",
            "bits": None,
            "endianness": None,
            "architectures": [],
            "os_abi": "unknown",
        }
    info["path"] = str(target.resolve())
    info["size_bytes"] = target.stat().st_size
    info["dynamic_dependencies"] = []
    info["minimum_os_version"] = None
    info["minimum_glibc_version"] = None

    if info["format"] == "ELF":
        readelf = _run("readelf", ["-d", str(target)])
        info["dynamic_dependencies"] = re.findall(
            r"\(NEEDED\)\s+Shared library:\s+\[([^\]]+)\]", readelf)
        versions = _run("readelf", ["--version-info", str(target)])
        glibc = re.findall(r"Name:\s+GLIBC_([0-9.]+)", versions)
        if glibc:
            info["minimum_glibc_version"] = max(
                glibc, key=lambda value: tuple(int(part) for part in value.split("."))
            )
    elif info["format"].startswith("Mach-O"):
        output = _run("otool", ["-L", str(target)])
        info["dynamic_dependencies"] = [
            line.strip().split(" (compatibility")[0].strip()
            for line in output.splitlines()[1:] if line.strip()
        ]
        load_commands = _run("otool", ["-l", str(target)])
        match = re.search(r"\bminos\s+([0-9.]+)", load_commands)
        if not match:
            match = re.search(r"\bversion\s+([0-9.]+)", load_commands)
        if match:
            info["minimum_os_version"] = match.group(1)
    elif info["format"] == "PE":
        output = _run("objdump", ["-p", str(target)])
        info["dynamic_dependencies"] = re.findall(r"DLL Name:\s*(\S+)", output)
    return info


def normalize_arch(value: str) -> str:
    value = (value or "").lower()
    aliases = {
        "amd64": "x86_64", "x64": "x86_64", "x86-64": "x86_64",
        "aarch64": "arm64", "armv8": "arm64",
    }
    return aliases.get(value, value)


def expected_format(system: str) -> str:
    value = (system or "").lower()
    if value in ("linux", "bsd", "freebsd", "openbsd", "netbsd", "dragonfly"):
        return "ELF"
    if value in ("macos", "darwin"):
        return "Mach-O"
    if value in ("windows", "win32"):
        return "PE"
    return "unknown"


def host_compatibility(binary: Dict[str, Any], system: str, machine: str) -> Dict[str, Any]:
    expected = expected_format(system)
    actual = binary.get("format", "unknown")
    format_ok = (actual.startswith("Mach-O") if expected == "Mach-O" else actual == expected)
    archs = [normalize_arch(item) for item in binary.get("architectures", [])]
    host_arch = normalize_arch(machine)
    arch_ok = host_arch in archs
    return {
        "expected_format": expected,
        "format_matches": format_ok,
        "host_architecture": host_arch,
        "artifact_architectures": archs,
        "architecture_matches": arch_ok,
        "compatible": bool(format_ok and arch_ok),
    }


def host_description() -> Dict[str, str]:
    return {
        "system": host_platform.system(),
        "release": host_platform.release(),
        "machine": host_platform.machine(),
        "python": host_platform.python_version(),
    }
