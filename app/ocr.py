"""Reading a prescription photo (OCR) to save typing at the counter. Runs on this PC: nothing leaves it.

OCR here is a typing aid, never evidence. Every check still runs on what staff submit, and three rules keep
a misread from becoming a sale:

1. A medicine is filled in as read, never swapped for the nearest known name: Metformin and Metoprolol are
   one tap apart in a suggestion list and a dispensing error apart in real life. It is filled in when its
   whole name is one this pharmacy knows (the drug list, or entered at this counter before) and it was read
   with at least FILL_MIN, or when it was read with at least CONFIRM_ABOVE. Anything else is shown with the
   part of the slip it came from, for staff to type. Confidence alone is weak evidence: on the synthetic
   demo handwriting the engine reported 0.9 for nonsense.
2. What OCR filled in and staff submitted unchanged must be ticked as checked against the slip when it is a
   high-risk medicine, a name not on file, a Regn No (a misread digit can name another doctor, and that is
   RED), or read with a confidence below CONFIRM_ABOVE.
3. OCR never overwrites what staff typed, a clinic-signed code, or a demo prefill.

The thresholds are placeholders, not measured values: the counter records how often OCR values were kept
or corrected (audit `ocr-use`, counts only), and the shadow pilot sets them from that.

Engine: RapidOCR (PP-OCR models on onnxruntime), optional. Without it the counter works as before.
    pip install onnxruntime==1.24.4 pyclipper==1.4.0 shapely==2.2.0 PyYAML==6.0.3 six==1.17.0
    pip install --no-deps rapidocr_onnxruntime==1.4.4
(--no-deps: it asks for opencv-python, which would sit beside the headless OpenCV the checks were tuned on.)
Switch off with RXCHECK_OCR=off.

Try it on one image:  python app/ocr.py slip.jpg
"""
import os
import re
import sys
import threading
import time
from datetime import date

FILL_MIN = 0.80        # below this a reading is shown as unclear, never filled in
CONFIRM_ABOVE = 0.95   # a filled value read with less than this needs a tick
MAX_SIDE = 2000        # longest image side handed to the engine (px)

_engine = None
_engine_lock = threading.Lock()   # one read at a time: a queue of photos must not starve the counter checks


def enabled():
    return os.environ.get("RXCHECK_OCR", "on").strip().lower() not in ("off", "0", "no", "false")


def available():
    """True when OCR is switched on and the engine can be imported."""
    if not enabled():
        return False
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except Exception:
        return False
    return True


def _get_engine():
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _engine = RapidOCR()
    return _engine


def load(path):
    """The image as the engine and the crops see it (EXIF rotation applied), so their coordinates agree."""
    import cv2
    import numpy as np
    img = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("not a readable image")
    return img


def crop_jpeg(path, box, pad=14, max_w=900):
    """JPEG of the part of the slip a reading came from, shown next to it so staff compare like with like."""
    import cv2
    img = load(path)
    h, w = img.shape[:2]
    x0, y0, x1, y1 = (int(v) for v in box)
    x0, y0, x1, y1 = max(0, x0 - pad), max(0, y0 - pad), min(w, x1 + pad), min(h, y1 + pad)
    if x1 <= x0 or y1 <= y0:
        raise ValueError("empty crop")
    c = img[y0:y1, x0:x1]
    if c.shape[1] > max_w:
        c = cv2.resize(c, (max_w, max(1, int(c.shape[0] * max_w / c.shape[1]))), interpolation=cv2.INTER_AREA)
    return cv2.imencode(".jpg", c, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()


def read_segments(img):
    """[(x0, y0, x1, y1), text, confidence] for each piece of text, in the image's own pixel coordinates."""
    import cv2
    h, w = img.shape[:2]
    scale = min(1.0, MAX_SIDE / max(h, w))
    small = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA) if scale < 1 else img
    with _engine_lock:
        res, _ = _get_engine()(small, use_cls=False)   # the classifier only helps upside-down text; it costs ~1 s
    out = []
    for box, text, conf in res or []:
        xs, ys = [p[0] / scale for p in box], [p[1] / scale for p in box]
        out.append(((int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))), str(text), float(conf)))
    return out


def group_lines(segments):
    """Pieces of text on the same printed line, joined left to right: [{'text', 'conf', 'box'}], top to bottom.
    A line's confidence is its weakest piece's."""
    lines = []
    for box, text, conf in sorted(segments, key=lambda s: (s[0][1] + s[0][3]) / 2):
        mid = (box[1] + box[3]) / 2
        for ln in lines:
            top, bottom = ln["box"][1], ln["box"][3]
            if top <= mid <= bottom:
                ln["parts"].append((box, text, conf))
                ln["box"] = (min(ln["box"][0], box[0]), min(top, box[1]), max(ln["box"][2], box[2]), max(bottom, box[3]))
                break
        else:
            lines.append({"parts": [(box, text, conf)], "box": box})
    out = []
    for ln in lines:
        parts = sorted(ln["parts"], key=lambda p: p[0][0])
        out.append({"text": " ".join(p[1].strip() for p in parts), "conf": min(p[2] for p in parts),
                    "box": ln["box"]})
    return sorted(out, key=lambda ln: ln["box"][1])


