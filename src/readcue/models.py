from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

SUMMARY_MODES = ("scheduled", "now")


@dataclass(frozen=True)
class Course:
    id: int
    name: str
    # "scheduled": summarize once a chapter is due within the summary window.
    # "now": summarize each chapter as soon as it's added.
    summary_mode: str = "scheduled"
    notify_on_summary: bool = True  # Pushover ping when a summary is complete


@dataclass(frozen=True)
class Definition:
    term: str
    definition: str


@dataclass
class Summary:
    overview: str
    key_points: list[str] = field(default_factory=list)
    definitions: list[Definition] = field(default_factory=list)
    model: str = ""
    created_at: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> Summary:
        """Tolerant of the shapes small local models tend to produce; raises ValueError if unusable."""
        overview = data.get("overview") or ""
        if isinstance(overview, list):
            overview = "\n\n".join(str(p) for p in overview)
        key_points = data.get("key_points") or []
        if isinstance(key_points, str):
            key_points = [key_points]
        definitions = data.get("definitions") or []
        if isinstance(definitions, dict):
            definitions = [{"term": k, "definition": v} for k, v in definitions.items()]
        if not isinstance(key_points, list) or not isinstance(definitions, list):
            raise ValueError("key_points and definitions must be lists")

        parsed: list[Definition] = []
        for item in definitions:
            if isinstance(item, dict):
                term = str(item.get("term") or "").strip()
                meaning = str(item.get("definition") or "").strip()
                if term and meaning:
                    parsed.append(Definition(term, meaning))
        return cls(
            overview=str(overview).strip(),
            key_points=[s for p in key_points if (s := str(p).strip())],
            definitions=parsed,
        )

    def to_dict(self) -> dict:
        return {
            "overview": self.overview,
            "key_points": self.key_points,
            "definitions": [{"term": d.term, "definition": d.definition} for d in self.definitions],
        }

    @property
    def paragraphs(self) -> list[str]:
        return [p.strip() for p in self.overview.split("\n\n") if p.strip()]

    def tldr(self, limit: int = 380) -> str:
        first = self.overview.split("\n\n", 1)[0].strip()
        return first if len(first) <= limit else first[: limit - 1].rstrip() + "…"

    def to_markdown(self, *, course: str, chapter: int, title: str) -> str:
        heading = f"{course} — Chapter {chapter}" + (f": {title}" if title else "")
        lines = [f"# {heading}", "", "## Summary", "", self.overview, "", "## Key points", ""]
        lines += [f"- {p}" for p in self.key_points]
        lines += ["", "## Definitions", ""]
        lines += [f"- **{d.term}**: {d.definition}" for d in self.definitions]
        return "\n".join(lines) + "\n"


@dataclass
class Chapter:
    id: int
    course_id: int
    course_name: str
    number: int
    title: str
    summary_status: str  # pending | running | done | error
    summary_error: str = ""
    summarize_now: bool = False  # user asked for it; skips the wait for the due-date window
    summary_mode: str = "scheduled"  # the course's setting
    text: str = ""  # only loaded when explicitly requested


@dataclass
class Reading:
    id: int
    course_id: int
    course_name: str
    chapter: int
    title: str
    due: date
    chapter_id: int | None = None
    summary_status: str | None = None
    summarize_now: bool = False
    summary_mode: str = "scheduled"
    notify_on_summary: bool = True
    notifications: frozenset[str] = frozenset()

    @property
    def summary_ready(self) -> bool:
        return self.summary_status == "done"

    def summarize_on(self, lead_days: int) -> date:
        """First day the summary is generated automatically."""
        return self.due - timedelta(days=lead_days)

    def waiting(self, today: date, lead_days: int) -> bool:
        """Chapter is uploaded but its summary is held back until the due date is close."""
        return (
            self.summary_status == "pending"
            and not self.summarize_now
            and self.summary_mode != "now"
            and self.summarize_on(lead_days) > today
        )

    def stage(self, today: date, lead_days: int) -> str:
        """Where the chapter is in its life: missing, waiting, queued, running, done or error."""
        if self.chapter_id is None:
            return "missing"
        if self.summary_status == "pending":
            return "waiting" if self.waiting(today, lead_days) else "queued"
        return self.summary_status or "missing"

    @property
    def reminded(self) -> bool:
        return "reminder" in self.notifications
