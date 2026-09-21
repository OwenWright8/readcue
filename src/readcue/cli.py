"""Command line entry point: `readcue serve` (what the container runs), `check`, and `backup`."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from datetime import date, datetime
from pathlib import Path

from . import __version__
from .config import LOG_LEVELS, Config, check_exposure, load_dotenv
from .db import Database
from .errors import ReadcueError
from .llm import make_provider
from .notify import PushoverNotifier
from .scheduler import notify_summary_complete, run_check, run_scheduler
from .worker import run_worker

log = logging.getLogger("readcue")

BACKUP_PATTERN = "readcue-*.db"


def cmd_serve(args: argparse.Namespace) -> int:
    import waitress

    from .web import create_app

    cfg = Config.from_env()
    check_exposure(cfg, args.host)
    db = Database(cfg.db_path)
    notifier = PushoverNotifier(cfg)
    stop, wake = threading.Event(), threading.Event()

    def on_complete(chapter, summary):
        notify_summary_complete(db, cfg, notifier, chapter, summary, today=date.today())

    threads = [
        threading.Thread(
            target=run_worker,
            args=(db, lambda: make_provider(cfg), stop, wake, cfg.summarize_days_before, on_complete),
            daemon=True,
            name="worker",
        ),
        threading.Thread(target=run_scheduler, args=(db, cfg, notifier, stop), daemon=True, name="scheduler"),
    ]
    for thread in threads:
        thread.start()

    if not notifier.configured:
        log.warning(
            "Pushover isn't configured (PUSHOVER_APP_TOKEN / PUSHOVER_USER_KEY); reminders won't be sent"
        )
    if cfg.notify_days_before > cfg.summarize_days_before:
        log.warning(
            "Reminders go out %d days ahead but summaries are only generated %d days ahead, so early "
            "reminders will say the summary isn't ready (a follow-up is sent once it is)",
            cfg.notify_days_before,
            cfg.summarize_days_before,
        )
    if not cfg.password:
        log.warning("No READCUE_PASSWORD set: anyone who can reach this app can use it")
    log.info(
        "readcue %s listening on http://%s:%s (LLM: %s:%s)",
        __version__,
        args.host,
        args.port,
        cfg.provider,
        cfg.model_name,
    )
    app = create_app(cfg, db, wake=wake, notifier=notifier, threads=threads)
    try:
        waitress.serve(app, host=args.host, port=args.port, threads=8, ident="readcue")
    finally:
        stop.set()
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """One-off reminder check, handy for testing: `readcue check --dry-run --today 2026-10-17`."""
    cfg = Config.from_env()
    db = Database(cfg.db_path)
    today = date.fromisoformat(args.today) if args.today else date.today()
    results = run_check(
        db,
        cfg,
        PushoverNotifier(cfg),
        today=today,
        now=datetime.now().time(),
        dry_run=args.dry_run,
        force=True,
    )
    for result in results:
        print(result.describe())
    if not results:
        print("Nothing to send.")
    return 1 if any(r.outcome == "error" for r in results) else 0


def cmd_backup(args: argparse.Namespace) -> int:
    """Copy the database while the app is running: `readcue backup` or `readcue backup --to file.db`."""
    cfg = Config.from_env()
    db = Database(cfg.db_path)
    default_dir = cfg.data_dir / "backups"
    dest = Path(args.to) if args.to else default_dir / f"readcue-{datetime.now():%Y%m%d-%H%M%S}.db"
    db.backup(dest)
    print(dest)
    if not args.to and args.keep > 0:  # only prune the automatic backups, never a file the user named
        for old in sorted(default_dir.glob(BACKUP_PATTERN))[: -args.keep]:
            old.unlink()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="readcue", description=__doc__)
    parser.add_argument("--version", action="version", version=f"readcue {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the web UI, summary worker and reminder scheduler")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.set_defaults(func=cmd_serve)

    check = sub.add_parser("check", help="run the reminder check once")
    check.add_argument("--dry-run", action="store_true", help="show what would be sent without sending")
    check.add_argument("--today", metavar="YYYY-MM-DD", help="pretend it's this date")
    check.set_defaults(func=cmd_check)

    backup = sub.add_parser("backup", help="write a consistent copy of the database")
    backup.add_argument("--to", metavar="FILE", help="where to write it (default: <data dir>/backups/)")
    backup.add_argument(
        "--keep", type=int, default=14, help="automatic backups to keep (default 14; 0 = all)"
    )
    backup.set_defaults(func=cmd_backup)
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv(Path(".env"))
    level = os.environ.get("READCUE_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level if level in LOG_LEVELS else "INFO",
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ReadcueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
