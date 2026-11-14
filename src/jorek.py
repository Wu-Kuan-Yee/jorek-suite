#!/usr/bin/env python3
"""JOREK model/feature matrix, configuration generation, and builders."""

from __future__ import annotations

import itertools
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from common import (
    BuildError,
    PlatformInfo,
    copy_tree,
    ensure_dir,
    git_describe,
    output,
    read_text,
    run,
    sha256_file,
    which,
    write_text,
)


MODEL_RE = re.compile(r"model(\d{3})")
PROGRAM_RE = re.compile(r"(?im)^\s*program\s+([a-z_][a-z0-9_]*)\s*$")
MODULE_RE = re.compile(r"(?im)^\s*module\s+(?!procedure\b)([a-z_][a-z0-9_]*)\s*$")
USE_RE = re.compile(r"(?im)^\s*use\s*(?:,\s*[^:]+::)?\s*([a-z_][a-z0-9_]*)")


HARDCODED_PARAMS = (
    "n_tor", "n_coord_tor", "l_pol_domm", "n_period", "n_coord_period",
    "n_plane", "n_order", "n_nodes_max", "n_elements_max", "n_boundary_max",
    "n_pieces_max",
)

MODELS: Tuple[str, ...] = (
    "001", "002", "003", "004", "180", "183", "199", "303", "305", "306", "307",
    "333", "401", "500", "501", "502", "600", "710", "711", "712", "750",
)

FAMILY_TOGGLES: Dict[str, Tuple[str, ...]] = {
    "180": ("with_TiTe", "with_vpar"),
    "183": ("with_TiTe", "with_vpar"),
    "600": ("with_TiTe", "with_vpar", "with_rho", "with_neutrals", "with_impurities"),
    "750": ("with_TiTe", "with_neutrals", "with_impurities"),
}

#: Options that the JOREK makefile understands and that are safe to toggle
#: independently.  ``solver`` is handled separately.
BOOLEAN_FEATURES: Dict[str, Tuple[str, str]] = {
    "hdf5": ("USE_HDF5", "-DUSE_HDF5"),
    "fftw": ("USE_FFTW", "-DUSE_FFTW"),
    "boost": ("USE_BOOST", "-DUSE_BOOST"),
    "std_besselk": ("USE_STD_BESSELK", "-DUSE_STD_BESSELK"),
    "block": ("USE_BLOCK", "-DUSE_BLOCK"),
    "direct": ("USE_DIRECT_CONSTRUCTION", "-DDIRECT_CONSTRUCTION"),
    "no_tree": ("USE_NO_TREE", "-DUSE_NO_TREE"),
    "quadtree": ("USE_QUADTREE", "-DUSE_QUADTREE"),
    "complex_precond": ("USE_COMPLEX_PRECOND", "-DUSE_COMPLEX_PRECOND"),
    "int64": ("USE_INTSIZE64", "-DINTSIZE64"),
    "taskloop": ("USE_TASKLOOP", "-DUSE_TASKLOOP"),
    "domm": ("USE_DOMM", "-DUSE_DOMM"),
    "ext_field": ("USE_EXT_FIELD", "-DUSE_EXT_FIELD"),
    "bicgstab": ("USE_BICGSTAB", "-DUSE_BICGSTAB"),
    "jec": ("JECCD", "-DJECCD"),
    "jec2diag": ("JEC2DIAG", "-DJEC2DIAG"),
    "imas": ("USE_IMAS", "-DUSE_IMAS"),
    "catalyst": ("USE_CATALYST", "-DUSE_CATALYST"),
}

DEFAULT_FEATURES = {
    "hdf5": True,
    "fftw": True,
    "boost": True,
    "std_besselk": False,
    "block": True,
    "direct": True,
    "no_tree": False,
    "quadtree": True,
    "complex_precond": False,
    "int64": False,
    "taskloop": True,
    "domm": True,
    "ext_field": False,
    "bicgstab": False,
    "jec": False,
    "jec2diag": False,
    "imas": False,
    "catalyst": False,
    "murge": False,
}

SOLVERS = ("mumps", "pastix6", "pastix5", "strumpack", "wsmp", "hips")


@dataclass
class ModelInfo:
    model: str
    path: Path
    settings: Path
    text: str
    defaults: Dict[str, bool] = field(default_factory=dict)
    params: Dict[str, int] = field(default_factory=dict)
    toggles: Tuple[str, ...] = ()

    @property
    def number(self) -> int:
        return int(self.model)


def _parse_logicals(text: str) -> Dict[str, bool]:
    result = {}
    for match in re.finditer(r"(?im)^\s*logical\s*,\s*parameter\s*::\s*([a-z_][a-z0-9_]*)\s*=\s*\.(true|false)\.", text):
        result[match.group(1).lower()] = match.group(2).lower() == "true"
    return result


def _parse_integer_params(text: str) -> Dict[str, int]:
    result = {}
    for match in re.finditer(r"(?im)^\s*integer\s*,\s*parameter\s*::\s*([a-z_][a-z0-9_]*)\s*=\s*([0-9]+)", text):
        result[match.group(1).lower()] = int(match.group(2))
    return result


def discover_models(root: Path) -> Dict[str, ModelInfo]:
    models: Dict[str, ModelInfo] = {}
    base_settings = root / "models" / "mod_settings.f90"
    base_text = read_text(base_settings)
    base_params = _parse_integer_params(base_text)
    for directory in sorted((root / "models").glob("model*")):
        if not directory.is_dir():
            continue
        match = MODEL_RE.fullmatch(directory.name)
        if not match:
            continue
        model = match.group(1)
        settings = directory / "mod_model_settings.f90"
        text = read_text(settings)
        defaults = _parse_logicals(text)
        params = dict(base_params)
        params.update(_parse_integer_params(text))
        models[model] = ModelInfo(model, directory, settings, text, defaults, params, FAMILY_TOGGLES.get(model, ()))
    # Honour the model number declared in the source rather than the directory
    # name if they ever diverge.
    for model, info in list(models.items()):
        found = re.search(r"(?im)^\s*integer\s*,\s*parameter\s*::\s*jorek_model\s*=\s*(\d+)", info.text)
        if found and found.group(1).lstrip("0") != model.lstrip("0"):
            models[found.group(1)] = info
    return models


