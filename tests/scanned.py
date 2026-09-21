"""Make a genuinely image-only ("scanned") PDF for tests: render text pages to pictures, drop the text."""

import struct
import subprocess
import tempfile
from pathlib import Path

from conftest import make_pdf


def _png_pixels(png: bytes) -> tuple[int, int, bytes]:
    """Width, height and the raw (still deflated, PNG-predicted) pixel stream of an 8-bit grayscale PNG."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    position, data, width, height = 8, b"", 0, 0
    while position < len(png):
        length, kind = struct.unpack(">I4s", png[position : position + 8])
        chunk = png[position + 8 : position + 8 + length]
        if kind == b"IHDR":
            width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", chunk)
            assert (depth, color, interlace) == (8, 0, 0), "expected a plain 8-bit grayscale PNG"
        elif kind == b"IDAT":
            data += chunk
        position += 12 + length
    return width, height, data


def scanned_pdf(pages: list[str], *, dpi: int = 150, size: int = 24) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "text.pdf"
        source.write_bytes(make_pdf(pages, size=size))
        images = []
        for n in range(1, len(pages) + 1):
            out = Path(tmp) / f"p{n}"
            cmd = ["pdftoppm", "-r", str(dpi), "-gray", "-png", "-singlefile", "-f", str(n), "-l", str(n)]
            subprocess.run(cmd + [str(source), str(out)], check=True)
            images.append(_png_pixels(Path(f"{out}.png").read_bytes()))

    kids = " ".join(f"{3 + 3 * i} 0 R" for i in range(len(images)))
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {len(images)} >>".encode(),
    ]
    for i, (width, height, pixels) in enumerate(images):
        pw, ph = width * 72 / dpi, height * 72 / dpi
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {pw:.2f} {ph:.2f}] /Contents {4 + 3 * i} 0 R "
            f"/Resources << /XObject << /Im0 {5 + 3 * i} 0 R >> >> >>".encode()
        )
        draw = f"q {pw:.2f} 0 0 {ph:.2f} 0 0 cm /Im0 Do Q".encode()
        objs.append(b"<< /Length %d >>\nstream\n" % len(draw) + draw + b"\nendstream")
        objs.append(
            (
                f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} /ColorSpace /DeviceGray "
                f"/BitsPerComponent 8 /Filter /FlateDecode /DecodeParms << /Predictor 15 /Colors 1 "
                f"/BitsPerComponent 8 /Columns {width} >> /Length {len(pixels)} >>\nstream\n"
            ).encode()
            + pixels
            + b"\nendstream"
        )
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out
