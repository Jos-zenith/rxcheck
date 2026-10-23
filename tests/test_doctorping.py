"""Authenticated doctor confirmation: enrolment from the council register, a minimal ping, app-tap and one-time-code
answers, signed receipts bound to one prescription, and what a receipt changes in hold resolution.

Run:  python tests/test_doctorping.py
"""
import inspect
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

import cv2

from scenarios import ROOT, r, load_samples, other_slip

sys.path.insert(0, str(ROOT / "app"))
import doctorping as dp  # noqa: E402

ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not cond else ""))


def raises(exc, fn, *a, **k):
    try:
        fn(*a, **k)
    except exc as ex:
        return str(ex) or "raised"
    return None


def new_home(tmp, name, policy=None):
    h = tmp / name
    (h / "data").mkdir(parents=True)
    for f in ("drug_risk.json", "policy.json", "registry.json"):
        shutil.copyfile(ROOT / "data" / f, h / "data" / f)
    if policy:
        (h / "data" / "policy.json").write_text(json.dumps(policy), encoding="utf-8")
    return h


def point(h):
    r.HOME = h
    r.LEDGER_DIR, r.DB, r.FEATURES, r.REPORTS = h / "ledger", h / "ledger" / "rxcheck.db", h / "ledger" / "features", h / "reports"
    r.REGISTRY, r.DRUG_RISK, r.POLICY = h / "data" / "registry.json", h / "data" / "drug_risk.json", h / "data" / "policy.json"


today = date.today().isoformat()
off = [0.0]
dp._clock = lambda: time.time() + off[0]     # the whole flow runs on this clock, so expiry can be tested
PHONE = "+91 98400 11122"
COUNCIL = {"REGN-X": {"name": "Dr. Example", "source": "council register (test)", "verified_on": today,
                      "phone": PHONE, "phone_source": "council register (test)"},
           "REGN-NOPHONE": {"name": "Dr. NoPhone", "source": "council register (test)", "verified_on": today}}


def next_hour():
    off[0] += 3700


