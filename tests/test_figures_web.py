import io
import re
from datetime import date

import pytest
from conftest import FakeProvider, make_pdf, summary_json

from readcue.config import Config
from readcue.figures import figure_dir, figure_path
from readcue.models import Summary
from readcue.pdfs import chapter_pdf_path
from readcue.web import create_app

TODAY = date(2026, 10, 1)


@pytest.fixture
def client(cfg, db, notifier):
    provider = FakeProvider(lambda user: summary_json())
    return create_app(
        cfg, db, provider_factory=lambda: provider, notifier=notifier, today_fn=lambda: TODAY
    ).test_client()


@pytest.fixture
def chapter_with_figure(db, cfg):
    course = db.add_course("BIO 101")
    db.upsert_reading(course.id, 4, date(2026, 10, 18), "Cells")
    chapter_id = db.save_chapter(course.id, 4, "Cells", "text", "ch4.pdf")
    db.save_summary(chapter_id, Summary("Overview.", ["point"], []), "fake")
    figure_id = db.add_figure(chapter_id, 3, "The cell membrane", "It shows how molecules cross.")
    path = figure_path(cfg, chapter_id, figure_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\nfake-image-bytes")
    return course, chapter_id, figure_id


def test_the_summary_page_shows_each_figure_with_its_caption(client, chapter_with_figure):
    _, chapter_id, figure_id = chapter_with_figure
    page = client.get(f"/summary/{chapter_id}").data.decode()
    assert "Key figures" in page and "The cell membrane" in page and "It shows how molecules cross." in page
    assert f'src="/figures/{figure_id}.png"' in page and "Chapter PDF page 3" in page
    assert page.index("Key figures") < page.index("Key points")


def test_a_summary_without_figures_has_no_figures_section(client, db):
    course = db.add_course("BIO 101")
    chapter_id = db.save_chapter(course.id, 4, "", "text", "x")
    db.save_summary(chapter_id, Summary("Overview.", ["p"], []), "fake")
    assert b"Key figures" not in client.get(f"/summary/{chapter_id}").data


def test_the_image_is_served_as_a_png(client, chapter_with_figure):
    _, _, figure_id = chapter_with_figure
    resp = client.get(f"/figures/{figure_id}.png")
    assert (
        resp.status_code == 200 and resp.mimetype == "image/png" and resp.data.endswith(b"fake-image-bytes")
    )


def test_missing_figures_and_files_are_404(client, db, cfg, chapter_with_figure):
    _, chapter_id, figure_id = chapter_with_figure
    assert client.get("/figures/999.png").status_code == 404
    figure_path(cfg, chapter_id, figure_id).unlink()
    assert client.get(f"/figures/{figure_id}.png").status_code == 404


def test_a_figure_can_be_removed_from_the_summary(client, db, cfg, chapter_with_figure):
    _, chapter_id, figure_id = chapter_with_figure
    resp = client.post(f"/figures/{figure_id}/delete")
    assert resp.status_code == 302 and resp.headers["Location"].endswith(f"/summary/{chapter_id}")
    assert db.list_figures(chapter_id) == [] and not figure_path(cfg, chapter_id, figure_id).exists()
    assert b"Key figures" not in client.get(f"/summary/{chapter_id}").data


def test_replacing_a_chapter_discards_figures_that_belonged_to_the_old_pages(
    client, db, cfg, chapter_with_figure
):
    course, chapter_id, _ = chapter_with_figure
    client.post("/chapters", data={"course_id": course.id, "number": "4", "text": "brand new text"})
    assert db.list_figures(chapter_id) == [] and not figure_dir(cfg, chapter_id).exists()


def test_deleting_a_chapter_or_course_removes_its_figure_files(client, db, cfg, chapter_with_figure):
    course, chapter_id, _ = chapter_with_figure
    client.post(f"/chapters/{chapter_id}/delete")
    assert not figure_dir(cfg, chapter_id).exists()

    other = db.save_chapter(course.id, 5, "", "text", "x")
    db.add_figure(other, 1, "c", "w")
    figure_path(cfg, other, 1).parent.mkdir(parents=True, exist_ok=True)
    figure_path(cfg, other, 1).write_bytes(b"x")
    client.post(f"/courses/{course.id}/delete")
    assert not figure_dir(cfg, other).exists()


def test_the_course_setting_turns_figures_on_and_off(client, db):
    course = db.add_course("BIO 101")
    client.post(f"/courses/{course.id}/settings", data={"summary_mode": "scheduled", "include_figures": "on"})
    assert db.get_course(course.id).include_figures is True
    client.post(f"/courses/{course.id}/settings", data={"summary_mode": "scheduled"})
    assert db.get_course(course.id).include_figures is False


def test_the_toggle_is_offered_on_the_dashboard(client, db):
    db.add_course("BIO 101")
    page = client.get("/").data.decode()
    assert "Clip key figures into summaries" in page and 'name="include_figures"' in page
    assert "Claude picks the few figures that matter most" in page


def test_with_ollama_the_toggle_is_disabled_and_cant_be_switched_on(db, cfg, notifier):
    cfg.provider = "ollama"
    course = db.add_course("BIO 101")
    client = create_app(cfg, db, notifier=notifier, today_fn=lambda: TODAY).test_client()
    page = client.get("/").data.decode()
    assert "Needs the Claude API" in page
    assert re.search(r'name="include_figures"[^>]*disabled', page)
    client.post(f"/courses/{course.id}/settings", data={"summary_mode": "scheduled", "include_figures": "on"})
    assert db.get_course(course.id).include_figures is False


def test_the_summary_explains_when_figures_are_wanted_but_there_are_no_pages(client, db):
    course = db.add_course("BIO 101")
    db.update_course_settings(course.id, "now", False, include_figures=True)
    chapter_id = db.save_chapter(course.id, 4, "", "pasted", "pasted text")
    db.save_summary(chapter_id, Summary("Overview.", ["p"], []), "fake")
    assert b"has no original pages to pick them from" in client.get(f"/summary/{chapter_id}").data


def test_uploading_a_pdf_keeps_pages_that_figures_can_come_from(client, db, cfg):
    course = db.add_course("BIO 101")
    client.post(
        "/chapters",
        data={
            "course_id": course.id,
            "number": "4",
            "file": (io.BytesIO(make_pdf(["A page with some words on it"])), "c.pdf"),
        },
        content_type="multipart/form-data",
    )
    chapter = db.get_chapter(1)
    assert chapter.has_pdf and chapter_pdf_path(cfg, chapter.id).is_file() and isinstance(cfg, Config)
