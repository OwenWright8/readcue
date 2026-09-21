"""Run inside the Docker image: prove OCR really reads text from a rendered page and from a photo."""

import subprocess
import sys
import tempfile
from pathlib import Path

from readcue import ocr
from readcue.extract import extract_text

TEXT = "The nucleus stores DNA"


def make_pdf(text: str) -> bytes:
    content = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 5 0 R "
        b"/Resources << /Font << /F1 3 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def main() -> int:
    assert ocr.available(), "tesseract and pdftoppm should be installed in the image"
    pdf = make_pdf(TEXT)

    page_text = ocr.ocr_pdf_pages(pdf, [1], "eng")[1]
    print("PDF page OCR:", page_text.strip())
    assert "nucleus" in page_text.lower(), page_text

    # A photo-style upload: render the page to a PNG and read that through the normal extraction path.
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "in.pdf").write_bytes(pdf)
        subprocess.run(
            [
                "pdftoppm",
                "-r",
                "200",
                "-png",
                "-singlefile",
                str(Path(tmp) / "in.pdf"),
                str(Path(tmp) / "photo"),
            ],
            check=True,
        )
        png = (Path(tmp) / "photo.png").read_bytes()
    photo_text = extract_text(png, "IMG_0001.png")
    print("Photo OCR:", photo_text)
    assert "nucleus" in photo_text.lower(), photo_text
    print("OCR OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
