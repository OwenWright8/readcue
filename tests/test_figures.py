"""Claude picks the figures that matter most in a chapter; they are clipped out of its pages."""

import json
import logging
import shutil
import struct
from datetime import date

import anthropic
import httpx2
import pytest
from conftest import FakeProvider, make_pdf, summary_json

from readcue import figures, render, schemas
from readcue.config import Config
from readcue.errors import LLMError, ReadcueError
from readcue.figures import (
    MAX_FIGURES,
    Choice,
    clip_rect,
    extract_figures,
    figure_dir,
    figure_path,
    find_figures,
    parse_choices,
)
from readcue.llm.anthropic_provider import AnthropicProvider
from readcue.llm.ollama import OllamaProvider
from readcue.models import Summary
from readcue.pdfs import chapter_pdf_path
from readcue.worker import process_next

SUMMARY = Summary("Cells are the unit of life.", ["Cells have organelles", "The nucleus stores DNA"], [])


def item(page, importance=9, box=(0.1, 0.2, 0.8, 0.6), caption="A cell", why="It shows the parts."):
    x0, y0, x1, y1 = box
    return {
        "page": page,
        "x0": x0,
        "y0": y0,
        "x1": x1,
        "y1": y1,
        "caption": caption,
        "why": why,
        "importance": importance,
    }


# ---- reading the model's answer ---------------------------------------------------------


def test_parse_choices_keeps_good_figures():
    got = parse_choices({"figures": [item(3), item(4, box=(0.0, 0.0, 1.0, 0.5))]}, pages=[3, 4, 5])
    assert [(c.page, c.importance, c.caption) for c in got] == [(3, 9, "A cell"), (4, 9, "A cell")]


@pytest.mark.parametrize(
    "bad",
    [
        item(99),  # a page that wasn't shown
        {**item(3), "x0": None},
        {**item(3), "importance": "high"},
        {**item(3), "x0": 0.5, "x1": 0.5},  # no width
        "not a dict",
    ],
)
def test_parse_choices_drops_malformed_figures(bad):
    assert parse_choices({"figures": [bad]}, pages=[3, 4]) == []


def test_parse_choices_clamps_and_orders_coordinates():
    (c,) = parse_choices({"figures": [item(3, box=(0.9, 1.4, -0.2, 0.3))]}, pages=[3])
    assert (c.x0, c.y0, c.x1, c.y1) == (0.0, 0.3, 0.9, 1.0)


def test_parse_choices_copes_with_nothing():
    assert parse_choices({}, pages=[1]) == parse_choices({"figures": None}, pages=[1]) == []


# ---- turning a box into pixels ---------------------------------------------------------


def test_clip_rect_pads_the_box_and_stays_on_the_page():
    x, y, w, h = clip_rect(Choice(1, 0.1, 0.2, 0.5, 0.6, "", "", 9), 1000, 2000)
    assert (x, y) == (88, 376) and w == 424 and h == 848  # a 1.2% pad on every side
    x, y, w, h = clip_rect(Choice(1, 0.0, 0.0, 1.0, 1.0, "", "", 9), 1000, 2000)
    assert (x, y, w, h) == (0, 0, 1000, 2000)  # never spills past the page


def test_clip_rect_refuses_slivers():
    assert clip_rect(Choice(1, 0.4, 0.4, 0.41, 0.41, "", "", 9), 1000, 1000) is None


# ---- asking Claude: images go in labelled, the schema comes back -----------------------


def claude(handler) -> AnthropicProvider:
    provider = AnthropicProvider(Config(provider="anthropic", anthropic_api_key="k"))
    provider._client = anthropic.Anthropic(
        api_key="k", http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler))
    )
    return provider


def test_claude_receives_labelled_page_images_before_the_question():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json={"id": "m", "type": "message", "role": "assistant", "model": "claude-sonnet-5",
                  "content": [{"type": "text", "text": '{"figures": []}'}], "stop_reason": "end_turn",
                  "usage": {"input_tokens": 1, "output_tokens": 1}},
        )  # fmt: skip

    claude(handler).complete(
        "sys",
        "Pick figures",
        schema=schemas.FIGURES_SCHEMA,
        images=[("Page 1", b"\x89PNGa"), ("Page 2", b"\x89PNGb")],
    )
    content = seen[0]["messages"][0]["content"]
    assert [b["type"] for b in content] == ["text", "image", "text", "image", "text"]
    assert content[0]["text"] == "Page 1" and content[4]["text"] == "Pick figures"
    assert content[1]["source"]["media_type"] == "image/png" and content[1]["source"]["type"] == "base64"
    assert seen[0]["output_config"]["format"]["schema"] == schemas.FIGURES_SCHEMA


