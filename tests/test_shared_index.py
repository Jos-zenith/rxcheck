"""Shared (cross-pharmacy) index: a slip dispensed at pharmacy B is recognised at pharmacy A, and nothing readable
leaves either pharmacy. Two pharmacies are two separate RXCHECK_HOME folders; B always acts in its own process.

Run:  python tests/test_shared_index.py
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

import cv2
import numpy as np

from synthetic_slips import (ROOT, r, load_samples, whatsapp, forwarded, photocopy, screen_recapture, added_line,
                       flipped_body, other_slip, cropped, dim_shop_light, shaky)

sys.path.insert(0, str(ROOT / "app"))
import shared_index as sx  # noqa: E402

ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not cond else ""))


def save(tmp, name, img):
    p = tmp / f"{name}.jpg"
    cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tofile(str(p))
    return p


def new_home(tmp, name):
    h = tmp / name
    (h / "data").mkdir(parents=True)
    for f in ("drug_risk.json", "policy.json", "registry.json"):
        shutil.copyfile(ROOT / "data" / f, h / "data" / f)
    return h


def point(h):
    """Make this process act as the pharmacy living in folder h."""
    r.HOME = h
    r.LEDGER_DIR, r.DB, r.FEATURES, r.REPORTS = h / "ledger", h / "ledger" / "rxcheck.db", h / "ledger" / "features", h / "reports"
    r.REGISTRY, r.DRUG_RISK, r.POLICY = h / "data" / "registry.json", h / "data" / "drug_risk.json", h / "data" / "policy.json"


def cli(h, *args, stdin=None):
    return subprocess.run([sys.executable, str(ROOT / "app" / "rxcheck.py"), *args], input=stdin, capture_output=True,
                          text=True, env=dict(os.environ, RXCHECK_HOME=str(h)), timeout=120)


def d(a, b):
    return int(sx._POP[np.bitwise_xor(np.frombuffer(a, np.uint8), np.frombuffer(b, np.uint8))].sum())


orig, fake = load_samples()
today = date.today().isoformat()
KEY = bytes(range(32))
sig = lambda img, key=KEY: sx.signature(key, r.features(r.paper_crop(img))["fp"])

# ---- stage 1: the keyed signature
base = sig(orig)
family = {"WhatsApp": whatsapp(orig), "forwarded 5x": forwarded(orig), "photocopy": photocopy(orig),
          "phone-screen photo": screen_recapture(orig), "edited copy": fake, "added drug line": added_line(orig),
          "added line + WhatsApp": whatsapp(added_line(orig))}
dist = {k: d(base, sig(v)) for k, v in family.items()}
check(f"same slip, {len(family)} kinds of copy: within the stage-1 radius (worst {max(dist.values())} of {sx.HAMMING_MAX} bits)",
      max(dist.values()) <= sx.HAMMING_MAX, str(dist))
others = [d(base, sig(other_slip(i))) for i in (0, 1, 2)]
check(f"unrelated slips are far (nearest {min(others)} bits)", min(others) > sx.HAMMING_MAX, str(others))
other_key = d(base, sig(orig, bytes(range(1, 33))))
check(f"a different key gives an unrelated signature ({other_key} bits): the hub cannot test an image it was not given",
      80 < other_key < 176)
check("signature is 32 bytes and carries no image", len(base) == 32)

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as t:
    tmp = Path(t)
    A, B = new_home(tmp, "pharmacy_A"), new_home(tmp, "pharmacy_B")
    hub, keyfile = tmp / "hub", tmp / "network.key"
    check("hub-init writes the key outside the hub folder",
          cli(tmp, "shared", "hub-init", str(hub), "--key-out", str(keyfile)).returncode == 0
          and keyfile.is_file() and not (hub / "network.key").exists())
    for h in (A, B):
        res = cli(h, "shared", "join", "--hub", str(hub), "--key", str(keyfile))
        check(f"{h.name} joined", res.returncode == 0 and "joined as node" in res.stdout, res.stderr[-200:])
    point(A)
    cfg_a = sx.config()
    point(B)
    cfg_b = sx.config()
    point(A)
    check("each pharmacy has its own pseudonymous node id", cfg_a["node"] != cfg_b["node"] and len(cfg_a["node"]) == 8)

    # registry entry so the prescriber is valid (isolates the shared-index effect from the prescriber rule)
    reg = {"REGN-X": {"name": "Dr. Example", "source": "test fixture", "verified_on": today}}
    A.joinpath("data", "registry.json").write_text(json.dumps(reg), encoding="utf-8")

    # ---- pharmacy B (its own process) dispenses a high-risk slip
    slip_b = save(tmp, "b_slip", orig)
    res = cli(B, "add", str(slip_b), "--id", "RX-B-1", "--dispensed", today, "--regn", "REGN-X", "--doctor",
              "Dr. Example", "--drugs", "Alprax 0.25")
    check("B records the dispensed slip (and publishes it)", res.returncode == 0, res.stderr[-300:])
    st = sx.Hub(hub).stats()
    check("hub holds exactly one entry", st["entries"] == 1 and st["members"] == 2, str(st))

    # ---- what the hub can see
    raw = (hub / "hub.db").read_bytes()
    con = sqlite3.connect(hub / "hub.db")
    cols = {tb: [c[1] for c in con.execute(f"PRAGMA table_info({tb})")] for tb in ("idx", "members")}
    row = con.execute("SELECT node, token, sig, date, kind FROM idx").fetchone()
    con.close()
    check("hub entry = node, opaque token, 32-byte signature, date, D/S - nothing else",
          cols["idx"] == ["node", "token", "sig", "date", "kind"] and len(row[2]) == 32 and row[4] == "D", str(cols))
    secrets_seen = [w for w in (b"Alprax", b"alprazolam", b"REGN-X", b"Dr. Example", b"0.25", b"RX-B-1",
                                r.sha256(slip_b).encode()) if w in raw]
    check("no medicine, Regn No, doctor, local id or image hash anywhere in the hub file", not secrets_seen, str(secrets_seen))

    # ---- pharmacy A meets clones of B's slip
    calls = []
    real = sx.call_peer
    sx.call_peer = lambda *a: (calls.append(a[2]), real(*a))[1]
    clones = {"WhatsApp forward": whatsapp(orig), "cropped photo": cropped(orig),
              "dim shop light": dim_shop_light(orig), "edited clone (added drug line)": added_line(orig),
              "edited clone, then WhatsApp": whatsapp(added_line(orig))}
    for label, img in clones.items():
        rep = r.run_check(save(tmp, "a_" + label.split()[0], img), "Alprax 0.25", draw=False)
        sh, f = rep["shared"], rep["flags"]
        check(f"A catches {label}: seen at another pharmacy ({sh.get('corr')}), AMBER, call the clinic",
              sh["status"] == "match" and f["shared"] and f["repeat"] and not f["new_slip"]
              and rep["verdicts"]["high"] == "AMBER" and rep["path"] == "call" and sh["kind"] == "dispensed",
              f"{sh} {rep['verdicts']} {rep['path']}")
    # known gaps: the compact check inherits the local no-image index's limits. A density-only alignment fallback was
    # tried and rejected: it cannot tell a phone-screen photo (0.71-0.74) from a different slip on the same letterhead (0.82).
    for label, img in {"photo of a phone screen": screen_recapture(orig), "heavily blurred photo": shaky(orig),
                       "forwarded 5 times": forwarded(orig)}.items():
        gap = r.run_check(save(tmp, "gap_" + label.split()[-1], img), "Alprax 0.25", draw=False)
        print(f"[INFO] known gap - {label}: shared lookup says {gap['shared']['status']}; high-risk verdict "
              f"{gap['verdicts']['high']} ({'by the new-slip policy' if gap['flags']['new_slip'] else 'by the shared match'})")
        check(f"known gap - {label}: if the network misses it, the policy still holds a high-risk sale",
              gap["verdicts"]["high"] == "AMBER", str(gap["verdicts"]))
    pc = r.run_check(save(tmp, "gap_photocopy", photocopy(orig)), "Alprax 0.25", draw=False)
    check("a washed-out photocopy is sent back for a retake before any network call (quality gate)",
          pc["flags"]["unusable"] and pc["shared"]["status"] == "off" and pc["path"] == "retake", str(pc["shared"]))
    check("the report names no pharmacy, only the date and kind",
          "pharmacies" in rep["shared"] and cfg_b["node"] not in json.dumps(rep["shared"]))
    check("shared hits never RED (without the image, edits cannot be located)",
          "RED" not in rep["verdicts"].values())
    check("a routine request on the same slip is not blocked by the layout match alone",
          r.decide({**rep["flags"], "mismatch": False}, "routine") == "GREEN")

    rep = r.run_check(save(tmp, "a_strength", whatsapp(orig)), "Alprax 0.5", draw=False)
    check("raised strength is noticed without the medicine name crossing the wire",
          any("strength differs" in x for x in rep["findings"]) and rep["flags"]["drug_mismatch"], str(rep["findings"]))
    rep = r.run_check(save(tmp, "a_added", whatsapp(orig)), "Alprax 0.25, Tramadol 50", draw=False)
    held = {i["item"]: i["verdict"] for i in rep["items"]}
    check("an added medicine is noticed and held by itself", any("tramadol" in x for x in rep["findings"])
          and held.get("Tramadol 50") == "AMBER", str(held))

    # ---- different slips
    calls.clear()
    rep = r.run_check(save(tmp, "a_other", other_slip()), "Alprax 0.25", regn="REGN-X", doctor="Dr. Example", draw=False)
    check("an unrelated slip is clear and never sent to a peer", rep["shared"]["status"] == "clear" and not calls
          and rep["flags"]["new_slip"], str(rep["shared"]))
    f_sig = sig(flipped_body(orig))
    if d(base, f_sig) <= sx.HAMMING_MAX:
        rep = r.run_check(save(tmp, "a_flipped", flipped_body(orig)), "Alprax 0.25", regn="REGN-X", doctor="Dr. Example",
                          draw=False)
        check(f"stage 1 collides ({d(base, f_sig)} bits) but stage 2 rejects it: a layout hit alone is never a verdict",
              calls and rep["shared"]["status"] == "clear" and rep["flags"]["new_slip"] and not rep["flags"].get("shared"),
              f"{calls and len(calls)} {rep['shared']}")
    else:
        print("[INFO] no stage-1 collision among the fixtures; stage-2 rejection not exercised")

    # ---- local first: a slip A already knows is not sent anywhere
    first = r.run_check(save(tmp, "a_local1", whatsapp(orig, angle=2.0)), "Alprax 0.25", draw=False)
    r.record_check(first, "Alprax 0.25")
    calls.clear()
    again = r.run_check(save(tmp, "a_local2", forwarded(orig)), "Alprax 0.25", draw=False)
    check("a slip already in A's own index is decided locally: no network call",
          not calls and again["flags"]["repeat"] and not again["flags"].get("shared"), f"{len(calls)}")
    sx.call_peer = real

    # ---- what a peer discloses, and to whom
    compact = r.compact(first["_features"], first["_dens"])
    msg_kb = len(sx.pack_compact(compact)) / 1024
    check(f"stage-2 message is a compact entry only ({msg_kb:.0f} KB, limit {sx.MSG_MAX_BYTES // 1024} KB)",
          msg_kb * 1024 < sx.MSG_MAX_BYTES)
    cands, _ = sx.Hub(hub).query(cfg_a["node"], sx.signature(cfg_a["key"], first["_features"]["fp"]), "2000-01-01")
    tok = cands[0]["token"]

    def ask(req, home=B):
        return json.loads(cli(home, "shared", "answer", stdin=json.dumps(req)).stdout)

    good = ask(sx.make_request(cfg_a, tok, compact))
    check("B answers a match with date, kind and keyed tags only",
          set(good) == {"match", "corr", "date", "kind", "drug_tags"} and good["match"] and good["kind"] == "dispensed",
          str(good))
    txt = json.dumps(good).lower()
    check("the answer carries no medicine name, strength, Regn No, doctor or id",
          not any(w in txt for w in ("alprax", "alprazolam", "0.25", "regn", "example", "rx-b")), txt[:200])
    other_c = r.compact(r.features(r.paper_crop(other_slip())), r.density(r.paper_crop(other_slip())))
    check("a non-match answer is just {match: false}", ask(sx.make_request(cfg_a, tok, other_c)) == {"match": False})
    check("an unknown token is also just {match: false}", ask(sx.make_request(cfg_a, "0" * 24, compact)) == {"match": False})
    req = sx.make_request(cfg_a, tok, compact)
    check("tampered request refused", ask({**req, "body": req["body"].replace(tok, "1" * 24)}).get("error") == "not authenticated")
    forged = sx.make_request({**cfg_a, "key": bytes(range(5, 37))}, tok, compact)
    check("request signed with another key refused", ask(forged).get("error") == "not authenticated")
    stale = {**req, "ts": req["ts"] - 3600}
    check("replayed (stale) request refused", ask(stale).get("error") == "not authenticated")
    big = {**req, "body": req["body"] + " " * sx.MSG_MAX_BYTES}
    check("oversized request refused", ask(big).get("error") == "message too large")
    point(B)
    log = [dict(x) for x in r.db().execute("SELECT detail FROM audit WHERE action = 'shared-answer'")]
    point(A)
    check("B's audit trail records who asked and whether it matched (no content)",
          len(log) >= 5 and all("asked_by" in json.loads(x["detail"]) for x in log)
          and not any("alprax" in x["detail"].lower() for x in log))

    # ---- hub or peer down: never more lenient, re-checked afterwards
    A.joinpath("data", "policy.json").write_text(json.dumps({"confirm_new_high_risk": False}), encoding="utf-8")
    fresh = save(tmp, "a_fresh", other_slip(5))
    rep = r.run_check(fresh, "Alprax 0.25", regn="REGN-X", doctor="Dr. Example", draw=False)
    check("baseline: with the policy off, a new slip from a valid prescriber is GREEN",
          rep["verdicts"]["high"] == "GREEN", str(rep["verdicts"]))
    slip_b2 = save(tmp, "b_slip2", other_slip(7))
    check("B dispenses another slip", cli(B, "add", str(slip_b2), "--id", "RX-B-2", "--dispensed", today, "--regn", "REGN-X",
                                           "--doctor", "Dr. Example", "--drugs", "Alprax 0.25").returncode == 0)
    (hub / "OFFLINE").write_text("down", encoding="utf-8")
    rep = r.run_check(save(tmp, "a_offline", whatsapp(other_slip(7))), "Alprax 0.25", regn="REGN-X", doctor="Dr. Example",
                      draw=False)
    check("hub down: high-risk waits for confirmation even with the policy off",
          rep["shared"]["status"] == "unreachable" and rep["flags"]["shared_unreachable"]
          and rep["verdicts"]["high"] == "AMBER" and rep["path"] == "call", f"{rep['shared']} {rep['verdicts']}")
    check("hub down: the step says why", any("shared index could not be reached" in s for s in rep["steps"]), str(rep["steps"]))
    rout = r.run_check(save(tmp, "a_offline_r", whatsapp(other_slip(7))), "Dolo 650", draw=False)
    check("hub down: routine sales continue", rout["verdicts"]["routine" if "routine" in rout["verdicts"] else "high"] == "GREEN",
          str(rout["verdicts"]))
    r.record_check(rep, "Alprax 0.25", "REGN-X", "Dr. Example")
    con = r.db()
    pending = con.execute("SELECT published, unchecked FROM shared_map WHERE unchecked = 1").fetchall()
    check("the slip is recorded as unchecked and unpublished, waiting for sync", len(pending) == 1 and pending[0][0] == 0,
          str([tuple(x) for x in pending]))
    s_off = sx.sync()
    check("sync while still down changes nothing and says so", s_off["still_offline"] and s_off["rechecked"] == 0, str(s_off))
    (hub / "OFFLINE").unlink()
    holds_before = con.execute("SELECT COUNT(*) FROM holds").fetchone()[0]
    s_on = sx.sync()
    check("connection back: unpublished entry published, offline check repeated, late match reported",
          not s_on["still_offline"] and s_on["published"] >= 1 and s_on["rechecked"] == 1 and len(s_on["late_matches"]) == 1,
          str(s_on))
    check("the late match opens a hold and is in the audit trail",
          con.execute("SELECT COUNT(*) FROM holds").fetchone()[0] == holds_before + 1
          and con.execute("SELECT COUNT(*) FROM audit WHERE action = 'shared-late-match'").fetchone()[0] == 1)
    check("a second sync finds nothing left to do", sx.sync()["rechecked"] == 0)

    # ---- off by default, and scale
    C = new_home(tmp, "pharmacy_C")
    point(C)
    check("a pharmacy that has not joined: no network code runs", sx.lookup({"fp": base}, None, None) == {"status": "off"}
          and sx.publish("x", "seen", base, today) is None)
    point(A)
    big = sx.Hub(tmp / "big")
    big.register("n0", tmp)
    rng = np.random.default_rng(3)
    con2 = sqlite3.connect(tmp / "big" / "hub.db")
    con2.executemany("INSERT INTO idx VALUES (?, ?, ?, ?, ?)",
                     [(f"n{i % 50}", f"{i:024d}", rng.bytes(32), today, "S") for i in range(20000)])
    con2.commit()
    con2.close()
    t0 = time.perf_counter()
    cands, total = big.query("nx", base, "2000-01-01")
    ms = (time.perf_counter() - t0) * 1000
    check(f"hub query over {total} entries: {ms:.0f} ms (random entries are almost never within {sx.HAMMING_MAX} bits: "
          f"{len(cands)} candidates)", ms < 1000 and len(cands) <= sx.MAX_CANDIDATES)
    r.close_db()

print("\nALL PASSED" if ok else "\nFAILURES")
sys.exit(0 if ok else 1)
