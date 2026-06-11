"""
MD_CREATOR — Universal Markdown Converter
FastAPI backend powered by Microsoft MarkItDown + MinerU
"""

import asyncio
import html
import os
import secrets
import shutil
import time
import tempfile
import traceback
import warnings
import zipfile
from dataclasses import asdict
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, UploadFile, File, Form, Header, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

# Optional MarkItDown dependencies emit noisy import-time warnings in CI/local
# environments even when the affected converters are not being used.
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

from batch_convert import BatchConfigurationError, run_batch
from diagnostics import build_diagnostics_report, record_recent_failure
from markitdown import MarkItDown
from markdown_cleanup import normalize_markdown_for_source
from spreadsheet_convert import (
    SpreadsheetConversionError,
    SpreadsheetConversionOptions,
    SpreadsheetLimitError,
    convert_spreadsheet_to_path,
    is_spreadsheet_path,
)

# MinerU cloud SDK (optional — graceful if unavailable)
try:
    from mineru import MinerU
    MINERU_AVAILABLE = True
except ImportError:
    MINERU_AVAILABLE = False

# ── App Setup ──────────────────────────────────────────────────────────────────

APP_NAME = os.getenv("APP_NAME") or Path(__file__).resolve().parent.name
APP_HOST = os.getenv("APP_HOST", "127.0.0.1")
APP_PORT = int(os.getenv("APP_PORT", "8000"))
APP_RELOAD = os.getenv("APP_RELOAD", os.getenv("MD_CREATOR_RELOAD", "0")) == "1"
APP_TOKEN = os.getenv("APP_TOKEN") or secrets.token_urlsafe(32)
CONVERSION_CONCURRENCY = max(1, int(os.getenv("CONVERSION_CONCURRENCY", "1")))
CONVERSION_ARTIFACT_DIR = Path(
    os.getenv(
        "CONVERSION_ARTIFACT_DIR",
        str(Path(tempfile.gettempdir()) / "mdcreator_artifacts"),
    )
)
CONVERSION_ARTIFACT_TTL_SECONDS = max(60, int(os.getenv("CONVERSION_ARTIFACT_TTL_SECONDS", "3600")))
BULK_ARTIFACT_DIR = Path(
    os.getenv(
        "BULK_ARTIFACT_DIR",
        str(Path(tempfile.gettempdir()) / "mdcreator_bulk_jobs"),
    )
)
BULK_ARTIFACT_TTL_SECONDS = max(60, int(os.getenv("BULK_ARTIFACT_TTL_SECONDS", "3600")))
BULK_MAX_FILES = max(1, int(os.getenv("BULK_MAX_FILES", "200")))
BULK_MAX_TOTAL_SIZE = max(50 * 1024 * 1024, int(os.getenv("BULK_MAX_TOTAL_SIZE", str(500 * 1024 * 1024))))
SPREADSHEET_OPTIONS = SpreadsheetConversionOptions(
    preview_rows_per_sheet=max(1, int(os.getenv("SPREADSHEET_PREVIEW_ROWS_PER_SHEET", "100"))),
    preview_max_chars=max(1, int(os.getenv("SPREADSHEET_PREVIEW_MAX_CHARS", "500000"))),
    max_sheets=max(1, int(os.getenv("SPREADSHEET_MAX_SHEETS", "100"))),
    max_columns=max(1, int(os.getenv("SPREADSHEET_MAX_COLUMNS", "256"))),
    max_cell_chars=max(1, int(os.getenv("SPREADSHEET_MAX_CELL_CHARS", "2000"))),
    max_output_chars=max(1, int(os.getenv("SPREADSHEET_MAX_OUTPUT_CHARS", str(25 * 1024 * 1024)))),
    timeout_seconds=max(1, int(os.getenv("SPREADSHEET_TIMEOUT_SECONDS", "120"))),
    max_xlsx_uncompressed_bytes=max(
        1,
        int(os.getenv("SPREADSHEET_MAX_XLSX_UNCOMPRESSED_BYTES", str(512 * 1024 * 1024))),
    ),
    max_xlsx_compression_ratio=max(1, int(os.getenv("SPREADSHEET_MAX_XLSX_COMPRESSION_RATIO", "200"))),
    header_scan_rows=max(1, int(os.getenv("SPREADSHEET_HEADER_SCAN_ROWS", "25"))),
)

