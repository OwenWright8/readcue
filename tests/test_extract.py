import io
import zipfile

import pytest
from conftest import make_pdf
from pypdf import PdfWriter

from readcue import ocr
from readcue.errors import ReadcueError
from readcue.extract import extract_text, parse_page_range


def page(n: int) -> str:
    return f"This is page {n} of the chapter and it has plenty of real text on it."


def make_docx(body_xml: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="x"><w:body>{body_xml}</w:body></w:document>')
    return buf.getvalue()


def test_plain_text_is_normalised():
    text = extract_text(b"one  \r\n\r\n\r\n\r\ntwo\x00", "notes.md")
    assert text == "one\n\ntwo"


def test_pdf_text_and_page_ranges():
    pdf = make_pdf([page(1), page(2), page(3)])
    everything = extract_text(pdf, "book.pdf")
    assert "page 1 " in everything and "page 3 " in everything
    only_second = extract_text(pdf, "book.pdf", pages="2")
    assert "page 2 " in only_second and "page 1 " not in only_second
    both = extract_text(pdf, "book.pdf", pages="2-3")
    assert "page 2 " in both and "page 3 " in both and "page 1 " not in both


def test_pdf_page_range_out_of_bounds():
    with pytest.raises(ReadcueError, match="doesn't fit"):
        extract_text(make_pdf([page(1)]), "b.pdf", pages="5-9")


def blank_pdf(pages=1) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(200, 200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


@pytest.fixture
def fake_ocr(monkeypatch):
    calls = []
    monkeypatch.setattr(ocr, "available", lambda: True)

    def ocr_pdf_pages(data, pages, lang):
        calls.append((pages, lang))
        return {p: f"OCR of page {p}" for p in pages}

    monkeypatch.setattr(ocr, "ocr_pdf_pages", ocr_pdf_pages)
    monkeypatch.setattr(ocr, "ocr_image", lambda data, lang: f"  photo text ({lang}) \n")
    return calls


def test_scanned_pdf_pages_are_ocred_and_text_pages_kept_in_order(fake_ocr):
    text = extract_text(make_pdf([page(1), "", page(3)]), "book.pdf", ocr_lang="eng+spa")
    assert fake_ocr == [([2], "eng+spa")]  # only the page without a text layer
    assert text.index("page 1 ") < text.index("OCR of page 2") < text.index("page 3 ")


def test_ocr_page_numbers_are_absolute_when_a_range_is_given(fake_ocr):
    extract_text(make_pdf([page(1), "", ""]), "book.pdf", pages="2-3")
    assert fake_ocr == [([2, 3], "eng")]


def test_fully_scanned_pdf_without_ocr_tools_says_how_to_fix_it(monkeypatch):
    monkeypatch.setattr(ocr, "available", lambda: False)
    with pytest.raises(ReadcueError, match="tesseract"):
        extract_text(blank_pdf(2), "scan.pdf")


def test_mostly_text_pdf_survives_a_missing_ocr_install(monkeypatch):
    monkeypatch.setattr(ocr, "available", lambda: False)
    text = extract_text(make_pdf([page(1), "", page(3)]), "book.pdf")
    assert "page 1 " in text and "page 3 " in text


def test_too_many_scanned_pages_asks_for_a_page_range(fake_ocr, monkeypatch):
    monkeypatch.setattr(ocr, "MAX_OCR_PAGES", 2)
    with pytest.raises(ReadcueError, match="page range"):
        extract_text(blank_pdf(3), "scan.pdf")
    assert fake_ocr == []


def test_photo_of_a_page_is_ocred(fake_ocr):
    assert extract_text(b"jpeg bytes", "IMG_0001.JPG", ocr_lang="spa") == "photo text (spa)"


def test_ocr_that_finds_nothing_is_an_error(monkeypatch):
    monkeypatch.setattr(ocr, "ocr_image", lambda data, lang: "  \n")
    with pytest.raises(ReadcueError, match="even after OCR"):
        extract_text(b"x", "blank.png")


def test_garbage_pdf_is_a_clean_error():
    with pytest.raises(ReadcueError):
        extract_text(b"definitely not a pdf", "x.pdf")


def test_docx_keeps_table_rows_on_one_line():
    xml = (
        "<w:p><w:r><w:t>Schedule</w:t></w:r></w:p>"
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Oct 18</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>Read Ch. 4 &amp; 5</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
    )
    assert extract_text(make_docx(xml), "syllabus.docx") == "Schedule\nOct 18 | Read Ch. 4 & 5"


def test_rejects_unsupported_types_and_stray_page_ranges():
    with pytest.raises(ReadcueError, match="Unsupported"):
        extract_text(b"x", "book.epub")
    with pytest.raises(ReadcueError, match="only apply to PDF"):
        extract_text(b"x", "notes.txt", pages="1-2")


def test_parse_page_range():
    assert list(parse_page_range("3", 10)) == [2]
    assert list(parse_page_range(" 3 - 5 ", 10)) == [2, 3, 4]
    for bad in ("abc", "0-2", "5-3", "1-11"):
        with pytest.raises(ReadcueError):
            parse_page_range(bad, 10)
