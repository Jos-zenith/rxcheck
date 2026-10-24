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
   high-risk medicine, a name not on file, a strength not dispensed here before for that medicine (a confident
   reading can still drop a digit: 500 -> 50), a Regn No (a misread digit can name another doctor, and that
   is RED), or read with a confidence below CONFIRM_ABOVE.
3. OCR never overwrites what staff typed, a clinic-signed code, or a demo prefill.

The thresholds are placeholders, not measured values: the counter records how often OCR values were kept
or corrected (audit `ocr-use`, counts only), and the shadow pilot sets them from that.

Engine: RapidOCR (PP-OCR models on onnxruntime), optional. Without it the counter works as before.
    pip install onnxruntime==1.24.4 pyclipper==1.4.0 shapely==2.2.0 PyYAML==6.0.3 six==1.17.0
    pip install --no-deps rapidocr_onnxruntime==1.4.4
(--no-deps: it asks for opencv-python, which would sit beside the headless OpenCV the checks were tuned on.)
Switch off with RXCHECK_OCR=off.

Try it on one image:  python app/slip_reader.py slip.jpg
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
NUMBERING = r"^\s*(?:\d{1,2}\s*[.)]\s*)?"
# 'Tab. Dolo', 'Tab Dolo', and - OCR drops spaces - 'TabDolo' (only for the longer forms, before a capital)
MED_LINE = re.compile(NUMBERING + rf"{FORM}(?:\b\s*\.?\s*|(?<=[a-z]{{3}})(?=(?-i:[A-Z])))(?P<rest>[A-Za-z].*)$", re.I)
# the strength: the whole number, never a part of it - '40mgafterfood' must not become 4 - and a unit that may
# be glued to what follows ('mgTDS'); 'g' / 'iu' / '%' only standing alone
NAME_DOSE = re.compile(r"^(?P<name>[A-Za-z][A-Za-z\- ]*?)\s*(?P<num>\d+(?:\.\d+)?)(?![\d.])\s*"
                       r"(?P<unit>mcg|mg|ml|µg|(?:g|iu|%)(?![A-Za-z]))?", re.I)
NAME_ONLY = re.compile(r"^(?P<name>[A-Za-z][A-Za-z\-]*)")
# what comes after the medicine on the line and is not part of it - stripped before the name and strength are read
DIRECTIONS = [
    re.compile(r"\s*\d\s*[-–]\s*\d\s*[-–]\s*\d.*$"),                                   # 1-0-1 and all after
    re.compile(r"\s*x?\s*\d+\s*(?:days?|weeks?|months?|d)\b.*$", re.I),                    # x 5 days
    re.compile(r"\s*(?:after|before)\s*(?:food|meals?|breakfast|dinner)\b.*$", re.I),
    re.compile(r"\s*(?:at\s*night|empty\s*stomach|(?:once|twice|thrice)\s*(?:a\s*)?daily|daily)\b.*$", re.I),
    re.compile(r"(?<=[a-z0-9\s.])\s*(?:BD|BID|TDS|TID|QID|OD|QD|HS|SOS|STAT|PRN)\s*$"),       # glued or not
    re.compile(r"\s+x\s*$", re.I),
]


def strip_directions(rest):
    """'Telma40mgafterfood' -> 'Telma40mg'; 'Becosules 1-1-1' -> 'Becosules'; 'UltracetTDS' -> 'Ultracet'."""
    prev = None
    while prev != rest:
        prev = rest
        for rx in DIRECTIONS:
            rest = rx.sub("", rest)
    return rest.strip()
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
    rest = strip_directions((m.group("rest") if m else re.sub(NUMBERING, "", line["text"])).strip())
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
            "known": bool(hit), "strength": bool(num), "name": name, "num": num}


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


