"""
Folder batch conversion for MarkItDown.

This is intentionally separate from the FastAPI single-file workflow so the
desktop app remains stable while batch jobs can evolve independently.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import warnings
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from markdown_cleanup import normalize_markdown_for_source
from spreadsheet_convert import (
    SpreadsheetConversionOptions,
    convert_spreadsheet_to_path,
    is_spreadsheet_path,
)


MAX_FILE_SIZE = 50 * 1024 * 1024
VALID_ENGINES = {"standard", "academic", "auto"}

warnings.filterwarnings(
    "ignore",
    message="Couldn't find ffmpeg or avconv.*",
    category=RuntimeWarning,
    module="pydub.utils",
)
warnings.filterwarnings(
    "ignore",
    message="Unsupported Windows version.*ONNX Runtime supports Windows 10 and above, only.",
    category=UserWarning,
    module="onnxruntime.capi.onnxruntime_validation",
)


class BatchConfigurationError(ValueError):
    """Raised when a batch job cannot be planned safely."""


@dataclass
class BatchFileResult:
    source: str
    output: str | None
    status: str
    engine_requested: str
    engine_used: str | None = None
    file_size: int = 0
    markdown_length: int = 0
    warning: str | None = None
    error: str | None = None


@dataclass
class BatchSummary:
    report_path: str
    results: list[BatchFileResult]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def converted(self) -> int:
        return sum(1 for item in self.results if item.status == "converted")

    @property
    def skipped(self) -> int:
        return sum(1 for item in self.results if item.status == "skipped")

    @property
    def failed(self) -> int:
        return sum(1 for item in self.results if item.status == "error")

    @property
    def success(self) -> bool:
        return self.failed == 0

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "total": self.total,
            "converted": self.converted,
            "skipped": self.skipped,
            "failed": self.failed,
            "report_path": self.report_path,
            "results": [asdict(item) for item in self.results],
        }


def run_batch(
    input_dir: str | Path,
    output_dir: str | Path,
    *,
    engine: str = "standard",
    overwrite: bool = False,
    max_file_size: int = MAX_FILE_SIZE,
    md_engine=None,
    mineru_client=None,
    spreadsheet_options: SpreadsheetConversionOptions | None = None,
    report_name: str = "batch-results.json",
) -> BatchSummary:
    engine = engine.lower()
    if engine not in VALID_ENGINES:
        raise BatchConfigurationError(f"Engine must be one of: {', '.join(sorted(VALID_ENGINES))}")
    if max_file_size < 1:
        raise BatchConfigurationError("Max file size must be at least 1 byte")

    input_path = Path(input_dir).resolve()
    output_path = Path(output_dir).resolve()
    _validate_directories(input_path, output_path)
    output_path.mkdir(parents=True, exist_ok=True)

    report_path = output_path / report_name
    results: list[BatchFileResult] = []
    planned_files = _plan_files(input_path, output_path, engine, results)

    def get_markitdown():
        nonlocal md_engine
        if md_engine is None:
            md_engine = _build_markitdown()
        return md_engine

    mineru_checked = mineru_client is not None

    def get_mineru():
        nonlocal mineru_checked, mineru_client
        if not mineru_checked:
            mineru_client = _build_mineru()
            mineru_checked = True
        return mineru_client

    for source, target in planned_files:
        results.append(
            _convert_one(
                source,
                target,
                input_path,
                output_path,
                engine,
                overwrite,
                max_file_size,
                get_markitdown,
                get_mineru,
                spreadsheet_options,
            )
        )

    summary = BatchSummary(report_path=str(report_path), results=results)
    _write_json_atomic(report_path, summary.to_dict())
    return summary


def _validate_directories(input_path: Path, output_path: Path) -> None:
    if not input_path.exists():
        raise BatchConfigurationError(f"Input directory does not exist: {input_path}")
    if not input_path.is_dir():
        raise BatchConfigurationError(f"Input path is not a directory: {input_path}")
    if output_path.exists() and not output_path.is_dir():
        raise BatchConfigurationError(f"Output path is not a directory: {output_path}")
    if output_path == input_path or _is_relative_to(output_path, input_path):
        raise BatchConfigurationError("Output directory must not be inside the input directory")


def _plan_files(
    input_path: Path,
    output_path: Path,
    engine: str,
    results: list[BatchFileResult],
) -> list[tuple[Path, Path]]:
    planned_files: list[tuple[Path, Path]] = []
    seen_outputs: dict[Path, str] = {}

    for source in sorted(item for item in input_path.rglob("*") if item.is_file()):
        target = _target_path(source, input_path, output_path)
        source_label = _relative_posix(source, input_path)
        output_label = _relative_posix(target, output_path)

        if target in seen_outputs:
            results.append(
                BatchFileResult(
                    source=source_label,
                    output=output_label,
                    status="error",
                    engine_requested=engine,
                    file_size=source.stat().st_size,
                    error=f"Output collision with {seen_outputs[target]}",
                )
            )
            continue

        seen_outputs[target] = source_label
        planned_files.append((source, target))

    return planned_files


def _convert_one(
    source: Path,
    target: Path,
    input_path: Path,
    output_path: Path,
    engine: str,
    overwrite: bool,
    max_file_size: int,
    get_markitdown: Callable,
    get_mineru: Callable,
    spreadsheet_options: SpreadsheetConversionOptions | None,
) -> BatchFileResult:
    source_label = _relative_posix(source, input_path)
    output_label = _relative_posix(target, output_path)
    file_size = source.stat().st_size

    if target.exists() and not overwrite:
        return BatchFileResult(
            source=source_label,
            output=output_label,
            status="skipped",
            engine_requested=engine,
            file_size=file_size,
            warning="Output already exists. Use --overwrite to replace it.",
        )

    if file_size > max_file_size:
        return BatchFileResult(
            source=source_label,
            output=output_label,
            status="error",
            engine_requested=engine,
            file_size=file_size,
            error=f"File too large. Maximum size is {max_file_size} bytes.",
        )

    try:
        if is_spreadsheet_path(source):
            result = convert_spreadsheet_to_path(source, target, spreadsheet_options)
            warnings = []
            if engine == "academic":
                warnings.append("Academic engine only supports PDF. Using spreadsheet converter.")
            warnings.extend(result.warnings)
            return BatchFileResult(
                source=source_label,
                output=output_label,
                status="converted",
                engine_requested=engine,
                engine_used="standard",
                file_size=file_size,
                markdown_length=result.markdown_length,
                warning=" ".join(warnings) if warnings else None,
            )

        markdown_text, engine_used, warning = _convert_source(source, engine, get_markitdown, get_mineru)
        _write_text_atomic(target, markdown_text)
        return BatchFileResult(
            source=source_label,
            output=output_label,
            status="converted",
            engine_requested=engine,
            engine_used=engine_used,
            file_size=file_size,
            markdown_length=len(markdown_text),
            warning=warning,
        )
    except Exception as exc:
        return BatchFileResult(
            source=source_label,
            output=output_label,
            status="error",
            engine_requested=engine,
            file_size=file_size,
            error=f"Conversion failed: {exc}",
        )


def _convert_source(source: Path, engine: str, get_markitdown: Callable, get_mineru: Callable) -> tuple[str, str, str | None]:
    ext = source.suffix.lower()
    warning = None

    if engine == "academic" and ext != ".pdf":
        warning = "Academic engine only supports PDF. Using Standard."
        return _convert_standard(source, get_markitdown), "standard", warning

    if engine in {"academic", "auto"} and ext == ".pdf":
        mineru = get_mineru()
        if mineru is not None:
            try:
                result = mineru.flash_extract(str(source))
                return result.markdown or "", "academic", None
            except Exception as exc:
                warning = f"MinerU failed ({str(exc)[:80]}), fell back to Standard."
        elif engine == "academic":
            warning = "MinerU not available, using Standard engine."

    return _convert_standard(source, get_markitdown), "standard", warning


def _convert_standard(source: Path, get_markitdown: Callable) -> str:
    result = get_markitdown().convert(str(source))
    return normalize_markdown_for_source(result.text_content or "", source)


def _target_path(source: Path, input_path: Path, output_path: Path) -> Path:
    relative = source.relative_to(input_path)
    if relative.suffix:
        relative = relative.with_suffix(".md")
    else:
        relative = Path(f"{relative}.md")
    return output_path / relative


def _relative_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp.write(text)
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, path)
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def _write_json_atomic(path: Path, payload: dict) -> None:
    _write_text_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _build_markitdown():
    from markitdown import MarkItDown

    return MarkItDown()


def _build_mineru():
    try:
        from mineru import MinerU
    except ImportError:
        return None
    try:
        return MinerU()
    except Exception:
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Batch convert a folder of files to Markdown.")
    parser.add_argument("input_dir", help="Folder containing source files.")
    parser.add_argument("output_dir", help="Folder where Markdown files and batch-results.json are written.")
    parser.add_argument(
        "--engine",
        choices=sorted(VALID_ENGINES),
        default="standard",
        help="Conversion engine. 'academic' uses MinerU for PDFs and Standard for other files.",
    )
    parser.add_argument("--overwrite", action="store_true", help="Replace existing Markdown outputs.")
    parser.add_argument(
        "--max-file-size-mb",
        type=int,
        default=MAX_FILE_SIZE // (1024 * 1024),
        help="Per-file size limit in MB.",
    )
    parser.add_argument("--json", action="store_true", help="Print the full machine-readable report to stdout.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        summary = run_batch(
            args.input_dir,
            args.output_dir,
            engine=args.engine,
            overwrite=args.overwrite,
            max_file_size=args.max_file_size_mb * 1024 * 1024,
        )
    except BatchConfigurationError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(summary.to_dict(), indent=2, sort_keys=True))
    else:
        print(
            f"[OK] Batch complete: {summary.converted} converted, "
            f"{summary.skipped} skipped, {summary.failed} failed."
        )
        print(f"Report: {summary.report_path}")

    for item in summary.results:
        if item.status == "error":
            print(f"[ERROR] {item.source}: {item.error}", file=sys.stderr)

    return 1 if summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
