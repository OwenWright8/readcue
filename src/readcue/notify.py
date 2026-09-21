"""Pushover notifications."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from urllib.parse import urlencode

from . import http
from .config import Config
from .errors import NotifyError
from .models import Summary

API_URL = "https://api.pushover.net/1/messages.json"
MAX_TITLE, MAX_MESSAGE = 250, 1024

Transport = Callable[[str, bytes, dict[str, str], float], tuple[int, bytes]]


class PushoverNotifier:
    def __init__(self, cfg: Config, transport: Transport = http.post):
        self.cfg = cfg
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.cfg.pushover_app_token and self.cfg.pushover_user_key)

    def send(self, title: str, message: str, *, url: str | None = None, url_title: str | None = None) -> None:
        if not self.configured:
            raise NotifyError("Pushover isn't configured. Set PUSHOVER_APP_TOKEN and PUSHOVER_USER_KEY.")
        payload = {
            "token": self.cfg.pushover_app_token,
            "user": self.cfg.pushover_user_key,
            "title": _clip(title, MAX_TITLE),
            "message": _clip(message, MAX_MESSAGE),
        }
        if self.cfg.pushover_device:
            payload["device"] = self.cfg.pushover_device
        if self.cfg.pushover_priority:
            payload["priority"] = str(self.cfg.pushover_priority)
        if url:
            payload["url"] = url[:512]
            if url_title:
                payload["url_title"] = url_title[:100]

        try:
            status, raw = self._transport(
                API_URL,
                urlencode(payload).encode(),
                {"Content-Type": "application/x-www-form-urlencoded"},
                15,
            )
        except OSError as e:
            raise NotifyError(f"Couldn't reach Pushover: {e}") from e
        try:
            body = json.loads(raw)
        except ValueError:
            body = {}
        if status != 200 or body.get("status") != 1:
            detail = "; ".join(body.get("errors") or []) or raw[:200].decode("utf-8", errors="replace")
            raise NotifyError(f"Pushover rejected the notification (HTTP {status}): {detail}")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _when(due: date, today: date) -> str:
    days = (due - today).days
    if days < 0:
        return "was due yesterday" if days == -1 else f"was due {-days} days ago"
    return "due today" if days == 0 else "due tomorrow" if days == 1 else f"due in {days} days"


def build_summary_ready(
    *, course: str, chapter: int, title: str, due: date | None, today: date, summary: Summary
) -> tuple[str, str]:
    """(title, message) for "a summary just finished"."""
    label = f"Ch. {chapter}" + (f": {title}" if title else "")
    head = f"{label} is summarized."
    if due:
        head += f" It's {_when(due, today)} ({due:%a %b} {due.day})."
    lines = [head, "", summary.tldr()]
    if summary.definitions:
        lines += ["", "Key terms: " + ", ".join(d.term for d in summary.definitions[:8])]
    return f"Summary ready: Ch. {chapter} — {course}", "\n".join(lines)


def build_reminder(
    *,
    course: str,
    chapter: int,
    title: str,
    due: date,
    today: date,
    summary: Summary | None,
    has_chapter: bool,
) -> tuple[str, str]:
    """(title, message) for a reading reminder."""
    days = (due - today).days
    when = "today" if days <= 0 else "tomorrow" if days == 1 else f"in {days} days"
    label = f"Ch. {chapter}" + (f": {title}" if title else "")
    lines = [f"{label} is due {when} ({due:%a %b} {due.day})."]
    if summary:
        lines += ["Summary is ready.", "", summary.tldr()]
        if summary.definitions:
            lines += ["", "Key terms: " + ", ".join(d.term for d in summary.definitions[:8])]
    elif not has_chapter:
        lines.append("No summary yet: the chapter hasn't been added to readcue.")
    else:
        lines.append("No summary yet: it's queued or failed. Open readcue to check.")
    return f"Read Ch. {chapter} (due {when}) — {course}", "\n".join(lines)
