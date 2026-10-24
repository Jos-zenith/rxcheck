"""Counter app: sign-in, roles, CSRF, the decision page, and several billing counters at once.

Run:  python tests/test_counter_workflow.py
"""
import http.client
import json
import re
import secrets
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode

import cv2

from synthetic_slips import r, PRIVATE_PAIR, load_samples, fixture_path, whatsapp, forwarded, other_slip, angled_photo, glary_screen, mirrored_body, \
    flipped_body

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import counter_app as app  # noqa: E402

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
        text = data.decode("utf-8", "replace") if "text" in (res.getheader("Content-Type") or "") else ""
        t = re.search(r'name="csrf" value="([^"]+)"', text)
        if t:
            self.csrf = t.group(1)
        return res.status, res.getheader("Location"), text, data

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

    def upload(self, img_bytes, name="slip.jpg", path="/upload"):
        b = "----rx" + secrets.token_hex(8)
        body = (f"--{b}\r\nContent-Disposition: form-data; name=\"csrf\"\r\n\r\n{self.csrf}\r\n"
                f"--{b}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{name}\"\r\n"
                f"Content-Type: image/jpeg\r\n\r\n").encode() + img_bytes + f"\r\n--{b}--\r\n".encode()
        return self.req("POST", path, body, f"multipart/form-data; boundary={b}")

    def follow(self, loc):
        return self.get(loc)


def jpg(img):
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()


orig, fake = load_samples()

