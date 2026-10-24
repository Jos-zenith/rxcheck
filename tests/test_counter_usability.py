"""Counter app ergonomics: colour contrast (WCAG AAA) in every theme, display preferences, keyboard shortcuts,
lookup fields, who-did-what on the counter screens, and the audit log for compliance review.

Run:  python tests/test_counter_usability.py
"""
import csv
import http.client
import io
import json
import re
import secrets
import sys
import tempfile
import threading
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode, quote

import cv2

from synthetic_slips import r, load_samples, fixture_path, whatsapp, mirrored_body

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import counter_app as app  # noqa: E402

ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not cond else ""))


# ---- contrast: every text colour on every background it is used on, in all four themes
def lum(h):
    h = h.lstrip("#")
    h = "".join(c * 2 for c in h) if len(h) == 3 else h
    c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def ratio(a, b):
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def tokens(block):
    return dict(re.findall(r"--([\w-]+):(#[0-9a-fA-F]{3,6})", block))


css = app.CSS
light = tokens(re.search(r"^:root\{(.*?)\}", css, re.S | re.M).group(1))
dark = {**light, **tokens(re.search(r"@media \(prefers-color-scheme:dark\)\{:root\{(.*?)\}\}", css, re.S).group(1))}
hc = {**light, **tokens(re.search(r"^html\[data-contrast=high\]\{(.*?)\}", css, re.S | re.M).group(1))}
hc_dark = {**hc, **tokens(re.search(r"\{html\[data-contrast=high\]\{(.*?)\}\}", css, re.S).group(1))}
os_hc = tokens(re.search(r"@media \(prefers-contrast:more\)\{html:not\(\[data-contrast=std\]\)\{(.*?)\}\}", css, re.S).group(1))
os_hc_dark = tokens(re.search(r"and \(prefers-color-scheme:dark\)\{html:not\(\[data-contrast=std\]\)\{(.*?)\}\}", css,
                              re.S).group(1))
TEXT = [("ink", "bg"), ("ink", "card"), ("muted", "bg"), ("muted", "card"), ("accent", "card"), ("accent", "bg"),
        ("card", "accent"), ("red", "card"), ("red", "bg"), ("red", "red-bg"), ("amber", "amber-bg"),
        ("green", "green-bg"), ("ink", "amber-bg"), ("ink", "red-bg"), ("ink", "green-bg"), ("amber", "card"),
        ("green", "card")]
for name, t in (("standard light", light), ("standard dark", dark), ("high contrast light", hc),
                ("high contrast dark", hc_dark)):
    worst = min((ratio(t[a], t[b]), f"{a} on {b}") for a, b in TEXT)
    check(f"{name}: all text at least 7:1 (WCAG AAA)", worst[0] >= 7, f"{worst[1]} {worst[0]:.2f}:1")
    check(f"  {name}: focus ring and field borders at least 3:1", min(ratio(t["focus"], t["bg"]),
          ratio(t["focus"], t["card"]), ratio(t["muted"], t["bg"])) >= 3)
check("OS 'increase contrast' gets exactly the high-contrast palette", os_hc == tokens(
    re.search(r"^html\[data-contrast=high\]\{(.*?)\}", css, re.S | re.M).group(1)) and os_hc_dark == tokens(
    re.search(r"\{html\[data-contrast=high\]\{(.*?)\}\}", css, re.S).group(1)))
check("text size scales the whole page (rem), up to 150%", "font:1rem/1.5" in css and 'data-text="3"]{font-size:150%}' in css)

# ---- the Vue bundle: served from this PC, nothing that needs 'unsafe-eval'
js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
check("bundle built into static/ (web/ -> npm run build)", len(js) > 10_000 and (ROOT / "static" / "app.css").is_file())
check("bundle has no eval / new Function (CSP stays script-src 'self')", "new Function" not in js
      and not re.search(r"\beval\(", js))
check("bundle loads nothing from another host (only XML namespaces and Vue's error-reference text)",
      not re.search(r"https?://(?!www\.w3\.org/|vuejs\.org/error-reference/)", js))


