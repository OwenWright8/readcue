"""Production hardening: headers, health check, exposure guard, backups, input limits."""

import io
import re
import threading
import time
import zipfile
from pathlib import Path

import pytest
from conftest import FakeProvider, summary_json

from readcue import __version__, extract, ocr
from readcue.cli import main
from readcue.config import Config, check_exposure, is_loopback
from readcue.db import Database
from readcue.errors import ConfigError, ReadcueError
from readcue.web import create_app


@pytest.fixture
def client(cfg, db, notifier):
    provider = FakeProvider(lambda user: summary_json())
    return create_app(cfg, db, provider_factory=lambda: provider, notifier=notifier).test_client()


# ---- HTTP hardening --------------------------------------------------------------------


def test_pages_carry_security_headers_and_are_not_cached(client):
    resp = client.get("/")
    csp = resp.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Referrer-Policy"] == "same-origin"
    assert resp.headers["Cache-Control"] == "no-store"


def test_static_assets_may_be_cached(client):
    resp = client.get("/static/style.css")
    assert resp.status_code == 200 and resp.headers.get("Cache-Control") != "no-store"
    assert "Content-Security-Policy" in resp.headers


def test_templates_use_no_inline_scripts_or_handlers():
    """The CSP forbids inline script, so a stray onclick= would silently stop working."""
    for path in Path("src/readcue/templates").glob("*.html"):
        html = path.read_text()
        assert not re.search(r"\son[a-z]+\s*=", html), f"inline event handler in {path.name}"
        assert not re.search(r"<script(?![^>]*\ssrc=)", html), f"inline <script> in {path.name}"


