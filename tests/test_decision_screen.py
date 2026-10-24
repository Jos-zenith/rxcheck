"""The decision screen explains itself: the image, the prescriber and issuance, and the medicines are separate
checks, each with its finding, its evidence and what is NOT known - and nothing unverified is shown as fact.
Holds say who owns them and what happens next; the audit trail keeps what was observed and what was sent.

Run:  python tests/test_decision_screen.py
"""
import http.client
import json
import re
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import cv2

from synthetic_slips import (r, load_samples, fixture_path, whatsapp, forwarded, dim_shop_light, added_line, rewrite,
                       tiny, other_slip)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import counter_app as app  # noqa: E402

ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not cond else ""))


def areas(img, drugs, regn="00000", doctor="Dr. Example"):
    rep = r.public(r.run_check(img, drugs, regn, doctor, draw=False))
    return rep, {a["area"]: a for a in app.explain(rep)}


orig, _ = load_samples()
with tempfile.TemporaryDirectory() as t:
    tmp = Path(t)
    r.LEDGER_DIR, r.DB, r.FEATURES, r.REPORTS = tmp, tmp / "rxcheck.db", tmp / "features", tmp / "reports"
    r.REGISTRY = tmp / "registry.json"
    r.REGISTRY.write_text(json.dumps({"00000": {"name": "Dr. Example", "source": "test fixture",
                                                "verified_on": r.date.today().isoformat()},
                                      "11111": {"name": "Dr. Somebody Else", "source": "test fixture",
                                                "verified_on": r.date.today().isoformat()}}), encoding="utf-8")
    r.add_entry(fixture_path(), "RX-TEST-001", (r.date.today() - timedelta(days=30)).isoformat(), "00000", "Example",
                "Dolo 650, Pan 40, Alprax 0.25")   # within the 365-day comparison window

    def save(name, img):
        p = tmp / f"{name}.jpg"
        cv2.imwrite(str(p), img)
        return p

    # ---- each signal in its own place
    _, a = areas(save("edited", added_line(orig)), "Alprax 0.25")
    check("edited copy: an IMAGE finding, said to be a reason to confirm, not proof of fraud",
          a["Image"]["status"] == "finding" and a["Image"]["finding"].startswith("COPY of RX-TEST-001")
          and "not proof of fraud" in a["Image"]["unknown"], a["Image"])
    for name, img in (("forwarded on WhatsApp", whatsapp(orig)), ("forwarded 5 times", forwarded(orig)),
                      ("dim shop light", dim_shop_light(orig))):
        rep, a = areas(save(name.replace(" ", "_"), img), "Dolo 650, Pan 40")
        check(f"{name}: 'Seen before' in the image, not a finding; GREEN for routine; the repeat rule is the unknown",
              a["Image"]["status"] == "seen" and rep["verdicts"]["routine"] == "GREEN"
              and "repeat is allowed" in a["Image"]["unknown"], (a["Image"], rep["verdicts"]))
    rep, a = areas(save("rewritten", rewrite(orig, 15, 8)), "Dolo 650")
    check("a doctor's rewrite (legitimate correction): 'Not verified', and it says it could be the doctor",
          a["Image"]["status"] in ("unverified", "new") and ("doctor" in a["Image"]["unknown"]
                                                             or "not a verified one" in a["Image"]["unknown"]),
          a["Image"])
    rep, a = areas(save("thumbnail", tiny(orig)), "Dolo 650")
    check("an unusable photo (260 px thumbnail): an image finding that says nothing was compared, and to retake it",
          a["Image"]["status"] == "finding" and "Retake" in a["Image"]["unknown"], a["Image"])
    _, a = areas(save("new", other_slip(3)), "Alprax 0.25")
    check("no reference image (a new slip): 'Not seen before', and says a new slip is not a verified one",
          a["Image"]["status"] == "new" and "not a verified one" in a["Image"]["unknown"], a["Image"])

    # ---- prescriber: unverified is not invalid; only a code or the doctor settles authorship
    _, a = areas(save("new2", other_slip(4)), "Dolo 650", regn="99999", doctor="Dr. Unknown")
    p = a["Prescriber and issuance"]
    check("Regn No not in a partial registry: 'Not verified', and says that is not 'invalid'",
          p["status"] == "unverified" and "is not 'invalid'" in p["unknown"], p)
    _, a = areas(save("new3", other_slip(5)), "Dolo 650", regn="11111", doctor="Dr. Example")
    p = a["Prescriber and issuance"]
    check("Regn No that is another doctor's: a finding", p["status"] == "finding", p)
    _, a = areas(save("new4", other_slip(6)), "Dolo 650")
    p = a["Prescriber and issuance"]
    check("valid Regn No: OK - and still says it does not prove who wrote the slip",
          p["status"] == "ok" and "proves the doctor exists, not that they wrote it" in p["unknown"], p)

    # ---- medicines: changed strength or added drug against the record; tiers; nothing clinical claimed
    _, a = areas(save("resent", whatsapp(orig)), "Alprax 0.5")
    m = a["Medicines"]
    check("strength changed against the recorded slip: a MEDICINES finding, the tiers as evidence",
          m["status"] == "finding" and m["finding"].startswith("Differs from record of RX-TEST-001")
          and any("Alprax 0.5" in x for x in m["evidence"]), m)
    _, a = areas(save("resent2", whatsapp(orig)), "Dolo 650, Pan 40, Alprax 0.25, Zerodol")
    check("a medicine added to a recorded slip: a medicines finding", a["Medicines"]["status"] == "finding",
          a["Medicines"])
    _, a = areas(save("new5", other_slip(7)), "Panazep 50mg, Dolo 650")
    m = a["Medicines"]
    check("an unlisted look-alike: high-risk, with the reason as evidence; no clinical check is claimed",
          m["status"] == "high" and any("looks like a benzodiazepine" in x for x in m["evidence"])
          and "no interaction, allergy or dose checks" in m["unknown"], m)

    # ---- never present the unknown as fact
    reps = [areas(save(f"any{i}", img), drugs)
            for i, (img, drugs) in enumerate([(orig, "Dolo 650"), (whatsapp(orig), "Alprax 0.25"),
                                              (other_slip(8), "Dolo 650"), (added_line(orig), "Dolo 650")])]
    check("every area of every check says what is not known", all(x["unknown"].strip() for _, a in reps
                                                                  for x in a.values()))
    html = " ".join(app.explain_card(rep) for rep, _ in reps)
    check("no area calls a slip genuine, authentic or verified unless a clinic signature verified",
          not re.search(r"\b(genuine|authentic)\b", html, re.I) and "Clinic-signed code verified" not in html)

    # ---- holds: who owns them, what was tried, what happens next, how they ended
    r.add_staff("pharmacist-a", "pharmacist", "111111")
    r.add_staff("staff-a", "staff", "222222")
    r.close_db()
    srv = app.make_server("127.0.0.1", 0, inbox=tmp / "inbox")
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    class C:
        def __init__(self):
            self.cookie, self.csrf = None, None

        def req(self, method, path, body=None, ctype=None):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
            h = {"Cookie": f"rxs={self.cookie}"} if self.cookie else {}
            if ctype:
                h["Content-Type"] = ctype
            c.request(method, path, body=body, headers=h)
            res = c.getresponse()
            text = res.read().decode("utf-8", "replace")
            m = re.search(r"rxs=([^;]*)", res.getheader("Set-Cookie") or "")
            if m:
                self.cookie = m.group(1) or None
            tok = re.search(r'name="csrf" value="([^"]+)"', text)
            if tok:
                self.csrf = tok.group(1)
            return res.status, res.getheader("Location"), text

        def post(self, path, fields):
            return self.req("POST", path, urlencode(dict(fields, csrf=self.csrf), doseq=True),
                            "application/x-www-form-urlencoded")

        def login(self, name, pin):
            self.post("/login", {"name": name, "pin": pin})
            self.req("GET", "/")

        def upload(self, data):
            b = "----rx" + "0" * 8
            body = (f"--{b}\r\nContent-Disposition: form-data; name=\"csrf\"\r\n\r\n{self.csrf}\r\n--{b}\r\nContent-"
                    f"Disposition: form-data; name=\"image\"; filename=\"s.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n"
                    ).encode() + data + f"\r\n--{b}--\r\n".encode()
            return self.req("POST", "/upload", body, f"multipart/form-data; boundary={b}")

    ph, st = C(), C()
    ph.login("pharmacist-a", "111111")
    st.login("staff-a", "222222")
    s, _, home = st.req("GET", "/")
    check("upload: one action, 'Analyse prescription'; it says the analysis starts on choosing a file",
          "Analyse prescription" in home and "Check prescription</button>" not in home
          and "starts as soon as you" in home and "data-upload-status" in home)
    s, loc, _ = st.upload(cv2.imencode(".jpg", other_slip(9))[1].tobytes())
    cid = loc.rsplit("/", 1)[1]
    s, loc, page = st.post(f"/case/{cid}/check", {"drugs": "Alprax 0.25", "regn": "00000", "doctor": "Dr. Example"})
    s, _, page = st.req("GET", f"/case/{cid}")
    hid = re.search(r"Hold <b>(H-[\w-]+)</b>", page).group(1)
    check("decision page: 'What was checked' with three areas, ahead of the hold buttons",
          page.index("What was checked") < page.index('name="outcome"')
          and all(f'data-check="{x}"' in page for x in ("Image", "Prescriber and issuance", "Medicines")))
    check("  the hold says its owner (who opened it) and the next step: call now", "owner <b>staff-a</b>" in page
          and "now: call the clinic on the registry number" in page)
    s, _, holds = ph.req("GET", "/holds")
    check("holds page: id, opened, reason, owner, next step", hid in holds and "staff-a" in holds
          and "call the clinic" in holds and "Why it is held" in holds)
    s, loc, _ = ph.post(f"/hold/{hid}/take", {"case": cid})
    s, _, page = ph.req("GET", f"/case/{cid}")
    check("a pharmacist takes the hold: owner changes, history and audit say so", s == 200
          and "owner <b>pharmacist-a</b>" in page and "Took the hold - <b>pharmacist-a</b>" in page
          and r.db().execute("SELECT COUNT(*) FROM audit WHERE action = 'hold-assign' AND ref = ?", (hid,)).fetchone()[0] == 1)
    ph.post(f"/hold/{hid}/resolve", {"outcome": "unreachable", "case": cid, "note": "no answer"})
    s, _, page = ph.req("GET", f"/case/{cid}")
    due = (datetime.now() + timedelta(minutes=app.FOLLOW_UP_MIN)).strftime("%H:%M")
    check("  no answer -> the hold waits; next: call again in 2 h, 1 attempt", "call again at" in page
          and "(1 attempt so far)" in page and "nothing is approved on its own" in page, due)
    app.FOLLOW_UP_MIN = 0
    s, _, holds = ph.req("GET", "/holds")
    check("  past the follow-up time it is marked OVERDUE on the holds page", "OVERDUE since" in holds
          and 'class=late' in holds)
    app.FOLLOW_UP_MIN = 120
    ph.post(f"/hold/{hid}/resolve", {"outcome": "refused", "case": cid, "note": "clinic did not issue it"})
    s, _, holds = ph.req("GET", "/holds")
    check("  closed -> listed under 'Closed in the last 24 hours' with its outcome and who closed it",
          "Closed in the last 24 hours" in holds and "Refused" in holds and "pharmacist-a" in holds)
    check("  a closed hold cannot be taken", ph.post(f"/hold/{hid}/take", {"case": cid})[0] == 409)

    # ---- the audit record: what was observed and what was sent
    row = json.loads(r.db().execute("SELECT detail FROM audit WHERE action = 'check' ORDER BY seq DESC LIMIT 1")
                     .fetchone()[0])
    check("audit 'check': full image fingerprint, case, what was entered, findings and notes - no patient",
          len(row["sha256"]) == 64 and row["case"] == cid and row["entered"]["drugs"] == "Alprax 0.25"
          and row["entered"]["regn"] == "00000" and isinstance(row["notes"], list) and "patient" not in row, row)
    srv.shutdown()
    r.close_db()

print("\nALL PASS" if ok else "\nSOME CHECKS FAILED")
sys.exit(0 if ok else 1)
