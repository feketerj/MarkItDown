# MarkItDown — Universal Markdown Converter

OutPace's maintained converter and pipeline-distillation application for turning
supported document, web, data, image, archive, audio, and text formats into
clean Markdown. This repository—not an upstream repository—is the OPICS
integration and control surface.

![Python](https://img.shields.io/badge/Python-3.12+-blue?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?logo=fastapi&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)

---

## What It Does

Drop a supported file into the web UI and get clean, structured Markdown back in seconds.

## Doctrine that binds here
| Layer | Doc | Applies |
|---|---|---|
| Estate | `~/HAF/DCOM/REPORTING-INSTRUCTIONS.md` — the one door (→ PAD · SOD · ROE · canon) | always |
| Estate | `~/HAF/DCOM/docs/sop/SESSION-CONTINUITY-SOP.md` — threads disposable, disk is the record | always |
| Build | TPFDD/DSOE + `PROCESS-CHECKLIST.md` (tier · mode · gates · review dial) | when building |
| Local | `./docs/DECISION-LOG.md` — Rulings | this workspace only |

Pointers only — if this table disagrees with a doc, the doc wins.

---

VoiceThread caption formats (`.dfxp`, `.ttml`, `.srt`, `.vtt`) are not handled by the generic MarkItDown route. The LDR-602A collector owns caption download, student/item/asset binding, SHA-256 verification, and caption-to-Markdown normalization. MarkItDown must not be used as a substitute for that identity binding.

For multiple files, use the Bulk tab in the web UI, `batch.bat`, or `batch_convert.py`. Bulk mode writes one `.md` file per input file, preserves browser-provided folder paths, records results in `batch-results.json`, and packages the outputs as a ZIP.

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
    ├── Routes spreadsheets to local streaming converter
    ├── Routes other files to MarkItDown engine
    ├── MarkItDown detects non-spreadsheet formats by extension
    ├── Delegates to format-specific converter:
    │   ├── PDF  → pdfminer.six / pdfplumber
    │   ├── DOCX → mammoth
    │   ├── PPTX → python-pptx
    │   ├── XLSX/XLS/CSV → streaming openpyxl / xlrd / csv
    │   ├── HTML → markdownify (html→md)
    │   ├── Images → EXIF extraction + optional OCR
    │   ├── Audio → SpeechRecognition (optional)
    │   └── Text formats → direct read
    ├── Returns Markdown text, or bounded preview + download artifact
    ├── Cleans up temp file
    │   ↓
    └── JSON response → browser
        │
        ├── Raw Markdown displayed (left pane)
        ├── markdown-it renders HTML preview (right pane)
        ├── Copy to clipboard / Download as .md
        └── Small full results saved to localStorage history
```

### Data Flow

1. **Upload** — User drags a file or clicks to browse. The file is sent as a `multipart/form-data` POST to `/api/convert`.

2. **Temp File** — FastAPI receives the upload, writes it to a temporary file with the original extension preserved (MarkItDown needs a file path to detect format).

3. **Conversion** — Spreadsheet files use the local streaming converter in `spreadsheet_convert.py`, which writes Markdown incrementally and returns a bounded preview. Other files call `MarkItDown.convert(path)`. Internally, MarkItDown uses [magika](https://github.com/google/magika) for content-type detection and routes to the appropriate format handler.

4. **Response** — The server returns a JSON payload with filename, file size, character count, and conversion time. For large spreadsheet output, `markdown` contains the preview and `download_id` points to the full generated `.md` artifact. The upload temp file is deleted in a `finally` block.

5. **Preview** — The browser receives the Markdown and renders it two ways simultaneously:
   - **Raw pane** — `textContent` assignment (safe, no XSS)
   - **Preview pane** — [markdown-it](https://github.com/markdown-it/markdown-it) parses and renders to HTML

6. **Export** — User can copy to clipboard (Clipboard API with `execCommand` fallback) or download as `.md`. Large spreadsheet downloads are fetched from the authenticated artifact endpoint.

7. **History** — Recent conversions are saved to `localStorage` (last 20 entries). Large or preview-only results save metadata only because full artifacts expire server-side.

### Key Technical Details

- **No database** — All state is client-side (localStorage). Server is stateless.
- **Temp file cleanup** — Guaranteed via `try/finally` block. Files are never persisted.
- **Spreadsheet guardrails** — Streaming conversion avoids pandas whole-workbook loads and enforces sheet, column, cell, output, ZIP expansion, and timeout limits.
- **Artifact cleanup** — Large generated Markdown artifacts are stored under the OS temp directory and expire automatically.
- **50MB limit** — Enforced both client-side (pre-upload check) and server-side (post-read check).
- **Local-only by default** — Launchers bind to `127.0.0.1` and use a fixed app port.
- **No stale frontend** — `/` and `/static/*` are served with `Cache-Control: no-cache` so browsers revalidate after every code update instead of executing days-old JavaScript.
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

```bash
./start.sh
```

Open **http://127.0.0.1:8000** in your browser. Use `./stop.sh` to close the background server.

### Usage

1. **Drag** any supported file onto the drop zone (or click to browse)
2. **View** results in split pane — raw Markdown (left) and rendered preview (right)
3. **Copy** to clipboard or **Download** as `.md`
4. **History** — access past conversions from the sidebar
5. **Bulk** — switch to the Bulk tab to upload many files or a folder and download a ZIP

---

## Bulk Conversion in the UI

The Bulk tab is for converting many files without repeating the single-file upload flow:

1. Select files or a folder — or drag a folder straight onto the drop zone (dropped folders are traversed recursively, subfolders included).
2. Click **Bulk Convert**.
3. Watch live progress (`Converting files... 12/48` plus the current file).
4. Review per-file converted, skipped, and failed results.
5. Download the generated ZIP.

The ZIP contains one Markdown file per successful source file plus `batch-results.json`. Partial failures do not discard successful conversions.

Robustness behavior:

- Unreadable selections (a file deleted or moved after picking it) are skipped before upload with a toast naming the item, instead of killing the whole batch with a network error.
- Bulk batches run on their own conversion lane, so single-file conversions stay responsive while a long batch runs.
- If the server restarts while the tab is open, the page re-reads its session token and retries automatically — no manual refresh required.

---

## Diagnostics

For demo prep or troubleshooting, generate a small redacted failure packet:

```bash
.venv/bin/python tools/doctor.py
```

`start.sh` runs this same doctor automatically after dependency setup and before launching the server. It writes `.doctor.json` and blocks startup only when diagnostics returns a hard failure. Run the regression suite with `.venv/bin/python -m unittest discover -s tests -v`.

```powershell
$env:MD_CREATOR_SKIP_DOCTOR = "1"
.\start.bat
```

The same packet is available from the authenticated API:

```http
GET /api/diagnostics
X-MD-Creator-Token: <local session token>
```

Diagnostics classify likely failure areas without exposing raw local paths:

- `bad_deployment`
- `artifact_degraded`
- `provider_failed`
- `cap_tripped`
- `api_failed`
- `conversion_failed`

The packet includes runtime state, dependency availability, artifact directory health, upload limits, bulk job counts, spreadsheet guardrails, and recent failures.

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
- Spreadsheet files use the same streaming converter as the web app and write Markdown directly to the output path.

## OPICS Pipeline Distillation

`pipeline_distill.py` is the shared extraction boundary for pipeline-owned
manuals, brochures, drawings, Office files, spreadsheets, and other public
source documents. It does not infer domain fields or promote database rows.

It produces:

- immutable Markdown keyed by source, parser/runtime interpretation, and output
  hashes;
- a positioned DeepDoc OCR sidecar for PDFs when the isolated TDP OCR runtime is
  available;
- a JSON receipt with the public source identifier, source/output SHA-256,
  parser identity, model/shim hashes, warnings, and retention disposition.

The generic converter runs outside the orchestrator process behind a bounded
two-lane drum. It enforces archive expansion/nesting, Markdown output, wall
time, process-group memory, CPU, file-size, and file-descriptor limits. Generic
non-PDF output is labeled `parser_output_only`; only a parser with demonstrated
coverage can claim the full document.

Example:

```bash
.venv/bin/python pipeline_distill.py \
  /tmp/opics-intake/manual.pdf \
  /var/tmp/opics-distilled \
  --source-uri https://manufacturer.example/manual.pdf \
  --owned-root /tmp/opics-intake \
  --parser auto \
  --json
```

`--owned-root` is a custody boundary, not deletion authority. The input must be
a regular, non-symlink file beneath that exact root and artifacts must land
outside the root. After the initial receipt lands, the source moves atomically
into a private recoverable quarantine. Omit the option for caller-owned
documents, source-of-record caches, TDP proof packages, and other evidence that
must remain.

Raw deletion is a separate action. The consuming pipeline must first provide a
durable, non-symlink downstream evidence artifact proving either schema
extraction is complete or the source is terminally non-applicable. The
authorization writer computes that artifact's hash itself. `pipeline_reap.py`
then reopens and revalidates the downstream artifact, immutable receipt,
normalized artifact, authorization, quarantine identity, raw hash, and minimum
age before deletion. A distillation receipt or caller-supplied hash alone never
authorizes deletion.

Parser truth:

- OutPace MarkItDown handles ordinary text-bearing and Office/data formats.
- The locally proven RAGFlow DeepDoc component supplies OCR text, confidence,
  and positioned boxes for PDFs. It is labeled `deepdoc-ocr`.
- Full RAGFlow document parsing and table-structure recognition are not claimed;
  the local table recognizer is not yet verified.
- A source-located distilled receipt is eligible for a schema-bound extraction
  attempt. It never authorizes a complete-row promotion. Each consuming
  pipeline still owns field applicability, source-quote/page anchors,
  terminal-null decisions, promotion, and cleanup authorization.

---

## Project Structure

```
MarkItDown/
├── server.py              # FastAPI backend — file upload, MarkItDown conversion, static serving
├── spreadsheet_convert.py # Streaming XLSX/XLS/CSV → Markdown converter with guardrails
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
  "conversion_time": 1.23,
  "preview_truncated": false
}
```

Large spreadsheet responses include a bounded preview in `markdown` and a temporary download id:

```json
{
  "success": true,
  "filename": "large.xlsx",
  "extension": ".xlsx",
  "file_size": 245760,
  "markdown": "## Sheet1\n\n| Column | ...",
  "markdown_length": 10485760,
  "markdown_is_preview": true,
  "preview_truncated": true,
  "download_id": "temporary-id",
  "download_filename": "large.md",
  "converter": "spreadsheet"
}
```

### `GET /api/download/{download_id}`

Downloads a generated Markdown artifact. Requires the same `X-MD-Creator-Token` header as conversion requests.

### `POST /api/bulk/convert`

Creates a temporary bulk conversion job from uploaded files.

**Request:** `multipart/form-data` with repeated `files` fields, optional repeated `paths` fields for relative folder paths, and an `engine` field.

**Response:**
```json
{
  "job": {
    "id": "temporary-job-id",
    "status": "queued",
    "engine": "standard",
    "file_count": 2,
    "total_size": 12345
  }
}
```

### `GET /api/bulk/jobs/{job_id}`

Returns the current bulk job state and summary once complete.

### `GET /api/bulk/jobs/{job_id}/download`

Downloads the completed bulk ZIP artifact. Requires the same `X-MD-Creator-Token` header as conversion requests.

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
| **Engine** | [OutPace MarkItDown](https://github.com/feketerj/MarkItDown) | Governed file → Markdown conversion and receipts |
| **Frontend** | Vanilla HTML/CSS/JS | Zero-dependency UI |
| **Preview** | [markdown-it](https://github.com/markdown-it/markdown-it) | Markdown → HTML rendering |
| **Fonts** | [Inter](https://rsms.me/inter/) + [JetBrains Mono](https://www.jetbrains.com/lp/mono/) | Typography |

---
