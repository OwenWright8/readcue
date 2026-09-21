import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import make_pdf

from readcue import ocr
from readcue.errors import ReadcueError


@pytest.fixture
def tools(monkeypatch):
    """Pretend tesseract and pdftoppm are installed and record how they're called."""
    calls = []

    def fake_run(cmd, input=None, capture_output=None, timeout=None, env=None):
        calls.append({"cmd": cmd, "input": input, "env": env})
        if cmd[0] == "pdftoppm":
            Path(f"{cmd[-1]}.png").write_bytes(b"PNG" + cmd[cmd.index("-f") + 1].encode())
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        return subprocess.CompletedProcess(cmd, 0, b"text of " + input, b"")

    monkeypatch.setattr(ocr.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(ocr.subprocess, "run", fake_run)
    return calls


def test_ocr_image_feeds_tesseract_on_stdin(tools):
    assert ocr.ocr_image(b"IMG", "eng+spa") == "text of IMG"
    call = tools[0]
    assert call["cmd"] == ["tesseract", "stdin", "stdout", "-l", "eng+spa"]
    assert call["input"] == b"IMG"
    assert call["env"]["OMP_THREAD_LIMIT"] == "1"


def test_ocr_pdf_pages_renders_each_page_then_recognises_it(tools):
    result = ocr.ocr_pdf_pages(b"%PDF", [3, 5], "eng")
    assert result == {3: "text of PNG3", 5: "text of PNG5"}
    renders = [c["cmd"] for c in tools if c["cmd"][0] == "pdftoppm"]
    assert len(renders) == 2
    for cmd, page in zip(sorted(renders, key=lambda c: c[c.index("-f") + 1]), (3, 5), strict=True):
        assert cmd[1:9] == ["-r", "300", "-png", "-singlefile", "-f", str(page), "-l", str(page)]


def test_tool_failure_and_timeout_become_clean_errors(monkeypatch):
    monkeypatch.setattr(ocr.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        ocr.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, b"", b"Unknown image format"),
    )
    with pytest.raises(ReadcueError, match="Unknown image format"):
        ocr.ocr_image(b"x", "eng")

    def hang(*a, **k):
        raise subprocess.TimeoutExpired(a[0], 1)

    monkeypatch.setattr(ocr.subprocess, "run", hang)
    with pytest.raises(ReadcueError, match="too long"):
        ocr.ocr_image(b"x", "eng")


def test_missing_tools_explain_how_to_install(monkeypatch):
    monkeypatch.setattr(ocr.shutil, "which", lambda name: None)
    assert not ocr.available()
    with pytest.raises(ReadcueError, match="poppler-utils"):
        ocr.ocr_image(b"x", "eng")
    with pytest.raises(ReadcueError, match="poppler-utils"):
        ocr.ocr_pdf_pages(b"%PDF", [1], "eng")


@pytest.mark.skipif(
    not (shutil.which("tesseract") and shutil.which("pdftoppm")), reason="tesseract/poppler not installed"
)
def test_real_ocr_reads_a_rendered_pdf_page():
    text = ocr.ocr_pdf_pages(make_pdf(["The nucleus stores DNA"]), [1], "eng")[1]
    assert "nucleus" in text.lower()
