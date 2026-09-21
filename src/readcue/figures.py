"""Find the figures that matter most in a chapter, and clip them out of its pages.

Claude looks at the chapter's pages (rendered to images) together with the chapter's summary, and names the few
figures a student really needs, with a box around each. The boxes are approximate, so the clip is padded a little.
Each clipped figure is stored as a PNG and shown with the summary, where it can be removed.
"""

from __future__ import annotations

import logging
import math
import shutil
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from . import prompts, render
from .config import Config
from .db import Database
from .errors import ReadcueError
from .llm.base import LLMProvider, complete_structured
from .models import Chapter, Summary
from .pdfs import chapter_pdf_path
from .schemas import FIGURES_SCHEMA

log = logging.getLogger(__name__)

VIEW_DPI = 100  # resolution of the page images the model looks at
CLIP_DPI = 200  # resolution of the saved clip
BATCH_PAGES = 20  # pages per request
MAX_PAGES = 60  # pages looked at per chapter
MAX_FIGURES = 4  # kept per chapter
MIN_IMPORTANCE = 7  # of 10; below this it isn't "especially important"
PADDING = 0.012  # of the page, added around the model's box because such boxes are approximate
MIN_CLIP_PX = 80


@dataclass(frozen=True)
class Choice:
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    caption: str
    why: str
    importance: int


def figure_dir(cfg: Config, chapter_id: int) -> Path:
    return cfg.data_dir / "figures" / str(chapter_id)


def figure_path(cfg: Config, chapter_id: int, figure_id: int) -> Path:
    return figure_dir(cfg, chapter_id) / f"{figure_id}.png"


def clear_figures(db: Database, cfg: Config, chapter_id: int) -> None:
    """Forget a chapter's figures and delete their files."""
    db.delete_figures(chapter_id)
    shutil.rmtree(figure_dir(cfg, chapter_id), ignore_errors=True)


def _fraction(value: object) -> float | None:
    return min(max(float(value), 0.0), 1.0) if isinstance(value, (int, float)) else None


def parse_choices(data: dict, pages: list[int]) -> list[Choice]:
    """Usable figure choices from the model's reply; anything malformed or off the pages is dropped."""
    choices = []
    for item in data.get("figures") or []:
        if not isinstance(item, dict):
            continue
        page = item.get("page")
        coords = [_fraction(item.get(k)) for k in ("x0", "y0", "x1", "y1")]
        importance = item.get("importance")
        if page not in pages or None in coords or not isinstance(importance, int):
            continue
        x0, y0, x1, y1 = coords
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        if x1 - x0 <= 0 or y1 - y0 <= 0:
            continue
        caption = str(item.get("caption") or "").strip()[:160]
        choices.append(
            Choice(page, x0, y0, x1, y1, caption, str(item.get("why") or "").strip()[:300], importance)
        )
    return choices


def clip_rect(choice: Choice, width: int, height: int) -> tuple[int, int, int, int] | None:
    """(x, y, w, h) in pixels for a page `width` x `height`, padded and kept on the page. None if too small."""
    x0, y0 = max(choice.x0 - PADDING, 0.0), max(choice.y0 - PADDING, 0.0)
    x1, y1 = min(choice.x1 + PADDING, 1.0), min(choice.y1 + PADDING, 1.0)
    x, y = math.floor(x0 * width), math.floor(y0 * height)
    w, h = min(math.ceil((x1 - x0) * width), width - x), min(math.ceil((y1 - y0) * height), height - y)
    return (x, y, w, h) if w >= MIN_CLIP_PX and h >= MIN_CLIP_PX else None


def _summary_text(summary: Summary) -> str:
    points = "\n".join(f"- {p}" for p in summary.key_points)
    return f"{summary.overview}\n\nKey points:\n{points}"[:3000]


def find_figures(
    provider: LLMProvider, pdf: Path, *, course: str, number: int, title: str, summary: Summary
) -> list[Choice]:
    """Ask the model, a batch of pages at a time, which figures are especially important."""
    try:
        page_count = len(PdfReader(str(pdf)).pages)
    except PyPdfError as e:
        raise ReadcueError(f"Couldn't read the chapter's PDF: {e}") from e
    pages = list(range(1, min(page_count, MAX_PAGES) + 1))
    found: list[Choice] = []
    for start in range(0, len(pages), BATCH_PAGES):
        batch = pages[start : start + BATCH_PAGES]
        images = [(f"Page {p}", render.render_page(pdf, p, VIEW_DPI)) for p in batch]
        prompt = prompts.figures_prompt(course, number, title, batch, _summary_text(summary))
        found += complete_structured(
            provider,
            prompts.FIGURES_SYSTEM,
            prompt,
            lambda data, batch=batch: parse_choices(data, batch),
            schema=FIGURES_SCHEMA,
            images=images,
        )
    standout = [c for c in found if c.importance >= MIN_IMPORTANCE]
    return sorted(standout, key=lambda c: -c.importance)[:MAX_FIGURES]  # stable: ties keep page order


def extract_figures(
    db: Database, cfg: Config, provider: LLMProvider, chapter: Chapter, summary: Summary
) -> int:
    """Pick and clip a chapter's key figures. Returns how many were saved. Replaces any earlier ones."""
    pdf = chapter_pdf_path(cfg, chapter.id)
    if not pdf.is_file():
        return 0
    clear_figures(db, cfg, chapter.id)
    choices = find_figures(
        provider, pdf, course=chapter.course_name, number=chapter.number, title=chapter.title, summary=summary
    )
    saved = 0
    for choice in choices:
        figure_id = db.add_figure(chapter.id, choice.page, choice.caption, choice.why)
        try:
            width, height = render.page_size_px(pdf, choice.page, CLIP_DPI)
            rect = clip_rect(choice, width, height)
            if rect is None:
                raise ReadcueError("the box was too small to be a figure")
            dest = figure_path(cfg, chapter.id, figure_id)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(render.render_page(pdf, choice.page, CLIP_DPI, crop=rect))
            saved += 1
        except ReadcueError as e:
            log.warning("Dropped a figure on page %s of chapter %s: %s", choice.page, chapter.id, e)
            db.delete_figure(figure_id)
    log.info("Clipped %d key figure(s) for %s ch. %s", saved, chapter.course_name, chapter.number)
    return saved
