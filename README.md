# MarkItDown — Universal Markdown Converter

A premium web application that converts **any file** to clean Markdown. Powered by [Microsoft MarkItDown](https://github.com/microsoft/markitdown).

![Python](https://img.shields.io/badge/Python-3.12+-blue?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?logo=fastapi&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)

---

## What It Does

Drop any file into the web UI and get clean, structured Markdown back in seconds. The conversion engine is Microsoft's MarkItDown library — the most popular file-to-Markdown converter on GitHub (119k+ stars).

For folders of files, use `batch.bat` or `batch_convert.py`. Batch mode writes one `.md` file per input file, preserves subfolders, records results in `batch-results.json`, and leaves the single-file web app unchanged.

### Supported Formats

| Category | Formats |
|:---------|:--------|
| **Documents** | PDF, Word (.docx), PowerPoint (.pptx), Excel (.xlsx/.xls), Outlook (.msg), EPUB |
| **Web** | HTML, HTM |
| **Data** | CSV, JSON, XML |
| **Images** | JPEG, PNG, GIF, BMP, TIFF, WebP (EXIF metadata + OCR) |
| **Archives** | ZIP (recursive content extraction) |
| **Audio** | WAV, MP3 (requires speech transcription API key) |
| **Text** | TXT, Markdown, reStructuredText |

---

## How It Works

### Architecture

```
Browser (HTML/CSS/JS)
    │
    ├── Drag-and-drop file upload
    │   ↓
    ├── POST /api/convert (multipart form)
    │   ↓
FastAPI Server (server.py)
    │
    ├── Receives file → writes to temp file
    ├── Passes temp file path to MarkItDown engine
    ├── MarkItDown detects format by extension
    ├── Delegates to format-specific converter:
    │   ├── PDF  → pdfminer.six / pdfplumber
    │   ├── DOCX → mammoth
    │   ├── PPTX → python-pptx
    │   ├── XLSX → openpyxl / pandas
    │   ├── HTML → markdownify (html→md)
    │   ├── Images → EXIF extraction + optional OCR
    │   ├── Audio → SpeechRecognition (optional)
    │   └── Text formats → direct read
    ├── Returns structured Markdown text
    ├── Cleans up temp file
    │   ↓
    └── JSON response → browser
        │
        ├── Raw Markdown displayed (left pane)
        ├── markdown-it renders HTML preview (right pane)
        ├── Copy to clipboard / Download as .md
        └── Saved to localStorage history
```

### Data Flow

1. **Upload** — User drags a file or clicks to browse. The file is sent as a `multipart/form-data` POST to `/api/convert`.

2. **Temp File** — FastAPI receives the upload, writes it to a temporary file with the original extension preserved (MarkItDown needs a file path to detect format).

3. **Conversion** — `MarkItDown.convert(path)` is called. Internally, MarkItDown uses [magika](https://github.com/google/magika) for content-type detection and routes to the appropriate format handler. Each handler extracts text and structure, normalizing it to Markdown with headings, tables, lists, and links preserved.

4. **Response** — The server returns a JSON payload with the Markdown text, filename, file size, character count, and conversion time. The temp file is deleted in a `finally` block.

5. **Preview** — The browser receives the Markdown and renders it two ways simultaneously:
   - **Raw pane** — `textContent` assignment (safe, no XSS)
   - **Preview pane** — [markdown-it](https://github.com/markdown-it/markdown-it) parses and renders to HTML

6. **Export** — User can copy to clipboard (Clipboard API with `execCommand` fallback) or download as `.md` (Blob URL).

7. **History** — Each conversion is saved to `localStorage` (last 20 entries) with full Markdown content for instant recall.

### Key Technical Details

- **No database** — All state is client-side (localStorage). Server is stateless.
- **Temp file cleanup** — Guaranteed via `try/finally` block. Files are never persisted.
- **50MB limit** — Enforced both client-side (pre-upload check) and server-side (post-read check).
- **Local-only by default** — Launchers bind to `127.0.0.1` and use a fixed app port.
- **Hot reload opt-in** — Set `APP_RELOAD=1` before running `python server.py`.

---

## Quick Start

### Prerequisites

- Python 3.10+ (tested on 3.12)
- pip

### Install

```bash
git clone https://github.com/YOUR_USER/MarkItDown.git
cd MarkItDown
pip install -r requirements.txt
```

### Run

```bat
start.bat
```

Open **http://127.0.0.1:8000** in your browser. Use `stop.bat` to close the background server.

### Usage

1. **Drag** any supported file onto the drop zone (or click to browse)
2. **View** results in split pane — raw Markdown (left) and rendered preview (right)
3. **Copy** to clipboard or **Download** as `.md`
4. **History** — access past conversions from the sidebar

---

## Batch Conversion

Double-click `batch.bat` for the folder workflow:

1. The first run creates an `input` folder if it does not exist.
2. Drop files into `input`.
3. Run `batch.bat` again.
4. Markdown files are written to `output`, with subfolders preserved.
5. A machine-readable report is written to `output/batch-results.json`.

`batch.bat` uses academic mode by default. PDF files use MinerU when available and fall back to Standard if MinerU is unavailable or fails. Non-PDF files always use Standard in academic mode.

CLI usage:

```powershell
python batch_convert.py input output --engine academic
python batch_convert.py input output --engine standard --overwrite
python batch_convert.py input output --engine auto --json
```

Safety behavior:

- Existing `.md` outputs are skipped unless `--overwrite` is provided.
- The output folder cannot be inside the input folder, which prevents recursive output loops.
- Each source file keeps an independent success, skipped, or error record.
- Files over the configured size limit are reported as errors and are not converted.

---

## Project Structure

```
MarkItDown/
├── server.py              # FastAPI backend — file upload, MarkItDown conversion, static serving
├── batch.bat              # Double-click folder batch converter
├── batch_convert.py       # Testable batch conversion CLI/API
├── requirements.txt       # Python dependencies
└── static/
    ├── index.html         # Single-page application — drag/drop, results, history sidebar
    ├── css/
    │   └── style.css      # Design system — dark mode, glassmorphism, responsive
    └── js/
        └── app.js         # Client logic — upload, preview, clipboard, download, history
```

---

## API Reference

### `GET /api/formats`

Returns the list of supported input formats.

**Response:**
```json
{
  "formats": [
    { "ext": ".pdf", "label": "PDF", "icon": "📄", "category": "Documents" },
    ...
  ]
}
```

### `POST /api/convert`

Converts an uploaded file to Markdown.

**Request:** `multipart/form-data` with a `file` field.

**Response:**
```json
{
  "success": true,
  "filename": "report.pdf",
  "extension": ".pdf",
  "file_size": 245760,
  "markdown": "# Report Title\n\nContent here...",
  "markdown_length": 4521,
  "conversion_time": 1.23
}
```

**Error Response (413):**
```json
{ "detail": "File too large. Maximum size is 50MB" }
```

---

## Tech Stack

| Layer | Technology | Purpose |
|:------|:-----------|:--------|
| **Backend** | [FastAPI](https://fastapi.tiangolo.com/) | Async web framework |
| **Server** | [Uvicorn](https://www.uvicorn.org/) | ASGI server |
| **Engine** | [MarkItDown](https://github.com/microsoft/markitdown) | File → Markdown conversion |
| **Frontend** | Vanilla HTML/CSS/JS | Zero-dependency UI |
| **Preview** | [markdown-it](https://github.com/markdown-it/markdown-it) | Markdown → HTML rendering |
| **Fonts** | [Inter](https://rsms.me/inter/) + [JetBrains Mono](https://www.jetbrains.com/lp/mono/) | Typography |

---

## License

MIT
