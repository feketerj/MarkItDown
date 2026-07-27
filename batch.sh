#!/usr/bin/env bash
# MarkItDown — macOS batch convert (port of batch.bat). Usage: ./batch.sh [input_dir] [output_dir]
set -uo pipefail
cd "$(dirname "$0")"

VENV_PY=".venv/bin/python"
STAMP_FILE=".deps_installed"
INPUT_DIR="${1:-input}"
OUTPUT_DIR="${2:-output}"

echo "Batch converting — Input: ${INPUT_DIR}  Output: ${OUTPUT_DIR}"

if [ ! -d "$INPUT_DIR" ]; then
  mkdir -p "$INPUT_DIR"
  echo "[OK] Created ${INPUT_DIR}. Drop files in and re-run."
  exit 0
fi

if [ ! -x "$VENV_PY" ]; then
  echo "Creating virtual environment (uv, Python 3.12)..."
  uv venv --python 3.12 .venv || { echo "[ERROR] venv create failed — need uv + Python 3.12."; exit 1; }
fi

if [ ! -f "$STAMP_FILE" ] || [ "requirements.txt" -nt "$STAMP_FILE" ]; then
  echo "Installing dependencies into .venv..."
  uv pip install --python "$VENV_PY" -r requirements.txt || { echo "[ERROR] dependency install failed."; exit 1; }
  date > "$STAMP_FILE"
fi

set +e
"$VENV_PY" batch_convert.py "$INPUT_DIR" "$OUTPUT_DIR" --engine academic
rc=$?
set -e

echo
[ "$rc" = "0" ] \
  && echo "[OK] Batch conversion finished. See ${OUTPUT_DIR}/batch-results.json" \
  || echo "[WARN] Batch conversion finished with errors. See ${OUTPUT_DIR}/batch-results.json"
exit $rc
