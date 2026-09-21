import json

import pytest

from readcue.config import Config
from readcue.db import Database
from readcue.errors import NotifyError

SUMMARY = {
    "overview": "Cells are the basic unit of life.\n\nThey come in two broad types.",
    "key_points": ["Cells are basic", "Two cell types"],
    "definitions": [{"term": "Cell", "definition": "The basic unit of life."}],
}


def make_pdf(pages: list[str], size: int = 12) -> bytes:
    """A minimal text PDF, one line per page."""
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(pages)))
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, text in enumerate(pages):
        shown = " T* ".join(f"({line}) Tj" for line in text.split("\n"))
        content = f"BT /F1 {size} Tf 72 720 Td {size + 2} TL {shown} ET".encode()
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {5 + 2 * i} 0 R "
            "/Resources << /Font << /F1 3 0 R >> >> >>".encode()
        )
        objs.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def summary_json(**overrides) -> str:
    return json.dumps({**SUMMARY, **overrides})


class FakeProvider:
    """Stands in for an LLM. `responder` is a list of replies (consumed in order) or a callable."""

    name = "fake"
    model = "fake-1"
    label = "fake:fake-1"

    def __init__(self, responder, max_input_chars=100_000):
        self.responder = responder
        self.max_input_chars = max_input_chars
        self.calls: list[str] = []
        self.schemas: list[dict | None] = []

    def complete(self, system, user, *, json_mode=False, schema=None):
        self.calls.append(user)
        self.schemas.append(schema)
        if callable(self.responder):
            return self.responder(user)
        return self.responder.pop(0)


class FakeNotifier:
    def __init__(self, configured=True):
        self.configured = configured
        self.sent: list[dict] = []
        self.fail = False
        self.devices = ["iphone", "ipad"]  # what "Pushover" says the account has
        self.selected_devices: list[str] = []

    def list_devices(self):
        return list(self.devices)

    def send(self, title, message, *, url=None, url_title=None):
        if self.fail:
            raise NotifyError("boom")
        self.sent.append({"title": title, "message": message, "url": url, "url_title": url_title})


@pytest.fixture
def cfg(tmp_path):
    return Config(
        data_dir=tmp_path,
        pushover_app_token="tok",
        pushover_user_key="usr",
        base_url="http://readcue.test",
    )


@pytest.fixture
def db(cfg):
    return Database(cfg.db_path)


@pytest.fixture
def notifier():
    return FakeNotifier()
