"""Conservative, non-executing importer for legacy shell build scripts."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple


_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
_FEATURE_MAP = {
    "USE_HDF5": "hdf5", "USE_FFTW": "fftw", "USE_BOOST": "boost",
    "USE_STD_BESSELK": "std_besselk", "USE_BLOCK": "block",
    "USE_DIRECT_CONSTRUCTION": "direct", "USE_NO_TREE": "no_tree",
    "USE_QUADTREE": "quadtree", "USE_COMPLEX_PRECOND": "complex_precond",
    "USE_INTSIZE64": "int64", "USE_TASKLOOP": "taskloop",
    "USE_DOMM": "domm", "USE_EXT_FIELD": "ext_field",
    "USE_BICGSTAB": "bicgstab", "JECCD": "jec", "JEC2DIAG": "jec2diag",
    "USE_IMAS": "imas", "USE_CATALYST": "catalyst",
}
_VALUE_TRUE = {"1", "yes", "true", ".true.", "on", "enabled"}
_VALUE_FALSE = {"0", "no", "false", ".false.", "off", "disabled"}


def _simple_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return value


def import_legacy_text(text: str) -> Tuple[Dict[str, Any], List[str]]:
    """Extract only literal assignments; never evaluate shell expressions."""
    mapped: Dict[str, Any] = {"feature_states": {}}
    ignored: List[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ASSIGNMENT.match(line)
        if not match:
            ignored.append("%d: %s" % (number, stripped))
            continue
        name, raw = match.groups()
        value = _simple_value(raw)
        if any(token in value for token in ("$(", chr(96), "&&", "||", ";", "|")):
            ignored.append("%d: %s (shell expression not imported)" % (number, stripped))
            continue
        key = name.upper()
        if key in ("CC", "CXX", "FC", "MPIFC"):
            mapped[{"CC": "cc", "CXX": "cxx", "FC": "fc", "MPIFC": "fc"}[key]] = value
        elif key in ("MPI_PREFIX", "MPI_DIR", "MPICH_DIR"):
            mapped["mpi_prefix"] = value
        elif key in ("WORK", "WORKDIR", "BUILD_DIR"):
            mapped["work_dir"] = value
        elif key in ("MODEL", "JOREK_MODEL"):
            mapped["model"] = value
        elif key in ("JOBS", "NPROC", "MAKE_JOBS"):
            try:
                mapped["jobs"] = max(1, int(value))
            except ValueError:
                ignored.append("%d: invalid job count: %s" % (number, value))
        elif key in _FEATURE_MAP:
            normalized = value.lower()
            if normalized in _VALUE_TRUE:
                mapped["feature_states"][_FEATURE_MAP[key]] = True
            elif normalized in _VALUE_FALSE:
                mapped["feature_states"][_FEATURE_MAP[key]] = False
            else:
                ignored.append("%d: cannot interpret boolean %s=%s" % (number, key, value))
        elif key in ("USE_MUMPS", "USE_PASTIX", "USE_PASTIX6", "USE_PASTIX5",
                     "USE_STRUMPACK"):
            if value.lower() in _VALUE_TRUE:
                mapped["solver"] = {
                    "USE_MUMPS": "mumps", "USE_PASTIX": "pastix6",
                    "USE_PASTIX6": "pastix6", "USE_PASTIX5": "pastix5",
                    "USE_STRUMPACK": "strumpack",
                }[key]
        else:
            ignored.append("%d: %s (no safe GUI mapping)" % (number, stripped))
    return mapped, ignored
