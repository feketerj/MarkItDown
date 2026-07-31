#!/usr/bin/env python3
"""Governed reaper for pipeline-owned distillation quarantine.

Extraction receipts are immutable.  This reaper verifies the receipt, retention
policy, quarantine path, age, and raw SHA-256 before deletion, then records an
append-only cleanup event before and after the unlink.
"""

from __future__ import annotations

import argparse
import datetime
import fcntl
import hashlib
import json
import os
import stat
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path


EVENT_SCHEMA = "opics.document-cleanup-event.v1"
AUTHORIZATION_SCHEMA = "opics.document-cleanup-authorization.v1"
QUARANTINE_DIR = ".opics-distill-quarantine"
REAPER_DIR = ".opics-distill-reaper"
VALID_TERMINAL_DISPOSITIONS = {
    "SCHEMA_EXTRACTION_COMPLETE",
    "TERMINAL_SOURCE_NULL",
}


@dataclass
class ReapResult:
    receipt: str
    quarantine_name: str | None
    status: str
    detail: str


def write_cleanup_authorization(
    receipt_path: str | Path,
    *,
    pipeline: str,
    subject_id: str,
    terminal_disposition: str,
    evidence_paths: dict[str, str | Path],
) -> Path:
    supplied_path = Path(receipt_path).expanduser()
    if supplied_path.is_symlink():
        raise ValueError("distillation receipt must not be a symlink")
    path = supplied_path.resolve()
    if not path.is_file():
        raise ValueError("distillation receipt must be a regular file")
    receipt_bytes = path.read_bytes()
    receipt = json.loads(receipt_bytes)
    _validate_receipt(path, receipt, receipt_bytes)
    if terminal_disposition not in VALID_TERMINAL_DISPOSITIONS:
        raise ValueError("terminal disposition does not authorize cleanup")
    if not pipeline.strip() or not subject_id.strip():
        raise ValueError("pipeline and subject_id are required")
    if not evidence_paths or any(not name.strip() for name in evidence_paths):
        raise ValueError("at least one durable downstream evidence artifact is required")
    evidence_artifacts = {
        name: _read_evidence_artifact(evidence_path)
        for name, evidence_path in sorted(evidence_paths.items())
    }

    binding = {
        "schema_version": AUTHORIZATION_SCHEMA,
        "pipeline": pipeline,
        "subject_id": subject_id,
        "terminal_disposition": terminal_disposition,
        "receipt_name": path.name,
        "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
        "artifact_id": receipt["artifact_id"],
        "source_sha256": receipt["source"]["sha256"],
        "evidence_artifacts": evidence_artifacts,
    }
    target = path.parent / f"{path.name}.cleanup-auth.json"
    if target.is_symlink():
        raise ValueError("cleanup authorization path must not be a symlink")
    if target.exists():
        _validate_existing_authorization(target, binding)
        return target

    authorization = {
        **binding,
        "authorized_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    encoded = json.dumps(authorization, indent=2, sort_keys=True) + "\n"
    try:
        descriptor = os.open(
            target,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        _validate_existing_authorization(target, binding)
    return target


def _validate_existing_authorization(path: Path, binding: dict) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError("cleanup authorization must be a regular non-symlink file")
    if stat.S_IMODE(path.stat().st_mode) & 0o022:
        raise ValueError("cleanup authorization is group/world writable")
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("cleanup authorization is invalid") from exc
    if any(existing.get(key) != value for key, value in binding.items()):
        raise ValueError("cleanup authorization already exists with different content")
    _parse_timestamp(existing.get("authorized_at"))


def _read_evidence_artifact(path: str | Path) -> dict[str, str]:
    supplied = Path(path).expanduser()
    if supplied.is_symlink():
        raise ValueError("downstream evidence must not be a symlink")
    resolved = supplied.resolve()
    if not resolved.is_file():
        raise ValueError("downstream evidence artifact is missing")
    descriptor = os.open(
        resolved,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        current = os.fstat(descriptor)
        if not stat.S_ISREG(current.st_mode):
            raise ValueError("downstream evidence must be a regular file")
        if stat.S_IMODE(current.st_mode) & 0o022:
            raise ValueError("downstream evidence is group/world writable")
        digest = _hash_descriptor(descriptor)
    finally:
        os.close(descriptor)
    return {"path": str(resolved), "sha256": digest}


def reap_quarantine(
    owned_root: str | Path,
    receipts_dir: str | Path,
    *,
    min_age_seconds: int = 3600,
    dry_run: bool = False,
) -> list[ReapResult]:
    if min_age_seconds < 0:
        raise ValueError("min_age_seconds must not be negative")
    root = Path(owned_root).resolve()
    receipt_root = Path(receipts_dir).resolve()
    if not root.is_dir() or not receipt_root.is_dir():
        raise ValueError("owned_root and receipts_dir must be existing directories")
    if root == receipt_root or _is_relative_to(receipt_root, root):
        raise ValueError("receipts_dir must be outside owned_root")
    if stat.S_IMODE(receipt_root.stat().st_mode) & 0o022:
        raise ValueError("receipts_dir must not be group/world writable")

    events_dir = receipt_root / "cleanup-events"
    _ensure_private_directory(events_dir)
    results = []
    for receipt_path in sorted(receipt_root.glob("*.receipt.json")):
        try:
            results.append(
                _reap_one(
                    root,
                    receipt_path,
                    events_dir,
                    min_age_seconds=min_age_seconds,
                    dry_run=dry_run,
                )
            )
        except (OSError, ValueError) as exc:
            results.append(
                ReapResult(
                    receipt_path.name,
                    None,
                    "REFUSED",
                    f"{type(exc).__name__}: {exc}",
                )
            )
    return results


def _reap_one(
    root: Path,
    receipt_path: Path,
    events_dir: Path,
    *,
    min_age_seconds: int,
    dry_run: bool,
) -> ReapResult:
    if receipt_path.is_symlink() or not receipt_path.is_file():
        return ReapResult(receipt_path.name, None, "REFUSED", "receipt is not a regular file")
    try:
        receipt_bytes = receipt_path.read_bytes()
        receipt = json.loads(receipt_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        return ReapResult(receipt_path.name, None, "REFUSED", f"invalid receipt: {exc}")

    try:
        _validate_receipt(receipt_path, receipt, receipt_bytes)
    except ValueError as exc:
        return ReapResult(receipt_path.name, None, "REFUSED", str(exc))

    cleanup = receipt["cleanup"]
    quarantine_name = cleanup.get("quarantine_name")
    if cleanup.get("retention_policy") != "TEMP_DELETE":
        return ReapResult(
            receipt_path.name,
            quarantine_name,
            "SKIPPED",
            "retention policy does not authorize deletion",
        )
    if (
        cleanup.get("disposition") != "QUARANTINED"
        or not cleanup.get("custody_action_performed")
    ):
        return ReapResult(
            receipt_path.name,
            quarantine_name,
            "SKIPPED",
            "receipt does not prove quarantine admission",
        )
    try:
        relative = _validate_quarantine_name(quarantine_name)
    except ValueError as exc:
        return ReapResult(receipt_path.name, quarantine_name, "REFUSED", str(exc))

    source_sha256 = receipt["source"]["sha256"].lower()
    receipt_sha256 = hashlib.sha256(receipt_bytes).hexdigest()
    try:
        authorization, authorization_sha256 = _load_authorization(
            receipt_path,
            receipt,
            receipt_sha256,
            root,
        )
    except ValueError as exc:
        return ReapResult(receipt_path.name, quarantine_name, "REFUSED", str(exc))
    event_path = events_dir / f"{receipt_path.name}.cleanup.jsonl"
    lock_path = events_dir / f"{receipt_path.name}.lock"
    lock_fd = os.open(
        lock_path,
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        events = _read_events(
            event_path,
            receipt_path.name,
            receipt_sha256,
            quarantine_name,
            source_sha256,
            authorization_sha256,
        )
    except ValueError as exc:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        return ReapResult(receipt_path.name, quarantine_name, "REFUSED", str(exc))

    try:
        admitted_at = max(
            _parse_timestamp(receipt["completed_at"]),
            _parse_timestamp(authorization["authorized_at"]),
        )
    except ValueError as exc:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        return ReapResult(receipt_path.name, quarantine_name, "REFUSED", str(exc))
    age = time.time() - admitted_at
    if age < min_age_seconds:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        return ReapResult(
            receipt_path.name,
            quarantine_name,
            "DEFERRED",
            f"receipt quarantine age {age:.1f}s is below {min_age_seconds}s",
        )

    try:
        root_fd = os.open(
            root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        raise
    try:
        _ensure_private_directory_at(root_fd, REAPER_DIR)
        try:
            quarantine_fd = os.open(
                QUARANTINE_DIR,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root_fd,
            )
        except OSError as exc:
            return ReapResult(
                receipt_path.name,
                quarantine_name,
                "REFUSED",
                f"quarantine directory unavailable: {exc}",
            )
        reaper_fd = os.open(
            REAPER_DIR,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_fd,
        )
        try:
            quarantine_stat = os.fstat(quarantine_fd)
            if stat.S_IMODE(quarantine_stat.st_mode) & 0o077:
                return ReapResult(
                    receipt_path.name,
                    quarantine_name,
                    "REFUSED",
                    "quarantine directory permissions are too broad",
                )
            claim_name = f"{receipt_sha256}.claim"
            source_dir_fd = quarantine_fd
            source_name = relative.name
            quarantine_exists = _entry_exists(quarantine_fd, relative.name)
            claim_exists = _entry_exists(reaper_fd, claim_name)
            if quarantine_exists and claim_exists:
                return ReapResult(
                    receipt_path.name,
                    quarantine_name,
                    "REFUSED",
                    "both quarantine and reaper claim exist",
                )
            if not quarantine_exists and not claim_exists:
                if any(event["status"] == "DELETED" for event in events):
                    return ReapResult(
                        receipt_path.name,
                        quarantine_name,
                        "ALREADY_DELETED",
                        "append-only ledger proves prior deletion",
                    )
                return ReapResult(
                    receipt_path.name,
                    quarantine_name,
                    "REFUSED",
                    "quarantined source is absent without a DELETED event",
                )
            if claim_exists:
                source_dir_fd = reaper_fd
                source_name = claim_name

            raw_fd = os.open(
                source_name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=source_dir_fd,
            )
            try:
                raw_stat = os.fstat(raw_fd)
                if not stat.S_ISREG(raw_stat.st_mode):
                    return ReapResult(
                        receipt_path.name,
                        quarantine_name,
                        "REFUSED",
                        "quarantined source is not a regular file",
                    )
                actual_sha256 = _hash_descriptor(raw_fd)
                if actual_sha256 != source_sha256:
                    return ReapResult(
                        receipt_path.name,
                        quarantine_name,
                        "REFUSED",
                        "quarantined bytes do not match the extraction receipt",
                    )
                if dry_run:
                    return ReapResult(
                        receipt_path.name,
                        quarantine_name,
                        "DRY_RUN",
                        "hash and retention checks passed",
                    )
            finally:
                os.close(raw_fd)

            if source_dir_fd == quarantine_fd:
                os.rename(
                    relative.name,
                    claim_name,
                    src_dir_fd=quarantine_fd,
                    dst_dir_fd=reaper_fd,
                )
                os.fsync(quarantine_fd)
                os.fsync(reaper_fd)

            claim_fd = os.open(
                claim_name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=reaper_fd,
            )
            try:
                claim_stat = os.fstat(claim_fd)
                if _hash_descriptor(claim_fd) != source_sha256:
                    raise ValueError("claimed bytes do not match the extraction receipt")
                _append_event(
                    event_path,
                    _event(
                        "AUTHORIZED",
                        receipt_path.name,
                        receipt_sha256,
                        quarantine_name,
                        source_sha256,
                        authorization_sha256,
                    ),
                )
                named_stat = os.stat(
                    claim_name,
                    dir_fd=reaper_fd,
                    follow_symlinks=False,
                )
                if (
                    named_stat.st_dev != claim_stat.st_dev
                    or named_stat.st_ino != claim_stat.st_ino
                ):
                    raise ValueError("reaper claim identity changed before deletion")
                os.unlink(claim_name, dir_fd=reaper_fd)
                os.fsync(reaper_fd)
                _append_event(
                    event_path,
                    _event(
                        "DELETED",
                        receipt_path.name,
                        receipt_sha256,
                        quarantine_name,
                        source_sha256,
                        authorization_sha256,
                    ),
                )
                return ReapResult(
                    receipt_path.name,
                    quarantine_name,
                    "DELETED",
                    "verified pipeline-owned raw source deleted",
                )
            finally:
                os.close(claim_fd)
        finally:
            os.close(reaper_fd)
            os.close(quarantine_fd)
    finally:
        os.close(root_fd)
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _validate_receipt(receipt_path: Path, receipt: dict, receipt_bytes: bytes) -> None:
    if receipt.get("schema_version") != "opics.document-distillation.v1":
        raise ValueError("receipt schema is not a document-distillation receipt")
    if receipt.get("status") not in {"distilled", "distilled_partial"}:
        raise ValueError("receipt status does not authorize raw cleanup")
    artifact_id = receipt.get("artifact_id")
    observation_id = receipt.get("observation_id")
    run_id = receipt.get("run_id")
    if not _is_hex(artifact_id, 64) or not _is_hex(observation_id, 20):
        raise ValueError("receipt identity is invalid")
    if not isinstance(run_id, str) or not run_id.isdigit():
        raise ValueError("receipt run identity is invalid")
    expected_name = f"{artifact_id}.{observation_id}.{run_id}.receipt.json"
    if receipt_path.name != expected_name:
        raise ValueError("receipt filename does not match its internal identity")

    source = receipt.get("source") or {}
    if not _is_hex(source.get("sha256"), 64):
        raise ValueError("receipt source hash is invalid")
    cleanup = receipt.get("cleanup")
    if not isinstance(cleanup, dict):
        raise ValueError("receipt cleanup contract is missing")

    outputs = receipt.get("outputs") or {}
    for key, suffix in (
        ("markdown", ".md"),
        ("layout", ".deepdoc-ocr.json"),
    ):
        output = outputs.get(key)
        if output is None and key == "layout":
            continue
        if not isinstance(output, dict):
            raise ValueError(f"receipt {key} output is invalid")
        name = output.get("name")
        expected_sha256 = output.get("sha256")
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not name.startswith(artifact_id)
            or not name.endswith(suffix)
            or not _is_hex(expected_sha256, 64)
        ):
            raise ValueError(f"receipt {key} output identity is invalid")
        output_path = receipt_path.parent / name
        if output_path.is_symlink() or not output_path.is_file():
            raise ValueError(f"receipt {key} output is missing")
        if _hash_path(output_path) != expected_sha256:
            raise ValueError(f"receipt {key} output hash does not match")

    quarantine_name = cleanup.get("quarantine_name")
    if quarantine_name is not None:
        relative = _validate_quarantine_name(quarantine_name)
        prefix = f"{artifact_id}.{observation_id}."
        if not relative.name.startswith(prefix):
            raise ValueError("quarantine filename does not match receipt identity")
    _parse_timestamp(receipt.get("completed_at"))


def _load_authorization(
    receipt_path: Path,
    receipt: dict,
    receipt_sha256: str,
    owned_root: Path,
) -> tuple[dict, str]:
    path = receipt_path.parent / f"{receipt_path.name}.cleanup-auth.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError("consuming-pipeline cleanup authorization is missing")
    if stat.S_IMODE(path.stat().st_mode) & 0o022:
        raise ValueError("cleanup authorization is group/world writable")
    raw = path.read_bytes()
    try:
        authorization = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("cleanup authorization is invalid JSON") from exc
    if authorization.get("schema_version") != AUTHORIZATION_SCHEMA:
        raise ValueError("cleanup authorization schema is invalid")
    if authorization.get("terminal_disposition") not in VALID_TERMINAL_DISPOSITIONS:
        raise ValueError("cleanup authorization lacks a terminal disposition")
    if (
        authorization.get("receipt_name") != receipt_path.name
        or authorization.get("receipt_sha256") != receipt_sha256
        or authorization.get("artifact_id") != receipt["artifact_id"]
        or authorization.get("source_sha256") != receipt["source"]["sha256"]
    ):
        raise ValueError("cleanup authorization is not bound to this receipt")
    if not str(authorization.get("pipeline") or "").strip():
        raise ValueError("cleanup authorization pipeline is missing")
    if not str(authorization.get("subject_id") or "").strip():
        raise ValueError("cleanup authorization subject is missing")
    evidence = authorization.get("evidence_artifacts")
    if not isinstance(evidence, dict) or not evidence:
        raise ValueError("cleanup authorization evidence artifacts are invalid")
    for name, expected in evidence.items():
        if (
            not str(name).strip()
            or not isinstance(expected, dict)
            or not _is_hex(expected.get("sha256"), 64)
            or not str(expected.get("path") or "").strip()
        ):
            raise ValueError("cleanup authorization evidence artifacts are invalid")
        actual = _read_evidence_artifact(expected["path"])
        if _is_relative_to(Path(actual["path"]), owned_root):
            raise ValueError("cleanup evidence must be outside the raw-owned root")
        if actual != expected:
            raise ValueError("cleanup authorization evidence artifact changed")
    _parse_timestamp(authorization.get("authorized_at"))
    return authorization, hashlib.sha256(raw).hexdigest()


def _parse_timestamp(value: object) -> float:
    if not isinstance(value, str):
        raise ValueError("receipt completion timestamp is missing")
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("receipt completion timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("receipt completion timestamp lacks timezone")
    return parsed.timestamp()


def _read_events(
    path: Path,
    receipt_name: str,
    receipt_sha256: str,
    quarantine_name: str,
    source_sha256: str,
    authorization_sha256: str,
) -> list[dict]:
    if not path.exists():
        return []
    if path.is_symlink() or not path.is_file():
        raise ValueError("cleanup ledger is not a regular file")
    events = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"cleanup ledger line {line_number} is invalid") from exc
        if (
            event.get("schema_version") != EVENT_SCHEMA
            or event.get("status") not in {"AUTHORIZED", "DELETED"}
            or event.get("receipt_name") != receipt_name
            or event.get("receipt_sha256") != receipt_sha256
            or event.get("quarantine_name") != quarantine_name
            or event.get("source_sha256") != source_sha256
            or event.get("authorization_sha256") != authorization_sha256
        ):
            raise ValueError(f"cleanup ledger line {line_number} does not match receipt")
        events.append(event)
    return events


def _entry_exists(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _ensure_private_directory_at(root_fd: int, name: str) -> None:
    try:
        os.mkdir(name, mode=0o700, dir_fd=root_fd)
    except FileExistsError:
        pass
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=root_fd,
    )
    try:
        current = os.fstat(descriptor)
        if not stat.S_ISDIR(current.st_mode) or stat.S_IMODE(current.st_mode) & 0o077:
            raise ValueError(f"{name} is not a private directory")
    finally:
        os.close(descriptor)


def _is_hex(value: object, length: int) -> bool:
    if not isinstance(value, str) or len(value) != length:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _hash_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_quarantine_name(value: object) -> Path:
    if not isinstance(value, str):
        raise ValueError("receipt quarantine path is missing")
    relative = Path(value)
    if (
        relative.is_absolute()
        or len(relative.parts) != 2
        or relative.parts[0] != QUARANTINE_DIR
        or relative.parts[1] in {"", ".", ".."}
    ):
        raise ValueError("receipt quarantine path is outside the governed directory")
    return relative


def _ensure_private_directory(path: Path) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise ValueError("cleanup-events path is not a safe directory")
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ValueError("cleanup-events directory permissions are too broad")
        return
    path.mkdir(mode=0o700)


def _event(
    status: str,
    receipt_name: str,
    receipt_sha256: str,
    quarantine_name: str,
    source_sha256: str,
    authorization_sha256: str,
) -> dict:
    return {
        "schema_version": EVENT_SCHEMA,
        "status": status,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "receipt_name": receipt_name,
        "receipt_sha256": receipt_sha256,
        "quarantine_name": quarantine_name,
        "source_sha256": source_sha256,
        "authorization_sha256": authorization_sha256,
    }


def _append_event(path: Path, payload: dict) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(
            descriptor,
            (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
                "utf-8"
            ),
        )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _hash_descriptor(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Delete verified pipeline-owned raw files from distillation quarantine."
    )
    parser.add_argument("owned_root")
    parser.add_argument("receipts_dir")
    parser.add_argument("--min-age-seconds", type=int, default=3600)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        results = reap_quarantine(
            args.owned_root,
            args.receipts_dir,
            min_age_seconds=args.min_age_seconds,
            dry_run=args.dry_run,
        )
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    payload = [asdict(result) for result in results]
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for result in results:
            print(f"{result.status}: {result.receipt}: {result.detail}")
    return 1 if any(result.status == "REFUSED" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