def parse(lines, known, is_high=lambda text: False, today=None, strengths=None):
    """Turn OCR lines into suggestions for the counter form.
    known: lower-case medicine names this pharmacy knows. is_high: text -> True for a high-risk medicine.
    strengths: {name: {'650', ...}} strengths dispensed here before. An OCR-read strength that is not among them
    needs a tick: a clear, confident reading can still drop a digit (500 -> 50), and that is a tenfold dose."""
    strengths = {_norm(k): set(v) for k, v in (strengths or {}).items()}
    today = today or date.today()
    known = {_norm(k) for k in known if k}
    crops, drugs, unclear = [], [], []

    def crop(ln):
        crops.append({"page": ln.get("page", 0), "box": list(ln["box"])})
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
        med.pop("box")
        med["crop"], med["src"] = crop(ln), ln.get("src", "ocr")
        if med["conf"] < FILL_MIN:
            unclear.append({**med, "why": "hard to read"})
        elif not med["known"] and med["conf"] < CONFIRM_ABOVE:
            unclear.append({**med, "why": "name not on file, unclear"})
        else:
            new_strength = (med["src"] == "ocr" and med["strength"]
                            and med["num"] not in strengths.get(_norm(med["name"]), set()))
            why = ("high-risk medicine" if med["high"] else "name not on file" if not med["known"]
                   else "no strength read" if not med["strength"]
                   else "strength new here" if new_strength
                   else "low confidence" if med["conf"] < CONFIRM_ABOVE else "")
            drugs.append({**med, "confirm": bool(why), "why": why})

    out = {"drugs": drugs, "unclear": unclear, "crops": crops}
    for key, found, always in (("regn", regns, True), ("doctor", doctors, False), ("rx_date", dates, False)):
        best = single(found) if found else None
        if best and best[1]["conf"] >= FILL_MIN:
            value, ln = best
            out[key] = {"value": value, "conf": round(ln["conf"], 3), "crop": crop(ln), "src": ln.get("src", "ocr"),
                        "confirm": always or ln["conf"] < CONFIRM_ABOVE}
    return out


def read_pages(pages, known, is_high=lambda text: False, today=None, strengths=None):
    """Read every page of one prescription: pages = [{'path', 'lines'?}], 'lines' being a PDF page's text layer
    (pdf_pages.text_lines), used instead of OCR. Returns {'status': 'done', 'ms', 'engine', 'pages', 'drugs',
    'unclear', 'crops', 'regn'?, ...} or {'status': 'failed', 'message'}. Never raises: a failed read only
    means staff type as before. A page that needs OCR while the engine is missing is listed in 'unread'."""
    t0 = time.time()
    lines, engines, unread = [], set(), []
    try:
        for i, pg in enumerate(pages):
            if pg.get("lines"):
                segs, src = [(tuple(ln["box"]), ln["text"], ln["conf"]) for ln in pg["lines"]], "text"
            elif pg.get("recorded"):   # the public demo: a reading the engine made earlier, replayed
                segs, src = [(tuple(x["box"]), x["text"], x["conf"]) for x in pg["recorded"]["segments"]], "ocr"
                engines.add("recorded")
            elif available():
                segs, src = read_segments(load(pg["path"])), "ocr"
            else:
                unread.append(i + 1)
                continue
            engines.add("pdf-text" if src == "text" else "recorded" if pg.get("recorded") else "rapidocr")
            lines += [{**ln, "page": i, "src": src} for ln in group_lines(segs)]
        res = parse(lines, known, is_high, today, strengths)
    except Exception as ex:   # the engine, a model file, memory: none of it may stop a check
        return {"status": "failed", "message": f"{type(ex).__name__}: {ex}"[:200], "ms": int((time.time() - t0) * 1000)}
    return {"status": "done", "engine": "+".join(sorted(engines)) or "none", "ms": int((time.time() - t0) * 1000),
            "pages": len(pages), "unread": unread, **res}


def read_slip(path, known, is_high=lambda text: False, today=None, strengths=None):
    """OCR one prescription image (see read_pages)."""
    return read_pages([{"path": path}], known, is_high, today, strengths)


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
                        "issue": f"read from slip · {d['why']}"})
    labels = {"regn": "read from slip · check every character",
              "doctor": "read from slip · doctor's name",
              "rx_date": "read from slip · day and month"}
    for key, issue in labels.items():
        r = ocr.get(key)
        if r and r["confirm"] and _norm(fields.get(key)) == _norm(r["value"]):
            out.append({"item": r["value"], "crop": r["crop"], "issue": issue})
    return out


def band(conf, src="ocr"):
    """Confidence band for the pilot report: the thresholds are set from acceptance per band."""
    if src == "text":
        return "PDF text"
    return "0.95+" if conf >= 0.95 else "0.90-0.95" if conf >= 0.90 else "0.80-0.90" if conf >= FILL_MIN else "<0.80"


