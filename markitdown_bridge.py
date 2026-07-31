#!/usr/bin/env python3
"""Resource-bounded subprocess bridge for the OutPace MarkItDown engine."""

from __future__ import annotations

import argparse
import os
import resource
import sys
import zipfile
from pathlib import Path


MAX_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10_000
MAX_ARCHIVE_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_MEMBER_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_COMPRESSION_RATIO = 250
ARCHIVE_SUFFIXES = {".docx", ".epub", ".pptx", ".xlsx", ".zip"}


def convert_bounded(
    source: str | Path,
    output: str | Path,
    *,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
) -> int:
    supplied_source = Path(source).expanduser()
    if supplied_source.is_symlink():
        raise RuntimeError("MarkItDown input must not be a symlink")
    source_path = supplied_source.resolve()
    output_path = Path(output).resolve()
    if not source_path.is_file():
        raise RuntimeError("MarkItDown input must be a regular file")
    if output_path == source_path:
        raise RuntimeError("MarkItDown output must not overwrite its input")
    if max_output_bytes < 1 or max_output_bytes > MAX_OUTPUT_BYTES:
        raise RuntimeError("invalid MarkItDown output limit")

    _preflight_archive(source_path)
    _apply_resource_limits(max_output_bytes)

    from markitdown import MarkItDown

    result = MarkItDown().convert(str(source_path))
    text = result.text_content or ""
    encoded = text.encode("utf-8")
    if len(encoded) > max_output_bytes:
        raise RuntimeError(
            f"MarkItDown output exceeds {max_output_bytes} byte limit"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        output_path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        _write_all(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return len(encoded)


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise RuntimeError("MarkItDown output write made no progress")
        remaining = remaining[written:]


def _preflight_archive(path: Path) -> None:
    if path.suffix.lower() not in ARCHIVE_SUFFIXES or not zipfile.is_zipfile(path):
        return
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise RuntimeError("archive member count exceeds the safety limit")
        expanded = 0
        compressed = 0
        for member in members:
            if member.flag_bits & 0x1:
                raise RuntimeError("encrypted archive members are not supported")
            if member.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                raise RuntimeError("archive member exceeds the expanded-size limit")
            expanded += member.file_size
            compressed += member.compress_size
            if not member.is_dir():
                with archive.open(member) as handle:
                    if handle.read(4) in {
                        b"PK\x03\x04",
                        b"PK\x05\x06",
                        b"PK\x07\x08",
                    }:
                        raise RuntimeError("nested archives are not supported")
        if expanded > MAX_ARCHIVE_EXPANDED_BYTES:
            raise RuntimeError("archive expanded size exceeds the safety limit")
        if expanded and expanded / max(compressed, 1) > MAX_ARCHIVE_COMPRESSION_RATIO:
            raise RuntimeError("archive compression ratio exceeds the safety limit")


def _apply_resource_limits(max_output_bytes: int) -> None:
    limits = (
        ("RLIMIT_CPU", 60),
        ("RLIMIT_AS", 1536 * 1024 * 1024),
        ("RLIMIT_DATA", 1536 * 1024 * 1024),
        ("RLIMIT_FSIZE", max_output_bytes + 1024 * 1024),
        ("RLIMIT_NOFILE", 128),
    )
    for name, limit in limits:
        key = getattr(resource, name, None)
        if key is None:
            continue
        current_soft, current_hard = resource.getrlimit(key)
        soft = (
            limit
            if current_hard == resource.RLIM_INFINITY
            else min(limit, current_hard)
        )
        if current_soft != resource.RLIM_INFINITY:
            soft = min(soft, current_soft)
        try:
            resource.setrlimit(key, (soft, current_hard))
        except (OSError, ValueError) as exc:
            if name in {"RLIMIT_AS", "RLIMIT_DATA"}:
                # Darwin refuses useful low virtual-address limits after the
                # interpreter has mapped frameworks. The parent enforces a
                # live process-group RSS ceiling instead.
                continue
            raise RuntimeError(f"cannot enforce {name} safety limit: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run OutPace MarkItDown under bounded process resources."
    )
    parser.add_argument("source")
    parser.add_argument("output")
    parser.add_argument("--max-output-bytes", type=int, default=MAX_OUTPUT_BYTES)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        convert_bounded(
            args.source,
            args.output,
            max_output_bytes=args.max_output_bytes,
        )
    except Exception as exc:  # noqa: BLE001 - child reports a bounded failure
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
