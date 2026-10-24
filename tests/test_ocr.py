"""Reading the slip (OCR) as a typing aid: what is filled in, what must be ticked, what is only shown, and that
no misread can reach a decision unseen.

Run:  python tests/test_ocr.py
The parsing and counter-flow checks need no OCR engine (a stand-in returns fixed readings). The last section
reads a printed synthetic slip with the real engine when it is installed, and is skipped otherwise.
"""
import http.client
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

import cv2

from scenarios import r, other_slip
import make_pdf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import counter_app as app  # noqa: E402
import ocr  # noqa: E402

ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not cond else ""))


TODAY = date(2026, 10, 24)


def line(text, conf=0.98, y=0):
    return {"text": text, "conf": conf, "box": (40, y, 600, y + 40)}


SEEN = {"dolo": {"650"}, "pan": {"40"}, "alprax": {"0.25"}, "metformin": {"500"}}   # dispensed here before


def parse(lines, known=("dolo", "pan", "alprax", "metformin", "metoprolol"), high=("alprax",), strengths=SEEN):
    return ocr.parse(lines, set(known), lambda t: t.split()[0].lower() in high, TODAY, strengths)


# ---- what counts as a medicine line, and what is filled in
res = parse([line("SYNTHETIC TEST CLINIC"), line("Dr. Asha Example, MBBS, MD"), line("Regn No: TN-45821"),
             line("Date:18/10/2026"), line("Name:R.KumarAge:34/M"),
             line("1. Tab. Dolo 650 mg 1-0-1 x 5 days"), line("2.Tab.Pan40mg 1-0-0"),
             line("3. Tab. Alprax 0.25 mg 0-0-1"), line("4. Syp. Ascoril LS 5 ml TDS", 0.97),
             line("JAMPY& DAJA 01", 0.9)])
texts = [d["text"] for d in res["drugs"]]
check("printed slip: medicines read as name + strength, spaces or not", texts == ["Dolo 650", "Pan 40", "Alprax 0.25",
                                                                                   "Ascoril LS 5 ml"], texts)
check("  Regn No, doctor (no qualifications) and date (day first) read",
      res["regn"]["value"] == "TN-45821" and res["doctor"]["value"] == "Dr. Asha Example"
      and res["rx_date"]["value"] == "2026-10-18", {k: res.get(k) for k in ("regn", "doctor", "rx_date")})
check("  a line without Tab/Cap and not a known name is ignored (letterhead, scribble at 0.9 confidence)",
      not any("JAMPY" in d["raw"] for d in res["drugs"] + res["unclear"]))
conf = {d["text"]: (d["confirm"], d["why"]) for d in res["drugs"]}
check("  a known routine medicine read clearly needs no tick", conf["Dolo 650"] == (False, ""), conf)
check("  a high-risk medicine always needs a tick", conf["Alprax 0.25"] == (True, "high-risk medicine"), conf)
check("  a clearly read name not on file is filled in, with a tick", conf["Ascoril LS 5 ml"][0]
      and "not a name this pharmacy has on file" in conf["Ascoril LS 5 ml"][1], conf)
check("  the Regn No always needs a tick (a misread digit can name another doctor -> RED)", res["regn"]["confirm"])
check("  doctor and date read clearly need none", not res["doctor"]["confirm"] and not res["rx_date"]["confirm"])

# ---- look-alikes are shown, never swapped for the nearest known name
res = parse([line("Tab. Metfornin 500 mg", 0.93), line("Tab. Pan D 40", 0.99), line("Tab. Dolo 650", 0.6),
             line("Tab. Dolo 650", 0.85, y=100)])
un = {u["text"]: u["why"] for u in res["unclear"]}
check("'Metfornin' (0.93, not on file) is shown as read, not filled in, not turned into Metformin",
      "Metfornin 500" in un and not any("metformin" in d["text"].lower() for d in res["drugs"]), (un, res["drugs"]))
check("'Pan D' is not the known 'Pan': whole names only", all(d["text"] != "Pan 40" for d in res["drugs"])
      and any(d["text"] == "Pan D 40" and not d["known"] for d in res["drugs"]), res["drugs"])
check("below FILL_MIN even a known name is only shown", un.get("Dolo 650", "").startswith("hard to read"), un)
check("a known name read with middling confidence is filled in with a tick",
      any(d["text"] == "Dolo 650" and d["confirm"] and "low confidence" in d["why"] for d in res["drugs"]), res["drugs"])
