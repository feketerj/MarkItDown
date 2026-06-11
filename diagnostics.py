"""Small diagnostic reports for MD_CREATOR.

The diagnostics packet is intentionally boring: it should classify failures
quickly without exposing raw local paths or turning observability into a new
subsystem.
"""

from __future__ import annotations

import os
import platform
import re
import tempfile
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any


MAX_FAILURES = 20
PATH_RE = re.compile(r"([A-Za-z]:\\[^\s]+|/[^\s]+)")


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sanitize_message(message: object, *, max_length: int = 240) -> str:
    text = str(message)
    text = PATH_RE.sub("[redacted-path]", text)
    text = " ".join(text.split())
    if len(text) > max_length:
        return f"{text[: max_length - 3]}..."
    return text


def redact_path(path: str | Path) -> str:
    resolved = Path(path)
    name = resolved.name or str(resolved.anchor).rstrip("\\/")
    parent = resolved.parent.name if resolved.parent != resolved else ""
    return f".../{parent}/{name}" if parent else f".../{name}"


def record_recent_failure(
    recent_failures: list[dict[str, Any]],
    category: str,
    message: object,
    *,
    source_extension: str | None = None,
) -> None:
    entry = {
        "timestamp": utc_timestamp(),
        "category": category,
        "message": sanitize_message(message),
    }
    if source_extension:
        entry["source_extension"] = source_extension.lower()
    recent_failures.insert(0, entry)
    del recent_failures[MAX_FAILURES:]


def dependency_report(distributions: dict[str, bool]) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    dependencies: dict[str, dict[str, Any]] = {}
    checks: list[dict[str, str]] = []
    for dist_name, required in sorted(distributions.items()):
        check_id = f"dependency_{dist_name.replace('-', '_')}"
        try:
            version = metadata.version(dist_name)
            dependencies[dist_name] = {"available": True, "version": version, "required": required}
            checks.append({"id": check_id, "status": "ok", "message": f"{dist_name} {version} available"})
        except metadata.PackageNotFoundError:
            dependencies[dist_name] = {"available": False, "version": None, "required": required}
            status = "fail" if required else "warn"
            checks.append({"id": check_id, "status": status, "message": f"{dist_name} is not installed"})
    return dependencies, checks


def directory_report(path: str | Path, *, file_pattern: str = "*") -> tuple[dict[str, Any], dict[str, str]]:
    directory = Path(path)
    status = "ok"
    message = "directory is writable"
    exists = directory.exists()
    is_dir = directory.is_dir()
    writable = False
    file_count = 0
    total_bytes = 0

    try:
        directory.mkdir(parents=True, exist_ok=True)
        exists = directory.exists()
        is_dir = directory.is_dir()
        fd, probe_name = tempfile.mkstemp(prefix=".mdcreator_diag_", dir=directory)
        os.close(fd)
        probe = Path(probe_name)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        writable = True
        for item in directory.glob(file_pattern):
            if item.is_file():
                file_count += 1
                total_bytes += item.stat().st_size
    except OSError as exc:
        status = "fail"
        message = f"directory is not writable: {sanitize_message(exc)}"

    if exists and not is_dir:
        status = "fail"
        message = "path exists but is not a directory"

    report = {
        "path": redact_path(directory),
        "exists": exists,
        "is_dir": is_dir,
        "writable": writable,
        "file_count": file_count,
        "total_bytes": total_bytes,
    }
    check = {"id": f"directory_{directory.name}", "status": status, "message": message}
    return report, check


def static_file_check(path: str | Path, check_id: str, message: str) -> dict[str, str]:
    file_path = Path(path)
    if file_path.exists() and file_path.is_file():
        return {"id": check_id, "status": "ok", "message": message}
    return {"id": check_id, "status": "fail", "message": f"missing {redact_path(file_path)}"}


def classify_checks(checks: list[dict[str, str]]) -> list[str]:
    classifications: set[str] = set()
    for check in checks:
        if check["status"] == "ok":
            continue
        check_id = check["id"]
        if check_id == "dependency_mineru_open_sdk":
            classifications.add("provider_failed")
        elif check_id.startswith("dependency_") or check_id.startswith("static_"):
            classifications.add("bad_deployment")
        elif check_id.startswith("directory_"):
            classifications.add("artifact_degraded")
        elif check_id.startswith("engine_academic"):
            classifications.add("provider_failed")
    return sorted(classifications)


def aggregate_status(checks: list[dict[str, str]]) -> str:
    if any(check["status"] == "fail" for check in checks):
        return "fail"
    if any(check["status"] == "warn" for check in checks):
        return "degraded"
    return "ok"


def build_diagnostics_report(
    *,
    app: dict[str, Any],
    engines: dict[str, Any],
    limits: dict[str, Any],
    artifact_dirs: dict[str, Path],
    static_files: dict[str, Path],
    bulk_jobs: dict[str, dict[str, Any]],
    recent_failures: list[dict[str, Any]],
    supported_formats_count: int,
    spreadsheet_options: dict[str, Any],
) -> dict[str, Any]:
    required_dependencies = {
        "fastapi": True,
        "uvicorn": True,
        "markitdown": True,
        "python-multipart": True,
        "charset-normalizer": True,
        "openpyxl": True,
        "xlrd": True,
        "mineru-open-sdk": False,
    }
    dependencies, dependency_checks = dependency_report(required_dependencies)

    artifacts: dict[str, Any] = {}
    artifact_checks: list[dict[str, str]] = []
    for name, directory in artifact_dirs.items():
        pattern = "*.md" if name == "conversion" else "*"
        report, check = directory_report(directory, file_pattern=pattern)
        artifacts[name] = report
        artifact_checks.append(check)

    static_checks = [
        static_file_check(static_files["index"], "static_index", "frontend index is present"),
        static_file_check(static_files["markdown_it"], "static_markdown_it", "vendored markdown-it is present"),
    ]

    engine_checks = [{"id": "engine_standard", "status": "ok", "message": "standard engine available"}]
    if engines.get("academic", {}).get("available"):
        engine_checks.append({"id": "engine_academic", "status": "ok", "message": "academic engine available"})
    else:
        engine_checks.append({"id": "engine_academic", "status": "warn", "message": "academic engine unavailable"})

    checks = dependency_checks + artifact_checks + static_checks + engine_checks
    active_bulk_jobs = sum(1 for job in bulk_jobs.values() if job.get("status") in {"queued", "running"})
    completed_bulk_jobs = sum(1 for job in bulk_jobs.values() if job.get("status") == "completed")
    failed_bulk_jobs = sum(1 for job in bulk_jobs.values() if job.get("status") == "failed")

    return {
        "status": aggregate_status(checks),
        "generated_at": utc_timestamp(),
        "failure_classes": [
            "bad_deployment",
            "artifact_degraded",
            "provider_failed",
            "cap_tripped",
            "api_failed",
            "conversion_failed",
        ],
        "classifications": classify_checks(checks),
        "app": {
            **app,
            "python": platform.python_version(),
            "platform": platform.system(),
        },
        "engines": engines,
        "limits": limits,
        "artifacts": artifacts,
        "dependencies": dependencies,
        "bulk_jobs": {
            "tracked": len(bulk_jobs),
            "active": active_bulk_jobs,
            "completed": completed_bulk_jobs,
            "failed": failed_bulk_jobs,
        },
        "formats": {"supported": supported_formats_count},
        "spreadsheet": spreadsheet_options,
        "recent_failures": list(recent_failures[:MAX_FAILURES]),
        "checks": checks,
    }