def parse_makefile_programs(root: Path) -> List[str]:
    text = read_text(root / "Makefile")
    match = re.search(r"(?ms)^most:\s*(.*?)(?=^\S|\Z)", text)
    if not match:
        return []
    body = match.group(1).replace("\\\n", " ")
    return re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", body)


def discover_programs(root: Path, dirs: Sequence[str]) -> Dict[str, Path]:
    """Map executable stem -> source file for all PROGRAM sources in DIRS."""
    result: Dict[str, Path] = {}
    for directory in dirs:
        path = root / directory
        if not path.is_dir():
            continue
        for source in sorted(path.glob("*.f90")):
            text = read_text(source, "")
            match = PROGRAM_RE.search(text)
            if match:
                result.setdefault(match.group(1), source)
    return result


def parse_makefile_dirs(root: Path, model: str) -> List[str]:
    text = read_text(root / "Makefile")
    match = re.search(r"(?ms)^DIRS\s*:=\s*(.*?)(?=^[A-Za-z_$]|\Z)", text)
    if not match:
        return ["."]
    body = match.group(1).replace("\\\n", " ")
    body = body.replace("$(MODEL)", "model" + model)
    dirs = []
    for token in body.split():
        token = token.strip()
        if not token or token.startswith("#"):
            continue
        if "$(" in token or token.startswith("-"):
            continue
        if (root / token).is_dir():
            dirs.append(token)
    return dirs or ["."]


@dataclass
class FeatureSet:
    solver: str = "mumps"
    values: Dict[str, bool] = field(default_factory=lambda: dict(DEFAULT_FEATURES))
    debug: bool = False

    def enable(self, name: str, value: bool = True) -> None:
        if name not in self.values and name != "debug":
            raise BuildError("unknown feature: %s" % name)
        if name == "debug":
            self.debug = value
        else:
            self.values[name] = value

    def enabled(self, name: str) -> bool:
        return bool(self.values.get(name, False))

    def validate(self) -> None:
        if self.values.get("boost") and self.values.get("std_besselk"):
            raise BuildError("features 'boost' and 'std_besselk' are mutually exclusive")
        if self.values.get("bicgstab") and self.values.get("gmres"):
            raise BuildError("features 'bicgstab' and 'gmres' are mutually exclusive")
        if self.values.get("domm") and self.values.get("ext_field"):
            raise BuildError("features 'domm' and 'ext_field' are mutually exclusive")
        if self.values.get("no_tree") and self.values.get("quadtree"):
            raise BuildError("features 'no_tree' and 'quadtree' are mutually exclusive")
        if self.solver not in SOLVERS + ("all", "none"):
            raise BuildError("unknown solver: %s" % self.solver)
        if self.solver == "all":
            if self.values.get("pastix5") and self.values.get("pastix6"):
                raise BuildError("PaStiX 5 and PaStiX 6 cannot be selected together")
        if self.solver == "pastix5" and self.values.get("pastix6"):
            raise BuildError("PaStiX 5 and PaStiX 6 cannot be selected together")

    def feature_tokens(self) -> List[str]:
        tokens = []
        for name in sorted(self.values):
            if self.values[name]:
                tokens.append(name)
        if self.solver not in ("none",):
            tokens.append(self.solver)
            tokens.append("solver=" + self.solver)
        if self.debug:
            tokens.append("debug")
        return tokens

    def make_flags(self) -> List[str]:
        flags = []
        mapping = {
            "hdf5": "-DUSE_HDF5",
            "fftw": "-DUSE_FFTW",
            "boost": "-DUSE_BOOST",
            "std_besselk": "-DUSE_STD_BESSELK",
            "block": "-DUSE_BLOCK",
            "direct": "-DDIRECT_CONSTRUCTION",
            "no_tree": "-DUSE_NO_TREE",
            "quadtree": "-DUSE_QUADTREE",
            "complex_precond": "-DUSE_COMPLEX_PRECOND",
            "int64": "-DINTSIZE64",
            "taskloop": "-DUSE_TASKLOOP",
            "domm": "-DUSE_DOMM",
            "ext_field": "-DUSE_EXT_FIELD",
            "bicgstab": "-DUSE_BICGSTAB",
            "jec": "-DJECCD",
            "jec2diag": "-DJEC2DIAG",
            "imas": "-DUSE_IMAS",
            "catalyst": "-DUSE_CATALYST",
        }
        for name, flag in mapping.items():
            if self.values.get(name):
                flags.append(flag)
        return flags


@dataclass
class VariantSpec:
    name: str
    model: str
    features: FeatureSet
    params: Dict[str, int]
    toggles: Dict[str, bool]
    programs: Tuple[str, ...] = ("jorek2_main",)
    overrides: Dict[str, int] = field(default_factory=dict)
    source_root: Optional[Path] = None

    def param_tokens(self) -> List[str]:
        tokens = ["%s=%s" % (key, self.params[key]) for key in sorted(self.params)]
        for key in sorted(self.toggles):
            tokens.append("%s=%d" % (key, 1 if self.toggles[key] else 0))
        return tokens

    def build_key(self) -> str:
        import hashlib
        payload = "\n".join([
            "model=" + self.model,
            "solver=" + self.features.solver,
            "features=" + ",".join("%s=%d" % (k, 1 if v else 0) for k, v in sorted(self.features.values.items())),
            "debug=%d" % (1 if self.features.debug else 0),
            "params=" + ",".join("%s=%s" % (k, self.params[k]) for k in sorted(self.params)),
            "toggles=" + ",".join("%s=%d" % (k, 1 if self.toggles[k] else 0) for k in sorted(self.toggles)),
            "programs=" + ",".join(self.programs),
        ])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def safe_name(self) -> str:
        base = "model" + self.model
        extras = []
        for key in ("with_TiTe", "with_vpar", "with_rho", "with_neutrals", "with_impurities"):
            if key in self.toggles:
                extras.append(key.replace("with_", "").lower() + ("1" if self.toggles[key] else "0"))
        for key in sorted(self.overrides):
            extras.append("%s%s" % (key, self.overrides[key]))
        extras.append(self.features.solver)
        if self.features.debug:
            extras.append("debug")
        name = base
        if extras:
            name += "-" + "-".join(extras)
        return name