# ---------------------------------------------------------------- what the lines say

FORM = r"(?:tab(?:let)?s?|cap(?:sule)?s?|syp|syr(?:up)?|inj|oint|gel|drops?|t|c)"
MED_LINE = re.compile(rf"^\s*(?:\d{{1,2}}\s*[.)]\s*)?{FORM}\b\s*\.?\s*(?P<rest>[A-Za-z].*)$", re.I)
NAME_DOSE = re.compile(r"^(?P<name>[A-Za-z][A-Za-z\- ]*?)\s*(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>mg|mcg|µg|g|ml|iu|%)?"
                       r"(?![A-Za-z])", re.I)
NAME_ONLY = re.compile(r"^(?P<name>[A-Za-z][A-Za-z\-]*)")
REGN = re.compile(r"\breg(?:n|d|istration)?\b\.?\s*(?:no|number)?\.?\s*[:.\-]?\s*"
                  r"(?P<v>(?:[A-Z]{1,5}\s*[-/]?\s*)?\d{3,7}(?:\s*/\s*\d{2,4})?)", re.I)
DOCTOR = re.compile(r"^\s*dr\b\.?\s*(?P<v>[A-Za-z][A-Za-z. ]{2,60}?)\s*(?:,|$|\b(?:mbbs|md|ms|dnb|dpm|bds|bams|bhms)\b)",
                    re.I)
DATE = re.compile(r"(?<!\d)(?P<d>\d{1,2})\s*[/.\-|]\s*(?P<m>\d{1,2})\s*[/.\-|]\s*(?P<y>\d{4}|\d{2})(?!\d)")


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def medicine(line, known):
    """A medicine reading from one line, or None when the line does not look like a medicine."""
    m = MED_LINE.match(line["text"])
    rest = (m.group("rest") if m else line["text"]).strip()
    # with a strength: the name is everything before its first digit ('Ascoril LS 5 ml'); without one, the
    # first word only, so directions ('Betadine gargle twice daily') stay out of the name
    nd = NAME_DOSE.match(rest) or NAME_ONLY.match(rest)
    if not nd:
        return None
    name = re.sub(r"\s+", " ", nd.group("name")).strip(" -")
    hit = _norm(name) in known   # the whole name: 'Pan D' is not 'Pan'
    if not m and not hit:
        return None   # no 'Tab' / 'Cap' and not a name we know: a heading, an address, the patient line
    gd = nd.groupdict()
    num, unit = gd.get("num"), (gd.get("unit") or "").lower()
    text = name if not num else f"{name} {num}" + (f" {unit}" if unit and unit != "mg" else "")
    return {"text": text, "raw": line["text"], "conf": round(line["conf"], 3), "box": line["box"],
            "known": bool(hit), "strength": bool(num)}


def rx_date(text, today):
    """ISO date from a dd/mm/yyyy-style reading (day first, as written in India), or None if impossible or future."""
    m = DATE.search(text)
    if not m:
        return None
    y = int(m.group("y"))
    y = 2000 + y if y < 100 else y
    try:
        d = date(y, int(m.group("m")), int(m.group("d")))
    except ValueError:
        return None
    if d > today or (today - d).days > 2 * 365:
        return None
    return d.isoformat()


def parse(lines, known, is_high=lambda text: False, today=None):
    """Turn OCR lines into suggestions for the counter form.
    known: lower-case medicine names this pharmacy knows. is_high: text -> True for a high-risk medicine."""
    today = today or date.today()
    known = {_norm(k) for k in known if k}
    crops, drugs, unclear = [], [], []

    def crop(box):
        crops.append(list(box))
        return len(crops) - 1

    def single(found):
        """One value when every reading agrees, else None: two doctors on a letterhead, two dates, ..."""
        vals = {_norm(v) for v, _ in found}
        return min(found, key=lambda f: -f[1]["conf"]) if len(vals) == 1 else None

    regns, doctors, dates = [], [], []
    for ln in lines:
        rm = REGN.search(ln["text"])
        if rm:
            regns.append((re.sub(r"\s+", "", rm.group("v")).upper(), ln))
        dm = DOCTOR.match(ln["text"])
        if dm:
            doctors.append(("Dr. " + re.sub(r"\s+", " ", dm.group("v")).strip(" ."), ln))
        dv = rx_date(ln["text"], today)
        if dv:
            dates.append((dv, ln))
        if rm or dm:
            continue
        med = medicine(ln, known)
        if not med:
            continue
        med["high"] = bool(is_high(med["text"]))
        med["crop"] = crop(med.pop("box"))
        if med["conf"] < FILL_MIN:
            unclear.append({**med, "why": "hard to read - check the slip"})
        elif not med["known"] and med["conf"] < CONFIRM_ABOVE:
            unclear.append({**med, "why": "not a name this pharmacy has on file, and not clearly read - check the slip"})
        else:
            why = ("high-risk medicine" if med["high"] else "not a name this pharmacy has on file" if not med["known"]
                   else "no strength read" if not med["strength"]
                   else "read with low confidence" if med["conf"] < CONFIRM_ABOVE else "")
            drugs.append({**med, "confirm": bool(why), "why": why})

    out = {"drugs": drugs, "unclear": unclear, "crops": crops}
    for key, found, always in (("regn", regns, True), ("doctor", doctors, False), ("rx_date", dates, False)):
        best = single(found) if found else None
        if best and best[1]["conf"] >= FILL_MIN:
            value, ln = best
            out[key] = {"value": value, "conf": round(ln["conf"], 3), "crop": crop(ln["box"]),
                        "confirm": always or ln["conf"] < CONFIRM_ABOVE}
    return out


