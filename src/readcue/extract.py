"""Turn an uploaded file (PDF, DOCX, TXT, MD, or a scan/photo needing OCR) into plain text."""

from __future__ import annotations

import html
import io
import logging
import re
import zipfile
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from . import ocr
from .errors import ReadcueError

log = logging.getLogger(__name__)

TEXT_SUFFIXES = {".txt", ".md", ".markdown", ""}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
MIN_PAGE_CHARS = 25  # a PDF page with less text than this is treated as a scan and OCR'd
MAX_DOCX_XML_BYTES = 50 * 1024 * 1024  # uncompressed size of a DOCX body; guards against zip bombs
MAX_TEXT_CHARS = 3_000_000  # per upload, roughly a 1,500-page book


def normalize_text(text: str) -> str:
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_page_range(spec: str, page_count: int) -> range:
    """'80-112' or '80' (1-based, inclusive) -> zero-based range of page indexes."""
    match = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+))?\s*", spec)
    if not match:
        raise ReadcueError(f"Page range {spec!r} should look like 80-112.")
    first = int(match.group(1))
    last = int(match.group(2) or first)
    if first < 1 or last < first or last > page_count:
        raise ReadcueError(f"Page range {spec!r} doesn't fit a {page_count}-page PDF.")
    return range(first - 1, last)


def _pdf_text(data: bytes, pages: str | None, ocr_lang: str) -> str:
    """Embedded text where a page has it, OCR for pages that are scans."""
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ReadcueError("This PDF is password-protected.")
        indexes = parse_page_range(pages, len(reader.pages)) if pages else range(len(reader.pages))
        texts = {i: reader.pages[i].extract_text() or "" for i in indexes}
    except PyPdfError as e:
        raise ReadcueError(f"Couldn't read the PDF: {e}") from e

    scanned = [i for i, text in texts.items() if len(text.strip()) < MIN_PAGE_CHARS]
    if scanned and not ocr.available() and len(scanned) < len(texts):
        # Mostly-text PDF with a few image-only pages, and no OCR here: keep what we have.
        log.warning(
            "%d of %d pages had no text and OCR isn't installed; skipping them", len(scanned), len(texts)
        )
    elif scanned:
        if len(scanned) > ocr.MAX_OCR_PAGES:
            raise ReadcueError(
                f"{len(scanned)} pages need OCR, which is more than the {ocr.MAX_OCR_PAGES}-page limit. "
                "Give a page range to upload a smaller section."
            )
        log.info("Running OCR on %d PDF page(s)", len(scanned))
        for page, text in ocr.ocr_pdf_pages(data, [i + 1 for i in scanned], ocr_lang).items():
            texts[page - 1] = text
    return "\n\n".join(texts[i] for i in indexes)


def _docx_text(data: bytes) -> str:
    """Paragraphs one per line; table rows kept on a single line so schedules stay readable."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if archive.getinfo("word/document.xml").file_size > MAX_DOCX_XML_BYTES:
                raise ReadcueError("That Word document is too large to read.")
            xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, KeyError) as e:
        raise ReadcueError("Couldn't read the DOCX file.") from e

    def paragraph_text(p: str) -> str:
        runs = re.findall(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>|(<w:tab/>|<w:br/>)", p)
        return "".join(" " if gap else html.unescape(text) for text, gap in runs)

    lines = []
    for match in re.finditer(r"<w:tr[ >].*?</w:tr>|<w:p[ >].*?</w:p>", xml, flags=re.S):
        chunk = match.group(0)
        if chunk.startswith("<w:tr"):
            cells = re.findall(r"<w:tc[ >].*?</w:tc>", chunk, flags=re.S)
            texts = [
                " ".join(paragraph_text(p) for p in re.findall(r"<w:p[ >].*?</w:p>", c, flags=re.S))
                for c in cells
            ]
            lines.append(" | ".join(t.strip() for t in texts))
        else:
            lines.append(paragraph_text(chunk))
    return "\n".join(lines)


def extract_text(data: bytes, filename: str, pages: str | None = None, *, ocr_lang: str = "eng") -> str:
    suffix = Path(filename).suffix.lower()
    if pages and suffix != ".pdf":
        raise ReadcueError("Page ranges only apply to PDF files.")
    if suffix == ".pdf":
        text = _pdf_text(data, pages, ocr_lang)
    elif suffix in IMAGE_SUFFIXES:
        text = ocr.ocr_image(data, ocr_lang)
    elif suffix == ".docx":
        text = _docx_text(data)
    elif suffix in TEXT_SUFFIXES:
        text = data.decode("utf-8", errors="replace")
    else:
        raise ReadcueError(
            f"Unsupported file type {suffix!r}. Use PDF, DOCX, TXT, MD or an image (PNG, JPG, TIFF)."
        )
    text = normalize_text(text)
    if not text:
        raise ReadcueError(
            "No text found in the file, even after OCR. Is the scan legible, and is READCUE_OCR_LANG right?"
        )
    if len(text) > MAX_TEXT_CHARS:
        raise ReadcueError(
            f"That's {len(text):,} characters of text, over the {MAX_TEXT_CHARS:,} limit. "
            "Use a page range, or add it in smaller sections."
        )
    return text