def make_variant(
    model: str,
    models: Dict[str, ModelInfo],
    features: FeatureSet,
    params: Optional[Dict[str, int]] = None,
    toggles: Optional[Dict[str, bool]] = None,
    programs: Sequence[str] = ("jorek2_main",),
) -> VariantSpec:
    if model not in models:
        raise BuildError("unknown JOREK model: %s" % model)
    effective = replace(features, values=dict(features.values))
    # The stellarator equilibrium path in models 180/183 still calls the
    # R-tree API even when USE_QUADTREE is selected.  That upstream mismatch
    # makes the quadtree variant unlinkable; use the R-tree path instead.
    if model in ("180", "183"):
        effective.values["quadtree"] = False
        effective.values["no_tree"] = False
    info = models[model]
    merged_params = {k: v for k, v in info.params.items() if k in HARDCODED_PARAMS}
    if params:
        for key, value in params.items():
            if key not in HARDCODED_PARAMS:
                raise BuildError("unknown hard-coded parameter %s (known: %s)" % (key, ", ".join(HARDCODED_PARAMS)))
            merged_params[key] = int(value)
    merged_toggles = {key: bool(info.defaults.get(key.lower(), False)) for key in info.toggles}
    if toggles:
        for key, value in toggles.items():
            if key not in merged_toggles:
                raise BuildError("model %s has no extension '%s' (available: %s)" % (model, key, ", ".join(info.toggles)))
            merged_toggles[key] = bool(value)
    if features.enabled("imas") and features.enabled("catalyst"):
        pass
    spec = VariantSpec(
        name="",
        model=model,
        features=effective,
        params=merged_params,
        toggles=merged_toggles,
        programs=tuple(programs),
        overrides={k: int(v) for k, v in (params or {}).items() if info.params.get(k) != int(v)},
    )
    spec.name = spec.safe_name()
    return spec


def complete_variants(
    models: Dict[str, ModelInfo],
    features: FeatureSet,
    programs: Sequence[str] = ("jorek2_main",),
    include_families: bool = True,
) -> List[VariantSpec]:
    variants: List[VariantSpec] = []
    for model in sorted(models):
        info = models[model]
        if include_families and info.toggles:
            # with_refluid is declared but explicitly unsupported by the model;
            # do not generate builds that the source itself rejects.
            toggles = [t for t in info.toggles if t.lower() != "with_refluid"]
            for values in itertools.product((False, True), repeat=len(toggles)):
                mapping = dict(zip(toggles, values))
                spec = make_variant(model, models, replace(features), toggles=mapping, programs=programs)
                spec.name = spec.safe_name()
                variants.append(spec)
        else:
            variants.append(make_variant(model, models, replace(features), programs=programs))
    return variants


# ---------------------------------------------------------------------------
# Source staging and parameter rewriting
# ---------------------------------------------------------------------------
def _replace_declaration(text: str, name: str, value: str) -> Tuple[str, bool]:
    pattern = re.compile(
        r"(?im)^(\s*(?:integer|logical|real|character)[^\n:]*::\s*" + re.escape(name) + r"\s*=\s*)([^!\n]+?)(\s*(?:!.*)?)$"
    )
    found = False

    def repl(match: re.Match) -> str:
        nonlocal found
        found = True
        old = match.group(2).strip()
        return match.group(1) + value + match.group(3)

    new = pattern.sub(repl, text)
    return new, found


def patch_settings(source: Path, spec: VariantSpec) -> None:
    settings = source / "models" / "mod_settings.f90"
    text = read_text(settings)
    for key, value in spec.params.items():
        if key in ("n_tor", "n_coord_tor", "l_pol_domm", "n_period", "n_coord_period",
                   "n_plane", "n_order", "n_nodes_max", "n_elements_max", "n_boundary_max",
                   "n_pieces_max"):
            text, ok = _replace_declaration(text, key, str(value))
            if not ok:
                raise BuildError("could not set %s in models/mod_settings.f90" % key)
    if int(spec.params.get("n_order", 3)) % 2 == 0:
        raise BuildError("n_order must be odd (got %s)" % spec.params.get("n_order"))
    n_tor = int(spec.params.get("n_tor", 1))
    n_plane = int(spec.params.get("n_plane", 1))
    if n_plane < 2 * (n_tor - 1):
        raise BuildError("n_plane=%d is too small for n_tor=%d (need n_plane >= %d)" % (n_plane, n_tor, 2 * (n_tor - 1)))
    write_text(settings, text)

    model_settings = source / "models" / ("model" + spec.model) / "mod_model_settings.f90"
    model_text = read_text(model_settings)
    for key, value in spec.toggles.items():
        model_text, ok = _replace_declaration(model_text, key, ".true." if value else ".false.")
        if not ok:
            raise BuildError("could not set %s in %s" % (key, model_settings))
    write_text(model_settings, model_text)


