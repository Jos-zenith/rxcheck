"""Prescriptions sent as a PDF (a clinic portal, an e-mail, WhatsApp): each page becomes an image that the checks
and the slip reader can use, plus the text the PDF already carries.

- Image checks (seen before, edited copy, file metadata) run on page 1 only, and the counter says so. A
  prescription on page 2 of a PDF is not compared with the ledger.
- A page that carries a text layer (a PDF printed from software) is read from that text: no OCR guesswork.
  A text layer is no more trustworthy than pixels: it says what the file says, not who issued it.
- A page without one (a scan saved as PDF) is rendered and read by OCR like a photo.

pypdfium2 renders the pages (pinned in requirements.txt). The PDF itself is not kept, only the page images,
which follow the same 24-hour upload retention as photos.
"""
import io

MAX_PAGES = 6          # a prescription with more pages than this is not a prescription
DPI = 200              # enough for OCR and the checks; an A4 page is 1654 x 2339 px
MIN_TEXT_CHARS = 20    # fewer characters than this: a scan with a stray text object, read it as an image


class PdfError(ValueError):
    """A PDF the counter cannot use; the message is shown to staff as it is."""


def is_pdf(data):
    return data[:5] == b"%PDF-"


def pages(data):
    """[(page JPEG bytes, text-layer lines or None)] for each page, in order. Raises PdfError."""
    import pypdfium2 as pdfium
    try:
        doc = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as ex:
        if "password" in str(ex).lower():
            raise PdfError("This PDF is password-protected. Ask for an unlocked copy, or a photo of the slip.")
        raise PdfError("That PDF cannot be opened. Ask for a photo of the slip instead.")
    try:
        n = len(doc)
        if n == 0:
            raise PdfError("That PDF has no pages.")
        if n > MAX_PAGES:
            raise PdfError(f"That PDF has {n} pages; the counter takes at most {MAX_PAGES}. "
                           f"Send only the prescription pages.")
        out, scale = [], DPI / 72
        for i in range(n):
            page = doc[i]
            img = page.render(scale=scale).to_pil().convert("RGB")
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=90)
            out.append((buf.getvalue(), text_lines(page, scale)))
            page.close()
        return out
    except pdfium.PdfiumError:
        raise PdfError("That PDF cannot be read. Ask for a photo of the slip instead.")
    finally:
        doc.close()


def text_lines(page, scale):
    """The page's text layer as reader lines [{'text', 'conf', 'box'}] in rendered-pixel coordinates, or None
    when the page has (almost) no text layer. conf is 1.0: the characters are in the file, not guessed."""
    tp = page.get_textpage()
    try:
        n = tp.count_chars()
        if n < MIN_TEXT_CHARS:
            return None
        _, height = page.get_size()
        lines, chars, box = [], [], None

        def flush():
            text = "".join(chars).strip()
            if text and box:
                lines.append({"text": " ".join(text.split()), "conf": 1.0, "box": tuple(int(v) for v in box)})

        for i in range(n):
            ch = tp.get_text_range(i, 1)
            if ch in ("\r", "\n"):
                flush()
                chars, box = [], None
                continue
            chars.append(ch)
            if not ch.strip():
                continue
            try:
                left, bottom, right, top = tp.get_charbox(i)
            except Exception:
                continue
            b = (left * scale, (height - top) * scale, right * scale, (height - bottom) * scale)
            box = b if box is None else (min(box[0], b[0]), min(box[1], b[1]), max(box[2], b[2]), max(box[3], b[3]))
        flush()
        return lines or None
    finally:
        tp.close()