def usage(ocr, fields):
    """What the audit trail keeps about one check's photo reading - no content, nothing a patient can be found
    by: counts, and per reading its field, confidence band, whether it was filled in, needed a tick, and was
    kept as read. None when the reading was not used at all (switched off, not installed, demo, retake)."""
    st = (ocr or {}).get("status")
    if st in ("pending", "failed"):
        return {"status": st, "offered": 0, "kept": 0, "changed": 0, "unclear": 0, "ms": (ocr or {}).get("ms"),
                "engine": None, "readings": []}
    if st != "done":
        return None
    sent = _items(fields.get("drugs"))
    readings = []
    for d in ocr.get("drugs", []) + ocr.get("unclear", []):
        filled = d in ocr.get("drugs", [])
        readings.append({"field": "medicine", "band": band(d["conf"], d.get("src", "ocr")), "known": d["known"],
                         "high": d["high"], "filled": filled, "tick": bool(d.get("confirm")),
                         "kept": _norm(d["text"]) in sent})
    for key in ("regn", "doctor", "rx_date"):
        r = ocr.get(key)
        if r:
            readings.append({"field": key, "band": band(r["conf"], r.get("src", "ocr")), "known": None, "high": False,
                             "filled": True, "tick": bool(r["confirm"]),
                             "kept": _norm(fields.get(key)) == _norm(r["value"])})
    offered = [x["kept"] for x in readings if x["filled"]]
    return {"status": "done", "offered": len(offered), "kept": sum(offered), "changed": len(offered) - sum(offered),
            "unclear": len(ocr.get("unclear", [])), "ms": ocr.get("ms"), "engine": ocr.get("engine"),
            "readings": readings}


FIELD_ORDER = {"medicine": 0, "regn": 1, "doctor": 2, "rx_date": 3}
BAND_ORDER = {"PDF text": 0, "0.95+": 1, "0.90-0.95": 2, "0.80-0.90": 3, "<0.80": 4}


def report(details):
    """Pilot figures from audit 'ocr-use' details. Per field and confidence band: values filled in, how many
    staff kept as read (acceptance) or changed (correction), how many needed a tick; and for unclear readings
    (not filled in), how often staff typed exactly what was read - the reader was right but not trusted."""
    status = {"done": 0, "pending": 0, "failed": 0}
    ms, groups = [], {}
    for d in details:
        st = d.get("status", "done")
        status[st] = status.get(st, 0) + 1
        if st == "done" and d.get("ms"):
            ms.append(d["ms"])
        for x in d.get("readings", []):
            g = groups.setdefault((x["field"], x["band"]), {"filled": 0, "kept": 0, "ticked": 0, "unclear": 0,
                                                            "unclear_right": 0})
            if x["filled"]:
                g["filled"] += 1
                g["kept"] += bool(x["kept"])
                g["ticked"] += bool(x["tick"])
            else:
                g["unclear"] += 1
                g["unclear_right"] += bool(x["kept"])
    table = [{"field": f, "band": b, **g} for (f, b), g in
             sorted(groups.items(), key=lambda kv: (FIELD_ORDER.get(kv[0][0], 9), BAND_ORDER.get(kv[0][1], 9)))]
    ms.sort()
    return {"checks": len(details), **status, "median_ms": ms[len(ms) // 2] if ms else None, "table": table}


if __name__ == "__main__":
    import json
    if len(sys.argv) != 2:
        sys.exit("usage: python app/slip_reader.py IMAGE_OR_PDF")
    if not enabled():
        sys.exit("OCR is not available: install it (see the top of app/slip_reader.py) or unset RXCHECK_OCR=off")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import rxcheck as r
    table = r.drug_table() or r.EMPTY_TABLE
    path = sys.argv[1]
    if path.lower().endswith(".pdf"):
        import tempfile
        import pdf_pages
        tmp = tempfile.mkdtemp()
        pages = []
        for i, (jpg, lines) in enumerate(pdf_pages.pages(open(path, "rb").read())):
            pages.append({"path": os.path.join(tmp, f"p{i + 1}.jpg"), "lines": lines})
            open(pages[-1]["path"], "wb").write(jpg)
    else:
        pages = [{"path": path}]
    res = read_pages(pages, set(table["brands"]) | set(table["molecules"]),
                    lambda t: r.drug_risk(t)[0] == "high")
    print(json.dumps({k: v for k, v in res.items() if k != "crops"}, indent=1))
    print("would fill:", json.dumps(values(res)))
