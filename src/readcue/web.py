"""Flask web UI: manage courses, import syllabi, upload chapters, read summaries."""

from __future__ import annotations

import hmac
import logging
import os
import re
import shutil
import threading
from collections.abc import Callable, Sequence
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from . import __version__, ocr
from .books import book_path
from .config import Config
from .db import Database
from .detect import detect_chapters
from .errors import NotFoundError, NotifyError, ReadcueError
from .extract import MAX_TEXT_CHARS, extract_text, normalize_text
from .figures import clear_figures, figure_dir, figure_path
from .llm import make_provider
from .llm.base import LLMProvider
from .notify import DEVICES_SETTING, PushoverNotifier, notifier_for
from .pagecheck import check_range
from .pdfs import chapter_pdf_path, download_name, merge_pdfs, slice_pdf
from .syllabus import extract_schedule

log = logging.getLogger(__name__)

MAX_PASTED_BYTES = 20 * 1024 * 1024  # non-file form fields, i.e. pasted text

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'"
)


def _int(value: str | None, what: str) -> int:
    try:
        return int((value or "").strip())
    except ValueError:
        raise ReadcueError(f"{what} must be a whole number.") from None


def _date(value: str | None, what: str) -> date:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        raise ReadcueError(f"{what} must be a date.") from None


def relative_day(d: date, today: date) -> str:
    """'Today', 'Tomorrow', 'In 5 days', '3 days ago'."""
    days = (d - today).days
    if days == 0:
        return "Today"
    if days == 1:
        return "Tomorrow"
    if days == -1:
        return "Yesterday"
    return f"In {days} days" if days > 0 else f"{-days} days ago"


