"""Render PDF pages, or a rectangle of one, to PNG with poppler's pdftoppm (installed in the Docker image)."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from pypdf import PdfReader

from .errors import ReadcueError
from .ocr import INSTALL_HINT

TOOL_TIMEOUT = 120


def page_size_px(pdf: Path, page: int, dpi: int) -> tuple[int, int]:
    """Width and height in pixels a page renders to at `dpi`, allowing for the page's rotation."""
    try:
        target = PdfReader(str(pdf)).pages[page - 1]
        width, height = float(target.mediabox.width), float(target.mediabox.height)
        if target.rotation % 180:
            width, height = height, width
    except Exception as e:
        raise ReadcueError(f"Couldn't read page {page} of the PDF: {e}") from e
    return round(width * dpi / 72), round(height * dpi / 72)


def render_page(pdf: Path, page: int, dpi: int, crop: tuple[int, int, int, int] | None = None) -> bytes:
    """PNG bytes of one page. `crop` is (x, y, width, height) in pixels at `dpi`, from the top left."""
    if not shutil.which("pdftoppm"):
        raise ReadcueError(f"'pdftoppm' isn't installed. {INSTALL_HINT}")
    with tempfile.TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "page"
        cmd = ["pdftoppm", "-r", str(dpi), "-png", "-singlefile", "-f", str(page), "-l", str(page)]
        if crop:
            x, y, width, height = crop
            cmd += ["-x", str(x), "-y", str(y), "-W", str(width), "-H", str(height)]
        try:
            proc = subprocess.run(cmd + [str(pdf), str(prefix)], capture_output=True, timeout=TOOL_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise ReadcueError(f"pdftoppm took too long on page {page}.") from None
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", errors="replace").strip()[-300:]
            raise ReadcueError(f"pdftoppm failed on page {page}: {detail}")
        return Path(f"{prefix}.png").read_bytes()