def _apply_source_compatibility_fixes(source: Path, spec: VariantSpec) -> None:
    """Apply narrowly scoped fixes for known upstream model-source defects.

    These are not feature switches: they remove duplicate declarations that a
    strict modern compiler rejects.  Each fix is tied to a model and is logged
    in docs/VARIANTS.md.
    """
    if spec.model == "001":
        path = source / "models" / "model001" / "mod_elt_matrix.f90"
        text = read_text(path)
        duplicate = "type (type_node),optional :: aux_nodes(n_vertex_max)\n"
        if duplicate in text:
            text = text.replace(duplicate, "", 1)
            write_text(path, text)
    if spec.features.solver in ("pastix6", "pastix5", "all"):
        path = source / "solvers" / "ptx.cpp"
        text = read_text(path)
        if "#include <mkl_spblas.h>" in text:
            text = text.replace("#include <mkl_spblas.h>\n#include <mkl.h>\n", "")
        # PaStiX 6.3 added the leading dimension to task_solve.
        text = text.replace(
            "pastix_task_solve(pastix_data, nrhs, x, spm->n);",
            "pastix_task_solve(pastix_data, spm->n, nrhs, x, spm->n);")
        write_text(path, text)
    if spec.features.solver in ("strumpack", "all"):
        path = source / "solvers" / "spk.cpp"
        text = read_text(path)
        if "#include <sys/sysinfo.h>" in text:
            write_text(path, text.replace(
                "#include <sys/sysinfo.h>",
                "#ifndef __APPLE__\n#include <sys/sysinfo.h>\n#endif"))
    if spec.model == "712":
        path = source / "models" / "model712" / "initialise_parameters.f90"
        text = read_text(path)
        if "eta_ARAZ_on == .true." in text:
            write_text(path, text.replace("eta_ARAZ_on == .true.", "eta_ARAZ_on .eqv. .true."))
    if spec.model in ("401", "502", "711", "712"):
        replacements = {
            "diagnostics/integrals.f90": ("""#else
        call sources(xpoint, xcase, y_g(ms,mt), Z_xpoint, eq_g(var_psi,ms,mt), psi_axis, &
          psi_limit, part_src, heat_src)
#endif""", """#elif (JOREK_MODEL == 401) || (JOREK_MODEL == 502) || (JOREK_MODEL == 711) || (JOREK_MODEL == 712)
        call sources(xpoint, xcase, y_g(ms,mt), Z_xpoint, eq_g(var_psi,ms,mt), psi_axis, &
          psi_limit, part_src, heat_src_i, heat_src_e)
        heat_src = heat_src_i + heat_src_e
#else
        call sources(xpoint, xcase, y_g(ms,mt), Z_xpoint, eq_g(var_psi,ms,mt), psi_axis, &
          psi_limit, part_src, heat_src)
#endif"""),
            "diagnostics/mod_integrals3D.f90": ("""#else
        call sources(xpoint, xcase, y_g(mp,ms,mt), Z_xpoint, psi_as_coord, psi_axis, psi_bnd, &
                     particle_source,heat_source)
#endif""", """#elif (JOREK_MODEL == 401) || (JOREK_MODEL == 502) || (JOREK_MODEL == 711) || (JOREK_MODEL == 712)
        call sources(xpoint, xcase, y_g(mp,ms,mt), Z_xpoint, psi_as_coord, psi_axis, psi_bnd, &
                     particle_source,heat_source_i,heat_source_e)
        heat_source = heat_source_i + heat_source_e
#else
        call sources(xpoint, xcase, y_g(mp,ms,mt), Z_xpoint, psi_as_coord, psi_axis, psi_bnd, &
                     particle_source,heat_source)
#endif"""),
            "diagnostics/integrals_3D.f90": ("""#else
        call sources(xpoint, xcase, y_g(ms,mt), ES%Z_xpoint, ps0, ES%psi_axis, psi_limit, &
                     particle_source,heat_source)
#endif""", """#elif (JOREK_MODEL == 401) || (JOREK_MODEL == 502) || (JOREK_MODEL == 711) || (JOREK_MODEL == 712)
        call sources(xpoint, xcase, y_g(ms,mt), ES%Z_xpoint, ps0, ES%psi_axis, psi_limit, &
                     particle_source,heat_source_i,heat_source_e)
        heat_source = heat_source_i + heat_source_e
#else
        call sources(xpoint, xcase, y_g(ms,mt), ES%Z_xpoint, ps0, ES%psi_axis, psi_limit, &
                     particle_source,heat_source)
#endif"""),
        }
        for relative, (old, replacement) in replacements.items():
            path = source / relative
            text = read_text(path)
            if old in text:
                write_text(path, text.replace(old, replacement, 1))
    if spec.model == "333":
        path = source / "models" / "model333" / "construct_variables.f90"
        text = read_text(path)
        # Fortran is case-insensitive: the dummy VAR_t collides with the
        # model parameter var_T imported from mod_parameters.  Rename the
        # dummy argument consistently; all calls are positional.
        if "VAR_t" in text:
            write_text(path, text.replace("VAR_t", "VAR_t_d"))
    if spec.model == "306":
        path = source / "matrix" / "construct_matrix_mod.f90"
        text = read_text(path)
        text = text.replace(
            "        thread_struct(omp_tid)%delta_s, thread_struct(omp_tid)%delta_t, i_tor_min, i_tor_max,      &\n        aux_nodes, thread_struct(omp_tid)%ELM_pnn)",
            "        thread_struct(omp_tid)%delta_s, thread_struct(omp_tid)%delta_t, i_tor_min, i_tor_max,      &\n        thread_struct(omp_tid)%ELM_pnn)")
        text = text.replace(
            "        i_tor_min, i_tor_max, aux_nodes)\n    endif",
            "        i_tor_min, i_tor_max)\n    endif")
        write_text(path, text)
    if spec.model in ("305", "306"):
        path = source / "models" / ("model" + spec.model) / "initial_conditions.f90"
        text = read_text(path)
        if ":: Omega" not in text.split("contains")[0]:
            marker = "real*8     :: amplitude, psi, psi_n, theta\n"
            declaration = ("real*8     :: Omega, dOmega_dpsi, dOmega_dz, dOmega_dpsi2, "
                           "dOmega_dz2, dOmega_dpsi_dz\n")
            if marker in text:
                text = text.replace(marker, marker + declaration, 1)
                write_text(path, text)


def stage_source(jorek_root: Path, destination: Path, spec: VariantSpec) -> Path:
    destination = ensure_dir(destination)
    marker = destination / ".jorek-variant"
    if marker.exists() and marker.read_text(encoding="utf-8", errors="replace").strip() == spec.build_key():
        return destination
    if any(destination.iterdir()):
        shutil.rmtree(destination)
        ensure_dir(destination)
    copy_tree(jorek_root, destination)
    # Ensure the mutable configuration files are independent copies, not hard
    # links into the original checkout.
    for relative in ("models/mod_settings.f90", "models/model%s/mod_model_settings.f90" % spec.model):
        source = jorek_root / relative
        target = destination / relative
        if target.exists():
            target.unlink()
        shutil.copy2(source, target)
    patch_settings(destination, spec)
    _apply_source_compatibility_fixes(destination, spec)
    marker = destination / ".jorek-variant"
    marker.write_text(spec.build_key() + "\n", encoding="utf-8")
    return destination


