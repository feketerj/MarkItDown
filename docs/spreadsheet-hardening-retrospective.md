# Spreadsheet Hardening Retrospective

Date: 2026-05-28

## Context

Large spreadsheet uploads were choking the app because the default MarkItDown spreadsheet path loads full workbooks through pandas and returns full Markdown through one JSON response. The browser then rendered that entire result and tried to keep it around for copy, download, and history.

## What Changed

- Added `spreadsheet_convert.py`, a local streaming spreadsheet converter for `.xlsx`, `.xls`, and `.csv`.
- Routed spreadsheet uploads in `server.py` through the streaming converter instead of generic `MarkItDown.convert`.
- Added an authenticated `/api/download/{artifact_id}` route for generated Markdown artifacts.
- Updated `batch_convert.py` so batch spreadsheet conversion also writes directly to Markdown output.
- Updated the frontend so large spreadsheet output uses bounded preview plus full-file download instead of rendering or storing all Markdown in the browser.
- Reworked the browse-files UI from a styled span and `display:none` input to a real button with a visually hidden file input.
- Added clearer local-server-unreachable messaging instead of surfacing raw `Failed to fetch`.
- Added converter, API, batch, and frontend guard tests.

## What Went Wrong

- The first implementation solved the backend memory problem but left a fragile browse-files control. The UI depended on programmatic clicks against a hidden file input, which can be unreliable.
- The first browser smoke test did not catch the user's exact failure mode because the backend was later no longer serving on the default port. A stale browser page can still look usable while upload fetches fail.
- CSV uploads initially used the temporary server filename as the Markdown heading. Browser verification caught that provenance bug, and the converter now accepts the original display filename.
- The initial frontend error path let raw network exceptions surface as generic fetch failures. That was technically accurate but not operationally useful.

## What Went Right

- The core spreadsheet conversion path now avoids pandas whole-workbook loading for normal app and batch spreadsheet conversions.
- Conversion output is bounded by explicit sheet, column, cell, output-size, ZIP expansion, and timeout guardrails.
- Large spreadsheet output is no longer forced through full JSON response, full markdown-it rendering, or localStorage history.
- The authenticated artifact download route preserves the local session-token boundary.
- Focused tests caught a CSV delimiter-detection issue before broader rollout.
- Browser smoke testing caught the temporary-filename provenance issue before closeout.
- Temporary verification servers and artifact directories were cleaned up after use.

## Verification

Commands run:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
node --check static/js/app.js
.\.venv\Scripts\python.exe -m py_compile server.py batch_convert.py spreadsheet_convert.py
git diff --check
```

Observed result:

- Unit suite passed with 29 tests.
- JavaScript syntax check passed.
- Python compile check passed.
- Patch whitespace check passed.
- Browser smoke test confirmed browse-file upload and CSV spreadsheet conversion worked with no console errors.

## Governance Notes

- No repository SOP, governance, decision trace, or provenance files were present when searched.
- Existing untracked `deployment_prompt.md` was not part of this work and should remain excluded unless intentionally added later.
- The README already had a pre-existing unrelated license-section deletion in the working tree before this task. That should be handled separately if the repository owner wants it restored or committed.

## Recommended Next Steps

1. Run the new spreadsheet path against representative real workbooks and tune `SPREADSHEET_*` environment limits.
2. Add async progress and cancellation if real spreadsheet conversions regularly exceed a comfortable request cycle.
3. Add a small UI note for preview-only spreadsheet results showing preview/full-output limits and artifact expiry.
4. Decide separately whether the README license-section deletion should be restored or committed.