def read_slip(path, known, is_high=lambda text: False, today=None):
    """OCR one prescription image: {'status': 'done', 'ms', 'engine', 'drugs', 'unclear', 'crops', 'regn'?, ...}
    or {'status': 'failed', 'message'}. Never raises: a failed read only means staff type as before."""
    t0 = time.time()
    try:
        res = parse(group_lines(read_segments(load(path))), known, is_high, today)
    except Exception as ex:   # the engine, a model file, memory: none of it may stop a check
        return {"status": "failed", "message": f"{type(ex).__name__}: {ex}"[:200], "ms": int((time.time() - t0) * 1000)}
    return {"status": "done", "engine": "rapidocr", "ms": int((time.time() - t0) * 1000), **res}


# ---------------------------------------------------------------- the counter form

FIELDS = ("drugs", "regn", "doctor", "rx_date")


def values(ocr):
    """What OCR fills into empty form fields."""
    if not ocr or ocr.get("status") != "done":
        return {}
    v = {k: ocr[k]["value"] for k in ("regn", "doctor", "rx_date") if ocr.get(k)}
    if ocr.get("drugs"):
        v["drugs"] = ", ".join(d["text"] for d in ocr["drugs"])
    return v


def _items(s):
    return [_norm(x) for x in re.split(r"[,;\n]", s or "") if x.strip()]


def confirmations(ocr, fields):
    """Entries that came from OCR, were submitted unchanged and must be ticked as checked against the slip:
    [{'item', 'issue', 'crop'}]. Same input -> same list, so the tick numbers survive the round trip."""
    if not ocr or ocr.get("status") != "done":
        return []
    out = []
    sent = _items(fields.get("drugs"))
    for d in ocr.get("drugs", []):
        if d["confirm"] and _norm(d["text"]) in sent:
            out.append({"item": d["text"], "crop": d["crop"],
                        "issue": f"read from the photo ({d['why']}) - check the name and strength against the slip"})
    labels = {"regn": "Regn No read from the photo - check every character against the slip",
              "doctor": "doctor's name read from the photo - check it against the slip",
              "rx_date": "date read from the photo - check day and month against the slip"}
    for key, issue in labels.items():
        r = ocr.get(key)
        if r and r["confirm"] and _norm(fields.get(key)) == _norm(r["value"]):
            out.append({"item": r["value"], "crop": r["crop"], "issue": issue})
    return out


def usage(ocr, fields):
    """Counts for the audit trail, no content: how much OCR offered and how much of it staff kept."""
    if not ocr or ocr.get("status") != "done":
        return None
    sent = _items(fields.get("drugs"))
    offered = [_norm(d["text"]) in sent for d in ocr.get("drugs", [])]
    for key in ("regn", "doctor", "rx_date"):
        if ocr.get(key):
            offered.append(_norm(fields.get(key)) == _norm(ocr[key]["value"]))
    return {"offered": len(offered), "kept": sum(offered), "changed": len(offered) - sum(offered),
            "unclear": len(ocr.get("unclear", [])), "ms": ocr.get("ms"), "engine": ocr.get("engine")}


if __name__ == "__main__":
    import json
    if len(sys.argv) != 2:
        sys.exit("usage: python app/ocr.py IMAGE")
    if not available():
        sys.exit("OCR is not available: install it (see the top of app/ocr.py) or unset RXCHECK_OCR=off")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import rxcheck as r
    table = r.drug_table() or r.EMPTY_TABLE
    res = read_slip(sys.argv[1], set(table["brands"]) | set(table["molecules"]),
                    lambda t: r.drug_risk(t)[0] == "high")
    print(json.dumps({k: v for k, v in res.items() if k != "crops"}, indent=1))
    print("would fill:", json.dumps(values(res)))
