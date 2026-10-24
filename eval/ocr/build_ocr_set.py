"""Build the labelled slip-reading set: samples/ocr/ (images, PDFs, manifest.json). Generated locally, never committed.

    30 printed slips           - computer-printed, varied fonts, layouts, numbering and date formats
     5 poor-lighting photos    - printed slips seen through dim light, a side shadow, glare, an angle, forwarding
    10 handwriting stand-ins   - printed letterhead, medicine lines in handwriting-STYLE fonts (Ink Free, Segoe
                                 Print, Bradley Hand, ...). Far easier to read than a doctor's pen: their score is
                                 reported apart and is NOT handwriting accuracy
     5 multi-page PDFs         - 3 with a text layer (as printed from clinic software), 2 scanned (pixels only)

Everything is synthetic, says so on the slip, and uses made-up doctors and registration numbers. Real slips (the
10 handwritten ones above all) come from the consented field study: put them in eval/field/ocr/ with a
labels.json in the shape of eval/ocr/labels-template.json, and eval/ocr/score.py scores them as 'real'.

Run:  python eval/ocr/build_ocr_set.py      then  python eval/ocr/score.py
"""
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "app")]
from scenarios import dim_shop_light, side_shadow, blown_glare, angled_photo, forwarded  # noqa: E402
import make_pdf  # noqa: E402

