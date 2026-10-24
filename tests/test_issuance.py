"""Clinic-signed issuance codes: the independent path for new prescriptions that image matching cannot judge.

Run:  python tests/test_issuance.py
"""
import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

import cv2
import numpy as np

from scenarios import (r, load_samples, whatsapp, forwarded, photocopy, angled_photo, dim_shop_light,
                       screen_recapture, flipped_body, mirrored_body)

ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not cond else ""))


def print_qr(slip, code, frac=0.22):
    """The code as a clinic would print it: ~3.3 cm wide on an A5 slip, bottom left."""
    m = r.code_matrix(code)
    h, w = slip.shape[:2]
    side = int(frac * w)
    q = cv2.resize((~m).astype(np.uint8) * 255, (side, side), interpolation=cv2.INTER_AREA)
    out = slip.copy()
    y, x = int(0.78 * h), int(0.06 * w)
    out[y:y + side, x:x + side] = cv2.cvtColor(q, cv2.COLOR_GRAY2BGR)
    return out


def save(tmp, name, img):
    p = tmp / f"{name}.jpg"
    cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tofile(str(p))
    return p


orig, _ = load_samples()
today = date.today().isoformat()

# ---- encoding
rng = np.random.default_rng(1)
blobs = [bytes(rng.integers(0, 256, n, dtype=np.uint8)) for n in (0, 1, 2, 3, 65, 128, 131)]
check("base45 round-trips arbitrary bytes", all(r.b45decode(r.b45encode(b)) == b for b in blobs))

