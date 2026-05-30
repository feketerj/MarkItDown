"""
Markdown cleanup for converter-specific output quirks.

Keep these transforms narrow and source-aware so generic Markdown content is not
silently rewritten.
"""

from __future__ import annotations

import re
from pathlib import Path


_PPTX_SLIDE_MARKER_RE = re.compile(
    r"^\s*<!--\s*Slide number:\s*(\d+)\s*-->\s*(.*?)\s*$",
    re.IGNORECASE,
)


def normalize_markdown_for_source(markdown: str, source: str | Path) -> str:
    """Apply source-specific Markdown cleanup."""
    if Path(source).suffix.lower() == ".pptx":
        return normalize_pptx_slide_markers(markdown)
    return markdown


def normalize_pptx_slide_markers(markdown: str) -> str:
    """Replace MarkItDown PPTX slide comments with Markdown slide sections."""
    if "<!--" not in markdown:
        return markdown

    lines = markdown.splitlines()
    output: list[str] = []
    marker_seen = False
    suppress_next_blank = False

    for line in lines:
        marker = _PPTX_SLIDE_MARKER_RE.match(line)
        if marker is None:
            if suppress_next_blank and line.strip() == "":
                continue
            suppress_next_blank = False
            output.append(line)
            continue

        slide_number = marker.group(1)
        trailing_text = marker.group(2).strip()

        while output and output[-1].strip() == "":
            output.pop()

        if marker_seen:
            if output:
                output.append("")
            output.extend(["---", ""])
        elif output:
            output.append("")

        output.extend([f"### Slide Number: {slide_number}", ""])
        if trailing_text:
            output.append(trailing_text)

        marker_seen = True
        suppress_next_blank = True

    if not marker_seen:
        return markdown

    return "\n".join(output).strip()
