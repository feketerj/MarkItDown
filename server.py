"""
MD_CREATOR — Universal Markdown Converter
FastAPI backend powered by Microsoft MarkItDown
"""

import os
import time
import tempfile
import traceback
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from markitdown import MarkItDown

# ── App Setup ──────────────────────────────────────────────────────────────────

app = FastAPI(
    title="MD_CREATOR",
    description="Universal Markdown Converter — convert any file to Markdown",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# MarkItDown engine — singleton
md_engine = MarkItDown()

# Max upload size: 50 MB
MAX_FILE_SIZE = 50 * 1024 * 1024

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


# ── API Routes ─────────────────────────────────────────────────────────────────

@app.get("/api/formats")
async def get_formats():
    """Return list of supported input formats."""
    return JSONResponse(content={"formats": SUPPORTED_FORMATS})


@app.post("/api/convert")
async def convert_file(file: UploadFile = File(...)):
    """
    Convert an uploaded file to Markdown using MarkItDown.
    Returns JSON with the markdown content and metadata.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    # Read file content
    content = await file.read()

    # Check file size
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Maximum size is {MAX_FILE_SIZE // (1024*1024)}MB",
        )

    # Detect extension
    ext = Path(file.filename).suffix.lower()

    # Write to temp file (MarkItDown needs a file path)
    tmp_path = None
    try:
        start_time = time.time()

        with tempfile.NamedTemporaryFile(
            delete=False, suffix=ext, prefix="mdcreator_"
        ) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        # Convert with MarkItDown
        result = md_engine.convert(tmp_path)
        elapsed = round(time.time() - start_time, 2)

        markdown_text = result.text_content or ""

        # Build response
        return JSONResponse(
            content={
                "success": True,
                "filename": file.filename,
                "extension": ext,
                "file_size": len(content),
                "markdown": markdown_text,
                "markdown_length": len(markdown_text),
                "conversion_time": elapsed,
            }
        )

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
    return HTMLResponse(index_path.read_text(encoding="utf-8"))


# ── Entry Point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    print("\n>>> MD_CREATOR starting on http://localhost:8000\n")
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