with tempfile.TemporaryDirectory() as t:
    tmp = Path(t)
    r.LEDGER_DIR, r.DB, r.FEATURES, r.REPORTS = tmp, tmp / "rxcheck.db", tmp / "features", tmp / "reports"
    pub = r.clinic_keygen(tmp / "clinic.pem")
    other_pub = r.clinic_keygen(tmp / "other.pem")
    key, other = (tmp / "clinic.pem").read_bytes(), (tmp / "other.pem").read_bytes()
    r.REGISTRY = tmp / "registry.json"
    reg = {"00000": {"name": "Dr. Example", "source": "test fixture", "verified_on": today,
                     "issuer_key": pub, "issuer_key_source": "handed over in person (test)"},
           "55555": {"name": "Dr. Untrusted Key", "source": "test fixture", "verified_on": today,
                     "issuer_key": other_pub}}   # key without issuer_key_source
    r.REGISTRY.write_text(json.dumps(reg), encoding="utf-8")

    code = r.make_code(key, "00000", today, "Alprax 0.25, Dolo 650", "Sample Patient")
    v = r.verify_code(code)
    check(f"signed code verifies ({len(code)} chars, QR {r.code_matrix(code).shape[0] - 8} modules)",
          v["status"] == "verified" and v["fields"]["drugs"] == "Alprax 0.25, Dolo 650"
          and v["fields"]["date"] == today and v["fields"]["patient"] == "Sample Patient")
    check("QR stays small enough to print (<= version 7, 45 modules)", r.code_matrix(code).shape[0] - 8 <= 45)

    # one changed character anywhere must never verify (a changed Regn No may point at a doctor with no key
    # on record: then the code counts as absent - the slip is judged like any unsigned slip)
    results = []
    for i in range(len(r.ISSUE_PREFIX), len(code)):
        bad = code[:i] + ("0" if code[i] != "0" else "1") + code[i + 1:]
        results.append(r.verify_code(bad)["status"])
    check(f"altered codes never verify ({results.count('invalid')} invalid, {results.count('unverifiable')} "
          f"unverifiable of {len(results)} single-character changes)", "verified" not in results)
    forged = r.make_code(other, "00000", today, "Alprax 0.25, Dolo 650", "Sample Patient")
    check("code signed with another key, claiming this doctor -> invalid", r.verify_code(forged)["status"] == "invalid")
    check("key on record without issuer_key_source -> not trusted (unverifiable)",
          r.verify_code(r.make_code(other, "55555", today, "Dolo 650"))["status"] == "unverifiable")
    check("no key on record for that Regn No -> unverifiable",
          r.verify_code(r.make_code(key, "11111", today, "Dolo 650"))["status"] == "unverifiable")
    for args, what in ((("00000", "2026-13-01", "Dolo 650"), "bad date"), (("00000", today, "x" * 121), "too long"),
                       (("", today, "Dolo 650"), "no Regn No")):
        try:
            r.make_code(key, *args)
            check(f"refuses to sign: {what}", False)
        except ValueError:
            check(f"refuses to sign: {what}", True)

    # ---- the printed code survives what happens to a slip before it reaches the counter
    slip = print_qr(flipped_body(orig), code)
    variants = [("as printed", slip), ("WhatsApp", whatsapp(slip)), ("forwarded 5x", forwarded(slip)),
                ("dark, forwarded", forwarded(slip, 5, 0.5)), ("photocopy", photocopy(slip)),
                ("photo at an angle", angled_photo(slip)), ("dim shop light", dim_shop_light(slip)),
                ("phone screen", screen_recapture(slip))]
    read = [name for name, img in variants if code in r.find_codes(r.paper_crop(img))]
    check(f"QR read after counter conditions: {len(read)}/{len(variants)}", len(read) >= len(variants) - 1,
          f"missed {[n for n, _ in variants if n not in read]}")
    plain = r.find_codes(r.paper_crop(orig))
    check("slip without a QR code: nothing found", plain == [])

    # ---- end to end in a throw-away ledger: verdicts with and without a verified code
    def run(img_path, drugs, **kw):
        rep = r.run_check(img_path, drugs, draw=False, **kw)
        return rep, rep["verdicts"][rep["risk"]] if rep["risk"] else rep["verdicts"]

    p_signed = save(tmp, "signed", whatsapp(slip))
    p_unsigned = save(tmp, "unsigned", whatsapp(flipped_body(orig)))
    rep, v = run(p_unsigned, "Alprax 0.25", regn="00000", doctor="Example")
    check("new high-risk slip, valid doctor, no code -> AMBER (confirm issuance)", v == "AMBER", rep["reason"])
    rep, v = run(p_signed, "Alprax 0.25")
    check("same, with a verified clinic code -> GREEN, no call needed", v == "GREEN", rep["reason"])
    check("  Regn No prefilled from the signed code; issuance shown", rep["issuance"]["status"] == "verified"
          and any("taken from the signed issuance code" in n for n in rep["notes"]))

    # ---- a forger crops the code off: caught only where the clinic signs every slip (signs_all)
    signed_rep = rep   # recorded below; keep it
    rep, v = run(p_unsigned, "Dolo 650", regn="00000", doctor="Example")
    check("routine slip without a code, clinic not marked signs_all -> GREEN (judged like any unsigned slip)",
          v == "GREEN", rep["reason"])
    r.REGISTRY.write_text(json.dumps({**reg, "00000": {**reg["00000"], "signs_all": True},
                                      "55555": {**reg["55555"], "signs_all": True}}), encoding="utf-8")
    rep, v = run(p_unsigned, "Dolo 650", regn="00000", doctor="Example")
    check("clinic signs every slip, this one has no code -> AMBER even for a routine medicine, call the clinic",
          v == "AMBER" and rep["flags"].get("code_missing") and rep["path"] == "call"
          and "NO CLINIC CODE" in rep["reason"], rep["reason"])
    rep, v = run(p_signed, "Alprax 0.25")
    check("  ...a slip from that clinic WITH its verified code is still GREEN", v == "GREEN", rep["reason"])
    rep, v = run(p_unsigned, "Dolo 650", regn="55555", doctor="Untrusted Key")
    check("signs_all without a key collected in person is ignored (and reported)",
          not rep["flags"].get("code_missing")
          and any("signs_all needs a trusted issuer_key" in x for x in r.data_status()["registry"]["problems"]))
    r.REGISTRY.write_text(json.dumps(reg), encoding="utf-8")
    rep = signed_rep
    r.record_check(rep, "Alprax 0.25", "00000", "Example")
    rep, v = run(p_signed, "Alprax 0.5")
    check("strength differs from the signed prescription -> AMBER", v == "AMBER" and rep["flags"]["mismatch"],
          rep["reason"])
    rep, v = run(p_signed, "Dolo 650, Lonazep 0.5")
    check("medicine not in the signed prescription -> AMBER", v == "AMBER", rep["reason"])
    rep, v = run(p_signed, "Alprax 0.25", regn="11111")
    check("Regn No entered differs from the signed one -> AMBER", v == "AMBER", rep["reason"])

    # the same code copied onto a different slip, presented again: serial already seen here
    p_copy = save(tmp, "copied_qr", whatsapp(print_qr(mirrored_body(orig), code), angle=-2))
    rep, v = run(p_copy, "Alprax 0.25")
    check("copied code presented again -> repeat, AMBER", v == "AMBER" and rep["flags"]["repeat"], rep["reason"])

    p_forged = save(tmp, "forged", whatsapp(print_qr(mirrored_body(orig), forged)))
    rep, v = run(p_forged, "Alprax 0.25", regn="00000")
    check("code forged with another key -> RED for high-risk", v == "RED" and "ISSUANCE CODE FAILED" in rep["reason"],
          rep["reason"])
    rep, v = run(p_forged, "Dolo 650", regn="00000")
    check("  ... AMBER for routine medicine", v == "AMBER", rep["reason"])

    # a payment / website QR on the slip is ignored, not treated as a failed code
    p_upi = save(tmp, "upi", print_qr(mirrored_body(orig), "upi://pay?pa=clinic@bank&pn=Clinic"))
    rep, v = run(p_upi, "Dolo 650")
    check("non-issuance QR (UPI) is ignored", rep["issuance"]["status"] == "none" and v == "GREEN"
          and any("not a clinic issuance code" in n for n in rep["notes"]), rep["reason"])
    r.close_db()

sys.exit(0 if ok else 1)
