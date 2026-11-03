"""In-process text extractors (pdf/epub, docx, pptx, xlsx) for pdf_to_markdown.py.

Heavy libraries are imported lazily inside the extractor that needs them. Each returns
(text, error) with exactly one of the two None."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

Extraction = tuple[Optional[str], Optional[str]]


def extract_text_fitz(path: Path) -> Extraction:
    """pdf and epub — fitz opens both the same way. See docstring point 7:
    ~0.04-0.4s/doc in-process versus ~4-5s/doc via a markitdown subprocess."""
    import fitz  # PyMuPDF

    # MuPDF prints malformed-CSS/XObject warnings straight to stderr for a lot
    # of real-world epub/pdf files — cosmetic noise (extraction still succeeds),
    # but it floods the log at thousands-of-docs scale. Silence it.
    fitz.TOOLS.mupdf_display_errors(False)
    fitz.TOOLS.mupdf_display_warnings(False)
    doc = fitz.open(path)
    try:
        if doc.needs_pass:
            return None, "password-protected document"
        return "\n\n".join(page.get_text() for page in doc), None
    finally:
        doc.close()


def extract_text_docx(path: Path) -> Extraction:
    """Paragraphs, then tables as `a | b` rows."""
    import docx as python_docx

    d = python_docx.Document(str(path))
    parts = [p.text for p in d.paragraphs if p.text.strip()]
    for table in d.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text for cell in row.cells))
    return "\n\n".join(parts), None


def extract_text_pptx(path: Path) -> Extraction:
    """One `## Slide N` block per non-empty slide (text frames and tables)."""
    import pptx as python_pptx

    prs = python_pptx.Presentation(str(path))
    parts = []
    for i, slide in enumerate(prs.slides, 1):
        lines = [f"## Slide {i}"]
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    line = "".join(run.text for run in para.runs)
                    if line.strip():
                        lines.append(line)
            if shape.has_table:
                for row in shape.table.rows:
                    lines.append(" | ".join(cell.text for cell in row.cells))
        if len(lines) > 1:
            parts.append("\n".join(lines))
    return "\n\n".join(parts), None


def extract_text_xlsx(path: Path) -> Extraction:
    """No .xlsx currently exists in the catalog (the original import's
    ALLOWED_EXTENSIONS never included it) — kept for completeness/future
    catalog extensions rather than anything this run will actually hit."""
    import openpyxl

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    try:
        parts = []
        for ws in wb.worksheets:
            lines = [f"## Sheet: {ws.title}"]
            for row in ws.iter_rows(values_only=True):
                if any(c is not None for c in row):
                    lines.append(" | ".join("" if c is None else str(c) for c in row))
            if len(lines) > 1:
                parts.append("\n".join(lines))
        return "\n\n".join(parts), None
    finally:
        wb.close()
