#!/usr/bin/env python3
"""Run the proven TDP DeepDoc shim as an isolated layout sidecar.

The script is intended to run under the TDP lane's ``.venv-ocr`` interpreter.
It keeps the vendored RAGFlow tree and its model environment in their owning
workspace while exposing a narrow, versioned JSON contract to A7.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


SCHEMA_VERSION = "opics.deepdoc-ocr.v1"
MAX_PDF_PAGES = 2_000
MAX_PAGE_PIXELS = 40_000_000
KEYWORDS = (
    "specification",
    "specifications",
    "technical data",
    "dimensions",
    "capacity",
    "travel",
    "spindle",
    "axis",
    "accuracy",
    "tolerance",
    "weight",
    "power",
    "model",
)


def extract_layout(
    source: str | Path,
    output: str | Path,
    *,
    deepdoc_root: str | Path,
    dpi: int = 200,
    max_pages: int = 8,
) -> dict:
    supplied_source = Path(source).expanduser()
    if supplied_source.is_symlink():
        raise RuntimeError("DeepDoc layout input must not be a symlink")
    source_path = supplied_source.resolve()
    output_path = Path(output).resolve()
    if output_path == source_path:
        raise RuntimeError("DeepDoc output must not overwrite its PDF input")
    root = Path(deepdoc_root).resolve()
    if not source_path.is_file() or source_path.suffix.lower() != ".pdf":
        raise RuntimeError("DeepDoc layout input must be a local PDF")
    if not (root / "deepdoc_shim.py").is_file():
        raise RuntimeError("deepdoc_shim.py is missing from the configured DeepDoc root")
    if dpi < 72 or dpi > 300:
        raise RuntimeError("dpi must be between 72 and 300")
    if max_pages < 1 or max_pages > 50:
        raise RuntimeError("max_pages must be between 1 and 50")

    sys.path.insert(0, str(root))
    import deepdoc_shim
    import fitz
    import numpy as np
    from PIL import Image

    document = fitz.open(source_path)
    if document.needs_pass:
        raise RuntimeError("encrypted PDF requires a password")
    if len(document) < 1 or len(document) > MAX_PDF_PAGES:
        raise RuntimeError(
            f"PDF page count must be between 1 and {MAX_PDF_PAGES}"
        )
    selected = _select_pages(document, max_pages)
    ocr = deepdoc_shim.ocr_engine()
    pages = []
    for page_index, reason in selected:
        page = document[page_index]
        embedded_text = page.get_text("text") or ""
        estimated_pixels = (
            page.rect.width * dpi / 72
        ) * (
            page.rect.height * dpi / 72
        )
        if estimated_pixels > MAX_PAGE_PIXELS:
            raise RuntimeError(
                f"page {page_index + 1} exceeds {MAX_PAGE_PIXELS} pixel OCR limit"
            )
        pixmap = page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB, alpha=False)
        image = np.array(
            Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        )
        boxes = []
        for item in ocr(image):
            polygon, (text, confidence) = item[0], item[1]
            xs = [float(point[0]) for point in polygon]
            ys = [float(point[1]) for point in polygon]
            if not (text or "").strip():
                continue
            boxes.append(
                {
                    "text": text,
                    "confidence": round(float(confidence), 4),
                    "x0": round(min(xs) / pixmap.width, 6),
                    "x1": round(max(xs) / pixmap.width, 6),
                    "y0": round(min(ys) / pixmap.height, 6),
                    "y1": round(max(ys) / pixmap.height, 6),
                }
            )
        boxes.sort(key=lambda item: (item["y0"], item["x0"]))
        pages.append(
            {
                "page": page_index + 1,
                "selection_reason": reason,
                "pixels": [pixmap.width, pixmap.height],
                "embedded_text_chars": len(embedded_text),
                "ocr_text": "\n".join(item["text"] for item in boxes),
                "boxes": boxes,
            }
        )

    payload = {
        "schema_version": SCHEMA_VERSION,
        "parser": "deepdoc-ocr",
        "engine_scope": "text_detection_recognition_and_positioned_boxes",
        "table_structure_recognition": "not_verified_not_used",
        "source_name": source_path.name,
        "source_sha256": _hash_file(source_path),
        "runtime": _runtime_receipt(root),
        "dpi": dpi,
        "page_count": len(document),
        "selected_pages": [index + 1 for index, _ in selected],
        "selection_truncated": len(document) > len(selected),
        "pages": pages,
    }
    _write_json_atomic(output_path, payload)
    return payload


def _select_pages(document, max_pages: int) -> list[tuple[int, str]]:
    page_info = []
    for index, page in enumerate(document):
        text = (page.get_text("text") or "").lower()
        keyword_score = sum(text.count(keyword) for keyword in KEYWORDS)
        page_info.append(
            {
                "index": index,
                "blank": not text.strip(),
                "keyword_score": keyword_score,
            }
        )

    page_count = len(page_info)
    if page_count <= max_pages:
        return [
            (item["index"], _selection_reason(item))
            for item in page_info
        ]

    chosen: dict[int, str] = {}

    # Front matter is identity/context, but it may not consume the whole budget.
    for index in range(min(2, max_pages, page_count)):
        chosen[index] = "front_matter"

    # Specification-bearing pages outrank scan-only pages. The former selector
    # assigned every blank page 10x the score of a specification page and could
    # therefore miss the only valuable page in a mixed manual.
    keyword_pages = sorted(
        (
            item
            for item in page_info
            if item["keyword_score"] and item["index"] not in chosen
        ),
        key=lambda item: (-item["keyword_score"], item["index"]),
    )
    keyword_budget = max(1, (max_pages - len(chosen) + 1) // 2)
    for item in keyword_pages[:keyword_budget]:
        if len(chosen) >= max_pages:
            break
        chosen[item["index"]] = "specification_keyword"

    # Sample scan-only pages across the whole document instead of taking the
    # first N blanks. This preserves breadth without starving keyword pages.
    remaining_budget = max_pages - len(chosen)
    blank_pages = [
        item["index"]
        for item in page_info
        if item["blank"] and item["index"] not in chosen
    ]
    blank_budget = min(len(blank_pages), max(1, remaining_budget // 2))
    for index in _evenly_spaced(blank_pages, blank_budget):
        if len(chosen) >= max_pages:
            break
        chosen[index] = "scan_sample"

    # Fill any remaining capacity with evenly distributed document coverage.
    candidates = [
        item["index"] for item in page_info if item["index"] not in chosen
    ]
    for index in _evenly_spaced(candidates, max_pages - len(chosen)):
        chosen[index] = "document_sample"

    return sorted(chosen.items())


def _selection_reason(item: dict) -> str:
    if item["keyword_score"]:
        return "specification_keyword"
    if item["blank"]:
        return "no_embedded_text"
    if item["index"] < 2:
        return "front_matter"
    return "document_sample"


def _evenly_spaced(values: list[int], count: int) -> list[int]:
    if count <= 0 or not values:
        return []
    if count >= len(values):
        return values
    if count == 1:
        return [values[len(values) // 2]]
    last = len(values) - 1
    indexes = [round(position * last / (count - 1)) for position in range(count)]
    return [values[index] for index in indexes]


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _runtime_receipt(root: Path) -> dict:
    models = root / "models" / "deepdoc"
    vendor = root / "vendor" / "ragflow-deepdoc"
    completed = subprocess.run(
        ["git", "-C", str(vendor), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    commit = completed.stdout.strip() if completed.returncode == 0 else None
    model_hashes = {
        name: _hash_file(models / name)
        for name in ("det.onnx", "rec.onnx", "ocr.res")
        if (models / name).is_file()
    }
    return {
        "ragflow_commit": commit,
        "shim_sha256": _hash_file(root / "deepdoc_shim.py"),
        "model_sha256": model_hashes,
        "license": "Apache-2.0",
    }


def _write_json_atomic(path: Path, payload: dict) -> None:
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
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            temp_path = Path(handle.name)
        os.replace(temp_path, path)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract DeepDoc PDF layout JSON.")
    parser.add_argument("source")
    parser.add_argument("output")
    parser.add_argument("--deepdoc-root", required=True)
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--max-pages", type=int, default=8)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _apply_resource_limits()
        extract_layout(
            args.source,
            args.output,
            deepdoc_root=args.deepdoc_root,
            dpi=args.dpi,
            max_pages=args.max_pages,
        )
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


def _apply_resource_limits() -> None:
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (300, 300))
    resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))


if __name__ == "__main__":
    raise SystemExit(main())