with tempfile.TemporaryDirectory() as t:
    tmp = Path(t)
    r.LEDGER_DIR, r.DB, r.FEATURES, r.REPORTS = tmp, tmp / "rxcheck.db", tmp / "features", tmp / "reports"
    r.REGISTRY = tmp / "registry.json"   # this pharmacy has verified the doctor; it lists only some doctors
    r.REGISTRY.write_text(json.dumps({"00000": {"name": "Dr. Example", "source": "test fixture",
                                                "verified_on": r.date.today().isoformat()}}), encoding="utf-8")
    r.add_entry(fixture_path(), "RX-TEST-001", "2025-01-15", "00000",
                "Example", "Dolo 650, Pan 40, Alprax 0.25")
    r.add_staff("pharmacist-a", "pharmacist", "111111")
    r.add_staff("staff-a", "staff", "222222")
    r.add_staff("staff-b", "staff", "333333")
    r.add_staff("staff-c", "staff", "444444")
    r.close_db()
    srv = app.make_server("127.0.0.1", 0, inbox=tmp / "inbox")
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # ---- sign-in
    anon = Client(port)
    st, loc, _, _ = anon.get("/")
    check("not signed in -> sent to sign-in page", st == 303 and loc == "/login")
    st, _, _, _ = anon.req("POST", "/upload", b"x", "application/octet-stream")
    check("not signed in -> POST refused", st == 401)
    st, _, _, data = anon.get("/evidence/..%2F..%2Frxcheck.db")
    check("signed-out evidence request -> sign-in", st == 303)
    bad = Client(port)
    statuses = [bad.login("staff-a", "000000") for _ in range(6)]
    check("wrong PIN refused; locked after 5 tries", statuses[:5] == [401] * 5 and statuses[5] == 429, statuses)
    check("  even the right PIN is refused while locked", Client(port).login("staff-a", "222222") == 429)
    check("unknown name refused", Client(port).login("nobody", "111111") == 401)
    ph, st_ = Client(port), Client(port)
    check("pharmacist signs in", ph.login("pharmacist-a", "111111") == 303)
    check("another staff member still signs in (lockout is per name)", st_.login("staff-b", "333333") == 303)

    # ---- CSRF and traversal
    s, _, _, _ = ph.post("/inbox/take", {"name": "x.jpg"}, csrf=False)
    check("POST without the form token -> 403", s == 403)
    for path in ("/evidence/..%2Frxcheck.db", "/evidence/rxcheck.db", "/img/..%2F..%2Frxcheck.db", "/case/../holds"):
        s, _, _, _ = ph.get(path)
        check(f"no access outside reports/uploads: {path} -> {s}", s in (404, 303) and s != 200)

    # ---- the counter flow on the synthetic edit: upload -> entry -> confirm entry -> decision
    s, loc, _, _ = ph.upload(jpg(fake), "suspect.jpg")
    check("upload -> case page", s == 303 and loc.startswith("/case/"))
    cid = loc.split("/")[-1]
    s, _, page, _ = ph.follow(loc)
    check("case page asks what is being sold, shows pre-check", s == 200 and "What is the customer buying?" in page
          and 'class="status-row pre-check"' in page)
    s, _, page, _ = ph.post(f"/case/{cid}/check", {"drugs": "Alprx .25", "regn": "11111"})
    check("ambiguous entry -> must confirm against the slip first", s == 200 and "Tick against the slip" in page
          and page.count('name="confirm"') == 2)
    s, loc, _, _ = ph.post(f"/case/{cid}/check", {"drugs": "Alprx .25", "regn": "11111", "confirm": ["0"]})
    check("  partial confirmation is not enough", s == 200)
    s, loc, _, _ = ph.post(f"/case/{cid}/check", {"drugs": "Alprax 0.25", "regn": "11111"})
    s, _, page, _ = ph.follow(loc)
    if PRIVATE_PAIR:
        check("decision page: one verdict word, the reason, the next action",
              "HOLD" in page and "COPY of RX-TEST-001" in page and "Call the clinic" in page
              and "edited copy of a slip already dispensed: do not dispense" in page)
    ev = re.findall(r'src="(/evidence/[^"]+)"', page)
    if PRIVATE_PAIR:
        check("evidence (close-ups + side by side) is shown and served",
              len(ev) == 2 and all(ph.get(e)[0] == 200 and ph.get(e)[3][:4] == b"\x89PNG" for e in ev))
    else:
        print("[SKIP] image-edit evidence assertions require local sample images; synthetic fixtures are not a clinical benchmark")
    hid = re.search(r"Hold (H-[\w-]+)", page).group(1)

    # ---- roles on hold resolution
    s, _, page, _ = st_.get(f"/case/{cid}")
    check("staff sees no 'confirmed' button", s == 200 and 'value="confirmed"' not in page and 'value="refused"' in page)
    s, _, _, _ = st_.post(f"/hold/{hid}/resolve", {"outcome": "confirmed", "case": cid})
    check("staff cannot record 'confirmed' (403)", s == 403)
    s, _, _, _ = st_.post(f"/hold/{hid}/resolve", {"outcome": "unreachable", "case": cid, "note": "line busy"})
    check("staff records 'unreachable' -> hold waits", s == 303 and
          r.db().execute("SELECT status FROM holds WHERE id = ?", (hid,)).fetchone()[0] == "waiting")
    s, _, _, _ = ph.post(f"/case/{cid}/record", {})
    check("cannot record as dispensed while the hold is unresolved (409)", s == 409)
    check("  ...and the page does not offer it while the hold is open",
          f'action="/case/{cid}/record"' not in ph.get(f"/case/{cid}")[2])
    ph.get(f"/case/{cid}")
    s, _, _, _ = ph.post(f"/hold/{hid}/resolve", {"outcome": "refused", "case": cid, "note": "doctor denied"})
    s2, _, _, _ = ph.post(f"/hold/{hid}/resolve", {"outcome": "confirmed", "case": cid})
    check("pharmacist closes the hold; a closed hold cannot be re-resolved", s == 303 and s2 == 409)
    rows = r.db().execute("SELECT actor, action FROM audit WHERE ref = ? ORDER BY seq", (hid,)).fetchall()
    check("audit names who did what: hold / unreachable by staff-b / refused by pharmacist-a",
          [(x[0], x[1]) for x in rows][1:] == [("staff-b", "resolve"), ("pharmacist-a", "resolve")], [tuple(x) for x in rows])

    # ---- high-risk GREEN path cannot happen without issuance; but recording is pharmacist-only
    s, _, _, _ = st_.post(f"/case/{cid}/record", {})
    check("staff cannot add to the ledger (403)", s == 403)

    # ---- after a warning: split the sale, take the quickest valid step, count overrides
    def counter_case(c, img, drugs, regn="00000"):
        s, loc, _, _ = c.upload(jpg(img))
        c.get(loc)
        cid = loc.split("/")[-1]
        s, loc2, page, _ = c.post(f"/case/{cid}/check", {"drugs": drugs, "regn": regn})
        s, _, page, _ = c.get(loc2 or loc)
        m = re.search(r"Hold (H-[\w-]+)", page)
        return cid, page, m.group(1) if m else None

    cid2, page, hid2 = counter_case(ph, whatsapp(orig, angle=-2), "Dolo 650, Pan 40, Alprax 0.25")
    if PRIVATE_PAIR:
        check("repeat of an H1 slip with routine items: routine handed over now, only the H1 item held",
              "Hand over now:</b> Dolo 650, Pan 40" in page and "Hold:</b> Alprax 0.25" in page)
        check("  ... settled at the counter, no call (ledger + H1 register)",
              "Check at the counter - no call needed" in page and "already presented on 2025-01-15" in page
              and 'value="confirmed"' not in page and 'value="repeat-allowed"' in page
              and 'value="paper-original"' not in page)
    else:
        print("[SKIP] repeat-sale image assertions require local sample images")
    s, _, page, _ = ph.post(f"/hold/{hid2}/resolve", {"outcome": "dispensed-after-review", "reason": "paper-original",
                                                      "case": cid2})
    check("  citing a check that does not settle this warning counts as an override (note required)",
          s == 409 and "override" in page)
    s, _, page, _ = ph.post(f"/hold/{hid2}/resolve", {"outcome": "dispensed-after-review", "case": cid2})
    check("  'checked - dispense' must say what was checked", s == 409 and "say what was checked" in page)
    s, _, _, _ = ph.post(f"/hold/{hid2}/resolve", {"outcome": "dispensed-after-review", "reason": "repeat-allowed",
                                                   "case": cid2})
    d = json.loads(r.db().execute("SELECT detail FROM audit WHERE action = 'resolve' AND ref = ?", (hid2,)).fetchone()[0])
    check("  counter check recorded with its reason, not as an override", s == 303 and d["reason"] == "repeat-allowed"
          and d["override"] is False)

    cid3, page, hid3 = counter_case(ph, mirrored_body(orig), "Alprax 0.25")
    check("new NDPS slip: call the clinic (registry number), 'no answer' parks it, no override offered",
          "Call the clinic" in page and 'value="unreachable"' in page
          and "Dispense without this step (override)" not in page and "No override: Alprax 0.25" in page)
    s, _, page, _ = ph.post(f"/hold/{hid3}/resolve", {"outcome": "dispensed-after-review", "reason": "other",
                                                      "note": "regular patient, OP card seen, clinic closed",
                                                      "case": cid3})
    check("  NDPS fails closed: an override is refused even with a note", s == 409 and "no override" in page
          and r.workload(1)["overrides"] == 0)
    s, _, _, _ = ph.post(f"/hold/{hid3}/resolve", {"outcome": "unreachable", "case": cid3})
    check("  ... 'no answer' still parks it for a call-back", s == 303
          and r.db().execute("SELECT status FROM holds WHERE id = ?", (hid3,)).fetchone()[0] == "waiting")

    cid5, page, hid5 = counter_case(ph, flipped_body(orig), "Lyrica 75")
    check("new H1 (non-NDPS) slip: call the clinic, override available to a pharmacist",
          "Call the clinic" in page and "Dispense without this step (override)" in page)
    s, _, page, _ = ph.post(f"/hold/{hid5}/resolve", {"outcome": "dispensed-after-review", "reason": "paper-original",
                                                      "case": cid5})
    check("  a counter reason cannot settle a warning that needs the clinic: override needs a note",
          s == 409 and "override" in page)
    s, _, _, _ = ph.post(f"/hold/{hid5}/resolve", {"outcome": "dispensed-after-review", "reason": "other",
                                                   "note": "regular patient, OP card seen, clinic closed", "case": cid5})
    w = r.workload(1)
    check("  override with a note is allowed, and counted against the person", s == 303 and w["overrides"] == 1
          and w["overrides_by"]["pharmacist-a"]["overrides"] == 1, w)

    cid4, page, hid4 = counter_case(st_, glary_screen(orig), "Dolo 650, Pan 40")
    check("unreadable photo: retake, no call", "Retake the photo - no call needed" in page
          and f'action="/case/{cid4}/retake"' in page)
    s, loc, _, _ = st_.upload(jpg(whatsapp(orig)), "retake.jpg", path=f"/case/{cid4}/retake")
    s, _, page, _ = st_.get(loc)
    check("  the new photo is checked with the same entries; old hold closed as 'retaken'",
          loc.split("/")[-1] != cid4 and "NO CONFLICT FOUND" in page
          and r.db().execute("SELECT status FROM holds WHERE id = ?", (hid4,)).fetchone()[0] == "closed"
          and app.hold_outcome(hid4) == "retaken")
    s, _, page, _ = ph.get("/")
    check("counter home shows today's load (checked / settled at the counter / needed a call / overrides)",
          "settled at the counter" in page and "needed a call" in page and "overrides" in page)

    # ---- session rules
    app.SESSION_IDLE = timedelta(seconds=1)
    time.sleep(1.5)
    s, loc, _, _ = st_.get("/")
    check("idle session expires -> sign in again", s == 303 and loc == "/login")
    app.SESSION_IDLE = timedelta(minutes=30)
    staff_c = Client(port)
    staff_c.login("staff-c", "444444")
    con = r.db()
    with con:
        con.execute("UPDATE staff SET active = 0 WHERE name = 'staff-c'")
    s, loc, _, _ = staff_c.get("/")
    check("staff disabled mid-shift is signed out at once", s == 303 and loc == "/login")
    with con:
        con.execute("UPDATE staff SET active = 1 WHERE name = 'staff-c'")

    # ---- several counters at once: four staff, two prescriptions each, all at the same time
    imgs = [whatsapp(orig), forwarded(orig), angled_photo(orig), whatsapp(orig, angle=-2),
            other_slip(0), other_slip(1), other_slip(2), whatsapp(other_slip(3))]
    staff = [("pharmacist-a", "111111"), ("staff-a", "222222"), ("staff-b", "333333"), ("staff-c", "444444")]
    time.sleep(0.1)
    app.STATE.failures.clear()   # staff-a was locked out above

    def counter(i):
        name, pin = staff[i]
        c = Client(port)
        if c.login(name, pin) != 303:
            return name, ["login failed"]
        out = []
        for k in range(2):
            s, loc, _, _ = c.upload(jpg(imgs[2 * i + k]), f"c{i}-{k}.jpg")
            if s != 303:
                out.append(f"upload {s}")
                continue
            c.get(loc)
            s, loc2, _, _ = c.post(f"{loc}/check", {"drugs": "Dolo 650, Pan 40", "regn": "00000"})
            s, _, page, _ = c.get(loc2 or loc)
            out.append("ok" if s == 200 and ("NO CONFLICT FOUND" in page or "CONFIRM FIRST" in page or "HOLD" in page)
                       else f"decision {s}")
        return name, out

    before = r.db().execute("SELECT COUNT(*) FROM audit WHERE action = 'check'").fetchone()[0]
    t0 = time.perf_counter()
    with ThreadPoolExecutor(4) as ex:
        results = list(ex.map(counter, range(4)))
    elapsed = time.perf_counter() - t0
    checks = r.db().execute("SELECT actor, detail FROM audit WHERE action = 'check' ORDER BY seq").fetchall()[before:]
    by = {n: sum(x["actor"] == n for x in checks) for n, _ in staff}
    check(f"4 counters x 2 checks at once: all decided ({elapsed:.1f}s total)",
          all(o == ["ok", "ok"] for _, o in results), results)
    check("  every check in the audit trail, attributed to the person at that counter",
          len(checks) == 8 and all(v == 2 for v in by.values()), by)
    r.close_thread_db()
    check("  ledger still verifies healthy", not r.verify_store(), r.verify_store()[:3])

    srv.shutdown()
    srv.server_close()
    r.close_db()

sys.exit(0 if ok else 1)
