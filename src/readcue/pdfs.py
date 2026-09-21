"""Keep each chapter's original pages as a small PDF of its own, so the chapter can be downloaded.

The PDF is cut out of the textbook, or assembled from the PDF(s) uploaded for the chapter. It doesn't depend on
the textbook staying around, and it's the source the key-figure finder looks at.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.errors import PyPdfError

from .config import Config
from .errors import ReadcueError
from .extract import parse_page_range


def chapter_pdf_path(cfg: Config, chapter_id: int) -> Path:
    return cfg.data_dir / "chapters" / f"{chapter_id}.pdf"


def _open(source: Path | io.BytesIO) -> PdfReader:
    try:
        reader = PdfReader(str(source) if isinstance(source, Path) else source)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ReadcueError("This PDF is password-protected.")
        return reader
    except PyPdfError as e:
        raise ReadcueError(f"Couldn't read the PDF: {e}") from e


def _write(pages: list, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    for page in pages:
        writer.add_page(page)
    partial = dest.with_suffix(".part")  # never leave a half-written PDF where a download could find it
    try:
        with partial.open("wb") as f:
            writer.write(f)
        partial.replace(dest)
    finally:
        partial.unlink(missing_ok=True)


def slice_pdf(source: Path, start: int, end: int, dest: Path) -> None:
    """Pages start..end (1-based, inclusive) of a PDF on disk, written to `dest`."""
    reader = _open(source)
    if not 1 <= start <= end <= len(reader.pages):
        raise ReadcueError(f"Pages {start} to {end} don't fit a {len(reader.pages)}-page PDF.")
    _write([reader.pages[i] for i in range(start - 1, end)], dest)


def merge_pdfs(blobs: list[bytes], pages: str | None, dest: Path) -> None:
    """The uploaded PDFs joined in order, keeping only `pages` ("80-112") when a range was given."""
    collected = []
    for blob in blobs:
        reader = _open(io.BytesIO(blob))
        indexes = parse_page_range(pages, len(reader.pages)) if pages else range(len(reader.pages))
        collected.extend(reader.pages[i] for i in indexes)
    _write(collected, dest)


def download_name(course: str, number: int, title: str) -> str:
    """A friendly, filesystem-safe name: "BIO 101 - Chapter 4 - Cell Structure.pdf"."""
    parts = [course, f"Chapter {number}"] + ([title] if title else [])
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", " - ".join(parts)).strip(" .")
    return f"{name[:150]}.pdf"