def test_without_images_the_message_stays_a_plain_string():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json={"id": "m", "type": "message", "role": "assistant", "model": "x", "stop_reason": "end_turn",
                  "content": [{"type": "text", "text": "OK"}], "usage": {"input_tokens": 1, "output_tokens": 1}},
        )  # fmt: skip

    claude(handler).complete("sys", "hello")
    assert seen[0]["messages"][0]["content"] == "hello"


def test_only_claude_can_look_at_pictures():
    assert AnthropicProvider.supports_images is True and OllamaProvider.supports_images is False
    with pytest.raises(LLMError, match="text-only"):
        OllamaProvider(Config(provider="ollama")).complete("s", "u", images=[("Page 1", b"x")])


# ---- picking figures for a chapter ------------------------------------------------------


@pytest.fixture
def fake_render(monkeypatch):
    calls = {"view": [], "clip": []}

    def render_page(pdf, page, dpi, crop=None):
        (calls["clip"] if crop else calls["view"]).append((page, dpi, crop))
        return b"\x89PNG-clip" if crop else b"\x89PNG-view"

    monkeypatch.setattr(render, "render_page", render_page)
    monkeypatch.setattr(render, "page_size_px", lambda pdf, page, dpi: (1700, 2200))
    return calls


def write_pdf(cfg, chapter_id, pages):
    path = chapter_pdf_path(cfg, chapter_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        make_pdf([f"Page {i} of the chapter with some words on it" for i in range(1, pages + 1)])
    )
    return path


def test_pages_are_sent_in_batches_with_the_summary_for_context(tmp_path, fake_render):
    pdf = tmp_path / "ch.pdf"
    pdf.write_bytes(make_pdf([f"Page {i} of the chapter with some words on it" for i in range(1, 26)]))

    def responder(user):
        if "pages 1 to 20" in user:
            return json.dumps({"figures": [item(3, 9), item(30, 10)]})  # page 30 wasn't shown: ignored
        return json.dumps({"figures": [item(22, 8), item(24, 5)]})  # 5 is below the bar

    provider = FakeProvider(responder)
    got = find_figures(provider, pdf, course="BIO 101", number=4, title="Cells", summary=SUMMARY)

    assert [(c.page, c.importance) for c in got] == [(3, 9), (22, 8)]
    assert [len(images) for images in provider.images] == [20, 5]
    assert [label for label, _ in provider.images[0][:2]] == ["Page 1", "Page 2"]
    assert provider.schemas == [schemas.FIGURES_SCHEMA] * 2
    assert "The nucleus stores DNA" in provider.calls[0] and "Chapter 4: Cells" in provider.calls[0]
    assert {dpi for _, dpi, _ in fake_render["view"]} == {figures.VIEW_DPI}


def test_only_the_best_few_are_kept(tmp_path, fake_render):
    pdf = tmp_path / "ch.pdf"
    pdf.write_bytes(make_pdf(["A page with some words on it here"] * 6))
    many = [item(p, importance=imp) for p, imp in [(1, 7), (2, 10), (3, 8), (4, 9), (5, 7), (6, 8)]]
    got = find_figures(
        FakeProvider([json.dumps({"figures": many})]), pdf, course="C", number=1, title="", summary=SUMMARY
    )
    assert len(got) == MAX_FIGURES and [c.importance for c in got] == [10, 9, 8, 8]


def test_a_long_chapter_is_looked_at_only_up_to_a_limit(tmp_path, fake_render):
    pdf = tmp_path / "ch.pdf"
    pdf.write_bytes(make_pdf(["A page with some words on it here"] * 70))
    provider = FakeProvider(lambda user: json.dumps({"figures": []}))
    assert find_figures(provider, pdf, course="C", number=1, title="", summary=SUMMARY) == []
    assert len(provider.calls) == 3 and len(fake_render["view"]) == figures.MAX_PAGES


# ---- clipping and storing --------------------------------------------------------------


