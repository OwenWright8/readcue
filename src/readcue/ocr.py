"""OCR for scanned pages and photos, using the tesseract and pdftoppm (poppler) command-line tools.

Both are installed in the Docker image. Running outside Docker needs `tesseract-ocr` and
`poppler-utils` (apt) or `tesseract poppler` (brew).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .errors import ReadcueError

DPI = 300  # resolution PDF pages are rendered at before recognition
MAX_OCR_PAGES = 150  # per upload; keeps a forgotten page range from tying the server up for an hour
TOOL_TIMEOUT = 180  # seconds, per tool invocation (one page)

# Each PDF already fans out across every CPU, so concurrent uploads take turns instead of piling up
# dozens of tesseract processes.
_ocr_lock = threading.Lock()

INSTALL_HINT = (
    "OCR needs the tesseract and pdftoppm tools. The Docker image includes them; to run locally, "
    "install tesseract-ocr and poppler-utils (brew: tesseract poppler)."
)


def available() -> bool:
    return bool(shutil.which("tesseract") and shutil.which("pdftoppm"))


def _run(cmd: list[str], *, stdin: bytes | None = None) -> bytes:
    if not shutil.which(cmd[0]):
        raise ReadcueError(f"'{cmd[0]}' isn't installed. {INSTALL_HINT}")
    try:
        proc = subprocess.run(
            cmd,
            input=stdin,
            capture_output=True,
            timeout=TOOL_TIMEOUT,
            env={
                **os.environ,
                "OMP_THREAD_LIMIT": "1",
            },  # tesseract is faster with pages run in parallel instead
        )
    except subprocess.TimeoutExpired:
        raise ReadcueError(f"{cmd[0]} took too long on one page.") from None
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()[-300:]
        raise ReadcueError(f"{cmd[0]} failed: {detail}")
    return proc.stdout


def _recognize(data: bytes, lang: str) -> str:
    out = _run(["tesseract", "stdin", "stdout", "-l", lang], stdin=data)
    return out.decode("utf-8", errors="replace")


def ocr_image(data: bytes, lang: str) -> str:
    """Recognise text in one image (PNG, JPEG, TIFF, BMP)."""
    with _ocr_lock:
        return _recognize(data, lang)


def ocr_pdf_pages(pdf: bytes, pages: list[int], lang: str) -> dict[int, str]:
    """Render and recognise the given 1-based pages of a PDF, several at a time."""
    if not available():
        raise ReadcueError(INSTALL_HINT)
    with _ocr_lock, tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "in.pdf"
        source.write_bytes(pdf)

        def one(page: int) -> tuple[int, str]:
            prefix = Path(tmp) / f"page{page}"
            _run(
                [
                    "pdftoppm",
                    "-r",
                    str(DPI),
                    "-png",
                    "-singlefile",
                    "-f",
                    str(page),
                    "-l",
                    str(page),
                    str(source),
                    str(prefix),
                ]
            )
            return page, _recognize(Path(f"{prefix}.png").read_bytes(), lang)

        with ThreadPoolExecutor(max_workers=max(1, min(len(pages), os.cpu_count() or 2))) as pool:
            return dict(pool.map(one, pages))
