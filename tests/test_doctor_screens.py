"""Counter app: the 'Ask the doctor' and 'enter the code' screens on a hold, with roles, CSRF, every answer path
(app tap, read-out code, denial), expiry, lockout, rate limit, service down, and what the audit trail shows.

Run:  python tests/test_doctor_screens.py
"""
import html
import http.client
import json
import re
import secrets
import sys
import tempfile
import threading
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

import cv2

from scenarios import r, load_samples, fixture_path, mirrored_body, flipped_body, other_slip

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import counter_app as app  # noqa: E402
import doctorping as dp  # noqa: E402

ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not cond else ""))


class Client:
    """A browser at one counter: keeps its session cookie and the form token of the last page."""

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
        text = data.decode("utf-8", "replace") if "text" in (res.getheader("Content-Type") or "") or "json" in (res.getheader("Content-Type") or "") else ""
        t = re.search(r'name="csrf" value="([^"]+)"', text)
        if t:
            self.csrf = t.group(1)
        return res.status, res.getheader("Location"), html.unescape(text), data

    def get(self, path):
        return self.req("GET", path)

    def post(self, path, fields, csrf=True):
        fields = dict(fields, **({"csrf": self.csrf} if csrf and self.csrf else {}))
        return self.req("POST", path, urlencode(fields, doseq=True), "application/x-www-form-urlencoded")

    def login(self, name, pin):
        st, loc, text, _ = self.post("/login", {"name": name, "pin": pin}, csrf=False)
        if st == 303:
            self.get("/")
        return st

    def upload(self, img_bytes, name="slip.jpg"):
        b = "----rx" + secrets.token_hex(8)
        body = (f"--{b}\r\nContent-Disposition: form-data; name=\"csrf\"\r\n\r\n{self.csrf}\r\n"
                f"--{b}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{name}\"\r\n"
                f"Content-Type: image/jpeg\r\n\r\n").encode() + img_bytes + f"\r\n--{b}--\r\n".encode()
        return self.req("POST", "/upload", body, f"multipart/form-data; boundary={b}")