check("every shown or filled reading points at its part of the slip",
      all(0 <= x["crop"] < len(res["crops"]) for x in res["drugs"] + res["unclear"]))

# ---- the strength: whole numbers only, directions stripped, a strength new to this counter ticked
lines = ["2.Tab.Telma40mgafterfood", "3.TabAtorva10mgTDS", "TabZolfresh 10 mgafter food", "2. Tab Thyronorm 50 mcgSOS",
         "2.Cap Becosules 1-1-1", "Cap. Becosules x 5 days", "1.Tab.Ultracetafterfood", "4.Syp Ascoril LS 5 ml 1-0-1"]
got = [ocr.medicine({"text": t, "conf": 0.99, "box": (0, 0, 1, 1)}, set())["text"] for t in lines]
check("glued text and directions: the whole strength, never a dose or a duration",
      got == ["Telma 40", "Atorva 10", "Zolfresh 10", "Thyronorm 50 mcg", "Becosules", "Becosules", "Ultracet",
              "Ascoril LS 5 ml"], got)
res = parse([line("Tab. Dolo 65 mg"), line("Tab. Dolo 650 mg", y=100)])
why = [(d["text"], d["confirm"], d["why"]) for d in res["drugs"]]
check("a clear reading of a known name with a strength not dispensed here (650 -> 65) needs a tick",
      why == [("Dolo 65", True, "strength not dispensed here before for this medicine"), ("Dolo 650", False, "")], why)
check("  with no history of strengths given, every OCR strength needs a tick",
      all(d["confirm"] for d in parse([line("Tab. Dolo 650 mg")], strengths=None)["drugs"]))

# ---- dates and prescribers that cannot be pinned down are left empty
check("future date, impossible date, or two different dates -> no date",
      "rx_date" not in parse([line("Date: 30/10/2026")]) and "rx_date" not in parse([line("Date: 31/02/2026")])
      and "rx_date" not in parse([line("12/10/2026"), line("14/10/2026")]))
check("two-digit year and dot separators read", parse([line("Date 12.10.26")])["rx_date"]["value"] == "2026-10-12")
check("a dose like 1-0-1 is not a date", "rx_date" not in parse([line("Tab Dolo 650 1-0-1")]))
check("two doctors on the letterhead -> no doctor filled in",
      "doctor" not in parse([line("Dr. A Example, MBBS"), line("Dr. B Sample, MD")]))

# ---- what the form gets, what must be ticked, what the audit records
res = {"status": "done", "ms": 1200, "engine": "test",
       **parse([line("Regn No 73816"), line("Tab Dolo 650"), line("Tab Alprax 0.25"), line("Tab Mystery 5", 0.5)])}
check("values: filled fields, medicines joined", ocr.values(res) == {"regn": "73816", "drugs": "Dolo 650, Alprax 0.25"},
      ocr.values(res))
c = ocr.confirmations(res, {"drugs": "Dolo 650, Alprax 0.25", "regn": "73816"})
check("sent unchanged -> the high-risk medicine and the Regn No must be ticked",
      [x["item"] for x in c] == ["Alprax 0.25", "73816"], c)
c = ocr.confirmations(res, {"drugs": "Dolo 650, Alprax 0.5", "regn": "73818"})
check("corrected by staff -> nothing from the photo left to tick (the entry checks still apply)", c == [], c)
u = ocr.usage(res, {"drugs": "Dolo 650, Alprax 0.5, Mystery 5", "regn": "73816"})
check("usage: counts, plus per reading only field, band and flags - no text", u["offered"] == 3 and u["kept"] == 2
      and u["changed"] == 1 and u["unclear"] == 1 and u["readings"] == [
          {"field": "medicine", "band": "0.95+", "known": True, "high": False, "filled": True, "tick": False, "kept": True},
          {"field": "medicine", "band": "0.95+", "known": True, "high": True, "filled": True, "tick": True, "kept": False},
          {"field": "medicine", "band": "<0.80", "known": False, "high": False, "filled": False, "tick": False, "kept": True},
          {"field": "regn", "band": "0.95+", "known": None, "high": False, "filled": True, "tick": True, "kept": True}]
      and "Dolo" not in json.dumps(u) and "73816" not in json.dumps(u), u)