with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as t:
    tmp = Path(t)
    P, Q = new_home(tmp, "pharmacy_P"), new_home(tmp, "pharmacy_Q")
    S = tmp / "service"
    svc = dp.Service(S)
    pub = svc.init()
    check("service keys created", (S / "service.key").is_file() and (S / "service.pub").read_text().strip() == pub)

    # ---- enrolment: the contact comes from the council register
    check("enrolment takes no contact argument: nobody can choose where the code goes",
          "contact" not in inspect.signature(svc.enrol_start).parameters)
    check("no sourced phone on the register -> no enrolment",
          raises(dp.Refused, svc.enrol_start, "REGN-NOPHONE", COUNCIL["REGN-NOPHONE"]) is not None)
    res = svc.enrol_start("REGN-X", COUNCIL["REGN-X"])
    code = svc.inbox(PHONE)[-1]["otp"]
    check("code goes to the register's number; the caller only learns a masked hint",
          res == {"contact_hint": "******22"} and len(code) == 6 and "98400" not in json.dumps(res))
    dev_priv, dev_pub = dp.new_keypair()
    check("squatter without the code cannot enrol the doctor",
          raises(dp.Refused, svc.enrol_confirm, "REGN-X", "000000" if code != "000000" else "111111", dev_pub) is not None)
    check("right code enrols and registers the doctor's device key", svc.enrol_confirm("REGN-X", code, dev_pub) == {"status": "active"})
    check("the code is single use", raises(dp.Refused, svc.enrol_confirm, "REGN-X", code, dev_pub) is not None)
    check("an enrolled doctor cannot be re-enrolled by someone else",
          "already enrolled" in (raises(dp.Refused, svc.enrol_start, "REGN-X", COUNCIL["REGN-X"]) or ""))
    COUNCIL["REGN-L"] = {**COUNCIL["REGN-X"], "name": "Dr. Lock"}
    svc.enrol_start("REGN-L", COUNCIL["REGN-L"])
    lc = svc.inbox(PHONE)[-1]["otp"]
    for _ in range(dp.ENROL_MAX_TRIES):
        raises(dp.Refused, svc.enrol_confirm, "REGN-L", "999999" if lc != "999999" else "888888", dev_pub)
    check("5 wrong codes lock the enrolment, even for the right code later",
          raises(dp.Refused, svc.enrol_confirm, "REGN-L", lc, dev_pub) is not None)

    # ---- the pharmacy
    point(P)
    check("service refused before the pharmacy trusts one", dp.ask("H-0", "REGN-X", "Alprax 0.25", "AB, 30", today)["status"] == "not_configured")
    node_p = dp.trust(S)
    point(Q)
    node_q = dp.trust(S)
    point(P)
    check("each pharmacy is a pseudonymous node; only the service's PUBLIC key is stored",
          node_p != node_q and "service.key" not in (P / "data" / "doctorsvc.json").read_text())
    check("pharmacy-side ask() has no contact parameter",
          "contact" not in inspect.signature(dp.ask).parameters and "phone" not in inspect.signature(dp.ask).parameters)

    orig, _ = load_samples()
    counter = [0]

    def make_hold(home=None):
        """A hold on a high-risk (NDPS) item, as run_check/open_hold would leave it."""
        counter[0] += 1
        img = tmp / f"slip{counter[0]}.jpg"
        cv2.imencode(".jpg", other_slip(counter[0]))[1].tofile(str(img))
        hid = f"H-TEST-{counter[0]}"
        con = r.db()
        with con:
            con.execute("INSERT INTO holds VALUES (?, ?, ?, ?, ?, 'open')",
                        (hid, r.now(), str(img), json.dumps({"high": "AMBER"}), json.dumps(["first time seen"])))
            r.audit(con, "hold", hid, {"path": "call", "settled_by": [], "fail_closed": ["Alprax 0.25"]})
        return hid

    def doctor_answers(rid, decision, key=dev_priv):
        sha = svc._con().execute("SELECT summary_sha FROM requests WHERE id = ?", (rid,)).fetchone()[0]
        return svc.respond(rid, decision, dp.device_sign(key, rid, decision, sha))

    def last_otp():
        return [m for m in svc.inbox(PHONE) if m["type"] == "confirm-request"][-1]["otp"]

    # ---- not enrolled
    h0 = make_hold()
    ne = dp.ask(h0, "REGN-ZZ", "Alprax 0.25", "AB, 30", today)
    check("a doctor who is not enrolled: nothing sent, the hold falls back to a manual call",
          ne["status"] == "not_enrolled" and dp.hold_decision(h0) is None
          and r.db().execute("SELECT COUNT(*) FROM doctor_asks").fetchone()[0] == 0)

    # ---- the ping
    hold_a = make_hold()
    res = dp.ask(hold_a, "REGN-X", "Alprax 0.25", "Ravi Kumar, 34", today)
    msg = [m for m in svc.inbox(PHONE) if m["type"] == "confirm-request"][-1]
    check("ping pending, doctor's number masked in the reply",
          res["status"] == "pending" and res["contact_hint"] == "******22" and "98400" not in json.dumps(res), str(res))
    check("the doctor's phone gets initials and age, the medicines and the date - never the name",
          msg["patient"] == "RK, 34" and "Ravi" not in json.dumps(msg) and "Kumar" not in json.dumps(msg)
          and msg["drugs"] == "Alprax 0.25" and msg["rx_date"] == today and msg["from"].startswith("pharmacy "), str(msg))
    check("neither the image hash nor the pharmacy's name is in the message",
          r.sha256(Path(r.db().execute("SELECT image FROM holds WHERE id = ?", (hold_a,)).fetchone()[0])) not in json.dumps(msg))
    check("the service itself refuses a full name", "initials" in (raises(dp.Refused, svc.request, node_p, "REGN-X",
          "Ravi Kumar, 34", "Alprax 0.25", "alprazolam 0.25", today, "") or ""))
    check("no answer yet means no decision (nothing is auto-approved)", dp.hold_decision(hold_a) is None
          and dp.poll(res["request_id"])["status"] == "pending")

    # ---- a fraudster answering as the doctor
    sha_a = svc._con().execute("SELECT summary_sha FROM requests WHERE id = ?", (res["request_id"],)).fetchone()[0]
    fake_priv, _ = dp.new_keypair()
    check("a signature from any other key is refused",
          raises(dp.Refused, svc.respond, res["request_id"], "approved", dp.device_sign(fake_priv, res["request_id"], "approved", sha_a)) is not None)
    check("an approval signature cannot be replayed as a denial",
          raises(dp.Refused, svc.respond, res["request_id"], "denied", dp.device_sign(dev_priv, res["request_id"], "approved", sha_a)) is not None)
    check("the request is still open after those attempts", dp.poll(res["request_id"])["status"] == "pending")

    # ---- app tap approval
    check("the doctor's device approves", doctor_answers(res["request_id"], "approved") == {"status": "approved"})
    check("poll returns approved", dp.poll(res["request_id"])["status"] == "approved")
    said = dp.hold_decision(hold_a)
    check("verified receipt: approved via the app, for this request", said and said["decision"] == "approved"
          and said["via"] == "app" and said["request_id"] == res["request_id"], str(said))
    con = r.db()
    row = dict(con.execute("SELECT * FROM doctor_asks WHERE request_id = ?", (res["request_id"],)).fetchone())
    check("the receipt verifies against the service's public key",
          dp.verify_sig(pub, row["receipt"].encode(), row["sig"]))
    body = json.loads(row["receipt"])
    check("the receipt names doctor, decision, channel, pharmacy and the hash of what was asked",
          body["regn"] == "REGN-X" and body["decision"] == "approved" and body["node"] == node_p
          and body["summary_sha"] == row["summary_sha"] and "Ravi" not in row["receipt"] and "Alprax" not in row["receipt"])

    # ---- tampered / misapplied receipts are worth nothing
    def with_row(changes, hid=hold_a):
        old = dict(row)
        with con:
            con.execute(f"UPDATE doctor_asks SET {', '.join(k + ' = ?' for k in changes)} WHERE request_id = ?",
                        (*changes.values(), row["request_id"]))
        got = dp.hold_decision(hid)
        with con:
            con.execute("UPDATE doctor_asks SET receipt = ?, sig = ?, summary_sha = ?, hold_id = ? WHERE request_id = ?",
                        (old["receipt"], old["sig"], old["summary_sha"], old["hold_id"], row["request_id"]))
        return got

    check("edited receipt text -> no receipt", with_row({"receipt": row["receipt"].replace("REGN-X", "REGN-Y")}) is None)
    check("receipt for a different prescription (hash mismatch) -> no receipt", with_row({"summary_sha": "0" * 64}) is None)
    check("signature from another key -> no receipt", with_row({"sig": dp.sign(fake_priv, row["receipt"].encode())}) is None)
    point(Q)
    with r.db() as qcon:
        qcon.executescript(dp.CLIENT_SCHEMA)
        qhold = make_hold()
        qcon.execute("INSERT INTO doctor_asks VALUES (?, ?, ?, ?, ?, 'approved', 'app', ?, ?)",
                     (row["request_id"], qhold, "REGN-X", row["summary_sha"], r.now(), row["receipt"], row["sig"]))
    check("another pharmacy cannot use this receipt (it names the pharmacy it was issued to)",
          dp.hold_decision(qhold) is None)
    point(P)
    check("original still verifies after all of that", dp.hold_decision(hold_a)["decision"] == "approved")

    # ---- what a receipt changes in resolution
    r.resolve_hold(hold_a, "confirmed")
    det = json.loads(r.db().execute("SELECT detail FROM audit WHERE action = 'resolve' AND ref = ?", (hold_a,)).fetchone()[0])
    check("confirmed after a verified receipt is recorded as AUTHENTICATED, with channel and request",
          det["authenticated"] is True and det["via"] == "app" and det["request"] == res["request_id"], str(det))
    h_word = make_hold()
    r.resolve_hold(h_word, "confirmed")
    det = json.loads(r.db().execute("SELECT detail FROM audit WHERE action = 'resolve' AND ref = ?", (h_word,)).fetchone()[0])
    check("confirmed on the pharmacist's word is still possible, and recorded as NOT authenticated",
          det["authenticated"] is False and det["via"] is None)

    # ---- one-time code read out to the pharmacist
    next_hour()
    hold_b = make_hold()
    rb = dp.ask(hold_b, "REGN-X", "Alprax 0.25", "AB, 40", today)["request_id"]
    otp = last_otp()
    bad = "000000" if otp != "000000" else "111111"
    w1 = dp.submit_otp(rb, bad)
    check("wrong code: refused, 2 tries left", w1 == {"status": "wrong_otp", "tries_left": 2}, str(w1))
    check("a wrong code leaves the request open (it is not a state of the request)",
          [x["status"] for x in dp.asks(hold_b)] == ["pending"], str(dp.asks(hold_b)))
    point(Q)
    check("another pharmacy cannot submit the code for this request (not its request)",
          dp.submit_otp(rb, otp)["status"] == "refused")
    point(P)
    ok_otp = dp.submit_otp(rb, otp)
    said = dp.hold_decision(hold_b)
    check("right code: approved via the read-out channel", ok_otp["status"] == "approved" and said and said["via"] == "otp", str(ok_otp))
    check("the code is single use: submitting it again changes nothing", dp.submit_otp(rb, otp)["status"] == "approved"
          and r.db().execute("SELECT COUNT(*) FROM audit WHERE action = 'doctor-receipt' AND ref = ?", (hold_b,)).fetchone()[0] == 1)

    next_hour()
    hold_c = make_hold()
    rc = dp.ask(hold_c, "REGN-X", "Alprax 0.25", "AB, 40", today)["request_id"]
    otp_c = last_otp()
    wrong = "000000" if otp_c != "000000" else "111111"
    st = [dp.submit_otp(rc, wrong)["status"] for _ in range(dp.OTP_MAX_TRIES)]
    check("3 wrong codes lock the request", st[-1] == "locked", str(st))
    check("a locked request cannot be approved even with the right code",
          dp.submit_otp(rc, otp_c)["status"] == "locked" and dp.hold_decision(hold_c) is None)

    next_hour()
    hold_d = make_hold()
    rd = dp.ask(hold_d, "REGN-X", "Alprax 0.25", "AB, 40", today)["request_id"]
    otp_d = last_otp()
    hold_e = make_hold()
    re_ = dp.ask(hold_e, "REGN-X", "Alprax 0.5", "AB, 40", today)["request_id"]
    check("the code of one request does not work for another", dp.submit_otp(re_, otp_d)["status"] == "wrong_otp")
    off[0] += dp.REQUEST_TTL + 5
    check("after 15 minutes the code is dead", dp.submit_otp(rd, otp_d)["status"] == "expired" and dp.hold_decision(hold_d) is None)
    check("and the doctor can no longer answer it in the app", "no longer open" in (raises(dp.Refused, doctor_answers, rd, "approved") or ""))

    # ---- denial
    next_hour()
    hold_f = make_hold()
    rf = dp.ask(hold_f, "REGN-X", "Alprax 0.25", "AB, 40", today)["request_id"]
    otp_f = last_otp()
    doctor_answers(rf, "denied")
    dp.poll(rf)
    said = dp.hold_decision(hold_f)
    check("the doctor denies in the app: verified denial", said and said["decision"] == "denied" and said["via"] == "app")
    check("a denied request cannot be approved with its code afterwards",
          dp.submit_otp(rf, otp_f)["status"] == "denied" and dp.hold_decision(hold_f)["decision"] == "denied")
    check("'confirmed' on the pharmacist's word is refused after a denial",
          "DENIED" in (raises(ValueError, r.resolve_hold, hold_f, "confirmed") or ""))
    check("so is dispensing after review",
          "DENIED" in (raises(ValueError, r.resolve_hold, hold_f, "dispensed-after-review", "x" * 20, "other") or ""))
    off[0] += dp.RECEIPT_TTL + 3600
    check("a denial does not expire: still refused after 24 hours",
          "DENIED" in (raises(ValueError, r.resolve_hold, hold_f, "confirmed") or ""))
    cfg_file = P / "data" / dp.CONFIG_FILE
    cfg_file.rename(cfg_file.with_suffix(".off"))
    check("a denial survives removing the service config",
          "DENIED" in (raises(ValueError, r.resolve_hold, hold_f, "confirmed") or ""))
    cfg_file.with_suffix(".off").rename(cfg_file)
    check("refusing is allowed", r.resolve_hold(hold_f, "refused")[0] == "closed")

    # ---- spam
    next_hour()
    got = [dp.ask(make_hold(), "REGN-X", "Alprax 0.25", "AB, 40", today)["status"] for _ in range(dp.MAX_PENDING_PER_DOCTOR_HOUR + 1)]
    check(f"a doctor is pinged at most {dp.MAX_PENDING_PER_DOCTOR_HOUR} times an hour", got[-1] == "rate_limited" and got[:-1] == ["pending"] * 3, str(got))

    # ---- stale receipts
    next_hour()
    hold_g = make_hold()
    rg = dp.ask(hold_g, "REGN-X", "Alprax 0.25", "AB, 40", today)["request_id"]
    doctor_answers(rg, "approved")
    dp.poll(rg)
    check("fresh receipt counts", dp.hold_decision(hold_g)["decision"] == "approved")
    off[0] += dp.RECEIPT_TTL + 60
    check("a receipt older than 24 hours no longer counts", dp.hold_decision(hold_g) is None)

    # ---- policy: NDPS only on the prescriber's receipt
    next_hour()
    (P / "data" / "policy.json").write_text(json.dumps({"confirm_new_high_risk": True, "require_receipt_ndps": True}), encoding="utf-8")
    hold_h = make_hold()
    check("policy on: NDPS confirmation on the pharmacist's word alone is refused",
          "receipt" in (raises(ValueError, r.resolve_hold, hold_h, "confirmed") or ""))
    rh = dp.ask(hold_h, "REGN-X", "Alprax 0.25", "AB, 40", today)["request_id"]
    doctor_answers(rh, "approved")
    dp.poll(rh)
    check("policy on: with the doctor's receipt it is accepted", r.resolve_hold(hold_h, "confirmed")[0] == "closed")
    (P / "data" / "policy.json").write_text(json.dumps({"confirm_new_high_risk": True}), encoding="utf-8")

    # ---- service down
    next_hour()
    (S / "OFFLINE").write_text("down", encoding="utf-8")
    hold_i = make_hold()
    down = dp.ask(hold_i, "REGN-X", "Alprax 0.25", "AB, 40", today)
    check("service unreachable: reported, nothing recorded, nothing approved",
          down["status"] == "unreachable" and dp.hold_decision(hold_i) is None
          and r.db().execute("SELECT COUNT(*) FROM doctor_asks WHERE hold_id = ?", (hold_i,)).fetchone()[0] == 0)
    check("the hold can still wait for a call-back", r.resolve_hold(hold_i, "unreachable")[0] == "waiting")
    (S / "OFFLINE").unlink()

    # ---- what the pharmacy's audit trail holds
    rows = [x[0] for x in r.db().execute("SELECT detail FROM audit WHERE action IN ('doctor-ask', 'doctor-receipt')")]
    check("pharmacy audit: requests and receipts recorded, with no patient name, initials or medicine",
          rows and not any(w in " ".join(rows) for w in ("Ravi", "Kumar", "RK", "Alprax", "alprazolam")), str(rows[:2]))
    check("the audit chain is intact after all of it", r.check_chain(r.db()) == [], str(r.check_chain(r.db())))

    # ---- end to end through the CLI, in separate processes
    cli = lambda home, *args: subprocess.run([sys.executable, str(ROOT / "app" / "rxcheck.py"), *args], capture_output=True,
                                             text=True, env=dict(os.environ, RXCHECK_HOME=str(home)), timeout=120)
    R = new_home(tmp, "pharmacy_R")
    S2 = tmp / "service2"
    reg_file = tmp / "council.json"
    reg_file.write_text(json.dumps(COUNCIL), encoding="utf-8")
    dkey = tmp / "doctor_phone.key"
    steps = [cli(R, "doctor", "service-init", "--service", str(S2)),
             cli(R, "doctor", "enrol", "--service", str(S2), "--regn", "REGN-X", "--registry", str(reg_file))]
    otp_e = dp.Service(S2).inbox(PHONE)[-1]["otp"]
    steps += [cli(R, "doctor", "enrol-confirm", "--service", str(S2), "--regn", "REGN-X", "--otp", otp_e, "--device-key", str(dkey)),
              cli(R, "doctor", "trust", "--service", str(S2))]
    cli_slip = tmp / "cli_slip.jpg"
    cv2.imencode(".jpg", other_slip(11))[1].tofile(str(cli_slip))
    chk = cli(R, "check", str(cli_slip), "--drugs", "Alprax 0.25")
    hid = re.search(r"hold: (H-\S+)", chk.stdout).group(1)
    ask = cli(R, "doctor", "ask", "--hold", hid, "--regn", "REGN-X", "--drugs", "Alprax 0.25", "--patient", "Ravi Kumar, 34",
              "--rx-date", today)
    rid = json.loads(ask.stdout)["request_id"]
    inbox = cli(R, "doctor", "inbox", "--service", str(S2), "--contact", PHONE)
    steps += [chk, ask, inbox]
    check("CLI: service, enrolment, trust, check and ask all succeed in separate processes",
          all(s.returncode == 0 for s in steps) and "RK, 34" in inbox.stdout and "Ravi" not in inbox.stdout,
          " | ".join(s.stderr[-120:] for s in steps if s.returncode))
    before = cli(R, "resolve", hid, "--outcome", "confirmed")
    check("CLI: resolve before any answer still works on the pharmacist's word (not authenticated)",
          before.returncode == 0)
    cli(R, "doctor", "respond", rid, "--service", str(S2), "--device-key", str(dkey))
    st = cli(R, "doctor", "status", rid)
    check("CLI: the doctor approves from the phone key, the pharmacy sees approved",
          json.loads(st.stdout)["status"] == "approved", st.stdout + st.stderr[-200:])

    r.close_db()

print("\nALL PASSED" if ok else "\nFAILURES")
sys.exit(0 if ok else 1)