# ---------------------------------------------------------------------------
# Dependency layout for the makefile
# ---------------------------------------------------------------------------
class DepLayout:
    """Locate headers and static libraries supplied by deps.py or the host."""

    def __init__(
        self,
        prefixes: Sequence[Path],
        platform: PlatformInfo,
        mpi_prefix: Optional[Path] = None,
        imas_root: Optional[Path] = None,
        catalyst_root: Optional[Path] = None,
    ) -> None:
        self.prefixes = [Path(p) for p in prefixes]
        self.platform = platform
        self.mpi_prefix = Path(mpi_prefix) if mpi_prefix else None
        self.imas_root = Path(imas_root) if imas_root else None
        self.catalyst_root = Path(catalyst_root) if catalyst_root else None

    def find(self, relative: str) -> Optional[Path]:
        for prefix in self.prefixes:
            candidate = prefix / relative
            if candidate.exists():
                return candidate
            hits = list(prefix.rglob(Path(relative).name))
            for hit in hits:
                if hit.is_file():
                    return hit
        return None

    def lib(self, name: str) -> Path:
        # Prefer the conventional lib directories.  Do not recursively scan
        # arbitrary source trees: a source checkout may contain unrelated,
        # empty test archives with the same basename.
        for prefix in self.prefixes:
            for sub in ("lib", "lib64", "lib/static", "."):
                candidate = prefix / sub / name
                if candidate.exists() and candidate.stat().st_size > 8:
                    return candidate
        hits = []
        for prefix in self.prefixes:
            for sub in ("lib", "lib64"):
                base = prefix / sub
                if base.exists():
                    hits.extend(base.rglob(name))
        if hits:
            for hit in hits:
                if hit.stat().st_size > 8 and "rust" not in str(hit).lower() and "vcpkg" not in str(hit).lower():
                    return hit
        raise BuildError("static library %s not found in dependency prefixes: %s" % (name, ", ".join(map(str, self.prefixes))))

    def include(self, name: str) -> Optional[Path]:
        for prefix in self.prefixes:
            candidate = prefix / "include" / name
            if candidate.exists():
                return candidate
            hits = list((prefix / "include").rglob(name)) if (prefix / "include").exists() else []
            if hits:
                return hits[0]
        return None

    def mpi_include(self) -> Path:
        if self.mpi_prefix and (self.mpi_prefix / "include" / "mpi.h").exists():
            return self.mpi_prefix / "include"
        found = self.find("include/mpi.h") or self.find("mpi.h")
        if found:
            return found.parent
        raise BuildError("MPI headers not found; pass --mpi-prefix or build mpich")

    def mpi_libdir(self) -> Path:
        if self.mpi_prefix and any((self.mpi_prefix / "lib").glob("libmpi.*")):
            return self.mpi_prefix / "lib"
        found = self.find("lib/libmpi.a") or self.find("libmpi.a")
        if found:
            return found.parent
        raise BuildError("MPI libraries not found; pass --mpi-prefix or build mpich")

    def hdf5_include(self) -> Path:
        found = self.include("hdf5.h")
        if not found:
            raise BuildError("HDF5 headers not found")
        return found.parent

    def fftw_include(self) -> Path:
        found = self.include("fftw3.h")
        if not found:
            raise BuildError("FFTW headers not found")
        return found.parent

    def fftw_prefix(self) -> Path:
        return self.fftw_include().parent

    def mumps_include(self) -> Path:
        found = self.include("dmumps_struc.h")
        if not found:
            raise BuildError("MUMPS headers not found")
        return found.parent

    def zlib_lib(self) -> Optional[Path]:
        try:
            return self.lib("libz.a")
        except BuildError:
            return None

    def static_libs(self, names: Sequence[str]) -> List[str]:
        result = []
        for name in names:
            result.append(str(self.lib(name)))
        return result


