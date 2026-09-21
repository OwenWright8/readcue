"""Flask web UI: manage courses, import syllabi, upload chapters, read summaries."""

from __future__ import annotations

import hmac
import os
import re
import threading
from collections.abc import Callable, Sequence
from datetime import date, timedelta
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
    url_for,
)

from . import __version__, ocr
from .config import Config
from .db import Database
from .errors import NotFoundError, ReadcueError
from .extract import extract_text, normalize_text
from .llm import make_provider
from .llm.base import LLMProvider
from .notify import PushoverNotifier
from .syllabus import extract_schedule

MAX_UPLOAD_BYTES = 100 * 1024 * 1024
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
    provider_factory: Callable[[], LLMProvider] | None = None,
    notifier: PushoverNotifier | None = None,
    today_fn: Callable[[], date] = date.today,
    threads: Sequence[threading.Thread] = (),
) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
    app.config["MAX_FORM_MEMORY_SIZE"] = MAX_PASTED_BYTES
    app.secret_key = os.urandom(32)  # only signs flash messages; a restart just drops pending ones
    wake = wake or threading.Event()
    provider_factory = provider_factory or (lambda: make_provider(cfg))
    notifier = notifier or PushoverNotifier(cfg)

    app.jinja_env.globals["app_version"] = __version__
    # A reverse proxy may rewrite Host, so the public address from READCUE_BASE_URL also counts as same-origin.
    trusted_hosts = {urlsplit(cfg.base_url).netloc} - {""}

    app.jinja_env.filters["nice_date"] = lambda d: f"{d:%a %b} {d.day}"
    app.jinja_env.filters["relative_day"] = relative_day
    app.jinja_env.filters["month_abbr"] = lambda d: f"{d:%b}"
    app.jinja_env.filters["short_date"] = lambda d: f"{d:%b} {d.day}"

    def uploaded_text(pages: str | None = None) -> tuple[str, str]:
        """Text from the uploaded file(s) or the paste box, and a short description of the source.

        Several files (say, one photo per page) are read in filename order and joined.
        """
        files = sorted(
            (f for f in request.files.getlist("file") if f.filename), key=lambda f: _natural_key(f.filename)
        )
        pasted = request.form.get("text", "").strip()
        if files:
            if pages and len(files) > 1:
                raise ReadcueError("A page range only works with a single PDF.")
            text = "\n\n".join(
                extract_text(f.read(), f.filename, pages, ocr_lang=cfg.ocr_lang) for f in files
            )
            extra = f" (+{len(files) - 1} more)" if len(files) > 1 else ""
            return text, files[0].filename + extra
        if pasted:
            return normalize_text(pasted), "pasted text"
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
        flash(f"That file is too large (limit {MAX_UPLOAD_BYTES // 1024 // 1024} MB).", "error")
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
        today, sum_days = today_fn(), cfg.summarize_days_before
        groups = []
        for course in db.list_courses():
            mine = [r for r in readings if r.course_id == course.id]
            groups.append({"course": course, "readings": mine, "ready": sum(r.summary_ready for r in mine)})
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
            busy=any(s in ("queued", "running") for s in stages),
            today=today,
            lead=timedelta(days=cfg.notify_days_before),
            sum_days=sum_days,
        )

    # -- courses and schedule --------------------------------------------------------------

    @app.post("/courses")
    def course_add():
        course = db.add_course(request.form.get("name", ""))
        return redirect(url_for("syllabus_form", course_id=course.id))

    @app.post("/courses/<int:course_id>/delete")
    def course_delete(course_id: int):
        db.delete_course(course_id)
        flash("Course deleted.", "ok")
        return redirect(url_for("index"))

    @app.get("/courses/<int:course_id>/syllabus")
    def syllabus_form(course_id: int):
        return render_template("syllabus_form.html", course=db.get_course(course_id), model=cfg.model_name)

    @app.post("/courses/<int:course_id>/syllabus")
    def syllabus_preview(course_id: int):
        course = db.get_course(course_id)
        text, _ = uploaded_text()
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
            course.id, request.form.get("summary_mode", ""), bool(request.form.get("notify_on_summary"))
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
        text, source = uploaded_text(request.form.get("pages", "").strip() or None)

        reading = db.find_reading(course.id, number)
        title = request.form.get("title", "").strip() or (reading.title if reading else "")
        db.save_chapter(course.id, number, title, text, source)
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
        )

    @app.get("/summary/<int:chapter_id>.md")
    def summary_markdown(chapter_id: int):
        chapter = db.get_chapter(chapter_id)
        summary = db.get_summary(chapter_id)
        if summary is None:
            abort(404)
        body = summary.to_markdown(course=chapter.course_name, chapter=chapter.number, title=chapter.title)
        return Response(body, mimetype="text/markdown")

    @app.post("/chapters/<int:chapter_id>/resummarize")
    def chapter_resummarize(chapter_id: int):
        db.get_chapter(chapter_id)
        db.requeue_chapter(chapter_id)
        wake.set()
        return redirect(request.referrer or url_for("index"))

    @app.post("/chapters/<int:chapter_id>/delete")
    def chapter_delete(chapter_id: int):
        db.delete_chapter(chapter_id)
        flash("Chapter and its summary deleted.", "ok")
        return redirect(url_for("index"))

    # -- settings --------------------------------------------------------------------------

    @app.get("/settings")
    def settings():
        return render_template(
            "settings.html", cfg=cfg, pushover_ready=notifier.configured, ocr_ready=ocr.available()
        )

    @app.post("/settings/test-llm")
    def test_llm():
        provider = provider_factory()
        reply = provider.complete("You are a connectivity check.", "Reply with the single word OK.")
        flash(f"{provider.label} responded: {reply.strip()[:80]}", "ok")
        return redirect(url_for("settings"))

    @app.post("/settings/test-push")
    def test_push():
        notifier.send(
            "readcue test", "Pushover notifications are working.", url=cfg.link("/"), url_title="Open readcue"
        )
        flash("Test notification sent.", "ok")
        return redirect(url_for("settings"))

    return app
