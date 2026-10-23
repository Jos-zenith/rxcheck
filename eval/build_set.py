"""Build the labelled evaluation set (samples/eval/*.jpg, eval/manifest.csv, eval/setup.json).

Every image is generated from a synthetic test image, so this set measures the pipeline's behaviour
under counter conditions - it is not evidence of real-world accuracy.

Run:  python eval/build_set.py
"""
import csv
import json
import sys
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from scenarios import (synthetic_samples, whatsapp, photocopy, dim_shop_light, side_shadow, angled_photo,  # noqa: E402
                       shaky, cropped, forwarded, screenshot, screen_recapture, rewrite, op_chit, op_chit_redated,
                       mirrored_body, flipped_body, other_slip, THIRD_LINES, added_line, tamil_line, pasted_lower,
                       glary_screen)

orig, fake = synthetic_samples()
OUT = ROOT / "samples" / "eval"
OUT.mkdir(parents=True, exist_ok=True)
DOC = "Dr. Sample"
REGN = "TEST-REGN-001"
RECORDED = "Dolo 650, Pan 40, Alprax 0.25"
ROUTINE, H1 = "Dolo 650, Pan 40", "Alprax 0.25"


def save(name, img):
    p = OUT / f"{name}.jpg"
    cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tofile(str(p))
    return p.relative_to(ROOT).as_posix()


# what the pharmacy already has on record before the evaluation starts
setup = [
    {"kind": "dispensed", "id": "RX-TEST-001", "image": save("setup_reference", orig), "date": "2025-01-15",
     "regn": REGN, "doctor": DOC, "drugs": RECORDED},
    {"kind": "dispensed", "id": "RX-OPCHIT", "image": save("setup_opchit", op_chit(orig)), "date": "2026-09-20",
     "regn": "", "doctor": "", "drugs": ROUTINE},
    {"kind": "seen", "id": "SEEN-ROUTINE", "image": save("setup_seen_routine", other_slip(0)), "date": "2026-10-01",
     "regn": "", "doctor": "", "drugs": "Dolo 650, Pan 40, Ascoril"},
]