# ---------------------------------------------------------------------------
# Make-based builder
# ---------------------------------------------------------------------------
class MakeBuilder:
    def __init__(
        self,
        jorek_root: Path,
        spec: VariantSpec,
        layout: DepLayout,
        fc: str,
        cc: str,
        cxx: str,
        jobs: int = 1,
        keep_source: bool = False,
        dry_run: bool = False,
        log_dir: Optional[Path] = None,
        extra_libs: Sequence[str] = (),
        full_static: bool = False,
    ) -> None:
        self.jorek_root = jorek_root
        self.spec = spec
        self.layout = layout
        self.fc = fc
        self.cc = cc
        self.cxx = cxx
        self.jobs = max(1, jobs)
        self.keep_source = keep_source
        self.dry_run = dry_run
        self.log_dir = log_dir
        self.extra_libs = list(extra_libs)
        self.full_static = full_static
        if full_static and not layout.platform.is_macos:
            # -static already statically links libgfortran/libgcc/libstdc++.
            # The -static-lib* driver flags inject -Bdynamic afterwards and
            # break the final implicit -lm/-lc resolution.
            self.extra_libs = [
                item for item in self.extra_libs
                if not item.startswith("-static-lib")
            ]
        self.fc_family = self._compiler_family(fc)

    @staticmethod
    def _compiler_family(compiler: str) -> str:
        try:
            proc = subprocess.run([compiler, "--version"], stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, check=False)
            text = proc.stdout.decode("utf-8", "replace").lower()
        except OSError:
            return "unknown"
        if "intel" in text or "ifort" in text or "oneapi" in text:
            return "intel"
        if "gnu" in text or "gcc" in text or "gfortran" in text:
            return "gnu"
        if "nvidia" in text or "nvfortran" in text:
            return "nvidia"
        return "unknown"

    def makefile_inc(self) -> str:
        spec = self.spec
        feature = spec.features
        layout = self.layout
        model_number = spec.model
        lines = [
            "# Generated by build-jorek/build.py -- do not edit.",
            "MODEL = model%s" % model_number,
            "FC = %s" % self.fc,
            "CC = %s" % self.cc,
            "CXX = %s" % self.cxx,
            "DEBUG = %d" % (1 if feature.debug else 0),
            "",
        ]
        if self.fc_family == "intel":
            lines.append("FFLAGS += -r8 -O3")
            if feature.debug:
                lines[-1] = "FFLAGS += -O0 -g -check all -traceback"
        elif self.fc_family == "nvidia":
            lines.append("FFLAGS += -r8 -O3")
        else:
            lines.append("FFLAGS += -fdefault-real-8 -fdefault-double-8 -ffree-line-length-none -fallow-argument-mismatch")
            if feature.debug:
                lines[-1] += " -g -O0 -fcheck=all -fbacktrace"
            else:
                lines[-1] += " -O3"
        lines.append("CFLAGS += -O2 -fPIC")
        lines.append("CXXFLAGS += -O2 -fPIC")
        lines.append("LIBS += -lm")
        lines.append("")
        solver = feature.solver
        lines.append("USE_HIPS = %d" % (1 if solver == "hips" else 0))
        lines.append("USE_PASTIX = %d" % (1 if solver == "pastix5" else 0))
        lines.append("USE_PASTIX_MURGE = %d" % (1 if solver == "pastix5" and feature.enabled("murge") else 0))
        lines.append("USE_PASTIX6 = %d" % (1 if solver in ("pastix6", "all") else 0))
        lines.append("USE_MUMPS = %d" % (1 if solver in ("mumps", "all") else 0))
        lines.append("USE_WSMP = %d" % (1 if solver == "wsmp" else 0))
        lines.append("USE_STRUMPACK = %d" % (1 if solver in ("strumpack", "all") else 0))
        lines.append("USE_HDF5 = %d" % (1 if feature.enabled("hdf5") else 0))
        lines.append("USE_FFTW = %d" % (1 if feature.enabled("fftw") else 0))
        lines.append("USE_DOMM = %d" % (1 if feature.enabled("domm") else 0))
        lines.append("USE_EXT_FIELD = %d" % (1 if feature.enabled("ext_field") else 0))
        lines.append("USE_BLOCK = %d" % (1 if feature.enabled("block") else 0))
        lines.append("USE_NO_TREE = %d" % (1 if feature.enabled("no_tree") else 0))
        lines.append("USE_QUADTREE = %d" % (1 if feature.enabled("quadtree") else 0))
        lines.append("USE_DIRECT_CONSTRUCTION = %d" % (1 if feature.enabled("direct") else 0))
        lines.append("USE_COMPLEX_PRECOND = %d" % (1 if feature.enabled("complex_precond") else 0))
        lines.append("USE_INTSIZE64 = %d" % (1 if feature.enabled("int64") else 0))
        lines.append("USE_BOOST = %d" % (1 if feature.enabled("boost") else 0))
        lines.append("USE_STD_BESSELK = %d" % (1 if feature.enabled("std_besselk") else 0))
        lines.append("USE_BICGSTAB = %d" % (1 if feature.enabled("bicgstab") else 0))
        lines.append("USE_TASKLOOP = %d" % (1 if feature.enabled("taskloop") else 0))
        lines.append("")
        lines.append("INCLUDES += -I%s" % layout.mpi_include())
        mpi_libdir = layout.mpi_libdir()
        mpi_archives = []
        for name in ("libmpifort.a", "libmpi.a", "libpmpi.a"):
            candidate = mpi_libdir / name
            if candidate.exists():
                mpi_archives.append(str(candidate))
        if mpi_archives:
            lines.append("LIBS += %s" % " ".join(mpi_archives))
        else:
            lines.append("LIBS += -L%s -lmpi -lmpifort" % mpi_libdir)
        # HDF5
        if feature.enabled("hdf5"):
            hdf5_libs = ["libhdf5_hl_fortran.a", "libhdf5_fortran.a", "libhdf5_hl.a",
                         "libhdf5.a", "libhdf5hl_fortran.a"]
            selected = []
            for name in hdf5_libs:
                try:
                    selected.append(str(layout.lib(name)))
                except BuildError:
                    pass
            # Preserve ordering and remove duplicates.
            dedup = []
            for item in selected:
                if item not in dedup:
                    dedup.append(item)
            lines.append("HDF5INCLUDE = %s" % layout.hdf5_include())
            lines.append("HDF5LIB = %s" % " ".join(dedup))
            zlib = layout.zlib_lib()
            if zlib:
                lines.append("HDF5LIB += %s" % zlib)
            for extra in ("libsz.a", "libaec.a"):
                try:
                    lines.append("HDF5LIB += %s" % layout.lib(extra))
                except BuildError:
                    pass
            lines.append("HDF5LIB += -ldl")
        else:
            lines.append("USE_HDF5 = 0")
        # FFTW
        if feature.enabled("fftw"):
            lines.append("USE_FFTW = 1")
            lines.append("FFTW_HOME = %s" % layout.fftw_prefix())
            lines.append("INC_FFTW = -I%s" % layout.fftw_include())
            fftw = []
            for name in ("libfftw3.a", "libfftw3_threads.a", "libfftw3_omp.a"):
                try:
                    fftw.append(str(layout.lib(name)))
                except BuildError:
                    pass
            lines.append("LIBFFTW = %s" % " ".join(fftw))
        else:
            lines.append("USE_FFTW = 0")
        # Linear algebra
        blas = layout.lib("libopenblas.a")
        lines.append("LIBLAPACK = %s" % blas)
        lines.append("LIBBLAS =")
        # Solver-specific block
        if solver in ("mumps", "all"):
            lines.extend(self._mumps_block())
        if solver in ("pastix6", "all"):
            lines.extend(self._pastix6_block())
        if solver == "pastix5":
            lines.extend(self._pastix5_block())
        if solver in ("strumpack", "all"):
            lines.extend(self._strumpack_block())
        if solver == "wsmp":
            try:
                lines.append("LIB_WSMP = %s" % layout.lib("libpwsmp64.a"))
            except BuildError:
                lines.append("# WSMP is proprietary; supply libpwsmp64.a in a --dep-prefix")
        if solver == "hips":
            try:
                lines.append("LIBHIPS = %s" % layout.lib("libhips.a"))
                lines.append("INCHIPS = -I%s" % (layout.include("hips.h").parent if layout.include("hips.h") else ""))
            except BuildError:
                lines.append("# HIPS is legacy; supply libhips.a in a --dep-prefix")
        if solver == "none":
            lines.append("# No direct solver backend selected; JOREK needs one at run time.")
        if feature.enabled("boost"):
            try:
                lines.append("LIB_BOOST = %s" % layout.lib("libboost_math_tr1.a"))
                lines.append("INC_BOOST = -I%s" % layout.include("boost/math/special_functions/bessel.hpp").parents[2])
            except (BuildError, AttributeError):
                pass
        # Feature defines handled by defaults.mk for the standard switches; add
        # the ones that defaults.mk has no variable for.
        extra = []
        for name in ("jec", "jec2diag"):
            if feature.enabled(name):
                extra.append(BOOLEAN_FEATURES[name][1])
        if extra:
            lines.append("FFLAGS += %s" % " ".join(extra))
        if feature.enabled("imas"):
            if not layout.imas_root:
                raise BuildError("IMAS feature requested but --imas-root was not supplied")
            lines.append("USE_IMAS = 1")
            lines.append("IMASINCLUDE = -I%s/include" % layout.imas_root)
            lines.append("IMASLIB = -L%s/lib -limas" % layout.imas_root)
        if feature.enabled("catalyst"):
            if not layout.catalyst_root:
                raise BuildError("Catalyst feature requested but --catalyst-root was not supplied")
            lines.append("USE_CATALYST = 1")
            lines.append("CATALYSTINCLUDE = -I%s/include" % layout.catalyst_root)
            lines.append("CATALYSTLIB = -L%s/lib -lcatalyst" % layout.catalyst_root)
        try:
            lines.append("LIBS += %s" % layout.lib("libomp.a"))
        except BuildError:
            pass
        if self.extra_libs:
            lines.append("LIBS += %s" % " ".join(self.extra_libs))
        for name in ("libhwloc.a", "libpciaccess.a"):
            try:
                lines.append("LIBS += %s" % layout.lib(name))
            except BuildError:
                pass
        if layout.platform.is_macos:
            lines.append("LIBS += -lxml2 -llzma -framework IOKit -framework CoreFoundation -framework OpenCL")
        if self.full_static and not layout.platform.is_macos:
            # The link rule is $(FC) $(FLAGS) ... $(LIBS), with the compiler's
            # implicit -lm before LIBS.  Put -static in FLAGS so it also covers
            # the compiler's own libc/libm references; putting it at the end of
            # LIBS makes the linker resolve -lm dynamically first and then fail
            # with "attempted static link of dynamic object".
            lines.append("FLAGS += -static")
        if layout.platform.is_windows:
            lines.append("LIBS += -static-libgcc -static-libstdc++")
        lines.append("LIBS += %s -lstdc++" % ("-qopenmp" if self.fc_family == "intel" else "-fopenmp"))
        lines.append("")
        return "\n".join(lines)

    def _mumps_block(self) -> List[str]:
        layout = self.layout
        libs = layout.static_libs(("libdmumps.a", "libmumps_common.a", "libpord.a", "libscalapack.a"))
        lines = [
            "LIB_MUMPS = %s" % " ".join(libs[:2]),
            "ORDLIB = %s" % libs[2],
            "SCALAP = %s" % libs[3],
            "BLACS =",
            "INC_MUMPS = %s" % layout.mumps_include(),
        ]
        for name in ("libmetis.a", "libscotch.a", "libscotcherr.a", "libesmumps.a"):
            try:
                lines.append("LIBS += %s" % layout.lib(name))
            except BuildError:
                pass
        return lines

    def _pastix6_block(self) -> List[str]:
        layout = self.layout
        names = ("libpastix.a", "libspm.a", "libpastix_kernels.a",
                 "libmetis.a", "libGKlib.a", "libscalapack.a",
                 "libopenblas.a", "libomp.a", "libhwloc.a")
        libs = []
        for name in names:
            try:
                libs.append(str(layout.lib(name)))
            except BuildError:
                pass
        lines = [
            "PASTIX6_HOME = %s" % (layout.prefixes[0] if layout.prefixes else ""),
            "LIB_PASTIX = %s" % " ".join(libs),
            "LIB_PASTIX6_BLAS = %s" % layout.lib("libopenblas.a"),
            "INC_PASTIX = -I%s -I%s" % (
                layout.include("pastix.h").parent if layout.include("pastix.h") else "",
                layout.include("spm.h").parent if layout.include("spm.h") else ""),
            "USE_PASTIX6 = 1",
        ]
        return lines

    def _pastix5_block(self) -> List[str]:
        layout = self.layout
        libs = []
        for name in ("libpastix.a", "libmetis.a", "libopenblas.a",
                     "libomp.a", "libhwloc.a"):
            try:
                libs.append(str(layout.lib(name)))
            except BuildError:
                pass
        inc = layout.include("pastix_fortran.h") or layout.include("pastix.h")
        murge = []
        try:
            murge.append(str(layout.lib("libpastix_murge.a")))
        except BuildError:
            pass
        return [
            "USE_PASTIX = 1",
            "PASTIX_MEMORY_USAGE = 0",
            "LIB_PASTIX = %s" % " ".join(libs),
            "LIB_PASTIX_MURGE = %s" % " ".join(murge),
            "LIB_PASTIX_BLAS = %s" % layout.lib("libopenblas.a"),
            "INC_PASTIX = -I%s" % (inc.parent if inc else ""),
        ]

    def _strumpack_block(self) -> List[str]:
        layout = self.layout
        lib = layout.lib("libstrumpack.a")
        inc = layout.include("StrumpackSparseSolverMPIDist.hpp").parent
        libs = [str(lib)]
        # STRUMPACK is built on METIS/GKlib, ScaLAPACK, BLAS and its OpenMP
        # runtime; static archives need these on the final link line.
        for name in ("libmetis.a", "libGKlib.a", "libscalapack.a",
                     "libopenblas.a", "libomp.a"):
            try:
                libs.append(str(layout.lib(name)))
            except BuildError:
                pass
        return [
            "USE_STRUMPACK = 1",
            "STRUMPACKLIB = %s" % " ".join(libs),
            "STRUMPACKINC = -I%s" % inc,
            # Put ScaLAPACK before OpenBLAS so the LAPACK auxiliary symbols
            # come from one archive rather than triggering a duplicate in
            # OpenBLAS (notably dcombssq).
            "LIBLAPACK = %s %s" % (
                layout.lib("libscalapack.a"), layout.lib("libopenblas.a")),
        ]

    def configure(self, source: Path) -> None:
        write_text(source / "Makefile.inc", self.makefile_inc())
        if self.fc_family == "gnu":
            # defaults.mk unconditionally adds the Intel-only -heap-arrays flag
            # for the stellarator models.  GNU Fortran rejects it (and reports
            # the confusing "-h" error).  Strip it only for GNU toolchains.
            defaults = source / "defaults.mk"
            if defaults.exists():
                text = read_text(defaults)
                if "-heap-arrays" in text:
                    write_text(defaults, text.replace(" -heap-arrays", "").replace("-heap-arrays", ""))

    def build(self, source: Path, targets: Sequence[str], env: Optional[Dict[str, str]] = None) -> Tuple[int, List[str]]:
        self.configure(source)
        log = (self.log_dir / (self.spec.name + "-make.log")) if self.log_dir else None
        args = ["make", "-j%d" % self.jobs] + list(targets)
        proc = run(args, cwd=source, env=env, log=log, dry_run=self.dry_run, check=False)
        # ``make`` writes diagnostics to stdout; the return code is what matters.
        built = []
        for target in targets:
            path = source / target
            if path.exists():
                built.append(str(path))
        return proc.returncode, built


