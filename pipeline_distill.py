#!/usr/bin/env python3
"""Shared document-distillation boundary for OPICS data pipelines.

This module turns a caller-supplied document into content-addressed Markdown,
an optional DeepDoc layout sidecar, and a provenance receipt.  It deliberately
does not contain pipeline-specific field inference or promotion rules.

Pipeline-owned raw input can be moved into recoverable quarantine beneath an
explicit caller-declared owned root. Deletion is a separate, downstream-
authorized reaper action. Caller-owned files are never moved or deleted.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import importlib.metadata
import json
import mimetypes
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from markdown_cleanup import normalize_markdown_for_source


MAX_FILE_SIZE = 50 * 1024 * 1024
MAX_MARKDOWN_BYTES = 32 * 1024 * 1024
MARKITDOWN_TIMEOUT_SECONDS = 90
MARKITDOWN_RSS_LIMIT_BYTES = 1536 * 1024 * 1024
VALID_PARSERS = {"auto", "markitdown", "deepdoc"}
SCHEMA_VERSION = "opics.document-distillation.v1"
DEFAULT_DEEPDOC_ROOT = Path("/Users/outpace/experiments/tdp-independent")
OWNER_CODE_FILES = (
    "pipeline_distill.py",
    "markdown_cleanup.py",
    "markitdown_bridge.py",
    "deepdoc_layout_bridge.py",
)


class DistillationError(RuntimeError):
    """Raised when a document cannot be distilled safely."""


@dataclass
class DistillationResult:
    receipt_path: str
    markdown_path: str
    layout_path: str | None
    artifact_id: str
    source_sha256: str
    parser_used: str
    raw_disposition: str
    warnings: list[str]


def distill_file(
    source: str | Path,
    output_dir: str | Path,
    *,
    source_uri: str | None = None,
    parser: str = "auto",
    owned_root: str | Path | None = None,
    max_file_size: int = MAX_FILE_SIZE,
    markitdown_engine=None,
    layout_runner: Callable[[Path, Path], dict] | None = None,
) -> DistillationResult:
    """Distill one local document and write content-addressed artifacts.

    ``owned_root`` is the custody boundary.  When supplied, ``source`` must be
    a regular file below that root and is moved to a recoverable quarantine only
    after the Markdown, optional layout, and an initial receipt have landed.
    """

    requested_parser = parser.lower()
    if requested_parser not in VALID_PARSERS:
        raise DistillationError(
            f"parser must be one of: {', '.join(sorted(VALID_PARSERS))}"
        )
    if max_file_size < 1:
        raise DistillationError("max_file_size must be at least 1 byte")

    supplied_source = Path(source).expanduser()
    if supplied_source.is_symlink():
        raise DistillationError("source must not be a symlink")
    source_path = supplied_source.resolve()
    if not source_path.is_file():
        raise DistillationError("source must be an existing non-symlink regular file")

    output_path = Path(output_dir).resolve()
    owned_path = _validate_owned_root(source_path, output_path, owned_root)
    output_path.mkdir(parents=True, exist_ok=True)
    started_at = _utc_timestamp()
    warnings: list[str] = []
    with _owned_source_lock(owned_path, source_path), tempfile.TemporaryDirectory(
        dir=output_path,
        prefix=".distill-work-",
    ) as work:
        work_path = Path(work)
        staged_source = work_path / f"source{source_path.suffix.lower()}"
        source_sha256, file_size, source_identity = _stage_source(
            source_path,
            staged_source,
            max_file_size,
        )
        observation_id = _observation_id(source_uri, source_path.name)
        run_id = str(time.time_ns())
        staged_markdown = work_path / "content.md"
        staged_layout = work_path / "deepdoc-ocr.json"

        markdown_text = ""
        markitdown_error = None
        try:
            markdown_text = _convert_markitdown(staged_source, markitdown_engine)
        except Exception as exc:
            markitdown_error = f"{type(exc).__name__}: {exc}"
        parser_used = "outpace-markitdown"
        ocr_selection_reason = None
        layout_written = False
        layout_payload = None
        is_pdf = staged_source.suffix.lower() == ".pdf"
        quality_requires_ocr, markitdown_quality_reason = _needs_deepdoc(
            markdown_text,
            markitdown_error,
        )
        if requested_parser == "deepdoc" and not is_pdf:
            raise DistillationError("deepdoc parser is only available for PDF input")

        run_deepdoc = False
        if is_pdf and requested_parser == "deepdoc":
            run_deepdoc = True
            ocr_selection_reason = (
                f"explicit_deepdoc_request:{markitdown_quality_reason}"
            )
        elif is_pdf and requested_parser == "auto":
            run_deepdoc = quality_requires_ocr
            ocr_selection_reason = markitdown_quality_reason

        if run_deepdoc:
            runner = layout_runner or _default_layout_runner()
            if runner is None:
                message = "DeepDoc runtime is unavailable; retained MarkItDown output"
                if requested_parser == "deepdoc":
                    raise DistillationError(message)
                warnings.append(message)
            else:
                try:
                    runner(staged_source, staged_layout)
                    if not staged_layout.exists():
                        raise DistillationError("DeepDoc did not write its output artifact")
                    layout_payload = _validate_layout(
                        staged_layout,
                        expected_source_sha256=source_sha256,
                    )
                    parser_used = "outpace-markitdown+deepdoc-ocr"
                    layout_written = True
                except Exception as exc:
                    staged_layout.unlink(missing_ok=True)
                    message = f"DeepDoc layout failed: {type(exc).__name__}: {exc}"
                    if requested_parser == "deepdoc":
                        raise DistillationError(message) from exc
                    warnings.append(message)

        if quality_requires_ocr and layout_payload is not None:
            ocr_markdown = _markdown_from_deepdoc(
                layout_payload,
                source_path.name,
            )
            if ocr_markdown:
                if markdown_text.strip():
                    markdown_text = (
                        markdown_text.rstrip()
                        + "\n\n# DeepDoc OCR recovery\n\n"
                        + ocr_markdown
                    )
                    parser_used = "outpace-markitdown+deepdoc-ocr"
                    warnings.append(
                        "MarkItDown output was below the quality threshold; "
                        "DeepDoc OCR was appended to the final Markdown"
                    )
                else:
                    markdown_text = ocr_markdown
                    parser_used = "deepdoc-ocr"
                    warning = (
                        "MarkItDown returned no usable text; "
                        "DeepDoc OCR supplied Markdown"
                    )
                    if markitdown_error:
                        warning = (
                            f"MarkItDown failed ({markitdown_error}); "
                            "DeepDoc OCR supplied Markdown"
                        )
                    warnings.append(warning)
        elif markitdown_error:
            raise DistillationError(f"MarkItDown conversion failed: {markitdown_error}")

        if not markdown_text.strip():
            raise DistillationError("document parsers produced empty Markdown")
        _write_text_atomic(staged_markdown, markdown_text)
        markdown_sha256 = _hash_file(staged_markdown)
        layout_sha256 = _hash_file(staged_layout) if layout_written else None

        interpretation_fingerprint = _interpretation_fingerprint(
            requested_parser,
            source_path.suffix.lower(),
            layout_payload,
        )
        artifact_id = _artifact_id(
            source_sha256,
            source_path.suffix.lower(),
            requested_parser,
            interpretation_fingerprint,
            markdown_sha256,
            layout_sha256,
        )
        markdown_path = output_path / f"{artifact_id}.md"
        layout_path = output_path / f"{artifact_id}.deepdoc-ocr.json"
        receipt_path = output_path / (
            f"{artifact_id}.{observation_id}.{run_id}.receipt.json"
        )
        _publish_immutable(staged_markdown, markdown_path, markdown_sha256)
        if layout_written:
            _publish_immutable(staged_layout, layout_path, layout_sha256)

        source_locator_present = bool(source_uri and source_uri.strip())
        # Extraction readiness belongs to the FINAL Markdown artifact, not the
        # pre-OCR MarkItDown attempt that selected the fallback. A successful
        # DeepDoc recovery must be allowed to earn readiness; a still-sparse
        # OCR result must remain explicitly ineligible.
        final_quality_insufficient, final_quality_reason = _needs_deepdoc(
            markdown_text,
            None,
        )
        if final_quality_insufficient:
            warnings.append(
                f"Final Markdown is not extraction-ready: {final_quality_reason}"
            )
        coverage = _coverage(
            parser_used,
            layout_payload,
            is_pdf=is_pdf,
            text_quality_sufficient=not final_quality_insufficient,
        )
        cleanup = {
            "retention_policy": (
                "TEMP_DELETE" if owned_path else "SOURCE_OF_RECORD_KEEP"
            ),
            "custody_action": "quarantine_after_receipt" if owned_path else "retain",
            "custody_action_performed": not bool(owned_path),
            "disposition": "PENDING_QUARANTINE" if owned_path else "RETAINED",
            "raw_bytes_deleted": False,
            "detail": (
                "pending recoverable quarantine move"
                if owned_path
                else "caller-owned source retained"
            ),
        }
        receipt = _build_receipt(
            artifact_id=artifact_id,
            observation_id=observation_id,
            run_id=run_id,
            interpretation_fingerprint=interpretation_fingerprint,
            ocr_selection_reason=ocr_selection_reason,
            source_path=source_path,
            source_uri=source_uri,
            source_sha256=source_sha256,
            file_size=file_size,
            requested_parser=requested_parser,
            parser_used=parser_used,
            markdown_path=markdown_path,
            layout_path=layout_path if layout_written else None,
            warnings=warnings,
            source_locator_present=source_locator_present,
            coverage=coverage,
            cleanup=cleanup,
            started_at=started_at,
        )
        _write_json_atomic(receipt_path, receipt)

        raw_disposition = "RETAINED"
        if owned_path:
            try:
                quarantine_name = _quarantine_source(
                    source_path,
                    owned_path,
                    artifact_id,
                    observation_id,
                    source_identity,
                    source_sha256,
                )
                raw_disposition = "QUARANTINED"
                cleanup.update(
                    custody_action_performed=True,
                    disposition="QUARANTINED",
                    quarantine_name=quarantine_name,
                    detail="raw bytes retained in recoverable quarantine pending governed reaper",
                )
            except (OSError, DistillationError) as exc:
                source_still_present = (
                    source_path.exists()
                    and not source_path.is_symlink()
                    and source_path.is_file()
                )
                cleanup.update(
                    custody_action_performed=False,
                    disposition=(
                        "RETAINED" if source_still_present else "CUSTODY_ANOMALY"
                    ),
                    detail=(
                        f"quarantine move failed: {type(exc).__name__}: {exc}"
                        if source_still_present
                        else (
                            "quarantine move failed and the original source is absent: "
                            f"{type(exc).__name__}: {exc}"
                        )
                    ),
                )
                warnings.append(cleanup["detail"])

            receipt["warnings"] = warnings
            receipt["cleanup"] = cleanup
            receipt["completed_at"] = _utc_timestamp()
            _write_json_atomic(receipt_path, receipt)

        return DistillationResult(
            receipt_path=str(receipt_path),
            markdown_path=str(markdown_path),
            layout_path=str(layout_path) if layout_written else None,
            artifact_id=artifact_id,
            source_sha256=source_sha256,
            parser_used=parser_used,
            raw_disposition=raw_disposition,
            warnings=warnings,
        )


@contextlib.contextmanager
def _owned_source_lock(owned_root: Path | None, source_path: Path):
    if owned_root is None:
        yield
        return
    root_fd = os.open(
        owned_root,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    lock_dir_fd = None
    lock_fd = None
    try:
        try:
            os.mkdir(".opics-distill-locks", mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        lock_dir_fd = os.open(
            ".opics-distill-locks",
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_fd,
        )
        lock_dir_stat = os.fstat(lock_dir_fd)
        if (
            not stat.S_ISDIR(lock_dir_stat.st_mode)
            or stat.S_IMODE(lock_dir_stat.st_mode) & 0o077
        ):
            raise DistillationError("source admission lock directory is not private")
        relative = str(source_path.relative_to(owned_root))
        lock_name = hashlib.sha256(relative.encode("utf-8")).hexdigest() + ".lock"
        lock_fd = os.open(
            lock_name,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=lock_dir_fd,
        )
        lock_stat = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(lock_stat.st_mode)
            or stat.S_IMODE(lock_stat.st_mode) & 0o077
        ):
            raise DistillationError("source admission lock is not a private file")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if (
            not source_path.exists()
            or source_path.is_symlink()
            or not source_path.is_file()
        ):
            raise DistillationError(
                "source is unavailable after admission lock; it may already be quarantined"
            )
        yield
    finally:
        if lock_fd is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        if lock_dir_fd is not None:
            os.close(lock_dir_fd)
        os.close(root_fd)


def _validate_owned_root(
    source_path: Path,
    output_path: Path,
    owned_root: str | Path | None,
) -> Path | None:
    if owned_root is None:
        return None
    root = Path(owned_root).resolve()
    if not root.is_dir():
        raise DistillationError("owned_root must be an existing directory")
    try:
        source_path.relative_to(root)
    except ValueError as exc:
        raise DistillationError("source is outside the declared owned_root") from exc
    if output_path == root or _is_relative_to(output_path, root):
        raise DistillationError("output_dir must be outside owned_root")
    return root


def _stage_source(
    source_path: Path,
    staged_path: Path,
    max_file_size: int,
) -> tuple[str, int, dict]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source_path, flags)
    except OSError as exc:
        raise DistillationError(f"cannot open source safely: {exc}") from exc

    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise DistillationError("source must be a regular file")
        if before.st_size > max_file_size:
            raise DistillationError(
                f"source exceeds {max_file_size} byte distillation limit"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as source_handle:
            with staged_path.open("wb") as staged_handle:
                while True:
                    chunk = source_handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    staged_handle.write(chunk)
                staged_handle.flush()
                os.fsync(staged_handle.fileno())
        after = os.fstat(descriptor)
        identity = {
            "device": before.st_dev,
            "inode": before.st_ino,
            "size": before.st_size,
            "mtime_ns": before.st_mtime_ns,
        }
        if (
            after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
        ):
            raise DistillationError("source changed while it was being snapshotted")
        return digest.hexdigest(), before.st_size, identity
    finally:
        os.close(descriptor)


def _quarantine_source(
    source_path: Path,
    owned_root: Path,
    artifact_id: str,
    observation_id: str,
    expected_identity: dict,
    expected_sha256: str,
) -> str:
    source_parent_fd = os.open(
        source_path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    root_fd = os.open(
        owned_root,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    quarantine_name = ".opics-distill-quarantine"
    try:
        try:
            os.mkdir(quarantine_name, mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        quarantine_fd = os.open(
            quarantine_name,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_fd,
        )
        try:
            quarantine_stat = os.fstat(quarantine_fd)
            if not stat.S_ISDIR(quarantine_stat.st_mode):
                raise DistillationError("quarantine path is not a directory")
            if stat.S_IMODE(quarantine_stat.st_mode) & 0o077:
                raise DistillationError("quarantine directory permissions are too broad")

            source_fd = os.open(
                source_path.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=source_parent_fd,
            )
            try:
                current = os.fstat(source_fd)
                actual_identity = {
                    "device": current.st_dev,
                    "inode": current.st_ino,
                    "size": current.st_size,
                    "mtime_ns": current.st_mtime_ns,
                }
                if actual_identity != expected_identity:
                    raise DistillationError("source identity changed before quarantine")
                if _hash_descriptor(source_fd) != expected_sha256:
                    raise DistillationError("source bytes changed before quarantine")
            finally:
                os.close(source_fd)

            target_name = (
                f"{artifact_id}.{observation_id}.{time.time_ns()}."
                f"{source_path.name}.pending"
            )
            os.rename(
                source_path.name,
                target_name,
                src_dir_fd=source_parent_fd,
                dst_dir_fd=quarantine_fd,
            )
            target_fd = os.open(
                target_name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=quarantine_fd,
            )
            try:
                if _hash_descriptor(target_fd) != expected_sha256:
                    raise DistillationError(
                        "quarantined bytes do not match the source receipt"
                    )
            finally:
                os.close(target_fd)
            return f"{quarantine_name}/{target_name}"
        finally:
            os.close(quarantine_fd)
    finally:
        os.close(root_fd)
        os.close(source_parent_fd)


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


def _artifact_id(
    source_sha256: str,
    extension: str,
    requested_parser: str,
    interpretation_fingerprint: str,
    markdown_sha256: str,
    layout_sha256: str | None,
) -> str:
    payload = "\0".join(
        (
            source_sha256,
            extension,
            requested_parser,
            interpretation_fingerprint,
            markdown_sha256,
            layout_sha256 or "",
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _publish_immutable(staged_path: Path, target_path: Path, expected_sha256: str) -> None:
    try:
        os.link(staged_path, target_path)
    except FileExistsError:
        if _hash_file(target_path) != expected_sha256:
            raise DistillationError(
                f"immutable artifact collision at {target_path.name}"
            )
    if _hash_file(target_path) != expected_sha256:
        raise DistillationError(f"immutable artifact verification failed at {target_path.name}")


def _observation_id(source_uri: str | None, source_name: str) -> str:
    payload = json.dumps(
        {"source_uri": source_uri, "source_name": source_name},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _interpretation_fingerprint(
    requested_parser: str,
    extension: str,
    layout_payload: dict | None,
) -> str:
    root = Path(__file__).resolve().parent
    files = {}
    for name in OWNER_CODE_FILES:
        path = root / name
        if path.is_file():
            files[name] = _hash_file(path)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "requested_parser": requested_parser,
        "extension": extension,
        "code_sha256": files,
        "markitdown_dependency_version": _package_version("markitdown"),
        "deepdoc_runtime": (layout_payload or {}).get("runtime"),
        "deepdoc_dpi": (layout_payload or {}).get("dpi"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _convert_markitdown(source_path: Path, engine=None) -> str:
    if engine is not None:
        result = engine.convert(str(source_path))
        text = result.text_content or ""
    else:
        bridge = Path(__file__).resolve().with_name("markitdown_bridge.py")
        output = source_path.with_name("markitdown-output.md")
        safe_env = {
            key: value
            for key, value in os.environ.items()
            if key in {"HOME", "LANG", "LC_ALL", "PATH", "TMPDIR", "TZ"}
        }
        safe_env["PYTHONNOUSERSITE"] = "1"
        try:
            with _markitdown_drum_lock():
                with tempfile.TemporaryFile() as stderr_file:
                    process = subprocess.Popen(
                        [
                            sys.executable,
                            str(bridge),
                            str(source_path),
                            str(output),
                            "--max-output-bytes",
                            str(MAX_MARKDOWN_BYTES),
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=stderr_file,
                        env=safe_env,
                        start_new_session=True,
                    )
                    failure = _wait_for_markitdown(process)
                    stderr_file.seek(0)
                    stderr = stderr_file.read().decode("utf-8", errors="replace")
        except OSError as exc:
            raise DistillationError(f"cannot launch bounded MarkItDown: {exc}") from exc
        if failure:
            raise DistillationError(failure)
        if process.returncode != 0:
            detail = (stderr or "").strip()[-2_000:]
            raise DistillationError(
                f"bounded MarkItDown subprocess failed ({process.returncode}): {detail}"
            )
        if output.is_symlink() or not output.is_file():
            raise DistillationError("bounded MarkItDown subprocess wrote no regular output")
        if output.stat().st_size > MAX_MARKDOWN_BYTES:
            raise DistillationError("bounded MarkItDown output exceeds the byte limit")
        text = output.read_text(encoding="utf-8")
    normalized = normalize_markdown_for_source(text, source_path)
    if len(normalized.encode("utf-8")) > MAX_MARKDOWN_BYTES:
        raise DistillationError("normalized Markdown exceeds the byte limit")
    return normalized


def _wait_for_markitdown(process: subprocess.Popen) -> str | None:
    started = time.monotonic()
    while process.poll() is None:
        elapsed = time.monotonic() - started
        rss = _process_group_rss_bytes(process.pid)
        if elapsed > MARKITDOWN_TIMEOUT_SECONDS:
            _kill_process_group(process)
            return f"MarkItDown exceeded {MARKITDOWN_TIMEOUT_SECONDS}s wall timeout"
        if rss is not None and rss > MARKITDOWN_RSS_LIMIT_BYTES:
            _kill_process_group(process)
            return (
                "MarkItDown process group exceeded "
                f"{MARKITDOWN_RSS_LIMIT_BYTES} byte RSS limit"
            )
        time.sleep(0.1)
    return None


def _process_group_rss_bytes(process_group: int) -> int | None:
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pgid=,rss="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    total_kib = 0
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pgid, rss_kib = (int(value) for value in parts)
        except ValueError:
            continue
        if pgid == process_group:
            total_kib += rss_kib
    return total_kib * 1024


def _kill_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


@contextlib.contextmanager
def _markitdown_drum_lock(timeout_seconds: float = 120):
    try:
        lane_count = int(os.environ.get("OPICS_MARKITDOWN_LANES", "2"))
    except ValueError as exc:
        raise DistillationError("OPICS_MARKITDOWN_LANES must be an integer") from exc
    if not 1 <= lane_count <= 4:
        raise DistillationError("OPICS_MARKITDOWN_LANES must be between 1 and 4")
    descriptors = [
        os.open(
            Path(tempfile.gettempdir()) / f"opics-markitdown-{lane}.lock",
            os.O_CREAT | os.O_RDWR,
            0o600,
        )
        for lane in range(lane_count)
    ]
    acquired = None
    try:
        deadline = time.monotonic() + timeout_seconds
        while acquired is None:
            for descriptor in descriptors:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = descriptor
                    break
                except BlockingIOError:
                    continue
            if acquired is None:
                if time.monotonic() >= deadline:
                    raise DistillationError(
                        "MarkItDown drum queue exceeded 120 seconds"
                    )
                time.sleep(0.1)
        yield
    finally:
        if acquired is not None:
            fcntl.flock(acquired, fcntl.LOCK_UN)
        for descriptor in descriptors:
            os.close(descriptor)


def _needs_deepdoc(markdown_text: str, markitdown_error: str | None) -> tuple[bool, str]:
    if markitdown_error:
        return True, "markitdown_error"
    text = markdown_text.strip()
    if not text:
        return True, "markitdown_empty"
    if len(text) < 256:
        return True, "markitdown_sparse"
    replacement_ratio = text.count("\ufffd") / max(len(text), 1)
    if replacement_ratio > 0.01:
        return True, "markitdown_decode_damage"
    alphanumeric = sum(character.isalnum() for character in text)
    if alphanumeric / max(len(text), 1) < 0.35:
        return True, "markitdown_low_signal"
    return False, "markitdown_quality_sufficient"


def _default_layout_runner() -> Callable[[Path, Path], dict] | None:
    root = Path(os.environ.get("OPICS_DEEPDOC_ROOT", DEFAULT_DEEPDOC_ROOT)).resolve()
    # Do not resolve the venv launcher symlink: resolving it to the base Python
    # silently drops the OCR environment and its fitz/onnxruntime packages.
    python = Path(
        os.environ.get("OPICS_DEEPDOC_PYTHON", root / ".venv-ocr" / "bin" / "python")
    ).expanduser()
    bridge = Path(__file__).resolve().with_name("deepdoc_layout_bridge.py")
    if not (python.is_file() and bridge.is_file() and (root / "deepdoc_shim.py").is_file()):
        return None
    try:
        max_pages = int(os.environ.get("OPICS_DEEPDOC_MAX_PAGES", "8"))
    except ValueError:
        return None
    if not 1 <= max_pages <= 50:
        return None

    def run(source: Path, target: Path) -> dict:
        command = [
            str(python),
            str(bridge),
            str(source),
            str(target),
            "--deepdoc-root",
            str(root),
            "--max-pages",
            str(max_pages),
        ]
        safe_env = {
            "PATH": os.defpath,
            "HOME": tempfile.gettempdir(),
            "TMPDIR": tempfile.gettempdir(),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "PYTHONNOUSERSITE": "1",
            "CUDA_VISIBLE_DEVICES": "",
        }
        with _deepdoc_drum_lock():
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=240,
                cwd=root,
                env=safe_env,
                start_new_session=True,
            )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "unknown failure").strip()
            raise DistillationError(detail[-500:])
        return json.loads(target.read_text(encoding="utf-8"))

    return run


@contextlib.contextmanager
def _deepdoc_drum_lock(timeout_seconds: float = 120):
    lock_path = Path(tempfile.gettempdir()) / "opics-deepdoc-ocr.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise DistillationError(
                        "DeepDoc drum queue exceeded 120 seconds"
                    )
                time.sleep(0.1)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _validate_layout(path: Path, *, expected_source_sha256: str) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "opics.deepdoc-ocr.v1":
        raise DistillationError("DeepDoc returned an unsupported layout schema")
    if payload.get("parser") != "deepdoc-ocr":
        raise DistillationError("DeepDoc returned an unsupported parser identity")
    if payload.get("source_sha256") != expected_source_sha256:
        raise DistillationError("DeepDoc output does not match the source snapshot")
    pages = payload.get("pages")
    if not isinstance(pages, list) or not pages:
        raise DistillationError("DeepDoc layout is missing pages")
    page_count = payload.get("page_count")
    selected_pages = payload.get("selected_pages")
    if not isinstance(page_count, int) or page_count < 1:
        raise DistillationError("DeepDoc layout has an invalid page count")
    if (
        not isinstance(selected_pages, list)
        or not selected_pages
        or len(selected_pages) != len(set(selected_pages))
    ):
        raise DistillationError("DeepDoc layout has invalid selected pages")
    page_numbers = [page.get("page") for page in pages]
    if page_numbers != selected_pages:
        raise DistillationError("DeepDoc page payload and selection list disagree")
    if any(
        not isinstance(page, int) or page < 1 or page > page_count
        for page in selected_pages
    ):
        raise DistillationError("DeepDoc selected page is outside the document")

    total_boxes = 0
    for page in pages:
        boxes = page.get("boxes")
        if not isinstance(boxes, list):
            raise DistillationError("DeepDoc page is missing positioned boxes")
        for box in boxes:
            text = box.get("text")
            confidence = box.get("confidence")
            coordinates = [box.get(name) for name in ("x0", "x1", "y0", "y1")]
            if not isinstance(text, str) or not text.strip():
                raise DistillationError("DeepDoc box has empty text")
            if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
                raise DistillationError("DeepDoc box has invalid confidence")
            if any(
                not isinstance(value, (int, float)) or not 0 <= value <= 1
                for value in coordinates
            ):
                raise DistillationError("DeepDoc box has invalid coordinates")
            if box["x0"] > box["x1"] or box["y0"] > box["y1"]:
                raise DistillationError("DeepDoc box has inverted coordinates")
        total_boxes += len(boxes)
    if total_boxes < 1:
        raise DistillationError("DeepDoc found no positioned text")
    if not isinstance(payload.get("runtime"), dict):
        raise DistillationError("DeepDoc runtime provenance is missing")
    return payload


def _coverage(
    parser_used: str,
    layout_payload: dict | None,
    *,
    is_pdf: bool,
    text_quality_sufficient: bool,
) -> dict:
    if not layout_payload:
        scope = "unknown" if is_pdf else "parser_output_only"
        return {
            "content_scope": scope,
            "basis": "outpace-markitdown",
            "text_quality_sufficient_for_extraction_attempt": text_quality_sufficient,
            "page_count": None,
            "pages_examined": None,
            "selection_truncated": False,
        }
    page_count = int(layout_payload["page_count"])
    pages_examined = len(layout_payload["pages"])
    truncated = bool(layout_payload.get("selection_truncated"))
    content_scope = "selected_pages" if truncated else "full_document"
    return {
        "content_scope": content_scope,
        "basis": parser_used,
        "text_quality_sufficient_for_extraction_attempt": text_quality_sufficient,
        "page_count": page_count,
        "pages_examined": pages_examined,
        "page_fraction": round(pages_examined / page_count, 6),
        "selected_pages": layout_payload["selected_pages"],
        "selection_truncated": truncated,
        "deepdoc_scope": "selected_pages" if truncated else "full_document",
    }


def _markdown_from_deepdoc(payload: dict, source_name: str) -> str:
    page_sections = []
    for page in payload.get("pages", []):
        text = (page.get("ocr_text") or "").strip()
        if not text:
            continue
        page_number = page.get("page")
        page_sections.append(f"## Page {page_number}\n\n{text}")
    if not page_sections:
        return ""
    sections = [f"# {source_name}", *page_sections]
    return "\n\n".join(sections).strip() + "\n"


def _build_receipt(
    *,
    artifact_id: str,
    observation_id: str,
    run_id: str,
    interpretation_fingerprint: str,
    ocr_selection_reason: str | None,
    source_path: Path,
    source_uri: str | None,
    source_sha256: str,
    file_size: int,
    requested_parser: str,
    parser_used: str,
    markdown_path: Path,
    layout_path: Path | None,
    warnings: list[str],
    source_locator_present: bool,
    coverage: dict,
    cleanup: dict,
    started_at: str,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": (
            "distilled"
            if coverage["content_scope"] == "full_document"
            else "distilled_partial"
        ),
        "artifact_id": artifact_id,
        "observation_id": observation_id,
        "run_id": run_id,
        "interpretation_fingerprint": interpretation_fingerprint,
        "started_at": started_at,
        "completed_at": _utc_timestamp(),
        "source": {
            "uri": source_uri,
            "name": source_path.name,
            "extension": source_path.suffix.lower(),
            "media_type": mimetypes.guess_type(source_path.name)[0],
            "bytes": file_size,
            "sha256": source_sha256,
        },
        "parser": {
            "requested": requested_parser,
            "used": parser_used,
            "owner_repository": "https://github.com/feketerj/MarkItDown",
            **_owner_provenance(Path(__file__).resolve().parent),
            "markitdown_dependency_version": _package_version("markitdown"),
            "layout_schema": "opics.deepdoc-ocr.v1" if layout_path else None,
            "ocr_selection_reason": ocr_selection_reason,
        },
        "outputs": {
            "markdown": {
                "name": markdown_path.name,
                "sha256": _hash_file(markdown_path),
                "chars": len(markdown_path.read_text(encoding="utf-8")),
            },
            "layout": (
                {
                    "name": layout_path.name,
                    "sha256": _hash_file(layout_path),
                }
                if layout_path
                else None
            ),
        },
        "document_provenance": {
            "source_locator_present": source_locator_present,
            "content_hash_bound": True,
            "retrieval_chain_supplied": False,
        },
        "coverage": coverage,
        "eligible_for_schema_extraction_attempt": (
            source_locator_present
            and coverage["text_quality_sufficient_for_extraction_attempt"]
        ),
        "document_scope_complete": coverage["content_scope"] == "full_document",
        "eligible_for_complete_row_promotion": False,
        "promotion_authority": "consuming_pipeline_only",
        "next_gate": (
            "schema-bound field extraction with source quote/page provenance"
            if (
                source_locator_present
                and coverage["text_quality_sufficient_for_extraction_attempt"]
            )
            else (
                "supply a durable source URI before field extraction"
                if not source_locator_present
                else "improve document text quality before field extraction"
            )
        ),
        "cleanup": cleanup,
        "warnings": list(warnings),
    }


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _git_commit(root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def _owner_provenance(root: Path) -> dict:
    head_commit = _git_commit(root)
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--", *OWNER_CODE_FILES],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        completed = None
    if head_commit is None or completed is None or completed.returncode != 0:
        state = "unresolved"
    else:
        state = "dirty" if completed.stdout.strip() else "clean"
    code_hashes = {
        name: _hash_file(root / name)
        for name in OWNER_CODE_FILES
        if (root / name).is_file()
    }
    return {
        "owner_commit": head_commit if state == "clean" else None,
        "owner_head_commit": head_commit,
        "owner_worktree_state": state,
        "owner_code_sha256": code_hashes,
    }


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(text)
            temp_path = Path(handle.name)
        os.replace(temp_path, path)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink(missing_ok=True)


def _write_json_atomic(path: Path, payload: dict) -> None:
    _write_text_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Distill one pipeline document into Markdown, layout, and a receipt."
    )
    parser.add_argument("source", help="Local source file")
    parser.add_argument("output_dir", help="Durable artifact directory")
    parser.add_argument("--source-uri", help="Public source URL or durable source identifier")
    parser.add_argument(
        "--parser",
        choices=sorted(VALID_PARSERS),
        default="auto",
        help="auto uses MarkItDown and adds DeepDoc layout for PDFs when available",
    )
    parser.add_argument(
        "--owned-root",
        help=(
            "Move source into recoverable quarantine after receipt only when it "
            "is below this pipeline-owned root"
        ),
    )
    parser.add_argument(
        "--max-file-size-mb",
        type=int,
        default=MAX_FILE_SIZE // (1024 * 1024),
    )
    parser.add_argument("--json", action="store_true", help="Print the result as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = distill_file(
            args.source,
            args.output_dir,
            source_uri=args.source_uri,
            parser=args.parser,
            owned_root=args.owned_root,
            max_file_size=args.max_file_size_mb * 1024 * 1024,
        )
    except DistillationError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    payload = asdict(result)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"[OK] {result.parser_used}: {result.markdown_path} "
            f"(receipt {result.receipt_path})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