OUT = ROOT / "samples" / "ocr"
FONT_DIRS = [Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/dejavu"), Path("/Library/Fonts")]
PRINTED = ["arial.ttf", "calibri.ttf", "times.ttf", "verdana.ttf", "georgia.ttf", "tahoma.ttf", "segoeui.ttf",
           "cour.ttf", "DejaVuSans.ttf", "DejaVuSerif.ttf"]
HANDWRITING = ["Inkfree.ttf", "segoepr.ttf", "segoesc.ttf", "BRADHITC.TTF", "LHANDW.TTF"]

# (name as written, strength text, form) -> expected reading "name strength" (unit kept unless mg)
MEDICINES = [("Dolo", "650 mg", "Tab."), ("Pan", "40 mg", "Tab."), ("Augmentin", "625 mg", "Tab."),
             ("Azithral", "500 mg", "Tab."), ("Telma", "40 mg", "Tab."), ("Glycomet", "500 mg", "Tab."),
             ("Montair", "10 mg", "Tab."), ("Allegra", "120 mg", "Tab."), ("Ecosprin", "75 mg", "Tab."),
             ("Shelcal", "500 mg", "Tab."), ("Thyronorm", "50 mcg", "Tab."), ("Atorva", "10 mg", "Tab."),
             ("Rantac", "150 mg", "Tab."), ("Ascoril LS", "5 ml", "Syp."), ("Becosules", "", "Cap."),
             ("Omez", "20 mg", "Cap."), ("Alprax", "0.25 mg", "Tab."), ("Lonazep", "0.5 mg", "Tab."),
             ("Lyrica", "75 mg", "Cap."), ("Ultracet", "", "Tab."), ("Zolfresh", "10 mg", "Tab.")]
DOSES = ["1-0-1", "1-0-0", "0-0-1", "1-1-1", "x 5 days", "after food", "BD", "TDS", "SOS"]
FIRST = ["Asha", "Ravi", "Meena", "Karthik", "Lakshmi", "Suresh", "Divya", "Arun", "Priya", "Vijay"]
LAST = ["Example", "Sample", "Testcase", "Fixture", "Demo", "Placeholder"]
QUALS = ["MBBS", "MBBS, MD", "MBBS, DNB", "MBBS, MD (Gen Med)", "MBBS, DCH"]


def font(name, size):
    for d in FONT_DIRS:
        if (d / name).is_file():
            return ImageFont.truetype(str(d / name), size)
    return None


def fonts(names):
    return [n for n in names if font(n, 12)]


def expected_drug(name, strength):
    num, _, unit = strength.partition(" ")
    return name if not num else f"{name} {num}" + (f" {unit}" if unit and unit != "mg" else "")


def slip(rng, today, body_font, hand_font=None):
    """One synthetic slip: (PIL image, labels, lines as (x, y, size, text) for a PDF text layer)."""
    W, H = 1240, 1754
    im = Image.new("RGB", (W, H), (250, 250, 247))
    d = ImageDraw.Draw(im)
    doctor = f"Dr. {rng.choice(FIRST)} {rng.choice(LAST)}"
    regn = rng.choice([f"TN-{rng.randint(10000, 99999)}", f"{rng.randint(10000, 99999)}",
                       f"KMC {rng.randint(1000, 99999)}", f"MH/{rng.randint(1000, 9999)}"])
    rx = today - timedelta(days=rng.randint(0, 60))
    fmt = rng.choice(["%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y"])
    meds = rng.sample(MEDICINES, rng.randint(2, 5))
    lines = []

    def put(x, y, size, text, fnt=None, fill=(20, 20, 20)):
        d.text((x, y), text, font=fnt or font(body_font, size), fill=fill)
        lines.append((x * 595 / W, y * 842 / H + size * 0.8 * 842 / H, round(size * 595 / W, 1), text))

    put(80, 60, 50, "SYNTHETIC TEST CLINIC")
    put(80, 125, 24, "12, Example Road, Test Nagar - NOT A REAL PRESCRIPTION")
    put(80, 190, 34, f"{doctor}, {rng.choice(QUALS)}")
    put(80, 240, 28, rng.choice(["Regn No: ", "Regn. No. ", "Reg No ", "Registration No: "]) + regn)
    put(rng.choice([760, 820, 860]), 300, 30, "Date: " + rx.strftime(fmt))
    put(80, 360, 28, f"Name: R. Kumar   Age: {rng.randint(18, 80)} / {rng.choice('MF')}")
    put(80, 440, 46, "Rx")
    y = 540
    numbered = rng.random() < 0.6
    for i, (name, strength, form) in enumerate(meds, 1):
        form = rng.choice([form, form, form.rstrip("."), "T." if form == "Tab." else form])
        text = (f"{i}. " if numbered else "") + f"{form} {name} {strength}".replace("  ", " ") + "  " + rng.choice(DOSES)
        if hand_font:
            d.text((120, y), text, font=font(hand_font, rng.randint(36, 44)), fill=(25, 35, 90))
        else:
            put(120, y, rng.choice([30, 32, 34]), text)
        y += rng.choice([80, 90, 100])
    put(80, 1640, 26, "SYNTHETIC - FOR SOFTWARE TESTING ONLY")
    labels = {"drugs": [expected_drug(n, s) for n, s, _ in meds], "regn": regn.replace(" ", "").upper(),
              "doctor": doctor, "rx_date": rx.isoformat()}
    return im, labels, lines


def camera(im, rng):
    """A clean phone photo of a printed slip: a small tilt, a little blur, JPEG."""
    a = cv2.cvtColor(np.array(im), cv2.COLOR_RGB2BGR)
    h, w = a.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), rng.uniform(-1.5, 1.5), 1.0)
    a = cv2.warpAffine(a, M, (w, h), borderValue=(200, 200, 195))
    if rng.random() < 0.5:
        a = cv2.GaussianBlur(a, (3, 3), 0)
    return a


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    rng = random.Random(20261024)
    today = date.today()
    printed, hand = fonts(PRINTED), fonts(HANDWRITING)
    if not printed:
        sys.exit("no printed font found (Arial / DejaVu)")
    items = []

    def save_img(name, bgr, kind, labels, note=""):
        cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 88])[1].tofile(str(OUT / f"{name}.jpg"))
        items.append({"id": name, "file": f"{name}.jpg", "kind": kind, "synthetic": True, "note": note,
                      "expect": labels})

    for i in range(30):
        f = printed[i % len(printed)]
        im, labels, _ = slip(rng, today, f)
        save_img(f"printed-{i + 1:02d}", camera(im, rng), "printed", labels, f"font {f}")
    for i, (fn, what) in enumerate([(dim_shop_light, "dim shop light"), (side_shadow, "side shadow"),
                                    (blown_glare, "glare"), (angled_photo, "angled photo"),
                                    (forwarded, "forwarded 5 times")]):
        im, labels, _ = slip(rng, today, printed[i % len(printed)])
        save_img(f"lighting-{i + 1:02d}", fn(camera(im, rng)), "lighting", labels, what)
    if hand:
        for i in range(10):
            f = hand[i % len(hand)]
            im, labels, _ = slip(rng, today, printed[i % len(printed)], hand_font=f)
            save_img(f"handfont-{i + 1:02d}", camera(im, rng), "handwriting-font", labels,
                     f"stand-in: handwriting-style font {f}, not real handwriting")
    else:
        print("[SKIP] no handwriting-style fonts on this machine")
    for i in range(5):
        text_layer = i < 3
        pages, labels = [], None
        n_pages = 2 if i % 2 == 0 else 3
        for p in range(n_pages):
            im, lab, lines = slip(rng, today, printed[(i + p) % len(printed)])
            pages.append((im, lines))
            if p == 0:
                labels = lab
            elif p == 1:   # page 2 continues the same prescription: its medicines count, its letterhead repeats
                labels = dict(labels, drugs=labels["drugs"] + lab["drugs"])
                # same doctor, Regn No and date on every page, as a clinic's template prints them
                pages[-1] = slip_like(pages[-1], labels, lab, lines)
        # page 3, when there is one, holds advice only: no medicine, no letterhead
        if text_layer:
            data = make_pdf.text_pdf([ln for _, ln in pages[:2]]
                                     + [[(60, 80, 12, "Advice: plenty of fluids, review in 5 days")]] * (n_pages - 2))
        else:
            advice = Image.new("RGB", (1240, 1754), (250, 250, 247))
            ImageDraw.Draw(advice).text((80, 120), "Advice: plenty of fluids, review in 5 days",
                                        font=font(printed[0], 30), fill=(20, 20, 20))
            scans = [Image.fromarray(cv2.cvtColor(camera(im, rng), cv2.COLOR_BGR2RGB)) for im, _ in pages[:2]]
            data = make_pdf.image_pdf(scans + [advice] * (n_pages - 2))
        name = f"pdf-{'text' if text_layer else 'scan'}-{i + 1:02d}"
        (OUT / f"{name}.pdf").write_bytes(data)
        items.append({"id": name, "file": f"{name}.pdf", "kind": "pdf-text" if text_layer else "pdf-scan",
                      "synthetic": True, "note": f"{n_pages} pages; medicines on pages 1 and 2", "expect": labels})
    (OUT / "manifest.json").write_text(json.dumps({"_note": "Synthetic slips for the slip reader; not real "
                                                   "prescriptions. Built by eval/ocr/build_ocr_set.py.",
                                                   "built_on": today.isoformat(), "items": items}, indent=1),
                                       encoding="utf-8")
    counts = {}
    for it in items:
        counts[it["kind"]] = counts.get(it["kind"], 0) + 1
    print(f"{len(items)} slips in {OUT.relative_to(ROOT)}: " + ", ".join(f"{v} {k}" for k, v in counts.items()))