# ---------------------------------------------------------------------------
# Static dependency verification
# ---------------------------------------------------------------------------
SYSTEM_PREFIXES = ("/usr/lib", "/System/Library", "/usr/lib64", "/lib", "/lib64", "/usr/local/lib")
WINDOWS_SYSTEM_DLLS = {
    "kernel32.dll", "user32.dll", "advapi32.dll", "ws2_32.dll", "shell32.dll", "ole32.dll",
    "oleaut32.dll", "crypt32.dll", "bcrypt.dll", "iphlpapi.dll", "psapi.dll", "version.dll",
    "mswsock.dll", "secur32.dll", "winmm.dll", "gdi32.dll", "rpcrt4.dll", "ntdll.dll",
    "msvcrt.dll", "ucrtbase.dll", "api-ms-win-crt-runtime-l1-1-0.dll",
}


def verify_static(path: Path, platform: PlatformInfo) -> Tuple[bool, List[str]]:
    """Return (ok, offending dynamic dependencies)."""
    path = Path(path)
    if not path.exists():
        return False, ["missing executable"]
    if platform.is_linux:
        return _verify_linux(path)
    if platform.is_macos:
        return _verify_macos(path)
    if platform.is_windows:
        return _verify_windows(path)
    if platform.is_bsd:
        return _verify_bsd(path)
    return True, []


