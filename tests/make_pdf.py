"""Synthetic PDF prescriptions for the tests and the OCR evaluation set: no third-party writer needed.

text_pdf  - pages with a real text layer, like a PDF printed from clinic software
image_pdf - pages that are only images, like a scan saved as PDF
"""
import io

A4 = (595, 842)   # points


def _esc(s):
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def text_pdf(pages, password=None):
    """pages: [[(x, y_from_top, size, text), ...], ...] in points on A4. Standard Helvetica, no embedding.
    password: not supported here (see locked_pdf)."""
    objs = []

    def add(body):
        objs.append(body)
        return len(objs)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    kids = []
    pages_id = len(objs) + 1 + 2 * len(pages)   # reserved below
    for lines in pages:
        ops = "".join(f"BT /F1 {size} Tf {x} {A4[1] - y} Td ({_esc(t)}) Tj ET\n" for x, y, size, t in lines).encode()
        content = add(b"<< /Length %d >>\nstream\n" % len(ops) + ops + b"endstream")
        kids.append(add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] /Resources << /Font << /F1 %d 0 R >> >>"
                        b" /Contents %d 0 R >>" % (pages_id, A4[0], A4[1], font, content)))
    assert add(b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(b"%d 0 R" % k for k in kids), len(kids))) == pages_id
    catalog = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for i, body in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, catalog, xref))
    return out.getvalue()


def image_pdf(images):
    """images: PIL images (or BGR arrays), one per page; the PDF holds pixels only."""
    from PIL import Image
    pil = []
    for im in images:
        if not isinstance(im, Image.Image):
            import cv2
            im = Image.fromarray(cv2.cvtColor(im, cv2.COLOR_BGR2RGB))
        pil.append(im.convert("RGB"))
    buf = io.BytesIO()
    pil[0].save(buf, "PDF", resolution=150, save_all=True, append_images=pil[1:])
    return buf.getvalue()


def locked_pdf():
    """A PDF that needs a password to open (Standard security handler, RC4 40-bit, user password 'x')."""
    import pypdfium2  # noqa: F401  (only to fail early where the counter could not read PDFs either)
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        return None
    w = PdfWriter(clone_from=PdfReader(io.BytesIO(text_pdf([[(72, 72, 12, "Locked prescription page")]]))))
    w.encrypt(user_password="x", owner_password="y", algorithm="RC4-40")
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()