class Client:
    def __init__(self, port):
        self.port, self.cookie, self.csrf, self.extra = port, None, None, ""

    def req(self, method, path, body=None, ctype=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)
        h = dict(headers or {})
        cookies = "; ".join(x for x in (f"rxs={self.cookie}" if self.cookie else "", self.extra) if x)
        if cookies:
            h["Cookie"] = cookies
        if ctype:
            h["Content-Type"] = ctype
        c.request(method, path, body=body, headers=h)
        res = c.getresponse()
        data = res.read()
        c.close()
        m = re.search(r"rxs=([^;]*)", res.getheader("Set-Cookie") or "")
        if m:
            self.cookie = m.group(1) or None
        text = data.decode("utf-8", "replace")
        t = re.search(r'name="csrf" value="([^"]+)"', text)
        if t:
            self.csrf = t.group(1)
        return res, text

    def get(self, path, **kw):
        return self.req("GET", path, **kw)

    def json(self, path):
        res, text = self.get(path)
        return res.status, json.loads(text)

    def post(self, path, fields):
        return self.req("POST", path, urlencode(dict(fields, csrf=self.csrf), doseq=True),
                        "application/x-www-form-urlencoded")

    def login(self, name, pin):
        res, _ = self.req("POST", "/login", urlencode({"name": name, "pin": pin}), "application/x-www-form-urlencoded")
        self.get("/")
        return res.status

    def upload(self, img_bytes):
        b = "----rx" + secrets.token_hex(8)
        body = (f"--{b}\r\nContent-Disposition: form-data; name=\"csrf\"\r\n\r\n{self.csrf}\r\n"
                f"--{b}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"slip.jpg\"\r\n"
                f"Content-Type: image/jpeg\r\n\r\n").encode() + img_bytes + f"\r\n--{b}--\r\n".encode()
        return self.req("POST", "/upload", body, f"multipart/form-data; boundary={b}")


def jpg(img):
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()


orig, _ = load_samples()