def _verify_linux(path: Path) -> Tuple[bool, List[str]]:
    readelf = which("readelf")
    if not readelf:
        return True, ["readelf unavailable; could not verify"]
    proc = subprocess.run([readelf, "-d", str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    if proc.returncode != 0:
        return True, ["readelf failed; could not verify"]
    text = proc.stdout.decode("utf-8", "replace")
    needed = re.findall(r"\(NEEDED\)\s+Shared library:\s+\[([^\]]+)\]", text)
    offending = [x for x in needed if not _is_system_linux(x)]
    return not offending, offending


def _is_system_linux(name: str) -> bool:
    base = os.path.basename(name)
    allowed = {
        "libc.so.6", "libm.so.6", "libdl.so.2", "libpthread.so.0", "librt.so.1",
        "ld-linux-x86-64.so.2", "ld-linux-aarch64.so.1", "libgcc_s.so.1",
        "libresolv.so.2", "libutil.so.1", "libnsl.so.1", "libcrypt.so.1",
    }
    return base in allowed or base.startswith("ld-linux") or base.startswith("linux-vdso")


def _verify_macos(path: Path) -> Tuple[bool, List[str]]:
    proc = subprocess.run(["otool", "-L", str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    if proc.returncode != 0:
        return True, ["otool unavailable; could not verify"]
    lines = proc.stdout.decode("utf-8", "replace").splitlines()[1:]
    offending = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        dep = line.split(" (compatibility")[0].strip()
        if dep.startswith(("/usr/lib/", "/System/Library/", "/Library/Apple/")):
            continue
        offending.append(dep)
    return not offending, offending


def _verify_windows(path: Path) -> Tuple[bool, List[str]]:
    objdump = which("objdump") or which("x86_64-w64-mingw32-objdump")
    if not objdump:
        return True, ["objdump unavailable; could not verify"]
    proc = subprocess.run([objdump, "-p", str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    text = proc.stdout.decode("utf-8", "replace")
    dlls = re.findall(r"DLL Name:\s*(\S+)", text)
    offending = [dll for dll in dlls if dll.lower() not in WINDOWS_SYSTEM_DLLS and not dll.lower().startswith("api-ms-win-")]
    return not offending, offending


def _verify_bsd(path: Path) -> Tuple[bool, List[str]]:
    proc = subprocess.run(["ldd", str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    text = proc.stdout.decode("utf-8", "replace")
    if "not a dynamic executable" in text or "statically linked" in text:
        return True, []
    deps = re.findall(r"=>\s+(\S+)", text)
    offending = [d for d in deps if not d.startswith(("/usr/lib", "/lib", "/usr/local/lib"))]
    return not offending, offending