@pytest.fixture
def chapter(db, cfg):
    course = db.add_course("BIO 101")
    chapter_id = db.save_chapter(course.id, 4, "Cells", "text", "ch4.pdf")
    return db.get_chapter(chapter_id)


def test_extract_figures_saves_the_clip_and_its_caption(db, cfg, chapter, fake_render):
    write_pdf(cfg, chapter.id, 3)
    provider = FakeProvider(
        [json.dumps({"figures": [item(2, 9, box=(0.1, 0.2, 0.5, 0.6), caption="Cell parts")]})]
    )
    assert extract_figures(db, cfg, provider, chapter, SUMMARY) == 1

    (fig,) = db.list_figures(chapter.id)
    assert (fig.page, fig.caption, fig.why) == (2, "Cell parts", "It shows the parts.")
    assert figure_path(cfg, chapter.id, fig.id).read_bytes() == b"\x89PNG-clip"
    (page, dpi, crop) = fake_render["clip"][0]
    assert (page, dpi, crop) == (
        2,
        figures.CLIP_DPI,
        clip_rect(Choice(2, 0.1, 0.2, 0.5, 0.6, "", "", 9), 1700, 2200),
    )


def test_running_again_replaces_the_earlier_figures(db, cfg, chapter, fake_render):
    write_pdf(cfg, chapter.id, 3)
    for caption in ("first pass", "second pass"):
        provider = FakeProvider([json.dumps({"figures": [item(1, caption=caption)]})])
        extract_figures(db, cfg, provider, chapter, SUMMARY)
    assert [f.caption for f in db.list_figures(chapter.id)] == ["second pass"]
    assert len(list(figure_dir(cfg, chapter.id).glob("*.png"))) == 1


def test_a_clip_that_cant_be_made_is_dropped_without_losing_the_rest(
    db, cfg, chapter, fake_render, monkeypatch
):
    write_pdf(cfg, chapter.id, 3)
    tiny = item(1, caption="a speck", box=(0.4, 0.4, 0.401, 0.401))
    fine = item(2, caption="a diagram")
    assert (
        extract_figures(db, cfg, FakeProvider([json.dumps({"figures": [tiny, fine]})]), chapter, SUMMARY) == 1
    )
    assert [f.caption for f in db.list_figures(chapter.id)] == ["a diagram"]


def test_a_render_failure_drops_only_that_figure(db, cfg, chapter, fake_render, monkeypatch, caplog):
    write_pdf(cfg, chapter.id, 3)
    real = render.render_page

    def flaky(pdf, page, dpi, crop=None):
        if crop and page == 1:
            raise ReadcueError("pdftoppm failed")
        return real(pdf, page, dpi, crop)

    monkeypatch.setattr(render, "render_page", flaky)
    both = json.dumps({"figures": [item(1, caption="bad"), item(2, caption="good")]})
    with caplog.at_level(logging.WARNING):
        assert extract_figures(db, cfg, FakeProvider([both]), chapter, SUMMARY) == 1
    assert [f.caption for f in db.list_figures(chapter.id)] == ["good"] and "Dropped a figure" in caplog.text


def test_a_chapter_without_its_pages_is_left_alone(db, cfg, chapter):
    provider = FakeProvider([])
    assert extract_figures(db, cfg, provider, chapter, SUMMARY) == 0 and provider.calls == []


# ---- after a summary, in the worker ----------------------------------------------------

TODAY_LEAD = {"today": date(2026, 10, 1), "lead_days": 3}


def figure_setup(db, cfg, *, include=True, has_pdf=True):
    course = db.add_course("BIO 101")
    db.update_course_settings(course.id, "now", False, include_figures=include)
    chapter_id = db.save_chapter(course.id, 4, "Cells", "Cells are the unit of life.", "ch4.pdf")
    if has_pdf:
        write_pdf(cfg, chapter_id, 2)
        db.set_chapter_pdf(chapter_id, True)
    return chapter_id


def summary_and_figures(user):
    return (
        json.dumps({"figures": [item(1, caption="A diagram")]})
        if "especially important" in user
        else summary_json()
    )


def test_key_figures_follow_the_summary_when_the_course_wants_them(db, cfg, fake_render):
    chapter_id = figure_setup(db, cfg)
    provider = FakeProvider(summary_and_figures)
    assert process_next(db, lambda: provider, cfg=cfg, **TODAY_LEAD) is True
    assert db.get_chapter(chapter_id).summary_status == "done"
    assert [f.caption for f in db.list_figures(chapter_id)] == ["A diagram"]