check("nothing pending/failed/off fills or asks anything; pending and failed are still counted",
      all(ocr.values(x) == {} and ocr.confirmations(x, {"regn": "1"}) == [] for x in
          (None, {"status": "off"}, {"status": "pending"}, {"status": "failed", "message": "x"}))
      and ocr.usage(None, {}) is None and ocr.usage({"status": "off", "why": "demo"}, {}) is None
      and ocr.usage({"status": "pending"}, {})["status"] == "pending"
      and ocr.usage({"status": "failed", "ms": 5}, {})["status"] == "failed")
rep = ocr.report([u, ocr.usage({"status": "pending"}, {}), ocr.usage({"status": "failed"}, {}),
                  dict(u, ms=3000)])
med = next(t for t in rep["table"] if t["field"] == "medicine" and t["band"] == "0.95+")
low = next(t for t in rep["table"] if t["field"] == "medicine" and t["band"] == "<0.80")
check("report: acceptance and correction per field and band; unclear readings staff typed as read",
      rep["checks"] == 4 and rep["done"] == 2 and rep["pending"] == 1 and rep["failed"] == 1
      and rep["median_ms"] == 3000 and med == {"field": "medicine", "band": "0.95+", "filled": 4, "kept": 2,
                                               "ticked": 2, "unclear": 0, "unclear_right": 0}
      and low["unclear"] == 2 and low["unclear_right"] == 2 and rep["table"][0]["field"] == "medicine", rep)
check("bands: PDF text kept apart from OCR confidence",
      [ocr.band(c, s) for c, s in ((1.0, "text"), (0.97, "ocr"), (0.92, "ocr"), (0.85, "ocr"), (0.5, "ocr"))]
      == ["PDF text", "0.95+", "0.90-0.95", "0.80-0.90", "<0.80"])


# ---------------------------------------------------------------- the counter, with a stand-in engine

class Client:
    def __init__(self, port):
        self.port, self.cookie, self.csrf = port, None, None

    def req(self, method, path, body=None, ctype=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)
        h = {"Cookie": f"rxs={self.cookie}"} if self.cookie else {}
        if ctype:
            h["Content-Type"] = ctype
        c.request(method, path, body=body, headers=h)
        res = c.getresponse()
        data = res.read()
        c.close()
        m = re.search(r"rxs=([^;]*)", res.getheader("Set-Cookie") or "")
        if m:
            self.cookie = m.group(1) or None
        ctype_out = res.getheader("Content-Type") or ""
        text = data.decode("utf-8", "replace") if "text" in ctype_out or "json" in ctype_out else ""
        t = re.search(r'name="csrf" value="([^"]+)"', text)
        if t:
            self.csrf = t.group(1)
        return res.status, res.getheader("Location"), text, data, ctype_out

    def post(self, path, fields):
        return self.req("POST", path, urlencode(dict(fields, csrf=self.csrf), doseq=True),
                        "application/x-www-form-urlencoded")

    def upload(self, img_bytes, name="slip.jpg", ctype="image/jpeg"):
        b = "----rx" + secrets.token_hex(8)
        body = (f"--{b}\r\nContent-Disposition: form-data; name=\"csrf\"\r\n\r\n{self.csrf}\r\n"
                f"--{b}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{name}\"\r\n"
                f"Content-Type: {ctype}\r\n\r\n").encode() + img_bytes + f"\r\n--{b}--\r\n".encode()
        return self.req("POST", "/upload", body, f"multipart/form-data; boundary={b}")


