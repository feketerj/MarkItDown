"""
MD_CREATOR — Universal Markdown Converter
FastAPI backend powered by Microsoft MarkItDown + MinerU
"""

import asyncio
import html
import os
import secrets
import time
import tempfile
import traceback
import warnings
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
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

from markitdown import MarkItDown

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


async def run_converter(func, *args):
    """Run blocking converter SDK calls without blocking the event loop."""
    async with conversion_semaphore:
        return await run_in_threadpool(func, *args)


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
        }
    )


@app.get("/api/engines")
async def get_engines():
    """Return list of available conversion engines."""
    return JSONResponse(content={"engines": ENGINES})


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
                    raise HTTPException(
                        status_code=413,
                        detail=f"File too large. Maximum size is {MAX_FILE_SIZE // (1024*1024)}MB",
                    )
                tmp.write(chunk)

        engine_used = engine if engine in {"standard", "academic"} else "standard"
        fallback_warning = None

        # ── Academic engine (MinerU) ──
        if engine_used == "academic" and MINERU_AVAILABLE and ext == ".pdf":
            try:
                mineru_result = await run_converter(mineru_client.flash_extract, tmp_path)
                markdown_text = mineru_result.markdown or ""
            except Exception as mineru_err:
                # Fallback to MarkItDown
                traceback.print_exc()
                result = await run_converter(md_engine.convert, tmp_path)
                markdown_text = result.text_content or ""
                engine_used = "standard"
                fallback_warning = f"MinerU failed ({str(mineru_err)[:80]}), fell back to Standard."

        # ── Academic requested but not available or not PDF ──
        elif engine_used == "academic":
            if not MINERU_AVAILABLE:
                fallback_warning = "MinerU not available, using Standard engine."
            elif ext != ".pdf":
                fallback_warning = "Academic engine only supports PDF. Using Standard."
            result = await run_converter(md_engine.convert, tmp_path)
            markdown_text = result.text_content or ""
            engine_used = "standard"

        # ── Standard engine (MarkItDown) ──
        else:
            result = await run_converter(md_engine.convert, tmp_path)
            markdown_text = result.text_content or ""

        elapsed = round(time.time() - start_time, 2)

        # Build response
        response_data = {
            "success": True,
            "filename": file.filename,
            "extension": ext,
            "file_size": total_size,
            "markdown": markdown_text,
            "markdown_length": len(markdown_text),
            "conversion_time": elapsed,
            "engine": engine_used,
        }
        if fallback_warning:
            response_data["warning"] = fallback_warning

        return JSONResponse(content=response_data)

    except HTTPException:
        raise

    except Exception as e:
        traceback.print_exc()
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