def test_healthz_reports_version_and_ok(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json == {"status": "ok", "version": __version__, "problems": []}


def test_healthz_fails_when_the_database_is_unreachable(cfg, db, notifier, monkeypatch):
    client = create_app(cfg, db, notifier=notifier).test_client()

    def broken():
        raise OSError("disk I/O error")

    monkeypatch.setattr(db, "ping", broken)
    resp = client.get("/healthz")
    assert resp.status_code == 503 and "disk I/O error" in resp.json["problems"][0]


def test_healthz_fails_when_a_background_thread_has_died(cfg, db, notifier):
    dead = threading.Thread(target=lambda: None, name="worker")
    dead.start()
    dead.join()
    client = create_app(cfg, db, notifier=notifier, threads=[dead]).test_client()
    resp = client.get("/healthz")
    assert resp.status_code == 503 and resp.json["problems"] == ["worker thread stopped"]


def test_reverse_proxy_public_address_counts_as_same_origin(cfg, db, notifier):
    cfg.base_url = "https://readcue.example.com"
    client = create_app(cfg, db, notifier=notifier).test_client()
    # nginx's default rewrites Host to the upstream, so Origin no longer matches request.host
    ok = client.post("/courses", data={"name": "A"}, headers={"Origin": "https://readcue.example.com"})
    assert ok.status_code == 302
    bad = client.post("/courses", data={"name": "B"}, headers={"Origin": "https://evil.example.com"})
    assert bad.status_code == 403
    assert [c.name for c in db.list_courses()] == ["A"]


def test_a_long_pasted_chapter_is_accepted(client, db):
    course = db.add_course("BIO 101")
    text = "Cells are the unit of life. " * 40_000  # ~1.1 MB, past Werkzeug's 500 kB default
    resp = client.post("/chapters", data={"course_id": course.id, "number": "4", "text": text})
    assert resp.status_code == 302
    assert len(db.get_chapter(1, with_text=True).text) > 1_000_000


# ---- refusing to run open to the network -----------------------------------------------


def test_loopback_detection():
    assert all(is_loopback(a) for a in ("127.0.0.1", "127.5.5.5", "::1", "localhost"))
    assert not any(is_loopback(a) for a in ("0.0.0.0", "192.168.1.5", "example.com", ""))


@pytest.mark.parametrize(
    ("bind", "listen", "password", "insecure", "allowed"),
    [
        ("", "127.0.0.1", "", False, True),  # local dev
        ("", "0.0.0.0", "", False, False),  # dev server opened to the LAN, no login
        ("", "0.0.0.0", "pw", False, True),
        ("127.0.0.1", "0.0.0.0", "", False, True),  # Docker: published on localhost only
        ("0.0.0.0", "0.0.0.0", "", False, False),  # Docker: published everywhere, no login
        ("0.0.0.0", "0.0.0.0", "pw", False, True),
        ("0.0.0.0", "0.0.0.0", "", True, True),  # explicit opt-out
    ],
)
def test_exposure_guard(bind, listen, password, insecure, allowed):
    cfg = Config(bind=bind, password=password, insecure_no_auth=insecure)
    if allowed:
        check_exposure(cfg, listen)
    else:
        with pytest.raises(ConfigError, match="no login"):
            check_exposure(cfg, listen)


def test_serve_refuses_to_start_when_exposed_without_a_password(monkeypatch, tmp_path, capsys):
    for var in ("READCUE_PASSWORD", "READCUE_BIND", "READCUE_INSECURE_NO_AUTH"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("READCUE_DATA_DIR", str(tmp_path))
    assert main(["serve", "--host", "0.0.0.0"]) == 1
    assert "no login" in capsys.readouterr().err


def test_secrets_never_appear_in_repr():
    cfg = Config(
        anthropic_api_key="sk-ant-SECRET",
        password="hunter2",
        pushover_app_token="TOKEN",
        pushover_user_key="USER",
        ollama_api_key="OLLAMA-SECRET",
    )
    shown = repr(cfg)
    for secret in ("sk-ant-SECRET", "hunter2", "TOKEN", "USER", "OLLAMA-SECRET"):
        assert secret not in shown


def test_config_reads_bind_log_level_and_the_insecure_flag():
    cfg = Config.from_env(
        {"READCUE_BIND": "0.0.0.0", "READCUE_LOG_LEVEL": "debug", "READCUE_INSECURE_NO_AUTH": "Yes"}
    )
    assert (cfg.bind, cfg.log_level, cfg.insecure_no_auth) == ("0.0.0.0", "DEBUG", True)
    assert Config.from_env({}).insecure_no_auth is False
    with pytest.raises(ConfigError, match="READCUE_LOG_LEVEL"):
        Config.from_env({"READCUE_LOG_LEVEL": "chatty"})


# ---- backups and the CLI ---------------------------------------------------------------


def test_backup_is_a_usable_copy_of_the_live_database(db, tmp_path):
    db.add_course("BIO 101")
    dest = tmp_path / "nested" / "copy.db"
    db.backup(dest)
    assert [c.name for c in Database(dest).list_courses()] == ["BIO 101"]


def test_backup_command_writes_and_prunes_automatic_backups(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("READCUE_DATA_DIR", str(tmp_path))
    Database(tmp_path / "readcue.db").add_course("BIO 101")
    backups = tmp_path / "backups"
    backups.mkdir()
    for stamp in ("20200101-000000", "20200102-000000", "20200103-000000"):
        (backups / f"readcue-{stamp}.db").write_bytes(b"old")

    assert main(["backup", "--keep", "2"]) == 0
    written = Path(capsys.readouterr().out.strip())
    assert written.parent == backups and [c.name for c in Database(written).list_courses()] == ["BIO 101"]
    assert sorted(p.name for p in backups.glob("readcue-*.db")) == [
        "readcue-20200103-000000.db",
        written.name,
    ]

    named = tmp_path / "keep-me.db"
    assert main(["backup", "--to", str(named), "--keep", "1"]) == 0
    assert named.exists() and len(list(backups.glob("readcue-*.db"))) == 2  # a named file is never pruned


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"readcue {__version__}"


# ---- input limits ----------------------------------------------------------------------


def make_docx(xml_body: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", f"<w:document><w:body>{xml_body}</w:body></w:document>")
    return buf.getvalue()


def test_oversized_docx_is_rejected_before_it_is_expanded(monkeypatch):
    monkeypatch.setattr(extract, "MAX_DOCX_XML_BYTES", 50)
    with pytest.raises(ReadcueError, match="too large"):
        extract.extract_text(make_docx("<w:p><w:r><w:t>" + "x" * 500 + "</w:t></w:r></w:p>"), "big.docx")


def test_text_over_the_limit_asks_for_a_smaller_section(monkeypatch):
    monkeypatch.setattr(extract, "MAX_TEXT_CHARS", 100)
    with pytest.raises(ReadcueError, match="page range"):
        extract.extract_text(b"word " * 100, "huge.txt")
    assert extract.extract_text(b"word " * 10, "ok.txt")


def test_ocr_jobs_take_turns(monkeypatch):
    active, peak = 0, 0
    guard = threading.Lock()

    def fake_recognize(data, lang):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03)
        with guard:
            active -= 1
        return "text"

    monkeypatch.setattr(ocr, "_recognize", fake_recognize)
    workers = [threading.Thread(target=ocr.ocr_image, args=(b"x", "eng")) for _ in range(5)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    assert peak == 1