def wait_read(cl, cid, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        st, _, text, _, ctype = cl.req("GET", f"/case/{cid}/ocr")
        data = json.loads(text) if st == 200 else {}
        if data.get("status") != "pending":
            return st, ctype, data
        time.sleep(0.1)
    return None, None, {}


day = (date.today() - timedelta(days=3)).strftime("%d/%m/%Y")
SEGMENTS = [((40, 40, 700, 80), "Dr. Asha Example, MBBS", 0.99), ((40, 90, 400, 120), "Regn No: TN-45821", 0.98),
            ((500, 130, 760, 160), f"Date: {day}", 0.99), ((60, 300, 500, 340), "1. Tab. Dolo 650 mg 1-0-1", 0.99),
            ((60, 360, 500, 400), "2. Tab. Alprax 0.25 mg 0-0-1", 0.98),
            ((60, 420, 500, 460), "3. Tab. Metfornin 500", 0.91)]
calls = {"n": 0, "fail": False, "delay": 0.3, "segments": None}


def fake_segments(img):
    calls["n"] += 1
    time.sleep(calls["delay"])   # long enough for the form to be served while reading
    if calls["fail"]:
        raise RuntimeError("model file missing")
    return list(calls["segments"] or SEGMENTS)


real_available, real_segments = ocr.available, ocr.read_segments
ocr.available, ocr.read_segments = (lambda: True), fake_segments
os.environ["RXCHECK_OCR"] = "on"

with tempfile.TemporaryDirectory() as t:
    tmp = Path(t)
    r.LEDGER_DIR, r.DB, r.FEATURES, r.REPORTS = tmp, tmp / "rxcheck.db", tmp / "features", tmp / "reports"
    r.REGISTRY = tmp / "registry.json"
    r.REGISTRY.write_text("{}", encoding="utf-8")
    r.add_staff("pharmacist-a", "pharmacist", "111111")
    r.close_db()
    srv = app.make_server("127.0.0.1", 0, inbox=tmp / "inbox")
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ph = Client(port)
    ph.post("/login", {"name": "pharmacist-a", "pin": "111111"})
    ph.req("GET", "/")
    slip = cv2.imencode(".jpg", other_slip(seed=7), [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes()

    st, loc, *_ = ph.upload(slip)
    cid = loc.split("/")[-1]
    st, _, page, *_ = ph.req("GET", loc)
    check("the form opens at once while the slip is read: state 'Reading'", st == 200 and "data-ocr-pending" in page
          and 'data-ocr-state="reading"' in page and ">Reading</span>" in page and 'value="Dolo' not in page)
    st, ctype, data = wait_read(ph, cid)
    check("reading done: status, values and the summary panel as JSON", st == 200 and "json" in ctype
          and data["status"] == "done" and data["values"]["drugs"] == "Dolo 650, Alprax 0.25"
          and data["values"]["regn"] == "TN-45821" and "Read from the slip" in data["panel"], data)
    check("  state 'Needs confirmation (3)': what was filled in will need ticks if sent unchanged",
          'data-ocr-state="confirm"' in data["panel"] and "Needs confirmation (3)" in data["panel"], data["panel"][:300])
    check("  'Metfornin' is shown with its crop, not filled in",
          "Metfornin 500" in data["panel"] and "Metfornin" not in data["values"]["drugs"]
          and re.search(r'src="/case/[^"]+/crop/\d+"', data["panel"]))
    st, _, page, *_ = ph.req("GET", f"/case/{cid}")
    check("reloaded form: empty fields filled from the photo", 'value="Dolo 650, Alprax 0.25"' in page
          and 'value="TN-45821"' in page and "data-ocr-pending" not in page)
    crops = re.findall(r'src="(/case/[^"]+/crop/\d+)"', page)
    st, _, _, img, ctype = ph.req("GET", crops[0])
    check("crop of the slip served as a JPEG", st == 200 and ctype == "image/jpeg" and img[:2] == b"\xff\xd8")
    check("  a crop that does not exist -> 404", ph.req("GET", f"/case/{cid}/crop/99")[0] == 404
          and ph.req("GET", f"/case/{cid}/crop/x")[0] == 404)
    sent = {"drugs": "Dolo 650, Alprax 0.25", "regn": "TN-45821", "doctor": "Dr. Asha Example",
            "rx_date": (date.today() - timedelta(days=3)).isoformat()}
    st, _, page, *_ = ph.post(f"/case/{cid}/check", sent)
    ticks = page.count('name="confirm"')
    check("sent with ticks missing -> state 'Check blocked'", 'data-ocr-state="blocked"' in page
          and ">Check blocked</span>" in page and "Tick the 3 item(s)" in page)
    check("sent unchanged -> ticks for the name not on file (new counter: no Dolo yet), the high-risk medicine and "
          "the Regn No, each beside its crop",
          st == 200 and ticks == 3 and "not a name this pharmacy has on file" in page and "high-risk medicine" in page
          and "check every character against the slip" in page and page.count('class="crop"') >= 3, ticks)
    st, loc, *_ = ph.post(f"/case/{cid}/check", dict(sent, confirm=["0", "1"]))
    check("  two ticks of three are not enough", st == 200)
    st, loc, *_ = ph.post(f"/case/{cid}/check", dict(sent, confirm=["0", "1", "2"]))
    check("  all ticked -> decision", st == 303 and loc == f"/case/{cid}")
    rows = {a: json.loads(d) for a, d in r.db().execute("SELECT action, detail FROM audit WHERE ref = ?", (cid,))}
    check("audit: the ticked photo readings, and the counts of what was kept",
          any("read from the photo" in i["issue"] for i in rows["entry-confirmed"]["issues"])
          and {k: rows["ocr-use"][k] for k in ("status", "offered", "kept", "changed", "unclear", "engine")}
          == {"status": "done", "offered": 5, "kept": 5, "changed": 0, "unclear": 1, "engine": "rapidocr"}
          and len(rows["ocr-use"]["readings"]) == 6, rows.get("ocr-use"))
    check("  the audit line is readable", "5 value(s) filled in from the photo, 5 kept" in app.audit_summary(
        {"action": "ocr-use", "detail": json.dumps(rows["ocr-use"])}))

    # once dispensed and recorded here, the name is on file: a clear reading of it needs no tick
    st, loc, *_ = ph.upload(slip)
    cid2 = loc.split("/")[-1]
    st, _, data = wait_read(ph, cid2)
    check("a name entered at this counter before is on file: no tick for 'Dolo 650' next time",
          [x["item"] for x in ocr.confirmations(json.loads((r.LEDGER_DIR / "uploads" / f"{cid2}.json").read_text(
              encoding="utf-8"))["ocr"], sent)] == ["Alprax 0.25", "TN-45821"])

    # corrected entries: nothing from the photo to tick; the decision uses what staff sent
    st, loc, *_ = ph.post(f"/case/{cid2}/check", {"drugs": "Dolo 650", "regn": "TN-45824"})
    check("staff corrected the Regn No and dropped a medicine -> no tick, straight to the decision",
          st == 303, st)
    use = json.loads(r.db().execute("SELECT detail FROM audit WHERE ref = ? AND action = 'ocr-use'",
                                    (cid2,)).fetchone()[0])
    check("  counted as corrected", use["changed"] == 4 and use["kept"] == 1, use)
    final = json.loads((r.LEDGER_DIR / "uploads" / f"{cid2}.json").read_text(encoding="utf-8"))
    check("  the check ran on what staff sent, not on the reading", final["fields"]["regn"] == "TN-45824"
          and final["fields"]["drugs"] == "Dolo 650")

    # a failed reading changes nothing but the note
    calls["fail"] = True
    st, loc, *_ = ph.upload(slip)
    cid3 = loc.split("/")[-1]
    st, _, data = wait_read(ph, cid3)
    check("engine failure -> 'could not be read', form empty, check still works",
          data.get("status") == "failed" and data["values"] == {} and "could not be read" in data["panel"]
          and ph.post(f"/case/{cid3}/check", {"drugs": "Dolo 650"})[0] == 303, data)
    calls["fail"] = False

    # 'Ready': everything filled in is on file and clear, nothing to tick
    calls["segments"] = [((60, 300, 500, 340), "1. Tab. Dolo 650 mg 1-0-1", 0.99)]
    st, loc, *_ = ph.upload(slip)
    st, _, data = wait_read(ph, loc.split("/")[-1])
    check("state 'Ready' when nothing filled in needs a tick", 'data-ocr-state="ready"' in data["panel"]
          and ">Ready</span>" in data["panel"] and data["values"] == {"drugs": "Dolo 650"}, data["panel"][:200])
    calls["segments"] = None

    # sent before the reading finished: counted as such, nothing from the photo asked
    calls["delay"] = 2.0
    st, loc, *_ = ph.upload(slip)
    cid4 = loc.split("/")[-1]
    st, loc, *_ = ph.post(f"/case/{cid4}/check", {"drugs": "Dolo 650"})
    use = json.loads(r.db().execute("SELECT detail FROM audit WHERE ref = ? AND action = 'ocr-use'", (cid4,)).fetchone()[0])
    check("form sent while still reading -> decision as typed; audit counts it as 'pending'", st == 303
          and use["status"] == "pending", use)
    time.sleep(2.2)
    calls["delay"] = 0.3

    # PDFs: a text layer is read without the engine; scanned pages go through OCR; bad files are refused clearly
    n = calls["n"]
    pdf = make_pdf.text_pdf([[(60, 60, 20, "SYNTHETIC TEST CLINIC"), (60, 100, 12, "Dr. Asha Example, MBBS"),
                              (60, 118, 12, "Regn No: TN-45821"), (400, 140, 12, f"Date: {day}")],
                             [(60, 80, 12, "1. Tab. Dolo 650 mg 1-0-1"), (60, 110, 12, "2. Tab. Alprax 0.25 mg 0-0-1")]])
    st, loc, *_ = ph.upload(pdf, "rx.pdf", "application/pdf")
    cidp = loc.split("/")[-1]
    st, _, data = wait_read(ph, cidp)
    check("2-page PDF with a text layer: read from its text, medicines from page 2, no OCR engine call",
          st == 200 and data["values"].get("drugs") == "Dolo 650, Alprax 0.25" and data["values"].get("regn") == "TN-45821"
          and calls["n"] == n and "(2 pages)" in data["panel"], data)
    case = json.loads((r.LEDGER_DIR / "uploads" / f"{cidp}.json").read_text(encoding="utf-8"))
    p2 = next(i for i, c in enumerate(case["ocr"]["crops"]) if c["page"] == 1)
    st, _, _, img, ctype = ph.req("GET", f"/case/{cidp}/crop/{p2}")
    form = ph.req("GET", f"/case/{cidp}")[2]
    check("  crop from page 2 served; page 2 shown and served; the form says checks used page 1",
          st == 200 and img[:2] == b"\xff\xd8" and ph.req("GET", f"/case/{cidp}/page/2")[0] == 200
          and ph.req("GET", f"/case/{cidp}/page/3")[0] == 404 and "PDF, 2 pages" in form
          and f'src="/case/{cidp}/page/2"' in form)
    check("  the band for PDF text is its own", all(x["src"] == "text" for x in case["ocr"]["drugs"])
          and ocr.usage(case["ocr"], {"drugs": "Dolo 650"})["readings"][0]["band"] == "PDF text")
    st, loc, *_ = ph.post(f"/case/{cidp}/check", {"drugs": "Dolo 650", "regn": "TN-45824"})
    check("  the PDF case goes through the check like a photo", st == 303)
    from PIL import Image
    pages = [Image.fromarray(cv2.cvtColor(other_slip(seed=s), cv2.COLOR_BGR2RGB)) for s in (8, 9)]
    st, loc, *_ = ph.upload(make_pdf.image_pdf(pages), "scan.pdf", "application/pdf")
    st, _, data = wait_read(ph, loc.split("/")[-1])
    check("scanned 2-page PDF (no text layer): each page read by OCR", data.get("status") == "done"
          and calls["n"] == n + 2, (calls["n"] - n, data.get("status")))
    for bad, why in ((make_pdf.locked_pdf(), "password-protected"), (b"%PDF-1.4 not really", "cannot be opened"),
                     (make_pdf.text_pdf([[(60, 60, 12, "page")]] * 7), "at most 6")):
        if bad is None:
            print("[SKIP] password-protected PDF: pypdf not installed")
            continue
        st, _, text, *_ = ph.upload(bad, "x.pdf", "application/pdf")
        check(f"bad PDF refused with a clear message: {why}", st == 400 and why in text, (st, text[:200]))

    # the pilot report: pharmacists only; CSV of every reading, no content; the export is audited
    r.add_staff("staff-a", "staff", "222222")
    sa = Client(port)
    sa.post("/login", {"name": "staff-a", "pin": "222222"})
    check("photo reading report: staff cannot open it", sa.req("GET", "/ocr-report")[0] == 403)
    st, _, page, *_ = ph.req("GET", "/ocr-report?days=14")
    check("  pharmacist sees checks, acceptance per field and band", st == 200 and "Kept as read (acceptance)" in page
          and '<th scope="row">Medicine</th>' in page and "PDF text" in page and "sent while still reading" in page)
    st, _, text, data, ctype = ph.req("GET", "/ocr-report.csv?days=14")
    lines = text.lstrip("\ufeff").strip().splitlines()
    n_read = sum(len(json.loads(d).get("readings") or [None]) for (d,) in
                 r.db().execute("SELECT detail FROM audit WHERE action = 'ocr-use'"))
    check("  CSV: one row per reading, no slip text; the export is in the audit trail",
          st == 200 and "csv" in ctype and lines[0].startswith("at,actor,case,status") and len(lines) - 1 == n_read
          and "Dolo" not in text and "TN-45821" not in text
          and r.db().execute("SELECT COUNT(*) FROM audit WHERE action = 'audit-export' AND detail LIKE '%ocr-report%'"
                             ).fetchone()[0] == 1, (len(lines), n_read))

    # 'OCR disabled', and why
    n = calls["n"]
    os.environ["RXCHECK_OCR"] = "off"
    st, loc, *_ = ph.upload(slip)
    st, _, page, *_ = ph.req("GET", loc)
    check("switched off -> state 'OCR disabled', says why, nothing read", 'data-ocr-state="disabled"' in page
          and ">OCR disabled</span>" in page and "switched off on this counter" in page and calls["n"] == n
          and json.loads(ph.req("GET", f"{loc}/ocr")[2])["status"] == "off")
    os.environ["RXCHECK_OCR"] = "on"
    ocr.available = lambda: False
    st, loc, *_ = ph.upload(slip)
    st, _, page, *_ = ph.req("GET", loc)
    time.sleep(0.4)
    check("not installed -> 'OCR disabled', 'not installed on this counter'", 'data-ocr-state="disabled"' in page
          and "not installed on this counter" in page and calls["n"] == n)
    st, loc, *_ = ph.upload(pdf, "rx.pdf", "application/pdf")
    st, _, data = wait_read(ph, loc.split("/")[-1])
    check("  ...a PDF with a text layer is still read (no engine needed)",
          data["values"].get("drugs") == "Dolo 650, Alprax 0.25", data)
    srv.shutdown()
    r.close_db()

ocr.available, ocr.read_segments = real_available, real_segments

# ---------------------------------------------------------------- the real engine on a printed slip
FONT = Path("C:/Windows/Fonts/arial.ttf")
if not FONT.is_file():
    FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
try:
    import rapidocr_onnxruntime  # noqa: F401
    have_engine = True
except Exception:
    have_engine = False
if have_engine and FONT.is_file():
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGB", (1240, 1754), "white")
    d = ImageDraw.Draw(im)
    d.text((80, 60), "SYNTHETIC TEST CLINIC", font=ImageFont.truetype(str(FONT), 54), fill="black")
    f = ImageFont.truetype(str(FONT), 32)
    d.text((80, 140), "Dr. Asha Example, MBBS, MD", font=f, fill="black")
    d.text((80, 190), "Regn No: TN-45821", font=f, fill="black")
    d.text((880, 260), f"Date: {day}", font=f, fill="black")
    for i, s in enumerate(["1. Tab. Dolo 650 mg 1-0-1 x 5 days", "2. Tab. Alprax 0.25 mg 0-0-1 x 7 days"]):
        d.text((120, 520 + 90 * i), s, font=f, fill="black")
    d.text((80, 1600), "NOT A REAL PRESCRIPTION", font=f, fill="black")
    with tempfile.TemporaryDirectory() as t:
        p = Path(t) / "printed.jpg"
        im.save(p, quality=85)
        res = ocr.read_slip(p, {"dolo", "alprax"}, lambda s: s.lower().startswith("alprax"))
    v = ocr.values(res)
    check(f"real engine, printed slip: medicines, Regn No, doctor and date read ({res.get('ms')} ms)",
          v.get("drugs") == "Dolo 650, Alprax 0.25" and v.get("regn") == "TN-45821"
          and v.get("doctor") == "Dr. Asha Example" and v.get("rx_date") == (date.today() - timedelta(days=3)).isoformat(),
          v or res)
else:
    print("[SKIP] real-engine read: rapidocr_onnxruntime not installed (optional) or no test font")

# the public demo replays a recorded reading of demo/printed.jpg: it must be what the engine reads from that image
if have_engine:
    demo = json.loads((ROOT / "demo" / "samples.json").read_text(encoding="utf-8"))
    rec = next(s for s in demo["samples"] if s["key"] == "printed")["reading"]["segments"]
    fresh = ocr.read_segments(ocr.load(ROOT / "demo" / "printed.jpg"))
    check("the demo's recorded reading is what the engine reads from demo/printed.jpg (else: make_demo_samples.py)",
          [x["text"] for x in rec] == [t for _, t, _ in fresh]
          and all(abs(x["conf"] - c) < 0.01 for x, (_, _, c) in zip(rec, fresh)))

print("\nALL PASS" if ok else "\nSOME CHECKS FAILED")
sys.exit(0 if ok else 1)