# id, truth, scenario, image, drugs requested, regn as read off the slip, copy_of (ground truth)
L = "RX-TEST-001"
rows = [
    ("g01", "genuine", "re-sent via WhatsApp, routine meds", whatsapp(orig), ROUTINE, REGN, L),
    ("g02", "genuine", "re-sent via WhatsApp, H1 drug on the slip", whatsapp(orig), H1, REGN, L),
    ("g03", "genuine", "photocopy", photocopy(orig), ROUTINE, REGN, L),
    ("g04", "genuine", "photocopy then WhatsApp", whatsapp(photocopy(orig)), ROUTINE, REGN, L),
    ("g05", "genuine", "dim shop light", dim_shop_light(orig), ROUTINE, REGN, L),
    ("g06", "genuine", "side shadow", side_shadow(orig), ROUTINE, REGN, L),
    ("g07", "genuine", "photo at an angle", angled_photo(orig), ROUTINE, REGN, L),
    ("g08", "genuine", "shaky / blurred", shaky(orig), ROUTINE, REGN, L),
    ("g09", "genuine", "edges cropped", cropped(orig), ROUTINE, REGN, L),
    ("g10", "genuine", "E: new slip, valid doctor, H1 drug", mirrored_body(orig), H1, REGN, ""),
    ("g11", "genuine", "C: new slip, no Regn No (OP chit), H1 drug", flipped_body(orig), H1, "", ""),
    ("g12", "genuine", "C: new slip, no Regn No, routine, WhatsApp", whatsapp(flipped_body(orig)), ROUTINE, "", ""),
    ("g13", "genuine", "routine slip seen before (index only), re-sent", whatsapp(other_slip(0), angle=-2), ROUTINE, "", "SEEN"),
    ("g14", "genuine", "A: same doctor rewrites the slip, routine", rewrite(orig, 15, 8), ROUTINE, REGN, ""),
    ("g15", "genuine", "B: gallery screenshot", screenshot(orig), ROUTINE, REGN, L),
    ("g16", "genuine", "B: photo of a phone screen", screen_recapture(orig), ROUTINE, REGN, L),
    ("g17", "genuine", "B: forwarded 5 times", forwarded(orig), ROUTINE, REGN, L),
    ("g18", "genuine", "B: dark photo, forwarded 4 times", forwarded(orig, 5, 0.5), ROUTINE, REGN, L),
    ("g19", "genuine", "C: OP chit re-sent via WhatsApp", whatsapp(op_chit(orig)), ROUTINE, "", "RX-OPCHIT"),
    ("g20", "genuine", "different patient, new routine slip", other_slip(3, THIRD_LINES), "Metformin 500, Amlong 5", "", ""),
    ("g21", "genuine", "D: phone screen with glare (unusable), routine", glary_screen(orig), ROUTINE, REGN, ""),
    ("f01", "fraud", "synthetic edited copy", fake, ROUTINE, REGN, L),
    ("f02", "fraud", "synthetic edited copy, Regn No misread as original", fake, ROUTINE, REGN, L),
    ("f03", "fraud", "D: added H1 drug line", added_line(orig), H1, REGN, L),
    ("f04", "fraud", "D: added H1 drug line, then WhatsApp", whatsapp(added_line(orig)), H1, REGN, L),
    ("f05", "fraud", "fabricated slip, invented Regn No, H1 drug", whatsapp(mirrored_body(orig)), H1, "99999", ""),
    ("f06", "fraud", "fabricated slip, invented Regn No, routine", whatsapp(mirrored_body(orig)), ROUTINE, "99999", ""),
    ("f07", "fraud", "E: fabricated slip, VALID doctor identity, H1", photocopy(flipped_body(orig)), H1, REGN, ""),
    ("f08", "fraud", "real handwriting pasted lower, H1 (known gap)", pasted_lower(orig), H1, REGN, L),
    ("f09", "fraud", "D: seen routine slip, strength raised (Dolo 1000)", whatsapp(other_slip(0), angle=1), "Dolo 1000, Pan 40", "", "SEEN"),
    ("f10", "fraud", "D: seen routine slip, H1 drug not on it", whatsapp(other_slip(0), angle=-1), "Alprax 0.5", "", "SEEN"),
    ("f11", "fraud", "C: OP chit re-dated at the top, WhatsApp", whatsapp(op_chit_redated(orig)), ROUTINE, "", "RX-OPCHIT"),
    ("f12", "fraud", "C: added Tamil dosage line", tamil_line(orig), ROUTINE, REGN, L),
    ("f13", "fraud", "D: genuine slip re-sent, H1 strength raised (0.5)", whatsapp(orig, angle=-2), "Alprax 0.5", REGN, L),
    ("f14", "fraud", "synthetic edited copy shown on a glary phone screen (unusable)", glary_screen(fake), ROUTINE, REGN, ""),
]

# Provenance is limited to generated and transformed synthetic fixtures.
# Every case shares the thresholds' tuning history, so all are split "tune": there is no held-out set yet.
SYNTHETIC = {"g13", "g20", "f09", "f10"}   # other_slip(): synthetic handwriting


def provenance(rid):
    if rid in SYNTHETIC:
        return "synthetic", "synthetic-hand"
    return "derived", "synthetic-hand"


with open(ROOT / "eval" / "manifest.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["id", "truth", "scenario", "image", "drugs", "regn", "doctor", "copy_of", "origin", "prescriber",
                "pharmacy", "split"])
    for rid, truth, scenario, img, drugs, regn, copy_of in rows:
        w.writerow([rid, truth, scenario, save(rid, img), drugs, regn, DOC if regn else "", copy_of,
                    *provenance(rid), "lab", "tune"])
(ROOT / "eval" / "setup.json").write_text(json.dumps(setup, indent=2), encoding="utf-8")
print(f"wrote {len(rows)} cases + {len(setup)} setup records to {OUT}")
