"""SQLite storage. Every call opens its own short-lived connection so the web server, summary
worker and scheduler threads can share one Database safely."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

from .errors import NotFoundError, ReadcueError
from .models import SUMMARY_MODES, Book, Chapter, Course, Reading, Summary

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    summary_mode TEXT NOT NULL DEFAULT 'scheduled',
    notify_on_summary INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    chapter INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    due_date TEXT NOT NULL,
    UNIQUE (course_id, chapter)
);
CREATE TABLE IF NOT EXISTS chapters (
    id INTEGER PRIMARY KEY,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    added_at TEXT NOT NULL,
    summary_status TEXT NOT NULL DEFAULT 'pending',
    summary_error TEXT NOT NULL DEFAULT '',
    summarize_now INTEGER NOT NULL DEFAULT 0,
    UNIQUE (course_id, number)
);
CREATE TABLE IF NOT EXISTS summaries (
    chapter_id INTEGER PRIMARY KEY REFERENCES chapters(id) ON DELETE CASCADE,
    body TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    page_count INTEGER NOT NULL DEFAULT 0,
    pages_done INTEGER NOT NULL DEFAULT 0,
    scanned_pages INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    outline TEXT NOT NULL DEFAULT '[]',
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS book_pages (
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    page INTEGER NOT NULL,
    text TEXT NOT NULL,
    PRIMARY KEY (book_id, page)
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    reading_id INTEGER NOT NULL REFERENCES readings(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    sent_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            # Columns added after the first release, for databases that predate them.
            for table, column, ddl in [
                ("chapters", "summarize_now", "INTEGER NOT NULL DEFAULT 0"),
                ("courses", "summary_mode", "TEXT NOT NULL DEFAULT 'scheduled'"),
                ("courses", "notify_on_summary", "INTEGER NOT NULL DEFAULT 1"),
            ]:
                existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ping(self) -> None:
        """Raises if the database can't be queried (used by the health check)."""
        with self._conn() as conn:
            conn.execute("SELECT 1").fetchone()

    def backup(self, dest: Path) -> None:
        """Write a consistent copy of the database to `dest`, safe to run while the app is in use."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(dest)
        try:
            with self._conn() as source:
                source.backup(target)
        finally:
            target.close()

    # -- settings chosen in the UI (they override the environment) -------------------------

    def get_setting(self, key: str) -> str | None:
        """None if it was never set; an empty string is a real value (e.g. "all devices")."""
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))

    # -- courses ---------------------------------------------------------------------------

    def add_course(self, name: str) -> Course:
        name = " ".join(name.split())
        if not name:
            raise ReadcueError("Course name can't be empty.")
        with self._conn() as conn:
            conn.execute("INSERT OR IGNORE INTO courses (name) VALUES (?)", (name,))
            row = conn.execute(f"{self._COURSE_SELECT} WHERE name = ?", (name,)).fetchone()
        return self._course(row)

    _COURSE_SELECT = "SELECT id, name, summary_mode, notify_on_summary FROM courses"

    @staticmethod
    def _course(row: sqlite3.Row) -> Course:
        return Course(row["id"], row["name"], row["summary_mode"], bool(row["notify_on_summary"]))

    def get_course(self, course_id: int) -> Course:
        with self._conn() as conn:
            row = conn.execute(f"{self._COURSE_SELECT} WHERE id = ?", (course_id,)).fetchone()
        if row is None:
            raise NotFoundError("Course not found.")
        return self._course(row)

    def list_courses(self) -> list[Course]:
        with self._conn() as conn:
            rows = conn.execute(f"{self._COURSE_SELECT} ORDER BY name").fetchall()
        return [self._course(r) for r in rows]

    def update_course_settings(self, course_id: int, summary_mode: str, notify_on_summary: bool) -> None:
        if summary_mode not in SUMMARY_MODES:
            raise ReadcueError(f"Summary timing must be one of {', '.join(SUMMARY_MODES)}.")
        with self._conn() as conn:
            conn.execute(
                "UPDATE courses SET summary_mode = ?, notify_on_summary = ? WHERE id = ?",
                (summary_mode, int(notify_on_summary), course_id),
            )

    def delete_course(self, course_id: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM courses WHERE id = ?", (course_id,))

    # -- readings (the schedule) -----------------------------------------------------------

    def upsert_reading(self, course_id: int, chapter: int, due: date, title: str = "") -> str:
        """Returns 'added', 'updated' or 'unchanged'. Moving a due date re-arms its reminder."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, title, due_date FROM readings WHERE course_id = ? AND chapter = ?",
                (course_id, chapter),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO readings (course_id, chapter, title, due_date) VALUES (?, ?, ?, ?)",
                    (course_id, chapter, title, due.isoformat()),
                )
                return "added"
            new_title = title or row["title"]
            if row["due_date"] == due.isoformat() and row["title"] == new_title:
                return "unchanged"
            conn.execute(
                "UPDATE readings SET title = ?, due_date = ? WHERE id = ?",
                (new_title, due.isoformat(), row["id"]),
            )
            if row["due_date"] != due.isoformat():
                conn.execute("DELETE FROM notifications WHERE reading_id = ?", (row["id"],))
            return "updated"

    def update_reading(self, reading_id: int, due: date, title: str) -> None:
        reading = self.get_reading(reading_id)
        self.upsert_reading(reading.course_id, reading.chapter, due, title)

    def delete_reading(self, reading_id: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM readings WHERE id = ?", (reading_id,))

    _READING_SELECT = """
        SELECT r.id, r.course_id, c.name AS course_name, r.chapter, r.title, r.due_date,
               ch.id AS chapter_id, ch.summary_status, ch.summarize_now,
               c.summary_mode, c.notify_on_summary,
               (SELECT group_concat(kind) FROM notifications n WHERE n.reading_id = r.id) AS kinds
        FROM readings r
        JOIN courses c ON c.id = r.course_id
        LEFT JOIN chapters ch ON ch.course_id = r.course_id AND ch.number = r.chapter
    """

    @staticmethod
    def _reading(row: sqlite3.Row) -> Reading:
        return Reading(
            id=row["id"],
            course_id=row["course_id"],
            course_name=row["course_name"],
            chapter=row["chapter"],
            title=row["title"],
            due=date.fromisoformat(row["due_date"]),
            chapter_id=row["chapter_id"],
            summary_status=row["summary_status"],
            summarize_now=bool(row["summarize_now"]),
            summary_mode=row["summary_mode"],
            notify_on_summary=bool(row["notify_on_summary"]),
            notifications=frozenset((row["kinds"] or "").split(",")) - {""},
        )

    def get_reading(self, reading_id: int) -> Reading:
        with self._conn() as conn:
            row = conn.execute(f"{self._READING_SELECT} WHERE r.id = ?", (reading_id,)).fetchone()
        if row is None:
            raise NotFoundError("Reading not found.")
        return self._reading(row)

    def find_reading(self, course_id: int, chapter: int) -> Reading | None:
        with self._conn() as conn:
            row = conn.execute(
                f"{self._READING_SELECT} WHERE r.course_id = ? AND r.chapter = ?", (course_id, chapter)
            ).fetchone()
        return self._reading(row) if row else None

    def list_readings(self) -> list[Reading]:
        with self._conn() as conn:
            rows = conn.execute(f"{self._READING_SELECT} ORDER BY r.due_date, c.name, r.chapter").fetchall()
        return [self._reading(r) for r in rows]

    # -- chapters and summaries ------------------------------------------------------------

    def save_chapter(self, course_id: int, number: int, title: str, text: str, source: str) -> int:
        """Insert or replace a chapter's text and queue it for (re)summarizing."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id FROM chapters WHERE course_id = ? AND number = ?", (course_id, number)
            ).fetchone()
            if row is None:
                cur = conn.execute(
                    "INSERT INTO chapters (course_id, number, title, source, text, added_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (course_id, number, title, source, text, _now()),
                )
                return int(cur.lastrowid)
            conn.execute(
                "UPDATE chapters SET title = ?, source = ?, text = ?, added_at = ?,"
                " summary_status = 'pending', summary_error = '', summarize_now = 0 WHERE id = ?",
                (title, source, text, _now(), row["id"]),
            )
            conn.execute("DELETE FROM summaries WHERE chapter_id = ?", (row["id"],))
            return int(row["id"])

    @staticmethod
    def _chapter(row: sqlite3.Row, with_text: bool) -> Chapter:
        return Chapter(
            id=row["id"],
            course_id=row["course_id"],
            course_name=row["course_name"],
            number=row["number"],
            title=row["title"],
            summary_status=row["summary_status"],
            summary_error=row["summary_error"],
            summarize_now=bool(row["summarize_now"]),
            summary_mode=row["summary_mode"],
            text=row["text"] if with_text else "",
        )

    _CHAPTER_SELECT = (
        "SELECT ch.*, c.name AS course_name, c.summary_mode AS summary_mode"
        " FROM chapters ch JOIN courses c ON c.id = ch.course_id"
    )

    def get_chapter(self, chapter_id: int, *, with_text: bool = False) -> Chapter:
        with self._conn() as conn:
            row = conn.execute(f"{self._CHAPTER_SELECT} WHERE ch.id = ?", (chapter_id,)).fetchone()
        if row is None:
            raise NotFoundError("Chapter not found.")
        return self._chapter(row, with_text)

    def delete_chapter(self, chapter_id: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM chapters WHERE id = ?", (chapter_id,))

    def requeue_chapter(self, chapter_id: int) -> None:
        """User-requested (re)summarize: queue it now, regardless of the due-date window."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE chapters SET summary_status = 'pending', summary_error = '', summarize_now = 1"
                " WHERE id = ?",
                (chapter_id,),
            )

    def reset_running(self) -> None:
        """Called at startup: a job left 'running' by a crash or restart goes back in the queue."""
        with self._conn() as conn:
            conn.execute("UPDATE chapters SET summary_status = 'pending' WHERE summary_status = 'running'")

    def claim_pending_chapter(self, due_by: date) -> Chapter | None:
        """Take the oldest queued chapter that is eligible: due on or before `due_by`, requested by the
        user, or in a course set to summarize as soon as chapters are added."""
        with self._conn() as conn:
            row = conn.execute(
                "UPDATE chapters SET summary_status = 'running' WHERE id = ("
                " SELECT ch.id FROM chapters ch JOIN courses c ON c.id = ch.course_id"
                " WHERE ch.summary_status = 'pending'"
                "  AND (ch.summarize_now = 1 OR c.summary_mode = 'now'"
                "       OR EXISTS (SELECT 1 FROM readings r WHERE r.course_id = ch.course_id"
                "                  AND r.chapter = ch.number AND r.due_date <= ?))"
                " ORDER BY ch.id LIMIT 1) RETURNING id",
                (due_by.isoformat(),),
            ).fetchone()
        return self.get_chapter(row["id"], with_text=True) if row else None

    def save_summary(self, chapter_id: int, summary: Summary, model: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO summaries (chapter_id, body, model, created_at) VALUES (?, ?, ?, ?)",
                (chapter_id, json.dumps(summary.to_dict()), model, _now()),
            )
            conn.execute(
                "UPDATE chapters SET summary_status = 'done', summary_error = '' WHERE id = ?", (chapter_id,)
            )

    def mark_summary_error(self, chapter_id: int, message: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE chapters SET summary_status = 'error', summary_error = ? WHERE id = ?",
                (message, chapter_id),
            )

    def get_summary(self, chapter_id: int) -> Summary | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM summaries WHERE chapter_id = ?", (chapter_id,)).fetchone()
        if row is None:
            return None
        summary = Summary.from_dict(json.loads(row["body"]))
        summary.model, summary.created_at = row["model"], row["created_at"]
        return summary

    # -- textbooks -------------------------------------------------------------------------

    _BOOK_SELECT = (
        "SELECT b.id, b.course_id, c.name AS course_name, b.name, b.status, b.page_count, b.pages_done,"
        " b.scanned_pages, b.note, b.error FROM books b JOIN courses c ON c.id = b.course_id"
    )

    @staticmethod
    def _book(row: sqlite3.Row) -> Book:
        return Book(
            id=row["id"],
            course_id=row["course_id"],
            course_name=row["course_name"],
            name=row["name"],
            status=row["status"],
            page_count=row["page_count"],
            pages_done=row["pages_done"],
            scanned_pages=row["scanned_pages"],
            note=row["note"],
            error=row["error"],
        )

    def add_book(self, course_id: int, name: str) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO books (course_id, name, added_at) VALUES (?, ?, ?)", (course_id, name, _now())
            )
            return int(cur.lastrowid)

    def get_book(self, book_id: int) -> Book:
        with self._conn() as conn:
            row = conn.execute(f"{self._BOOK_SELECT} WHERE b.id = ?", (book_id,)).fetchone()
        if row is None:
            raise NotFoundError("Textbook not found.")
        return self._book(row)

    def list_books(self) -> list[Book]:
        with self._conn() as conn:
            rows = conn.execute(f"{self._BOOK_SELECT} ORDER BY b.id").fetchall()
        return [self._book(r) for r in rows]

    def claim_queued_book(self) -> Book | None:
        with self._conn() as conn:
            row = conn.execute(
                "UPDATE books SET status = 'reading', error = '' WHERE id ="
                " (SELECT id FROM books WHERE status = 'queued' ORDER BY id LIMIT 1) RETURNING id"
            ).fetchone()
        return self.get_book(row["id"]) if row else None

    def reset_reading_books(self) -> None:
        """At startup: a book being read when the app stopped goes back in the queue and resumes."""
        with self._conn() as conn:
            conn.execute("UPDATE books SET status = 'queued' WHERE status = 'reading'")

    def set_book_info(self, book_id: int, *, page_count: int, outline: list[tuple[str, int]]) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE books SET page_count = ?, outline = ? WHERE id = ?",
                (page_count, json.dumps(outline), book_id),
            )

    def stored_book_pages(self, book_id: int) -> set[int]:
        with self._conn() as conn:
            rows = conn.execute("SELECT page FROM book_pages WHERE book_id = ?", (book_id,)).fetchall()
        return {r["page"] for r in rows}

    def save_book_pages(self, book_id: int, texts: dict[int, str], *, scanned: int = 0) -> None:
        with self._conn() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO book_pages (book_id, page, text) VALUES (?, ?, ?)",
                [(book_id, page, text) for page, text in texts.items()],
            )
            conn.execute(
                "UPDATE books SET scanned_pages = scanned_pages + ?,"
                " pages_done = (SELECT COUNT(*) FROM book_pages WHERE book_id = ?) WHERE id = ?",
                (scanned, book_id, book_id),
            )

    def finish_book(self, book_id: int, note: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE books SET status = 'ready', note = ?, error = '' WHERE id = ?", (note, book_id)
            )

    def fail_book(self, book_id: int, message: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE books SET status = 'error', error = ? WHERE id = ?", (message, book_id))

    def requeue_book(self, book_id: int) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE books SET status = 'queued', error = '' WHERE id = ?", (book_id,))

    def delete_book(self, book_id: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM books WHERE id = ?", (book_id,))

    def get_book_outline(self, book_id: int) -> list[tuple[str, int]]:
        with self._conn() as conn:
            row = conn.execute("SELECT outline FROM books WHERE id = ?", (book_id,)).fetchone()
        return [(title, page) for title, page in json.loads(row["outline"])] if row else []

    def get_book_page_range(self, book_id: int, first: int, last: int) -> dict[int, str]:
        """Text of pages first..last (1-based, inclusive) that have been read, keyed by page number."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT page, text FROM book_pages WHERE book_id = ? AND page BETWEEN ? AND ?",
                (book_id, first, last),
            ).fetchall()
        return {row["page"]: row["text"] for row in rows}

    def get_book_page_texts(self, book_id: int, page_count: int) -> list[str]:
        """Text of every page in order (pages[0] is page 1); a page that wasn't read is an empty string."""
        pages = [""] * page_count
        with self._conn() as conn:
            for row in conn.execute("SELECT page, text FROM book_pages WHERE book_id = ?", (book_id,)):
                if 1 <= row["page"] <= page_count:
                    pages[row["page"] - 1] = row["text"]
        return pages

    # -- notifications ---------------------------------------------------------------------

    def record_notification(self, reading_id: int, kind: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO notifications (reading_id, kind, sent_at) VALUES (?, ?, ?)",
                (reading_id, kind, _now()),
            )
