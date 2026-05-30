# MD_CREATOR Deployment Prompt

Copy-paste the block below to an AI coding agent on the target machine when you want it to deploy this repository.

---

## The Prompt

```text
Deploy the MarkItDown / MD_CREATOR application from this repository:

https://github.com/feketerj/MarkItDown.git

## What It Is

MD_CREATOR is a local FastAPI web app that converts files to Markdown.

It supports:
- Single-file conversion through the main UI.
- Bulk conversion through the Bulk tab, returning a ZIP with one Markdown file per successful source file plus batch-results.json.
- Folder batch conversion through batch.bat or batch_convert.py.
- A local streaming spreadsheet converter for XLSX, XLS, and CSV, with sheet, column, cell, output, ZIP expansion, and timeout guardrails.
- PPTX cleanup that rewrites MarkItDown slide comments into NotebookLM-safe headings:
  ### Slide Number: N
- Authenticated temporary download artifacts for large spreadsheet and bulk outputs.
- A redacted diagnostics packet via tools/doctor.py and GET /api/diagnostics for fast failure classification during demos.
- start.bat runs diagnostics automatically after dependency setup and before launching the server.

## Stack

- Backend: Python + FastAPI + Uvicorn
- Conversion: markitdown[all], optional mineru-open-sdk, openpyxl, xlrd
- Frontend: Vanilla HTML/CSS/JS
- Preview: vendored markdown-it at static/js/markdown-it.min.js
- No Node build step

## Setup

1. Clone the repo:

   ```powershell
   git clone https://github.com/feketerj/MarkItDown.git
   cd MarkItDown
   ```

2. Create and activate a virtual environment:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\activate
   ```

3. Install dependencies:

   ```powershell
   pip install -r requirements.txt
   ```

4. Start the app:

   ```powershell
   .\start.bat
   ```

   The launcher writes `.doctor.json` during startup. It only blocks launch on a hard diagnostics failure. Set `MD_CREATOR_SKIP_DOCTOR=1` only when intentionally bypassing that preflight.

   Or run directly:

   ```powershell
   python server.py
   ```

5. Open the URL printed by the launcher. The default is:

   http://127.0.0.1:8000

6. Stop the background server with:

   ```powershell
   .\stop.bat
   ```

## Runtime Notes

- The app binds locally by default.
- APP_HOST, APP_PORT, APP_TOKEN, CONVERSION_ARTIFACT_DIR, BULK_ARTIFACT_DIR, and related limit/TTL variables can be overridden by environment variables.
- Large spreadsheet conversions may show only a bounded preview in the browser; use the authenticated download button for the full Markdown.
- Bulk conversion creates temporary job artifacts and ZIP downloads under the configured temp artifact directory.
- MinerU is optional. If unavailable or not applicable, the app falls back to Standard conversion where supported.

## Verify

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
.\.venv\Scripts\python.exe -m py_compile server.py batch_convert.py markdown_cleanup.py
.\.venv\Scripts\python.exe tools\doctor.py
node --check static\js\app.js
```

Then smoke-test in the browser:

1. Open the app.
2. Convert a small TXT or DOCX file from the single-file UI.
3. Convert a CSV or XLSX and confirm large outputs can be downloaded if preview-truncated.
4. Switch to the Bulk tab, upload multiple files, and download the ZIP.
5. Convert a PPTX and confirm output uses `### Slide Number: N` instead of `<!-- Slide number: N -->`.

## Important Files

- server.py: FastAPI app, upload endpoints, temporary artifacts, bulk jobs.
- batch_convert.py: reusable folder conversion engine.
- spreadsheet_convert.py: streaming spreadsheet converter.
- markdown_cleanup.py: PPTX slide marker cleanup.
- diagnostics.py: redacted diagnostics packet builder.
- tools/doctor.py: local diagnostics CLI.
- static/index.html: single-file and bulk UI.
- static/js/app.js: frontend upload, polling, preview, history, and download behavior.
- tests/: regression coverage for API, batch conversion, spreadsheet guardrails, and PPTX cleanup.
```