def _natural_key(name: str) -> list:
    """Sort key so page10.jpg comes after page2.jpg."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name.lower())]


def create_app(
    cfg: Config,
    db: Database,
    *,
    wake: threading.Event | None = None,
    wake_books: threading.Event | None = None,
    provider_factory: Callable[[], LLMProvider] | None = None,
    notifier: PushoverNotifier | None = None,
    today_fn: Callable[[], date] = date.today,
    threads: Sequence[threading.Thread] = (),
) -> Flask:
    app = Flask(__name__)
    max_upload = cfg.max_upload_mb * 1024 * 1024
    app.config["MAX_CONTENT_LENGTH"] = max_upload
    app.config["MAX_FORM_MEMORY_SIZE"] = MAX_PASTED_BYTES
    app.secret_key = os.urandom(32)  # only signs flash messages; a restart just drops pending ones
    wake = wake or threading.Event()
    wake_books = wake_books or threading.Event()
    provider_factory = provider_factory or (lambda: make_provider(cfg))
    notifier = notifier or notifier_for(cfg, db)

    app.jinja_env.globals["app_version"] = __version__
    # A reverse proxy may rewrite Host, so the public address from READCUE_BASE_URL also counts as same-origin.
    trusted_hosts = {urlsplit(cfg.base_url).netloc} - {""}

    app.jinja_env.filters["nice_date"] = lambda d: f"{d:%a %b} {d.day}"
    app.jinja_env.filters["relative_day"] = relative_day
    app.jinja_env.filters["month_abbr"] = lambda d: f"{d:%b}"
    app.jinja_env.filters["short_date"] = lambda d: f"{d:%b} {d.day}"

    def refresh_chapter_pdf(chapter_id: int, write: Callable[[Path], None] | None = None) -> None:
        """Replace the chapter's stored PDF (or just remove it when `write` is None).

        A failure here only means no download button; the chapter itself is already saved.
        """
        clear_figures(db, cfg, chapter_id)  # the clips belong to the old pages
        path = chapter_pdf_path(cfg, chapter_id)
        path.unlink(missing_ok=True)
        db.set_chapter_pdf(chapter_id, False)
        if write is None:
            return
        try:
            write(path)
            db.set_chapter_pdf(chapter_id, True)
        except ReadcueError as e:
            log.warning("Couldn't keep a PDF for chapter %s: %s", chapter_id, e)
            path.unlink(missing_ok=True)

    def uploaded_text(pages: str | None = None) -> tuple[str, str, list[bytes]]:
        """Text from the uploaded file(s) or the paste box, a short description of the source, and the PDF
        bytes (when everything uploaded was a PDF, so the chapter's pages can be kept).

        Several files (say, one photo per page) are read in filename order and joined.
        """
        files = sorted(
            (f for f in request.files.getlist("file") if f.filename), key=lambda f: _natural_key(f.filename)
        )
        pasted = request.form.get("text", "").strip()
        if files:
            if pages and len(files) > 1:
                raise ReadcueError("A page range only works with a single PDF.")
            blobs = [(f.filename, f.read()) for f in files]
            text = "\n\n".join(extract_text(data, name, pages, ocr_lang=cfg.ocr_lang) for name, data in blobs)
            extra = f" (+{len(files) - 1} more)" if len(files) > 1 else ""
            all_pdf = all(name.lower().endswith(".pdf") for name, _ in blobs)
            return text, files[0].filename + extra, [data for _, data in blobs] if all_pdf else []
        if pasted:
            return normalize_text(pasted), "pasted text", []
        raise ReadcueError("Choose a file or paste the text.")

    # -- guards ----------------------------------------------------------------------------

    @app.before_request
    def guard():
        if request.path == "/healthz":
            return None
        if cfg.password:
            auth = request.authorization
            supplied = ((auth.username or "") + "\0" + (auth.password or "")).encode() if auth else b""
            expected = f"{cfg.username}\0{cfg.password}".encode()
            if not hmac.compare_digest(supplied, expected):
                return Response("Sign in required.", 401, {"WWW-Authenticate": 'Basic realm="readcue"'})
        if request.method == "POST":  # refuse cross-site form posts
            origin = request.headers.get("Origin")
            if origin and urlsplit(origin).netloc not in trusted_hosts | {request.host}:
                abort(403)
        return None

    @app.after_request
    def secure_headers(response: Response) -> Response:
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if request.endpoint != "static":  # chapters and summaries are private; keep them out of shared caches
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.errorhandler(ReadcueError)
    def handle_error(error: ReadcueError):
        if isinstance(error, NotFoundError) and request.method == "GET":
            return Response("Not found.", 404, mimetype="text/plain")
        flash(str(error), "error")
        return redirect(request.referrer or url_for("index"))

    @app.errorhandler(413)
    def too_large(_):
        flash(f"That upload is too large (limit {cfg.max_upload_mb} MB).", "error")
        return redirect(request.referrer or url_for("index"))

    @app.get("/healthz")
    def healthz():
        problems = []
        try:
            db.ping()
        except Exception as e:
            problems.append(f"database: {e}")
        problems += [f"{t.name} thread stopped" for t in threads if not t.is_alive()]
        body = {"status": "ok" if not problems else "unhealthy", "version": __version__, "problems": problems}
        return jsonify(body), (200 if not problems else 503)

    # -- dashboard -------------------------------------------------------------------------

    @app.get("/")
    def index():
        readings = db.list_readings()
        books = db.list_books()
        today, sum_days = today_fn(), cfg.summarize_days_before
        groups = []
        for course in db.list_courses():
            mine = [r for r in readings if r.course_id == course.id]
            groups.append(
                {
                    "course": course,
                    "readings": mine,
                    "ready": sum(r.summary_ready for r in mine),
                    "books": [b for b in books if b.course_id == course.id],
                }
            )
        stages = [r.stage(today, sum_days) for r in readings]
        stats = {
            "due_soon": sum(1 for r in readings if 0 <= (r.due - today).days <= 7),
            "ready": stages.count("done"),
            "waiting": stages.count("waiting"),
            "to_add": sum(
                1 for r, s in zip(readings, stages, strict=True) if s == "missing" and r.due >= today
            ),
        }
        return render_template(
            "index.html",
            groups=groups,
            stats=stats,
            busy=any(s in ("queued", "running") for s in stages) or any(b.working for b in books),
            today=today,
            lead=timedelta(days=cfg.notify_days_before),
            sum_days=sum_days,
            figures_supported=cfg.provider == "anthropic",
        )

    # -- courses and schedule --------------------------------------------------------------

    @app.post("/courses")
    def course_add():
        course = db.add_course(request.form.get("name", ""))
        return redirect(url_for("syllabus_form", course_id=course.id))

    @app.post("/courses/<int:course_id>/delete")
    def course_delete(course_id: int):
        for chapter_id in db.chapter_ids(course_id):
            chapter_pdf_path(cfg, chapter_id).unlink(missing_ok=True)
            shutil.rmtree(figure_dir(cfg, chapter_id), ignore_errors=True)
        db.delete_course(course_id)
        flash("Course deleted.", "ok")
        return redirect(url_for("index"))

    @app.get("/courses/<int:course_id>/syllabus")
    def syllabus_form(course_id: int):
        return render_template("syllabus_form.html", course=db.get_course(course_id), model=cfg.model_name)

    @app.post("/courses/<int:course_id>/syllabus")
    def syllabus_preview(course_id: int):
        course = db.get_course(course_id)
        text, _, _ = uploaded_text()
        items = extract_schedule(provider_factory(), text, today=today_fn())
        if not items:
            raise ReadcueError(
                "No chapter readings were found in that syllabus. Try pasting just the schedule."
            )
        return render_template(
            "syllabus_preview.html", course=course, items=items, sum_days=cfg.summarize_days_before
        )

    @app.post("/courses/<int:course_id>/syllabus/save")
    def syllabus_save(course_id: int):
        course = db.get_course(course_id)
        saved = 0
        for i in range(_int(request.form.get("count"), "Row count")):
            if not request.form.get(f"include-{i}"):
                continue
            db.upsert_reading(
                course.id,
                _int(request.form.get(f"chapter-{i}"), "Chapter"),
                _date(request.form.get(f"due-{i}"), "Due date"),
                request.form.get(f"title-{i}", "").strip(),
            )
            saved += 1
        db.update_course_settings(
            course.id,
            request.form.get("summary_mode", course.summary_mode),
            bool(request.form.get("notify_on_summary")),
        )
        wake.set()  # a switch to "now" makes already-uploaded chapters eligible
        flash(f"Saved {saved} reading{'s' if saved != 1 else ''} for {course.name}.", "ok")
        return redirect(url_for("index"))

    @app.post("/courses/<int:course_id>/settings")
    def course_settings(course_id: int):
        course = db.get_course(course_id)
        db.update_course_settings(
            course.id,
            request.form.get("summary_mode", ""),
            bool(request.form.get("notify_on_summary")),
            include_figures=bool(request.form.get("include_figures")) and cfg.provider == "anthropic",
        )
        wake.set()
        flash(f"Summary settings updated for {course.name}.", "ok")
        return redirect(url_for("index"))

    @app.post("/courses/<int:course_id>/readings")
    def reading_add(course_id: int):
        course = db.get_course(course_id)
        db.upsert_reading(
            course.id,
            _int(request.form.get("chapter"), "Chapter"),
            _date(request.form.get("due"), "Due date"),
            request.form.get("title", "").strip(),
        )
        return redirect(url_for("index"))

    @app.post("/readings/<int:reading_id>/update")
    def reading_update(reading_id: int):
        db.update_reading(
            reading_id,
            _date(request.form.get("due"), "Due date"),
            request.form.get("title", "").strip(),
        )
        return redirect(url_for("index"))

    @app.post("/readings/<int:reading_id>/delete")
    def reading_delete(reading_id: int):
        db.delete_reading(reading_id)
        return redirect(url_for("index"))

    # -- chapters and summaries ------------------------------------------------------------

    @app.get("/chapters/new")
    def chapter_form():
        return render_template(
            "chapter_form.html",
            courses=db.list_courses(),
            course_id=request.args.get("course_id", type=int),
            chapter=request.args.get("chapter", type=int),
            model=cfg.model_name,
            sum_days=cfg.summarize_days_before,
        )

    @app.post("/chapters")
    def chapter_add():
        course = db.get_course(_int(request.form.get("course_id"), "Course"))
        number = _int(request.form.get("number"), "Chapter number")
        pages_spec = request.form.get("pages", "").strip() or None
        text, source, pdfs = uploaded_text(pages_spec)

        reading = db.find_reading(course.id, number)
        title = request.form.get("title", "").strip() or (reading.title if reading else "")
        chapter_id = db.save_chapter(course.id, number, title, text, source)
        refresh_chapter_pdf(chapter_id, (lambda dest: merge_pdfs(pdfs, pages_spec, dest)) if pdfs else None)
        wake.set()
        if reading is None:
            flash(
                f"Chapter {number} added, but it isn't on {course.name}'s schedule yet, so it won't be "
                "summarized or reminded until you add its due date below.",
                "warn",
            )
        elif (
            course.summary_mode != "now"
            and (starts := reading.summarize_on(cfg.summarize_days_before)) > today_fn()
        ):
            flash(
                f"Chapter {number} added. Its summary will be generated on {starts:%a %b} {starts.day}, "
                f"{cfg.summarize_days_before} days before it's due (you'll get a notification when it's done). "
                "Use Summarize now to do it earlier.",
                "ok",
            )
        else:
            flash(f"Chapter {number} added. The summary is being generated.", "ok")
        return redirect(url_for("index"))

    @app.get("/summary/<int:chapter_id>")
    def summary_page(chapter_id: int):
        chapter = db.get_chapter(chapter_id)
        summary = db.get_summary(chapter_id) if chapter.summary_status == "done" else None
        reading = db.find_reading(chapter.course_id, chapter.number)
        today, sum_days = today_fn(), cfg.summarize_days_before
        held_back = (
            chapter.summary_status == "pending"
            and not chapter.summarize_now
            and chapter.summary_mode != "now"
        )
        return render_template(
            "summary.html",
            chapter=chapter,
            summary=summary,
            reading=reading,
            today=today,
            waiting_until=reading.summarize_on(sum_days)
            if reading and reading.waiting(today, sum_days)
            else None,
            unscheduled=held_back and reading is None,
            sum_days=sum_days,
            figures=db.list_figures(chapter_id),
            wants_figures=db.get_course(chapter.course_id).include_figures,
        )

    @app.get("/summary/<int:chapter_id>.md")
    def summary_markdown(chapter_id: int):
        chapter = db.get_chapter(chapter_id)
        summary = db.get_summary(chapter_id)
        if summary is None:
            abort(404)
        body = summary.to_markdown(course=chapter.course_name, chapter=chapter.number, title=chapter.title)
        return Response(body, mimetype="text/markdown")

    @app.get("/chapters/<int:chapter_id>/pdf")
    def chapter_pdf(chapter_id: int):
        chapter = db.get_chapter(chapter_id)
        path = chapter_pdf_path(cfg, chapter_id)
        if not chapter.has_pdf or not path.is_file():
            abort(404)
        return send_file(
            path,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=download_name(chapter.course_name, chapter.number, chapter.title),
        )

    @app.get("/figures/<int:figure_id>.png")
    def figure_image(figure_id: int):
        figure = db.get_figure(figure_id)
        path = figure_path(cfg, figure.chapter_id, figure.id)
        if not path.is_file():
            abort(404)
        return send_file(path, mimetype="image/png", max_age=3600)

    @app.post("/figures/<int:figure_id>/delete")
    def figure_delete(figure_id: int):
        figure = db.get_figure(figure_id)
        figure_path(cfg, figure.chapter_id, figure.id).unlink(missing_ok=True)
        db.delete_figure(figure_id)
        return redirect(url_for("summary_page", chapter_id=figure.chapter_id))

    @app.post("/chapters/<int:chapter_id>/resummarize")
    def chapter_resummarize(chapter_id: int):
        db.get_chapter(chapter_id)
        db.requeue_chapter(chapter_id)
        wake.set()
        return redirect(request.referrer or url_for("index"))

    @app.post("/chapters/<int:chapter_id>/delete")
    def chapter_delete(chapter_id: int):
        refresh_chapter_pdf(chapter_id)
        db.delete_chapter(chapter_id)
        flash("Chapter and its summary deleted.", "ok")
        return redirect(url_for("index"))

    # -- whole textbooks ---------------------------------------------------------------------

    @app.get("/courses/<int:course_id>/books/new")
    def book_form(course_id: int):
        return render_template("book_form.html", course=db.get_course(course_id), max_mb=cfg.max_upload_mb)

    @app.post("/courses/<int:course_id>/books")
    def book_add(course_id: int):
        course = db.get_course(course_id)
        upload = request.files.get("file")
        if not upload or not upload.filename:
            raise ReadcueError("Choose the textbook PDF.")
        if not upload.filename.lower().endswith(".pdf"):
            raise ReadcueError("The textbook has to be a PDF. (For separate chapter files, use Add chapter.)")
        book_id = db.add_book(course.id, Path(upload.filename).name)
        path = book_path(cfg, book_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        upload.save(path)  # streamed to disk; a book can be hundreds of MB
        with path.open("rb") as f:
            looks_like_pdf = f.read(5) == b"%PDF-"
        if not looks_like_pdf:
            path.unlink(missing_ok=True)
            db.delete_book(book_id)
            raise ReadcueError("That file doesn't look like a PDF.")
        wake_books.set()
        return redirect(url_for("book_page", book_id=book_id))

    @app.get("/books/<int:book_id>")
    def book_page(book_id: int):
        book = db.get_book(book_id)
        rows = []
        if book.status == "ready":
            pages = db.get_book_page_texts(book.id, book.page_count)
            readings = {r.chapter: r for r in db.list_readings() if r.course_id == book.course_id}
            found = detect_chapters(
                pages, {n: r.title for n, r in readings.items()}, db.get_book_outline(book.id)
            )
            rows = [{"found": f, "reading": readings[f.number]} for f in found]
        pdf = book_path(cfg, book.id)
        pdf_mb = max(round(pdf.stat().st_size / 1024 / 1024, 1), 0.1) if pdf.is_file() else None
        return render_template("book.html", book=book, rows=rows, today=today_fn(), pdf_mb=pdf_mb)

    @app.get("/books/<int:book_id>/check")
    def book_check(book_id: int):
        """JSON for the "Check pages" panel: the pages around a proposed chapter range."""
        book = db.get_book(book_id)
        if book.status != "ready":
            return jsonify(error="The book is still being read."), 409
        try:
            start, end = int(request.args["start"]), int(request.args["end"])
            number = int(request.args.get("number") or 0)
        except (KeyError, ValueError):
            return jsonify(error="Enter both the first and last page."), 400
        pages = db.get_book_page_range(book.id, start - 1, end + 1)
        try:
            return jsonify(check_range(pages, book.page_count, start, end, number))
        except ValueError as e:
            return jsonify(error=str(e)), 400

    @app.post("/books/<int:book_id>/split")
    def book_split(book_id: int):
        book = db.get_book(book_id)
        pages = db.get_book_page_texts(book.id, book.page_count)
        chapters = []  # validated first, so a bad row doesn't leave the rest half-created
        for i in range(_int(request.form.get("count"), "Row count")):
            if not request.form.get(f"include-{i}"):
                continue
            number = _int(request.form.get(f"number-{i}"), "Chapter")
            start = _int(request.form.get(f"start-{i}"), f"Chapter {number} start page")
            end = _int(request.form.get(f"end-{i}"), f"Chapter {number} end page")
            if not 1 <= start <= end <= book.page_count:
                raise ReadcueError(
                    f"Chapter {number}: pages {start} to {end} don't fit a {book.page_count}-page book."
                )
            text = normalize_text("\n\n".join(pages[start - 1 : end]))
            if not text:
                raise ReadcueError(f"Chapter {number}: pages {start} to {end} have no readable text.")
            if len(text) > MAX_TEXT_CHARS:
                raise ReadcueError(
                    f"Chapter {number}: pages {start} to {end} are too long. Check the end page."
                )
            chapters.append((number, start, end, text))
        if not chapters:
            raise ReadcueError("Tick at least one chapter to create.")
        source = book_path(cfg, book.id)  # absent for a book read before PDFs were kept
        for number, start, end, text in chapters:
            reading = db.find_reading(book.course_id, number)
            chapter_id = db.save_chapter(
                book.course_id,
                number,
                reading.title if reading else "",
                text,
                f"{book.name} pp. {start}-{end}",
            )
            refresh_chapter_pdf(
                chapter_id,
                (lambda dest, s=start, e=end: slice_pdf(source, s, e, dest)) if source.is_file() else None,
            )
        wake.set()
        flash(f"Created {len(chapters)} chapter{'s' if len(chapters) != 1 else ''} from {book.name}.", "ok")
        return redirect(url_for("index"))

    @app.post("/books/<int:book_id>/retry")
    def book_retry(book_id: int):
        db.get_book(book_id)
        db.requeue_book(book_id)
        wake_books.set()
        return redirect(url_for("book_page", book_id=book_id))

    @app.post("/books/<int:book_id>/delete")
    def book_delete(book_id: int):
        db.get_book(book_id)
        book_path(cfg, book_id).unlink(missing_ok=True)
        db.delete_book(book_id)
        flash("Textbook deleted. Chapters already created from it are kept.", "ok")
        return redirect(url_for("index"))

    # -- settings --------------------------------------------------------------------------

    @app.get("/settings")
    def settings():
        return render_template(
            "settings.html",
            cfg=cfg,
            pushover_ready=notifier.configured,
            devices=notifier.selected_devices,
            ocr_ready=ocr.available(),
        )

    @app.post("/settings/test-llm")
    def test_llm():
        provider = provider_factory()
        reply = provider.complete("You are a connectivity check.", "Reply with the single word OK.")
        flash(f"{provider.label} responded: {reply.strip()[:80]}", "ok")
        return redirect(url_for("settings"))

    @app.get("/settings/devices")
    def devices_page():
        available, problem = [], ""
        if notifier.configured:
            try:
                available = notifier.list_devices()
            except NotifyError as e:
                problem = str(e)
        selected = notifier.selected_devices
        return render_template(
            "devices.html",
            configured=notifier.configured,
            available=available,
            selected=selected,
            missing=[name for name in selected if available and name not in available],
            problem=problem,
        )

    @app.post("/settings/devices")
    def devices_save():
        if request.form.get("mode") == "some":
            chosen = request.form.getlist("device")
            if not chosen:
                raise ReadcueError("Tick at least one device, or choose all devices.")
            known = notifier.list_devices()
            for name in chosen:
                if name not in known:
                    raise ReadcueError(f"Pushover has no device called {name}.")
            value = ",".join(chosen)
        else:
            value = ""  # every device
        db.set_setting(DEVICES_SETTING, value)
        where = value.replace(",", ", ") or "all your devices"
        if request.form.get("test"):
            notifier.send(
                "readcue test", f"This came to: {where}.", url=cfg.link("/"), url_title="Open readcue"
            )
            flash(f"Saved. A test notification went to {where}.", "ok")
        else:
            flash(f"Notifications will go to {where}.", "ok")
        return redirect(url_for("settings"))

    @app.post("/settings/test-push")
    def test_push():
        notifier.send(
            "readcue test", "Pushover notifications are working.", url=cfg.link("/"), url_title="Open readcue"
        )
        flash("Test notification sent.", "ok")
        return redirect(url_for("settings"))

    return app
