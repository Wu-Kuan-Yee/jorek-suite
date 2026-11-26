#!/usr/bin/env python3
"""Reusable, machine-readable verification checks for JOREK builds.

Checks are data-driven: callers register the checks appropriate to a build
plan, while this module owns consistent execution, logs, build events and the
JSON report format.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Union

from common import emit_build_event


@dataclass
class CheckResult:
    status: str = "passed"
    details: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.status not in ("passed", "warning", "failed", "skipped"):
            raise ValueError("invalid verification status: %s" % self.status)
        self.details = [str(item) for item in self.details]


@dataclass(frozen=True)
class VerificationCheck:
    id: str
    label: str
    category: str
    run: Callable[[], Optional[Union[CheckResult, List[str], str]]]
    severity: str = "error"

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", self.id):
            raise ValueError("verification check id must be a safe identifier")
        if self.severity not in ("error", "warning"):
            raise ValueError("verification severity must be error or warning")


def run_checks(
    checks: Iterable[VerificationCheck],
    log_dir: Path,
    *,
    report_name: str = "preflight-report.json",
    context: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    """Run every check, preserve individual logs, and return a JSON-ready report."""
    selected = list(checks)
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    emit_build_event(
        "stage_plan",
        phase="preflight",
        stages=[{
            "id": "preflight:" + check.id,
            "label": check.label,
            "category": check.category,
            "log_path": str(log_dir / (check.id + ".log")),
        } for check in selected],
    )

    results: List[Dict[str, object]] = []
    for check in selected:
        stage_id = "preflight:" + check.id
        log_path = log_dir / (check.id + ".log")
        emit_build_event(
            "stage_start",
            id=stage_id,
            label=check.label,
            category=check.category,
            phase="preflight",
            log_path=str(log_path),
        )
        try:
            raw = check.run()
            if raw is None:
                result = CheckResult()
            elif isinstance(raw, CheckResult):
                result = raw
            elif isinstance(raw, str):
                result = CheckResult(details=[raw] if raw else [])
            else:
                result = CheckResult(details=list(raw))
        except Exception as exc:  # noqa: BLE001 - report all independent checks
            result = CheckResult(status="failed", details=[str(exc)])

        if result.status == "failed" and check.severity == "warning":
            result.status = "warning"
        if not result.details:
            result.details = ["No issues found."] if result.status == "passed" else [
                "Check status: %s" % result.status
            ]

        log_text = "\n".join(result.details) + "\n"
        log_path.write_text(log_text, encoding="utf-8")
        print("")
        print("[%s] %s" % (result.status.upper(), check.label))
        for line in result.details:
            print("  " + line)

        stage_status = "failed" if result.status == "failed" else result.status
        emit_build_event(
            "stage_finish",
            id=stage_id,
            status=stage_status,
            error="; ".join(result.details) if result.status == "failed" else "",
        )
        results.append({
            "id": check.id,
            "label": check.label,
            "category": check.category,
            "severity": check.severity,
            "status": result.status,
            "details": result.details,
            "log_path": str(log_path),
        })

    failed = [
        item for item in results
        if item["status"] == "failed" and item["severity"] == "error"
    ]
    warnings = [item for item in results if item["status"] == "warning"]
    report: Dict[str, object] = {
        "schema_version": 1,
        "status": "failed" if failed else ("warning" if warnings else "passed"),
        "ok": not failed,
        "context": dict(context or {}),
        "summary": {
            "total": len(results),
            "passed": sum(item["status"] == "passed" for item in results),
            "warnings": len(warnings),
            "failed": len(failed),
            "skipped": sum(item["status"] == "skipped" for item in results),
        },
        "checks": results,
    }
    report_path = log_dir / report_name
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("")
    print("Preflight result: %s (%d checks, %d warnings, %d failures)" % (
        str(report["status"]).upper(),
        int(report["summary"]["total"]),
        int(report["summary"]["warnings"]),
        int(report["summary"]["failed"]),
    ))
    print("Report: %s" % report_path)
    return report