jpg = lambda img: cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()
today = date.today().isoformat()
PHONE = "+91 98400 11122"
off = [0.0]
dp._clock = lambda: time.time() + off[0]
orig, _ = load_samples()

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as t:
    tmp = Path(t)
    (tmp / "data").mkdir()
    r.HOME = tmp
    r.LEDGER_DIR, r.DB, r.FEATURES, r.REPORTS = tmp, tmp / "rxcheck.db", tmp / "features", tmp / "reports"
    r.REGISTRY = tmp / "registry.json"
    r.REGISTRY.write_text(json.dumps({"00000": {"name": "Dr. Example", "source": "test fixture", "verified_on": today}}),
                          encoding="utf-8")
    r.add_entry(fixture_path(), "RX-TEST-001", "2025-01-15", "00000", "Example", "Dolo 650, Pan 40, Alprax 0.25")
    r.add_staff("pharmacist-a", "pharmacist", "111111")
    r.add_staff("staff-a", "staff", "222222")
    r.close_db()
    S = tmp / "svc"
    svc = dp.Service(S)
    svc.init()
    svc.enrol_start("00000", {"name": "Dr. Example", "source": "council (test)", "verified_on": today,
                              "phone": PHONE, "phone_source": "council (test)"})
    dev_priv, dev_pub = dp.new_keypair()
    svc.enrol_confirm("00000", svc.inbox(PHONE)[-1]["otp"], dev_pub)

    srv = app.make_server("127.0.0.1", 0, inbox=tmp / "inbox")
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ph, st_ = Client(port), Client(port)
    check("sign in", ph.login("pharmacist-a", "111111") == 303 and st_.login("staff-a", "222222") == 303)

    def new_case(img, regn="00000", patient="Ravi Kumar"):
        s, loc, _, _ = ph.upload(jpg(img))
        ph.get(loc)
        cid = loc.split("/")[-1]
        s, loc2, _, _ = ph.post(f"/case/{cid}/check", {"drugs": "Alprax 0.25", "regn": regn, "patient": patient})
        s, _, page, _ = ph.get(loc2 or loc)
        return cid, page, re.search(r"Hold (H-[\w-]+)", page).group(1)

    def messages():
        return [m for m in svc.inbox(PHONE) if m["type"] == "confirm-request"]

    def answer(rid, decision):
        sha = svc._con().execute("SELECT summary_sha FROM requests WHERE id = ?", (rid,)).fetchone()[0]
        return svc.respond(rid, decision, dp.device_sign(dev_priv, rid, decision, sha))

    def pending_id(hid):
        return [a for a in dp.asks(hid) if a["status"] == "pending"][-1]["request_id"]

    def detail(hid, action="resolve"):
        return json.loads(r.db().execute("SELECT detail FROM audit WHERE action = ? AND ref = ? ORDER BY seq DESC", (action, hid)).fetchone()[0])

    # ---- not connected yet
    cid1, page, hid1 = new_case(mirrored_body(orig))
    check("a new high-risk slip is held on the call path", "Call the clinic" in page and "value=\"confirmed\"" in page)
    check("service not trusted: pharmacist is told, no form", "Doctor confirmation is not connected" in page
          and "/doctor/ask" not in page)
    check("service not trusted: staff sees nothing about it", "Doctor confirmation" not in st_.get(f"/case/{cid1}")[2])

    dp.trust(S)
    # ---- the ask screen
    s, _, page, _ = ph.get(f"/case/{cid1}")
    check("pharmacist sees the Ask-the-doctor form for the held medicine, Regn No prefilled",
          "Ask the doctor" in page and f'action="/hold/{hid1}/doctor/ask"' in page and 'value="00000"' in page
          and "<b>Alprax 0.25</b>" in page)
    check("patient field prefilled with initials only, never the name", 'name="patient" value="RK"' in page
          and "Ravi" not in page.split("Ask the doctor")[1].split("</form>")[0])
    check("the page says exactly what the doctor's phone receives", "never the patient's name or the slip" in page)
    s, _, spage, _ = st_.get(f"/case/{cid1}")
    check("staff sees no ask form, only that a pharmacist can ask", "/doctor/ask" not in spage
          and "A pharmacist can ask the prescriber" in spage)
    check("staff cannot ask (403)", st_.post(f"/hold/{hid1}/doctor/ask", {"case": cid1, "regn": "00000", "patient": "RK, 34"})[0] == 403)
    check("ask without the form token (403)", ph.post(f"/hold/{hid1}/doctor/ask", {"case": cid1, "regn": "00000", "patient": "RK, 34"}, csrf=False)[0] == 403)
    cid_other, _, hid_other = new_case(flipped_body(orig))
    check("a hold that is not this case's is refused (400)",
          ph.post(f"/hold/{hid_other}/doctor/ask", {"case": cid1, "regn": "00000", "patient": "RK, 34"})[0] == 400)
    s, _, page, _ = ph.post(f"/hold/{hid1}/doctor/ask", {"case": cid1, "regn": "00000", "patient": "", "rx_date": today})
    check("missing patient initials: told what to enter, nothing sent", s == 200 and "initials and age" in page and not messages())

    s, _, page, _ = ph.post(f"/hold/{hid1}/doctor/ask", {"case": cid1, "regn": "00000", "patient": "Ravi Kumar, 34", "rx_date": today})
    msg = messages()[-1]
    check("asked: told where it went (masked number) and how the doctor can answer",
          s == 200 and "Sent to the doctor's registered phone (******22)" in page and "98400" not in page)
    check("the doctor's phone got initials and age, the held medicine and the date - not the name",
          msg["patient"] == "RK, 34" and msg["drugs"] == "Alprax 0.25" and msg["rx_date"] == today
          and "Ravi" not in json.dumps(msg))
    check("page now offers the code field and a refresh, not a second ask",
          'name="code"' in page and "Check for the doctor" in page and "/doctor/ask" not in page)
    check("the code is never shown on any page", msg["otp"] not in page and msg["otp"] not in ph.get(f"/case/{cid1}")[2])
    s, _, page, _ = ph.post(f"/hold/{hid1}/doctor/ask", {"case": cid1, "regn": "00000", "patient": "RK, 34", "rx_date": today})
    check("a second ask while one is open is refused", "Already asked" in page and len(messages()) == 1)
    s, _, page, _ = st_.get(f"/case/{cid1}")
    check("staff sees the state and can refresh, but has no code field", "Check for the doctor" in page and 'name="code"' not in page)
    check("staff cannot enter a code (403)", st_.post(f"/hold/{hid1}/doctor/otp", {"case": cid1, "code": msg["otp"]})[0] == 403)

    bad = "000000" if msg["otp"] != "000000" else "111111"
    s, _, page, _ = ph.post(f"/hold/{hid1}/doctor/otp", {"case": cid1, "code": bad})
    check("wrong code: tries left, and the code typed is not echoed", "Wrong code - 2 tries left" in page and f'value="{bad}"' not in page)
    check("not 6 digits: refused before reaching the service", "The code is 6 digits" in ph.post(f"/hold/{hid1}/doctor/otp", {"case": cid1, "code": "12ab"})[2])
    s, _, page, _ = st_.post(f"/hold/{hid1}/doctor/status", {"case": cid1})
    check("no answer yet", "No answer yet" in page)

    # ---- the doctor approves in the app
    answer(pending_id(hid1), "approved")
    s, _, page, _ = st_.post(f"/hold/{hid1}/doctor/status", {"case": cid1})
    check("staff refresh shows the doctor's verified approval", "Doctor confirmed" in page and "signed receipt verified" in page)
    s, _, page, _ = ph.get(f"/case/{cid1}")
    check("pharmacist's confirm button now says it is backed by the doctor's receipt",
          "Clinic confirmed (doctor&#x27;s signed receipt)" in page or "Clinic confirmed (doctor's signed receipt)" in page)
    s, _, _, _ = ph.post(f"/hold/{hid1}/resolve", {"outcome": "confirmed", "case": cid1})
    d = detail(hid1)
    check("confirmed: recorded as authenticated, via the app", s == 303 and d["authenticated"] is True and d["via"] == "app")
    s, _, page, _ = ph.get(f"/case/{cid1}")
    check("closed hold's history shows the ask, the doctor's answer and the authenticated confirmation",
          "Doctor asked (message to ******22)" in page and "Doctor APPROVED via the app (signed receipt)" in page
          and "AUTHENTICATED by the doctor (app)" in page)

    # ---- denial
    cid2, _, hid2 = cid_other, None, hid_other
    s, _, page, _ = ph.post(f"/hold/{hid2}/doctor/ask", {"case": cid2, "regn": "00000", "patient": "AB, 40", "rx_date": today})
    check("second hold: asked", "Sent to the doctor's registered phone" in page)
    answer(pending_id(hid2), "denied")
    s, _, page, _ = ph.post(f"/hold/{hid2}/doctor/status", {"case": cid2})
    check("denial: shown in red words, no 'Clinic confirmed' button", "Doctor DENIED" in page and "Do not dispense" in page
          and 'value="confirmed"' not in page and 'value="refused"' in page)
    s, _, page, _ = ph.post(f"/hold/{hid2}/resolve", {"outcome": "confirmed", "case": cid2})
    check("a hand-made 'confirmed' after a denial is refused (409)", s == 409 and "DENIED" in page)
    s, _, page, _ = ph.get(f"/case/{cid2}")
    check("denial: only Refuse is offered (no 'no answer', no override)",
          'value="unreachable"' not in page and 'value="dispensed-after-review"' not in page and 'value="refused"' in page)
    cfg_file = tmp / "data" / dp.CONFIG_FILE
    cfg_file.rename(cfg_file.with_suffix(".off"))
    s, _, page, _ = ph.get(f"/case/{cid2}")
    check("denial still shown with the service disconnected", "Doctor DENIED" in page and 'value="confirmed"' not in page)
    cfg_file.with_suffix(".off").rename(cfg_file)
    check("refusing closes it", ph.post(f"/hold/{hid2}/resolve", {"outcome": "refused", "case": cid2, "note": "doctor denied"})[0] == 303)

    # ---- read-out code
    cid3, _, hid3 = new_case(other_slip(21))
    ph.post(f"/hold/{hid3}/doctor/ask", {"case": cid3, "regn": "00000", "patient": "CD, 55", "rx_date": today})
    otp = messages()[-1]["otp"]
    s, _, page, _ = ph.post(f"/hold/{hid3}/doctor/otp", {"case": cid3, "code": otp})
    check("the right code: doctor confirmed", "The doctor confirmed" in page and "Doctor confirmed" in page, page[-300:])
    ph.post(f"/hold/{hid3}/resolve", {"outcome": "confirmed", "case": cid3})
    d = detail(hid3)
    check("confirmed via the read-out code is recorded as authenticated (otp)", d["authenticated"] and d["via"] == "otp")
    check("history says so", "AUTHENTICATED by the doctor (read-out code)" in ph.get(f"/case/{cid3}")[2])

    # ---- rate limit, then expiry and lockout
    cid4, _, hid4 = new_case(other_slip(22))
    s, _, page, _ = ph.post(f"/hold/{hid4}/doctor/ask", {"case": cid4, "regn": "00000", "patient": "EF, 30", "rx_date": today})
    check("4th ping in an hour to the same doctor is refused, with the fallback", "Too many requests" in page
          and "call the clinic" in page.lower() and not dp.asks(hid4))
    off[0] += 3700
    ph.post(f"/hold/{hid4}/doctor/ask", {"case": cid4, "regn": "00000", "patient": "EF, 30", "rx_date": today})
    otp4 = messages()[-1]["otp"]
    wrong = "000000" if otp4 != "000000" else "111111"
    last = [ph.post(f"/hold/{hid4}/doctor/otp", {"case": cid4, "code": wrong})[2] for _ in range(3)]
    check("3 wrong codes lock the request", "Locked after 3 wrong codes" in last[-1])
    s, _, page, _ = ph.get(f"/case/{cid4}")
    check("after the lock the pharmacist can ask again (form is back, says why)", "ended without an answer (locked)" in page
          and f'action="/hold/{hid4}/doctor/ask"' in page)
    off[0] += 3700
    ph.post(f"/hold/{hid4}/doctor/ask", {"case": cid4, "regn": "00000", "patient": "EF, 30", "rx_date": today})
    off[0] += dp.REQUEST_TTL + 5
    s, _, page, _ = ph.post(f"/hold/{hid4}/doctor/status", {"case": cid4})
    check("after 15 minutes: expired, told to ask again or call", "expired without an answer" in page)

    # ---- enrolment and service problems
    off[0] += 3700
    s, _, page, _ = ph.post(f"/hold/{hid4}/doctor/ask", {"case": cid4, "regn": "99999", "patient": "EF, 30", "rx_date": today})
    check("a doctor who has not enrolled: told to call the clinic instead", "has not enrolled" in page)
    (S / "OFFLINE").write_text("down", encoding="utf-8")
    sent_before = len(messages())
    s, _, page, _ = ph.post(f"/hold/{hid4}/doctor/ask", {"case": cid4, "regn": "00000", "patient": "EF, 30", "rx_date": today})
    check("service down: nothing sent, told to call the clinic", "could not be reached" in page and len(messages()) == sent_before)
    (S / "OFFLINE").unlink()
    check("the hold can still be parked for a call-back",
          ph.post(f"/hold/{hid4}/resolve", {"outcome": "unreachable", "case": cid4, "note": "line busy"})[0] == 303)

    # ---- audit trail and log
    s, _, text, _ = ph.get("/api/audit?group=holds")
    actions = [x["action"] for x in json.loads(text)["rows"]]
    check("audit log (holds group) lists the asks and the doctor's answers", "doctor-ask" in actions and "doctor-receipt" in actions, str(set(actions)))
    check("no code, patient name or initials in the log", not any(w in text for w in (otp, msg["otp"], "Ravi", "RK, 34")))
    s, _, text, _ = ph.get("/audit?group=holds")
    check("audit page renders the new events readably", "Doctor asked" in text and "APPROVED via app" in text, text[-200:])
    check("static bundle and CSP unchanged: forms post to the same server only",
          "form-action 'self'" in app.CSP and "script-src 'self'" in app.CSP)

    srv.shutdown()
    r.close_db()

print("\nALL PASSED" if ok else "\nFAILURES")
sys.exit(0 if ok else 1)