@pytest.mark.parametrize("why", ["course turned it off", "no original pages", "model can't see images"])
def test_nothing_is_attempted_when_figures_arent_possible(db, cfg, fake_render, why):
    chapter_id = figure_setup(
        db, cfg, include=why != "course turned it off", has_pdf=why != "no original pages"
    )
    provider = FakeProvider(summary_and_figures)
    provider.supports_images = why != "model can't see images"
    process_next(db, lambda: provider, cfg=cfg, **TODAY_LEAD)
    assert db.get_chapter(chapter_id).summary_status == "done"
    assert len(provider.calls) == 1 and db.list_figures(chapter_id) == []  # just the summary request


def test_a_failure_picking_figures_never_costs_the_summary(db, cfg, fake_render, caplog):
    chapter_id = figure_setup(db, cfg)

    def responder(user):
        if "especially important" in user:
            raise LLMError("vision request failed")
        return summary_json()

    with caplog.at_level(logging.WARNING):
        process_next(db, lambda: FakeProvider(responder), cfg=cfg, **TODAY_LEAD)
    assert db.get_chapter(chapter_id).summary_status == "done" and db.get_summary(chapter_id) is not None
    assert "Couldn't pick key figures" in caplog.text


def test_the_notification_goes_out_before_the_slower_figure_step(db, cfg, fake_render, monkeypatch):
    figure_setup(db, cfg)
    order = []
    previous = render.render_page
    monkeypatch.setattr(render, "render_page", lambda *a, **k: order.append("figures") or previous(*a, **k))
    on_complete = lambda chapter, summary: order.append("notified")  # noqa: E731
    process_next(
        db, lambda: FakeProvider(summary_and_figures), cfg=cfg, on_complete=on_complete, **TODAY_LEAD
    )
    assert order[0] == "notified" and "figures" in order


def test_the_worker_runs_without_a_config_as_before(db):
    course = db.add_course("BIO 101")
    db.update_course_settings(course.id, "now", False, include_figures=True)
    chapter_id = db.save_chapter(course.id, 4, "Cells", "text", "x")
    provider = FakeProvider(summary_and_figures)
    process_next(db, lambda: provider, **TODAY_LEAD)
    assert len(provider.calls) == 1 and db.get_chapter(chapter_id).summary_status == "done"


def test_course_setting_defaults_and_leaves_figures_alone_when_not_given(db):
    course = db.add_course("BIO 101")
    assert course.include_figures is False
    db.update_course_settings(course.id, "now", True, include_figures=True)
    db.update_course_settings(
        course.id, "scheduled", False
    )  # e.g. the syllabus review doesn't mention figures
    assert db.get_course(course.id).include_figures is True


def test_figures_are_removed_with_their_chapter(db, cfg):
    course = db.add_course("BIO 101")
    chapter_id = db.save_chapter(course.id, 4, "", "text", "x")
    db.add_figure(chapter_id, 1, "c", "w")
    db.delete_chapter(chapter_id)
    assert db.list_figures(chapter_id) == []


# ---- real poppler: rendering and clipping -----------------------------------------------

needs_poppler = pytest.mark.skipif(not shutil.which("pdftoppm"), reason="poppler not installed")


def png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


@needs_poppler
def test_pages_render_and_clip_to_the_requested_rectangle(tmp_path):
    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(make_pdf(["A page with some words on it here", "Another page with words on it too"]))
    assert render.page_size_px(pdf, 1, 100) == (850, 1100)  # US letter
    assert png_size(render.render_page(pdf, 1, 100)) == (850, 1100)
    assert png_size(render.render_page(pdf, 2, 100, crop=(50, 100, 300, 200))) == (300, 200)


def test_page_size_reports_readable_errors(tmp_path):
    (tmp_path / "bad.pdf").write_bytes(b"not a pdf")
    with pytest.raises(ReadcueError, match="Couldn't read page 1"):
        render.page_size_px(tmp_path / "bad.pdf", 1, 100)


def test_figure_schema_is_registered_with_the_strict_schema_test():
    assert schemas.FIGURES_SCHEMA["properties"]["figures"]["items"]["required"] == [
        "page", "x0", "y0", "x1", "y1", "caption", "why", "importance",
    ]  # fmt: skip