def slip_like(page, labels, own, lines):
    """Page 2 of a PDF repeats page 1's doctor, Regn No and date (a clinic template), so the reading is single."""
    im, _ = page
    fix = {own["doctor"]: labels["doctor"], own["rx_date"]: labels["rx_date"]}
    new_lines = []
    for x, y, size, text in lines:
        for a, b in fix.items():
            text = text.replace(a, b)
        new_lines.append((x, y, size, text))
    # redraw the header band of the image with page 1's values
    d = ImageDraw.Draw(im)
    d.rectangle((0, 180, 1240, 340), fill=(250, 250, 247))
    f = font(PRINTED[0], 34) or font("DejaVuSans.ttf", 34)
    d.text((80, 190), f"{labels['doctor']}, MBBS", font=f, fill=(20, 20, 20))
    d.text((80, 240), f"Regn No: {labels['regn']}", font=font(PRINTED[0], 28) or f, fill=(20, 20, 20))
    rx = date.fromisoformat(labels["rx_date"]).strftime("%d/%m/%Y")
    d.text((820, 300), f"Date: {rx}", font=font(PRINTED[0], 30) or f, fill=(20, 20, 20))
    hdr = [(x, y, s, t) for x, y, s, t in new_lines if not t.startswith(("Dr.", "Regn", "Reg", "Date"))]
    hdr += [(80 * 595 / 1240, 190 * 842 / 1754 + 13, 16.3, f"{labels['doctor']}, MBBS"),
            (80 * 595 / 1240, 240 * 842 / 1754 + 11, 13.4, f"Regn No: {labels['regn']}"),
            (820 * 595 / 1240, 300 * 842 / 1754 + 12, 14.4, f"Date: {rx}")]
    return im, hdr


if __name__ == "__main__":
    main()
