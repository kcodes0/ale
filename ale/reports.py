"""Render a specialist Markdown report as a PDF artifact.

The Discord side wants a short Actor-written summary as the visible message, with
the full Engineer/Linguist report attached as a PDF the user can open later.
This module owns the PDF rendering. Markdown is parsed in the simplest way that
covers the structure agents actually emit (H1–H3, bullet lists, fenced code,
inline code). Anything fancier is rendered as plain text.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fpdf import FPDF


_LATIN1_REPLACEMENTS = {
    "—": "--",
    "–": "-",
    "“": '"',
    "”": '"',
    "‘": "'",
    "’": "'",
    "•": "*",
    "→": "->",
    "←": "<-",
    "…": "...",
    "✓": "[x]",
    "✗": "[ ]",
    "✅": "[x]",
    "❌": "[ ]",
    "🤖": "[bot]",
    "📎": "[file]",
    "©": "(c)",
    "®": "(r)",
}


@dataclass(frozen=True)
class PdfRenderResult:
    path: Path
    page_count: int
    bytes_written: int


def _sanitize(text: str) -> str:
    """Make text renderable by fpdf2's core fonts (Latin-1).

    The bundled core fonts don't ship a Unicode CMap, so we substitute the most
    common typographic characters and drop anything that still won't encode.
    The PDF is a backup artifact, not a typeset document — readable beats
    pretty.
    """

    for src, dst in _LATIN1_REPLACEMENTS.items():
        text = text.replace(src, dst)
    return text.encode("latin-1", "replace").decode("latin-1")


def render_report_pdf(
    *,
    title: str,
    body: str,
    output_path: Path,
    metadata: dict[str, str] | None = None,
) -> PdfRenderResult:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pdf = FPDF()
    pdf.set_margins(left=15, top=15, right=15)
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    line_w = pdf.epw

    pdf.set_font("Helvetica", "B", 16)
    pdf.multi_cell(line_w, 8, _sanitize(title or "Ale report"))
    pdf.ln(2)

    if metadata:
        pdf.set_font("Helvetica", "I", 9)
        for key, value in metadata.items():
            pdf.multi_cell(line_w, 4, _sanitize(f"{key}: {value}"))
        pdf.ln(2)

    in_code_block = False
    for raw_line in body.splitlines():
        stripped = raw_line.rstrip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            pdf.ln(1)
            continue
        if not stripped:
            pdf.ln(2)
            continue
        sanitized = _sanitize(stripped)
        if in_code_block:
            pdf.set_font("Courier", "", 9)
            pdf.multi_cell(line_w, 4, sanitized)
            continue
        if sanitized.startswith("# "):
            pdf.set_font("Helvetica", "B", 14)
            pdf.multi_cell(line_w, 7, sanitized[2:])
        elif sanitized.startswith("## "):
            pdf.set_font("Helvetica", "B", 12)
            pdf.multi_cell(line_w, 6, sanitized[3:])
        elif sanitized.startswith("### "):
            pdf.set_font("Helvetica", "B", 11)
            pdf.multi_cell(line_w, 5, sanitized[4:])
        elif sanitized.lstrip().startswith(("- ", "* ")):
            pdf.set_font("Helvetica", "", 10)
            pdf.multi_cell(line_w, 5, "* " + sanitized.lstrip()[2:])
        elif sanitized.lstrip()[:2].rstrip(".").isdigit() and ". " in sanitized:
            pdf.set_font("Helvetica", "", 10)
            pdf.multi_cell(line_w, 5, sanitized)
        else:
            pdf.set_font("Helvetica", "", 10)
            pdf.multi_cell(line_w, 5, sanitized)

    pdf.output(str(output_path))
    bytes_written = output_path.stat().st_size if output_path.exists() else 0
    return PdfRenderResult(
        path=output_path, page_count=pdf.pages_count, bytes_written=bytes_written
    )