with tempfile.TemporaryDirectory() as t:
    tmp = Path(t)
    r.LEDGER_DIR, r.DB, r.FEATURES, r.REPORTS = tmp, tmp / "rxcheck.db", tmp / "features", tmp / "reports"
    r.REGISTRY = tmp / "registry.json"
    r.REGISTRY.write_text(json.dumps({"00000": {"name": "Dr. Example", "source": "test fixture",
                                                "verified_on": date.today().isoformat()}}), encoding="utf-8")
    r.add_entry(fixture_path(), "RX-TEST-001", "2025-01-15", "00000",
                "Sample Patient", "Dolo 650, Pan 40, Alprax 0.25")
    r.add_staff("pharmacist-a", "pharmacist", "111111")
    r.add_staff("staff-a", "staff", "222222")
    r.close_db()
    srv = app.make_server("127.0.0.1", 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # ---- static bundle and headers
    anon = Client(port)
    res, _ = anon.get("/static/app.js")
    tag = res.getheader("ETag")
    check("bundle served before sign-in (the sign-in page has the display controls too)",
          res.status == 200 and res.getheader("Content-Type").startswith("text/javascript") and tag)
    res, _ = anon.get("/static/app.js", headers={"If-None-Match": tag})
    check("  revalidated by ETag: 304, no re-download", res.status == 304)
    for p in ("/static/../rxcheck.py", "/static/rxcheck.py", "/static/..%2Frxcheck.py", "/static/"):
        check(f"  nothing else under /static: {p}", anon.get(p)[0].status == 404)
    res, page = anon.get("/login")
    csp = res.getheader("Content-Security-Policy")
    check("CSP: scripts and fetches from this server only, nothing inline or eval'd",
          "script-src 'self'" in csp and "connect-src 'self'" in csp and "unsafe" not in csp and "default-src 'none'" in csp)
    check("every page: module script, display controls slot, skip link to the main content",
          '<script type="module" src="/static/app.js">' in page and 'id="rx-display"' in page
          and 'href="#main"' in page and '<main id="main"' in page)

    # ---- display preferences are rendered by the server: right size and contrast on first paint
    anon.extra = "rxui=t2-hc"
    check("cookie rxui=t2-hc -> <html data-text=2 data-contrast=high>",
          '<html lang="en" data-text="2" data-contrast="high">' in anon.get("/login")[1])
    anon.extra = "rxui=t1-std"
    check("  standard contrast chosen explicitly overrides the OS setting",
          '<html lang="en" data-text="1" data-contrast="std">' in anon.get("/login")[1])
    for bad in ("t9-hc", 't2-hc"><x', "t1-auto-x", ""):
        anon.extra = f"rxui={quote(bad)}" if bad else ""
        check(f"  malformed preference ignored: {bad!r}", '<html lang="en">' in anon.get("/login")[1])
    anon.extra = ""

    # ---- lookups need a session
    res, text = anon.get("/api/lookup?kind=drug&q=alp")
    check("lookup when signed out -> 401 JSON, not the sign-in page",
          res.status == 401 and json.loads(text)["error"])

    ph, st = Client(port), Client(port)
    check("pharmacist and staff sign in", ph.login("pharmacist-a", "111111") == 303 and st.login("staff-a", "222222") == 303)

    # ---- shortcut markup: the upload and the main button are marked; hold outcomes never are
    _, home = ph.get("/")
    check("home: upload form is the Ctrl+Enter form; file input is the Ctrl+U target",
          re.search(r'<form method="post" action="/upload"[^>]*data-primary', home)
          and 'data-shortcut="upload"' in home and 'aria-keyshortcuts="Control+U Meta+U"' in home)
    check("  shortcut hints hidden until the script runs", '<kbd data-keys="Mod+U" hidden>' in home)

    def counter_case(c, img, fields):
        res, _ = c.upload(jpg(img))
        cid = res.getheader("Location").split("/")[-1]
        _, entry = c.get(f"/case/{cid}")
        res, _ = c.post(f"/case/{cid}/check", fields)
        _, page = c.get(res.getheader("Location") or f"/case/{cid}")
        m = re.search(r"Hold (H-[\w-]+)", page)
        return cid, entry, page, m.group(1) if m else None

    cid, entry, page, hid = counter_case(ph, mirrored_body(orig),
                                         {"drugs": "Lyrica 75", "regn": "00000", "patient": "Sample  Patient"})
    check("entry form: medicines, Regn No, doctor and patient are lookup fields; it is the Ctrl+Enter form",
          all(f'data-lookup="{k}"' in entry for k in ("drug", "regn", "doctor", "patient"))
          and re.search(r'action="/case/[^"]+/check" data-primary', entry))
    check("  the form says suggestions fill one field only and give names without strengths",
          "fills only that field" in entry and "name only - type the strength" in entry)
    check("decision page: checked by whom, when, for which patient",
          "Checked by <b>pharmacist-a</b>, today" in page and "patient Sample Patient" in page)
    check("  hold history names who opened it", hid and "Hold opened - <b>pharmacist-a</b>" in page)
    check("  no hold outcome form is a keyboard-shortcut target",
          not re.search(r'action="/hold/[^"]+/resolve"[^>]*data-primary', page)
          and 'data-shortcut="next"' in page)

    # ---- an override with a note that a spreadsheet would run as a formula
    note = "=HYPERLINK(\"x\") regular patient, OP card seen"
    res, _ = ph.post(f"/hold/{hid}/resolve", {"outcome": "dispensed-after-review", "reason": "other", "note": note,
                                             "case": cid})
    check("override recorded", res.status == 303)
    _, page = ph.get(f"/case/{cid}")
    check("  closed hold shows its full history: opened, then the override, by whom",
          re.search(r"Hold opened - <b>pharmacist-a</b>.*Dispensed after review - OVERRIDE: &quot;=HYPERLINK", page, re.S))

    cid2, _, page2, hid2 = counter_case(st, whatsapp(orig), {"drugs": "Dolo 650, Pan 40", "regn": "00000",
                                                             "doctor": "Example"})
    for c, who in ((ph, "pharmacist"), (st, "staff")):
        _, home = c.get("/")
        check(f"counter home ({who}): last check and last override, with who and when",
              "Last check: <b>staff-a</b>, today" in home and "Last override: <b>pharmacist-a</b>, today" in home
              and "regular patient" in home)
    check("  only a pharmacist gets the audit log link", 'href="/audit"' in ph.get("/")[1]
          and 'href="/audit"' not in st.get("/")[1])

    # ---- lookups: this pharmacy's own data, names only for medicines, no cross-filling
    s, d = ph.json("/api/lookup?kind=drug&q=alp")
    items = {i["value"]: i for i in d["items"]}
    check("medicine lookup: brand and molecule, high-risk marked in words", s == 200 and "Alprax" in items
          and "Alprazolam" in items and items["Alprax"].get("tagText") == "High-risk")
    check("  medicine suggestions never carry a strength", all(not re.search(r"\d", i["value"]) for i in d["items"]))
    s, d = ph.json("/api/lookup?kind=drug&q=dol")
    check("  medicines entered here before are offered; prefix matches first (Dolo, then Tapentadol, Tramadol)",
          [i["value"] for i in d["items"]] == ["Dolo", "Tapentadol", "Tramadol"]
          and "entered here before" in d["items"][0]["detail"] and d["items"][2]["tag"] == "high", d["items"])
    s, d = ph.json("/api/lookup?kind=regn&q=00")
    check("Regn No lookup: registry entry with the doctor it belongs to and whether it is verified",
          d["items"] and d["items"][0]["value"] == "00000" and "Example" in d["items"][0]["detail"]
          and d["items"][0]["tag"] == "verified")
    check("  Regn Nos match from the first digit only", ph.json("/api/lookup?kind=regn&q=11")[1]["items"] == [])
    s, d = ph.json("/api/lookup?kind=doctor&q=example")
    check("doctor lookup: registry name once (not again for 'Example' on a slip), with its Regn No",
          [i["value"] for i in d["items"]] == ["Dr. Example"] and "00000" in d["items"][0]["detail"])
    s, d = st.json("/api/lookup?kind=patient&q=sample")
    check("patient lookup: names typed here before, whitespace tidied, for staff too",
          [i["value"] for i in d["items"]] == ["Sample Patient"] and "1 visit" in d["items"][0]["detail"])
    check("  unknown kind or one letter -> nothing", ph.json("/api/lookup?kind=staff&q=pr")[1]["items"] == []
          and ph.json("/api/lookup?kind=drug&q=a")[1]["items"] == [])
    check("  LIKE wildcards are literal", st.json("/api/lookup?kind=patient&q=%25%25")[1]["items"] == [])

    # ---- audit log: pharmacists only
    check("audit log page and API refused to staff", st.get("/audit")[0].status == 403
          and st.json("/api/audit")[0] == 403 and st.get("/audit.csv")[0].status == 403)
    res, page = ph.get("/audit")
    check("audit page works without the script too: latest entries in a table, CSV link",
          res.status == 200 and 'id="rx-audit"' in page and '<th scope="col">Who</th>' in page
          and 'href="/audit.csv"' in page)
    s, d = ph.json("/api/audit")
    check("API: newest first, readable summary next to the raw detail",
          s == 200 and d["rows"][0]["seq"] > d["rows"][-1]["seq"]
          and any(x["action"] == "check" and x["summary"].startswith(("HOLD", "CONFIRM", "OK")) for x in d["rows"]))
    s, d = ph.json("/api/audit?group=overrides")
    check("  'Overrides only' finds exactly the override, by whom", [(x["actor"], x["override"]) for x in d["rows"]]
          == [("pharmacist-a", True)])
    s, d = ph.json("/api/audit?actor=staff-a&group=checks")
    check("  by person and kind of event", d["rows"] and all(x["actor"] == "staff-a" and x["action"] == "check"
                                                              for x in d["rows"]))
    s, d = ph.json(f"/api/audit?q={hid}")
    check("  search by reference: the hold's opening and outcome", {x["action"] for x in d["rows"]} == {"hold", "resolve"})
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    check("  date range", ph.json(f"/api/audit?from={tomorrow}")[1]["rows"] == []
          and ph.json(f"/api/audit?to={date.today().isoformat()}")[1]["rows"])
    app.AUDIT_PAGE = 4
    n = r.db().execute("SELECT COUNT(*) FROM audit").fetchone()[0]
    seqs, before, pages = [], "", 0
    while before is not None and pages < 50:
        d = ph.json(f"/api/audit?before={before}")[1]
        seqs += [x["seq"] for x in d["rows"]]
        before, pages = d["next"], pages + 1
    check(f"  'load older' pages cover the whole trail once ({n} entries, {pages} pages of 4)",
          pages > 2 and len(seqs) == n and len(set(seqs)) == n and seqs == sorted(seqs, reverse=True), (len(seqs), n))
    app.AUDIT_PAGE = 50

    res, text = ph.get("/audit.csv?group=overrides")
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    check("CSV export: attachment with the filtered rows", res.status == 200 and "attachment" in
          res.getheader("Content-Disposition") and rows[0][:3] == ["seq", "at", "actor"] and len(rows) == 2)
    check("  a note starting with '=' cannot run as a spreadsheet formula", rows[1][6].startswith("Dispensed")
          and all(not cell.startswith("=") for cell in rows[1]))
    ex = r.db().execute("SELECT actor, detail FROM audit WHERE action = 'audit-export' ORDER BY seq DESC").fetchone()
    check("  the export itself is in the audit trail, with its filters",
          ex and ex["actor"] == "pharmacist-a" and json.loads(ex["detail"]) == {"filters": {"group": "overrides"}, "rows": 1})

    # ---- patient names are kept only as long as they are used
    con = r.db()
    with con:
        con.execute("INSERT INTO patients VALUES ('Old Visitor', '2024-01-01', ?, 1)",
                    ((date.today() - timedelta(days=r.PATIENT_RETENTION_DAYS + 1)).isoformat(),))
    removed = r.purge_patients()
    check("patient names unused for the retention period are dropped",
          removed == 1 and [x[0] for x in r.db().execute("SELECT name FROM patients")] == ["Sample Patient"])
    r.remember_patient("Sample Patient")
    check("  same name again on the same day: one row, one visit",
          r.db().execute("SELECT COUNT(*), MAX(visits) FROM patients").fetchone()[:] == (1, 1))

    srv.shutdown()
    srv.server_close()
    r.close_thread_db()
    r.close_db()

sys.exit(0 if ok else 1)