app = FastAPI(
    title=APP_NAME,
    description="Universal Markdown Converter — convert any file to Markdown",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        f"http://{APP_HOST}:{APP_PORT}",
        f"http://127.0.0.1:{APP_PORT}",
        f"http://localhost:{APP_PORT}",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def revalidate_frontend_assets(request: Request, call_next):
    """Force browsers to revalidate the SPA and its assets on every load.

    Without this, Chrome's heuristic caching can keep executing a stale
    app.js for days after the code on disk has changed.
    """
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# MarkItDown engine — singleton
md_engine = MarkItDown()

# MinerU engine — cloud SDK (no token = flash_extract mode)
try:
    mineru_client = MinerU() if MINERU_AVAILABLE else None
except Exception:
    traceback.print_exc()
    MINERU_AVAILABLE = False
    mineru_client = None

# Engine metadata
ENGINES = [
    {
        "id": "standard",
        "name": "Standard",
        "description": "Fast, broad format support (25+ types)",
        "provider": "Microsoft MarkItDown",
        "badge": "All Formats",
    },
    {
        "id": "academic",
        "name": "Academic",
        "description": "High-fidelity PDF parsing (math, tables, layouts)",
        "provider": "MinerU (OpenDataLab)",
        "badge": "PDF Expert",
        "available": MINERU_AVAILABLE,
        "note": "Cloud-processed. 10MB / 20 page limit (flash mode).",
    },
]

# Max upload size: 50 MB
MAX_FILE_SIZE = 50 * 1024 * 1024
MAX_REQUEST_SIZE = MAX_FILE_SIZE + (1024 * 1024)
UPLOAD_CHUNK_SIZE = 1024 * 1024
conversion_semaphore = asyncio.Semaphore(CONVERSION_CONCURRENCY)
# Bulk batches run on their own lane with their own engine instances so a long
# batch never starves single-file conversions waiting on conversion_semaphore.
bulk_semaphore = asyncio.Semaphore(max(1, int(os.getenv("BULK_CONCURRENCY", "1"))))
bulk_jobs: dict[str, dict] = {}
RECENT_FAILURES: list[dict] = []

# Supported formats metadata
SUPPORTED_FORMATS = [
    {"ext": ".pdf", "label": "PDF", "icon": "📄", "category": "Documents"},
    {"ext": ".docx", "label": "Word", "icon": "📝", "category": "Documents"},
    {"ext": ".pptx", "label": "PowerPoint", "icon": "📊", "category": "Documents"},
    {"ext": ".xlsx", "label": "Excel", "icon": "📈", "category": "Documents"},
    {"ext": ".xls", "label": "Excel (Legacy)", "icon": "📈", "category": "Documents"},
    {"ext": ".msg", "label": "Outlook", "icon": "📧", "category": "Documents"},
    {"ext": ".epub", "label": "EPUB", "icon": "📚", "category": "Documents"},
    {"ext": ".html", "label": "HTML", "icon": "🌐", "category": "Web"},
    {"ext": ".htm", "label": "HTML", "icon": "🌐", "category": "Web"},
    {"ext": ".csv", "label": "CSV", "icon": "📋", "category": "Data"},
    {"ext": ".json", "label": "JSON", "icon": "🔧", "category": "Data"},
    {"ext": ".xml", "label": "XML", "icon": "📦", "category": "Data"},
    {"ext": ".jpg", "label": "JPEG", "icon": "🖼️", "category": "Images"},
    {"ext": ".jpeg", "label": "JPEG", "icon": "🖼️", "category": "Images"},
    {"ext": ".png", "label": "PNG", "icon": "🖼️", "category": "Images"},
    {"ext": ".gif", "label": "GIF", "icon": "🖼️", "category": "Images"},
    {"ext": ".bmp", "label": "BMP", "icon": "🖼️", "category": "Images"},
    {"ext": ".tiff", "label": "TIFF", "icon": "🖼️", "category": "Images"},
    {"ext": ".webp", "label": "WebP", "icon": "🖼️", "category": "Images"},
    {"ext": ".zip", "label": "ZIP", "icon": "📦", "category": "Archives"},
    {"ext": ".wav", "label": "WAV Audio", "icon": "🎵", "category": "Audio"},
    {"ext": ".mp3", "label": "MP3 Audio", "icon": "🎵", "category": "Audio"},
    {"ext": ".txt", "label": "Plain Text", "icon": "📃", "category": "Text"},
    {"ext": ".md", "label": "Markdown", "icon": "✏️", "category": "Text"},
    {"ext": ".rst", "label": "reStructuredText", "icon": "📃", "category": "Text"},
]


async def run_converter(func, *args, **kwargs):
    """Run blocking converter SDK calls without blocking the event loop."""
    async with conversion_semaphore:
        return await run_in_threadpool(func, *args, **kwargs)


def remember_failure(category: str, message: object, filename: str | None = None) -> None:
    """Record a small redacted failure hint for diagnostics."""
    extension = Path(filename).suffix.lower() if filename else None
    record_recent_failure(RECENT_FAILURES, category, message, source_extension=extension)


def build_diagnostics_payload() -> dict:
    static_root = Path(__file__).parent / "static"
    return build_diagnostics_report(
        app={
            "name": APP_NAME,
            "host": APP_HOST,
            "port": APP_PORT,
            "reload": APP_RELOAD,
            "token_configured": bool(APP_TOKEN),
            "conversion_concurrency": CONVERSION_CONCURRENCY,
        },
        engines={
            "standard": {"available": True, "provider": "Microsoft MarkItDown"},
            "academic": {"available": MINERU_AVAILABLE, "provider": "MinerU"},
            "spreadsheet": {"available": True, "provider": "local streaming converter"},
            "bulk": {"available": True, "provider": "local batch converter"},
        },
        limits={
            "max_file_size": MAX_FILE_SIZE,
            "max_request_size": MAX_REQUEST_SIZE,
            "bulk_max_files": BULK_MAX_FILES,
            "bulk_max_total_size": BULK_MAX_TOTAL_SIZE,
            "upload_chunk_size": UPLOAD_CHUNK_SIZE,
            "conversion_artifact_ttl_seconds": CONVERSION_ARTIFACT_TTL_SECONDS,
            "bulk_artifact_ttl_seconds": BULK_ARTIFACT_TTL_SECONDS,
        },
        artifact_dirs={
            "conversion": CONVERSION_ARTIFACT_DIR,
            "bulk": BULK_ARTIFACT_DIR,
        },
        static_files={
            "index": static_root / "index.html",
            "markdown_it": static_root / "js" / "markdown-it.min.js",
        },
        bulk_jobs=bulk_jobs,
        recent_failures=RECENT_FAILURES,
        supported_formats_count=len(SUPPORTED_FORMATS),
        spreadsheet_options=asdict(SPREADSHEET_OPTIONS),
    )


def cleanup_conversion_artifacts() -> None:
    """Remove expired conversion artifacts opportunistically."""
    CONVERSION_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - CONVERSION_ARTIFACT_TTL_SECONDS
    for path in CONVERSION_ARTIFACT_DIR.glob("*.md"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except OSError:
            pass


def create_conversion_artifact_path() -> tuple[str, Path]:
    cleanup_conversion_artifacts()
    CONVERSION_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    artifact_id = secrets.token_urlsafe(24)
    return artifact_id, CONVERSION_ARTIFACT_DIR / f"{artifact_id}.md"


def resolve_conversion_artifact_path(artifact_id: str) -> Path | None:
    if not artifact_id or not all(ch.isalnum() or ch in {"-", "_"} for ch in artifact_id):
        return None
    root = CONVERSION_ARTIFACT_DIR.resolve()
    path = (root / f"{artifact_id}.md").resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path


def cleanup_bulk_artifacts() -> None:
    """Remove expired bulk job directories and stale in-memory records."""
    BULK_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - BULK_ARTIFACT_TTL_SECONDS

    for job_id, job in list(bulk_jobs.items()):
        if job.get("status") in {"queued", "running"}:
            continue
        if float(job.get("updated_at", job.get("created_at", 0))) < cutoff:
            bulk_jobs.pop(job_id, None)

    for path in BULK_ARTIFACT_DIR.iterdir():
        try:
            if path.name in bulk_jobs and bulk_jobs[path.name].get("status") in {"queued", "running"}:
                continue
            if path.stat().st_mtime >= cutoff:
                continue
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            pass


def create_bulk_job_paths() -> tuple[str, Path, Path, Path, Path]:
    cleanup_bulk_artifacts()
    BULK_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    job_id = secrets.token_urlsafe(24)
    job_dir = BULK_ARTIFACT_DIR / job_id
    input_dir = job_dir / "input"
    output_dir = job_dir / "output"
    zip_path = job_dir / "converted.zip"
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    return job_id, job_dir, input_dir, output_dir, zip_path


def resolve_bulk_job_dir(job_id: str) -> Path | None:
    if not job_id or not all(ch.isalnum() or ch in {"-", "_"} for ch in job_id):
        return None
    root = BULK_ARTIFACT_DIR.resolve()
    path = (root / job_id).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path


def safe_bulk_relative_path(raw_path: str | None, fallback_filename: str | None) -> Path:
    candidate = (raw_path or fallback_filename or "upload").replace("\\", "/").strip().lstrip("/")
    if not candidate:
        candidate = fallback_filename or "upload"

    parts = [part.strip() for part in candidate.split("/") if part.strip()]
    invalid = (
        not parts
        or any(part in {".", ".."} for part in parts)
        or any(":" in part or "\x00" in part for part in parts)
    )
    if invalid:
        raise HTTPException(status_code=400, detail=f"Invalid upload path: {raw_path or fallback_filename}")

    return Path(*parts)


async def save_bulk_uploads(files: list[UploadFile], paths: list[str] | None, input_dir: Path) -> tuple[int, int]:
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")
    if len(files) > BULK_MAX_FILES:
        raise HTTPException(status_code=413, detail=f"Too many files. Maximum batch size is {BULK_MAX_FILES} files.")

    root = input_dir.resolve()
    seen_paths: set[str] = set()
    total_size = 0

    for index, upload in enumerate(files):
        if not upload.filename:
            raise HTTPException(status_code=400, detail="Every uploaded file must include a filename")

        raw_path = paths[index] if paths and index < len(paths) else None
        relative_path = safe_bulk_relative_path(raw_path, upload.filename)
        relative_label = relative_path.as_posix()
        if relative_label in seen_paths:
            raise HTTPException(status_code=400, detail=f"Duplicate upload path: {relative_label}")
        seen_paths.add(relative_label)

        target_path = (root / relative_path).resolve()
        try:
            target_path.relative_to(root)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid upload path: {relative_label}")

        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            file_size = 0
            with target_path.open("wb") as target:
                while True:
                    chunk = await upload.read(UPLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    file_size += len(chunk)
                    total_size += len(chunk)
                    if file_size > MAX_FILE_SIZE:
                        raise HTTPException(
                            status_code=413,
                            detail=f"{upload.filename} is too large. Maximum size is {MAX_FILE_SIZE // (1024*1024)}MB per file.",
                        )
                    if total_size > BULK_MAX_TOTAL_SIZE:
                        raise HTTPException(
                            status_code=413,
                            detail=f"Batch too large. Maximum total upload size is {BULK_MAX_TOTAL_SIZE // (1024*1024)}MB.",
                        )
                    target.write(chunk)
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"Could not save upload path {relative_label}: {exc}")

    return len(files), total_size


def public_bulk_job_state(job: dict) -> dict:
    state = {
        "id": job["id"],
        "status": job["status"],
        "engine": job["engine"],
        "file_count": job.get("file_count", 0),
        "total_size": job.get("total_size", 0),
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "summary": job.get("summary"),
        "error": job.get("error"),
        "progress": job.get("progress"),
    }
    if job.get("status") == "completed":
        state["download_id"] = job["id"]
        state["download_filename"] = job.get("download_filename", "mdcreator-bulk.zip")
        state["download_url"] = f"/api/bulk/jobs/{job['id']}/download"
    return state


def create_bulk_zip(output_dir: Path, zip_path: Path) -> None:
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".zip",
            prefix=f".{zip_path.stem}.",
            dir=zip_path.parent,
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)

        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(item for item in output_dir.rglob("*") if item.is_file()):
                archive.write(path, path.relative_to(output_dir).as_posix())

        os.replace(tmp_path, zip_path)
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


async def run_bulk_conversion_job(job_id: str) -> None:
    job = bulk_jobs.get(job_id)
    if job is None:
        return

    job["status"] = "running"
    job["updated_at"] = time.time()

    def report_progress(done: int, total: int, current: str | None) -> None:
        job["progress"] = {"done": done, "total": total, "current": current}
        job["updated_at"] = time.time()

    try:
        async with bulk_semaphore:
            # md_engine/mineru_client are intentionally None: run_batch builds
            # its own instances so the shared single-file engines stay free.
            summary = await run_in_threadpool(
                run_batch,
                Path(job["input_dir"]),
                Path(job["output_dir"]),
                engine=job["engine"],
                overwrite=True,
                max_file_size=MAX_FILE_SIZE,
                md_engine=None,
                mineru_client=None,
                spreadsheet_options=SPREADSHEET_OPTIONS,
                progress_callback=report_progress,
            )
        create_bulk_zip(Path(job["output_dir"]), Path(job["zip_path"]))
        job["summary"] = summary.to_dict()
        job["status"] = "completed"
        job["download_filename"] = f"mdcreator-bulk-{job_id[:8]}.zip"
    except BatchConfigurationError as exc:
        job["status"] = "failed"
        job["error"] = str(exc)
        remember_failure("api_failed", exc)
    except Exception as exc:
        job["status"] = "failed"
        job["error"] = f"Bulk conversion failed: {exc}"
        remember_failure("conversion_failed", exc)
    finally:
        job["updated_at"] = time.time()


# ── API Routes ─────────────────────────────────────────────────────────────────

@app.get("/api/formats")
async def get_formats():
    """Return list of supported input formats."""
    return JSONResponse(content={"formats": SUPPORTED_FORMATS})


@app.get("/api/health")
async def get_health():
    """Return local health and launch metadata."""
    return JSONResponse(
        content={
            "status": "ok",
            "app_name": APP_NAME,
            "host": APP_HOST,
            "port": APP_PORT,
            "engines": {
                "standard": True,
                "academic": MINERU_AVAILABLE,
            },
            "mineru_available": MINERU_AVAILABLE,
            "spreadsheet_converter": True,
            "bulk_converter": True,
            "diagnostics": True,
        }
    )


@app.get("/api/engines")
async def get_engines():
    """Return list of available conversion engines."""
    return JSONResponse(content={"engines": ENGINES})


@app.get("/api/diagnostics")
async def get_diagnostics(
    x_mdcreator_token: str = Header(default="", alias="X-MD-Creator-Token"),
):
    """Return a small redacted diagnostics packet for local troubleshooting."""
    if not secrets.compare_digest(x_mdcreator_token, APP_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid local session token")

    cleanup_conversion_artifacts()
    cleanup_bulk_artifacts()
    return JSONResponse(content=build_diagnostics_payload())


@app.get("/api/download/{artifact_id}")
async def download_conversion_artifact(
    artifact_id: str,
    x_mdcreator_token: str = Header(default="", alias="X-MD-Creator-Token"),
):
    """Download a generated conversion artifact."""
    if not secrets.compare_digest(x_mdcreator_token, APP_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid local session token")

    cleanup_conversion_artifacts()
    artifact_path = resolve_conversion_artifact_path(artifact_id)
    if artifact_path is None or not artifact_path.exists():
        raise HTTPException(status_code=404, detail="Converted file is no longer available")

    return FileResponse(
        artifact_path,
        media_type="text/markdown; charset=utf-8",
        filename="converted.md",
    )


@app.post("/api/bulk/convert")
async def create_bulk_conversion(
    request: Request,
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    paths: list[str] | None = Form(None),
    engine: str = Form("standard"),
    x_mdcreator_token: str = Header(default="", alias="X-MD-Creator-Token"),
):
    """Create a temporary bulk conversion job from uploaded files."""
    if not secrets.compare_digest(x_mdcreator_token, APP_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid local session token")

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > BULK_MAX_TOTAL_SIZE + (len(files) * 1024 * 1024):
                raise HTTPException(
                    status_code=413,
                    detail=f"Batch too large. Maximum total upload size is {BULK_MAX_TOTAL_SIZE // (1024*1024)}MB.",
                )
        except ValueError:
            pass

    engine_used = engine.lower()
    if engine_used not in {"standard", "academic", "auto"}:
        engine_used = "standard"

    job_id, job_dir, input_dir, output_dir, zip_path = create_bulk_job_paths()
    now = time.time()
    bulk_jobs[job_id] = {
        "id": job_id,
        "status": "queued",
        "engine": engine_used,
        "created_at": now,
        "updated_at": now,
        "file_count": 0,
        "total_size": 0,
        "job_dir": str(job_dir),
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "zip_path": str(zip_path),
        "summary": None,
        "error": None,
    }

    try:
        file_count, total_size = await save_bulk_uploads(files, paths, input_dir)
        bulk_jobs[job_id]["file_count"] = file_count
        bulk_jobs[job_id]["total_size"] = total_size
        bulk_jobs[job_id]["updated_at"] = time.time()
    except HTTPException as exc:
        category = "cap_tripped" if exc.status_code == 413 else "api_failed"
        remember_failure(category, exc.detail)
        bulk_jobs.pop(job_id, None)
        shutil.rmtree(job_dir, ignore_errors=True)
        raise

    background_tasks.add_task(run_bulk_conversion_job, job_id)
    return JSONResponse(
        status_code=202,
        content={"job": public_bulk_job_state(bulk_jobs[job_id])},
        background=background_tasks,
    )


@app.get("/api/bulk/jobs/{job_id}")
async def get_bulk_conversion_job(
    job_id: str,
    x_mdcreator_token: str = Header(default="", alias="X-MD-Creator-Token"),
):
    """Return current bulk conversion job state."""
    if not secrets.compare_digest(x_mdcreator_token, APP_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid local session token")

    cleanup_bulk_artifacts()
    if resolve_bulk_job_dir(job_id) is None:
        raise HTTPException(status_code=404, detail="Bulk job not found")
    job = bulk_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Bulk job not found")
    return JSONResponse(content={"job": public_bulk_job_state(job)})


@app.get("/api/bulk/jobs/{job_id}/download")
async def download_bulk_conversion_job(
    job_id: str,
    x_mdcreator_token: str = Header(default="", alias="X-MD-Creator-Token"),
):
    """Download a completed bulk conversion ZIP artifact."""
    if not secrets.compare_digest(x_mdcreator_token, APP_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid local session token")

    cleanup_bulk_artifacts()
    if resolve_bulk_job_dir(job_id) is None:
        raise HTTPException(status_code=404, detail="Bulk job not found")
    job = bulk_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Bulk job not found")
    if job.get("status") != "completed":
        raise HTTPException(status_code=409, detail="Bulk job is not complete")

    zip_path = Path(job["zip_path"])
    if not zip_path.exists():
        raise HTTPException(status_code=404, detail="Bulk download is no longer available")

    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=job.get("download_filename", "mdcreator-bulk.zip"),
    )


@app.post("/api/convert")
async def convert_file(
    request: Request,
    file: UploadFile = File(...),
    engine: str = Form("standard"),
    x_mdcreator_token: str = Header(default="", alias="X-MD-Creator-Token"),
):
    """
    Convert an uploaded file to Markdown.
    Engine: 'standard' (MarkItDown) or 'academic' (MinerU).
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    if not secrets.compare_digest(x_mdcreator_token, APP_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid local session token")

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_REQUEST_SIZE:
                remember_failure("cap_tripped", "request exceeded maximum file size", file.filename)
                raise HTTPException(
                    status_code=413,
                    detail=f"Request too large. Maximum file size is {MAX_FILE_SIZE // (1024*1024)}MB",
                )
        except ValueError:
            pass

    # Detect extension
    ext = Path(file.filename).suffix.lower()

    # Write to temp file
    tmp_path = None
    try:
        start_time = time.time()

        total_size = 0
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext, prefix="mdcreator_") as tmp:
            tmp_path = tmp.name
            while True:
                chunk = await file.read(UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                total_size += len(chunk)
                if total_size > MAX_FILE_SIZE:
                    remember_failure("cap_tripped", "upload exceeded maximum file size", file.filename)
                    raise HTTPException(
                        status_code=413,
                        detail=f"File too large. Maximum size is {MAX_FILE_SIZE // (1024*1024)}MB",
                    )
                tmp.write(chunk)

        engine_used = engine if engine in {"standard", "academic"} else "standard"
        fallback_warning = None
        response_warnings = []
        spreadsheet_result = None
        download_id = None
        download_filename = Path(file.filename).with_suffix(".md").name

        # ── Local streaming spreadsheet converter ──
        if is_spreadsheet_path(file.filename):
            if engine_used == "academic":
                fallback_warning = "Academic engine only supports PDF. Using spreadsheet converter."
            engine_used = "standard"
            download_id, artifact_path = create_conversion_artifact_path()
            try:
                spreadsheet_result = await run_converter(
                    convert_spreadsheet_to_path,
                    tmp_path,
                    artifact_path,
                    SPREADSHEET_OPTIONS,
                    file.filename,
                )
            except SpreadsheetLimitError as spreadsheet_limit:
                artifact_path.unlink(missing_ok=True)
                remember_failure("cap_tripped", spreadsheet_limit, file.filename)
                raise HTTPException(status_code=413, detail=str(spreadsheet_limit))
            except SpreadsheetConversionError as spreadsheet_error:
                artifact_path.unlink(missing_ok=True)
                remember_failure("conversion_failed", spreadsheet_error, file.filename)
                raise HTTPException(
                    status_code=500,
                    detail=f"Spreadsheet conversion failed: {spreadsheet_error}",
                )
            markdown_text = spreadsheet_result.preview
            response_warnings.extend(spreadsheet_result.warnings)

        # ── Academic engine (MinerU) ──
        elif engine_used == "academic" and MINERU_AVAILABLE and ext == ".pdf":
            try:
                mineru_result = await run_converter(mineru_client.flash_extract, tmp_path)
                markdown_text = mineru_result.markdown or ""
            except Exception as mineru_err:
                # Fallback to MarkItDown
                remember_failure("provider_failed", mineru_err, file.filename)
                result = await run_converter(md_engine.convert, tmp_path)
                markdown_text = normalize_markdown_for_source(result.text_content or "", file.filename)
                engine_used = "standard"
                fallback_warning = f"MinerU failed ({str(mineru_err)[:80]}), fell back to Standard."

        # ── Academic requested but not available or not PDF ──
        elif engine_used == "academic":
            if not MINERU_AVAILABLE:
                fallback_warning = "MinerU not available, using Standard engine."
            elif ext != ".pdf":
                fallback_warning = "Academic engine only supports PDF. Using Standard."
            result = await run_converter(md_engine.convert, tmp_path)
            markdown_text = normalize_markdown_for_source(result.text_content or "", file.filename)
            engine_used = "standard"

        # ── Standard engine (MarkItDown) ──
        else:
            result = await run_converter(md_engine.convert, tmp_path)
            markdown_text = normalize_markdown_for_source(result.text_content or "", file.filename)

        elapsed = round(time.time() - start_time, 2)

        # Build response
        response_data = {
            "success": True,
            "filename": file.filename,
            "extension": ext,
            "file_size": total_size,
            "markdown": markdown_text,
            "markdown_length": spreadsheet_result.markdown_length if spreadsheet_result else len(markdown_text),
            "conversion_time": elapsed,
            "engine": engine_used,
        }
        if fallback_warning:
            response_warnings.insert(0, fallback_warning)
        if response_warnings:
            response_data["warning"] = response_warnings[0]
            response_data["warnings"] = response_warnings
        if spreadsheet_result:
            response_data.update(
                {
                    "converter": "spreadsheet",
                    "markdown_is_preview": spreadsheet_result.preview_truncated,
                    "preview_truncated": spreadsheet_result.preview_truncated,
                    "download_id": download_id,
                    "download_filename": download_filename,
                    "spreadsheet": {
                        "sheets": spreadsheet_result.sheets,
                        "rows": spreadsheet_result.rows,
                    },
                }
            )

        return JSONResponse(content=response_data)

    except HTTPException:
        raise

    except Exception as e:
        remember_failure("conversion_failed", e, file.filename)
        raise HTTPException(
            status_code=500,
            detail=f"Conversion failed: {str(e)}",
        )

    finally:
        # Cleanup temp file
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ── Static Files & Frontend ────────────────────────────────────────────────────

# Mount static directory for CSS/JS assets
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    """Serve the main HTML page."""
    index_path = static_dir / "index.html"
    if not index_path.exists():
        return HTMLResponse("<h1>MD_CREATOR — Frontend not found</h1>", status_code=404)
    index_html = index_path.read_text(encoding="utf-8").replace(
        "__MD_CREATOR_TOKEN__",
        html.escape(APP_TOKEN, quote=True),
    )
    return HTMLResponse(index_html)


# ── Entry Point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    print(f"\n>>> {APP_NAME} starting on http://{APP_HOST}:{APP_PORT}\n")
    uvicorn.run("server:app", host=APP_HOST, port=APP_PORT, reload=APP_RELOAD)
