"""Counter-scenario tests for rxcheck using generated fixtures and optional local samples.

Run:  python tests/test_robustness.py
"""
import sys
import time

import numpy as np

from scenarios import (r, rng, PRIVATE_PAIR, load_samples, whatsapp, photocopy, dim_shop_light, side_shadow, angled_photo,
                       shaky, cropped, forwarded, screenshot, screen_recapture, rewrite, op_chit, op_chit_redated,
                       mirrored_body, flipped_body, other_slip, added_line, tamil_line, pasted_lower,
                       tiny, heavy_blur, blown_glare, near_black, glary_screen)

orig, fake = load_samples()
ref = r.paper_crop(orig)
fref = r.features(ref)
opref = r.paper_crop(op_chit(orig))
fopref = r.features(opref)

EDITED, SAME, NOT_COPY, DIFFERENT = "edited copy", "same slip", "similar/different", "different"
cases = [
    # forgeries - must be "copy with handwritten edits" (RED)
    ("synthetic edited copy", ref, fref, fake, EDITED),
    ("D: added drug line", ref, fref, added_line(orig), EDITED),
    ("D: added drug line, then WhatsApp", ref, fref, whatsapp(added_line(orig)), EDITED),
    ("C: added Tamil dosage line", ref, fref, tamil_line(orig), EDITED),
    ("C: OP chit (no letterhead), re-dated at top", opref, fopref, op_chit_redated(orig), EDITED),
    ("C: ...same, then WhatsApp", opref, fopref, whatsapp(op_chit_redated(orig)), EDITED),
    # genuine re-presentations - must be "same slip", no false edits
    ("B: WhatsApp forward", ref, fref, whatsapp(orig), SAME),
    ("B: forwarded 5 times", ref, fref, forwarded(orig), SAME),
    ("B: dark photo, then forwarded 4 times", ref, fref, forwarded(orig, 5, 0.5), SAME),
    ("B: gallery screenshot", ref, fref, screenshot(orig), SAME),
    ("B: photo of a phone screen (moire, glare)", ref, fref, screen_recapture(orig), SAME),
    ("B: photocopy", ref, fref, photocopy(orig), SAME),
    ("B: photocopy then WhatsApp", ref, fref, whatsapp(photocopy(orig)), SAME),
    ("B: dim shop light", ref, fref, dim_shop_light(orig), SAME),
    ("B: side shadow", ref, fref, side_shadow(orig), SAME),
    ("B: photo taken at an angle", ref, fref, angled_photo(orig), SAME),
    ("B: shaky / blurred", ref, fref, shaky(orig), SAME),
    ("B: edges cropped", ref, fref, cropped(orig), SAME),
    ("C: OP chit, WhatsApp forward", opref, fopref, whatsapp(op_chit(orig)), SAME),
    # A: the same doctor rewriting - must never be called an edited copy
    ("A: rewrite, strokes ~0.5mm off (10px/20)", ref, fref, rewrite(orig, 10, 20), NOT_COPY),
    ("A: rewrite, strokes ~0.7mm off (15px/8)", ref, fref, rewrite(orig, 15, 8), NOT_COPY),
    ("A: rewrite, strokes ~1.2mm off (25px/8)", ref, fref, rewrite(orig, 25, 8), NOT_COPY),
    # different slips
    ("other Rx, same letterhead (mirrored)", ref, fref, mirrored_body(orig), DIFFERENT),
    ("other Rx, same letterhead (flipped)", ref, fref, flipped_body(orig), DIFFERENT),
    ("other patient's slip, other hand", ref, fref, other_slip(), DIFFERENT),
]
if not PRIVATE_PAIR:
    skipped = {"C: OP chit (no letterhead), re-dated at top", "C: ...same, then WhatsApp",
               "C: OP chit, WhatsApp forward", "B: dark photo, then forwarded 4 times"}
    cases = [case for case in cases if case[0] not in skipped]
    print(f"[SKIP] {len(skipped)} image regressions require a local consented fixture; no real images are bundled")
known_gaps = [("real handwriting pasted 170px lower", ref, fref, pasted_lower(orig))]

ok = True
print(f"{'scenario':46} agree strict  ncc  identity   edits -> result")
for name, rf, f, img, want in cases:
    sus = r.paper_crop(img)
    fs = r.features(sus)
    res = r.compare(rf, sus, f, fs) if float(f["fp"] @ fs["fp"]) >= r.FP_MIN else None
    ident = res["identity"] if res else "different"
    edits = [g for g in res["regions"] if g["zone"] == "handwritten"] if res else []
    got = (EDITED if edits else SAME) if ident == "copy" else ident
    passed = got == want or (want == NOT_COPY and got in ("similar", "different")) or \
        (want == DIFFERENT and got == "different")
    ok &= passed
    s = f"{res['agreement']:5.2f} {res['strict']:5.2f} {res['ncc'] or 0:5.2f}" if res else "    -     -     -"
    print(f"[{'PASS' if passed else 'FAIL'}] {name:46} {s}  {ident:9} {len(edits):3}  -> {got}")
for name, rf, f, img in known_gaps:
    sus = r.paper_crop(img)
    res = r.compare(rf, sus, f, r.features(sus))
    print(f"[GAP ] {name:46} identity={res['identity'] if res else 'different'} "
          f"edits={len([g for g in res['regions'] if g['zone'] == 'handwritten']) if res else 0}")

# ---- no-image index: "seen before" for every checked slip (layout correlation after alignment)
entry = r.compact(fref, r.density(ref))
print(f"\nno-image index entry: {sum(v.nbytes for v in entry.values()) // 1000} KB, "
      f"density map {entry['dens'].shape[1]}x{entry['dens'].shape[0]}px")
idx_cases = [("synthetic edit", fake, True), ("added line, WhatsApp", whatsapp(added_line(orig)), True),
             ("forwarded 5 times", forwarded(orig), True), ("dark, forwarded", forwarded(orig, 5, 0.5), True),
             ("phone screen", screen_recapture(orig), True), ("screenshot", screenshot(orig), True),
             ("mirrored", mirrored_body(orig), False), ("flipped", flipped_body(orig), False),
             ("other patient's slip", other_slip(), False)]
if not PRIVATE_PAIR:
    idx_cases = [case for case in idx_cases if case[0] not in {"forwarded 5 times", "dark, forwarded", "phone screen"}]
for name, img, want in idx_cases:
    sus = r.paper_crop(img)
    c = r.index_match(entry, r.features(sus), r.density(sus))
    passed = (c >= r.INDEX_MIN_CORR) == want
    ok &= passed
    print(f"[{'PASS' if passed else 'FAIL'}] index {name:28} corr={c:5.2f} -> {'seen before' if c >= r.INDEX_MIN_CORR else 'not seen'}")

# ---- quality gate: degraded-but-usable passes; unusable goes to manual review (never "new slip")
q_cases = [("WhatsApp forward", whatsapp(orig), True), ("forwarded 5 times", forwarded(orig), True),
           ("dark photo, forwarded", forwarded(orig, 5, 0.5), True), ("phone screen", screen_recapture(orig), True),
           ("shaky / blurred", shaky(orig), True), ("photocopy then WhatsApp", whatsapp(photocopy(orig)), True),
           ("synthetic edit", fake, True), ("other patient's slip", other_slip(), True),
           ("260px thumbnail", tiny(orig), False), ("heavy blur", heavy_blur(orig), False),
           ("glare over the writing", blown_glare(orig), False), ("near-black photo", near_black(orig), False),
           ("phone screen with glare", glary_screen(orig), False), ("blank paper", np.full_like(orig, 200), False)]
if not PRIVATE_PAIR:
    q_cases = [case for case in q_cases if case[0] not in {"heavy blur", "near-black photo"}]
print()
for name, img, usable in q_cases:
    sheet = r.paper_crop(img)
    reasons = r.quality(img, sheet, r.features(sheet))
    passed = (not reasons) == usable
    ok &= passed
    print(f"[{'PASS' if passed else 'FAIL'}] quality {name:26} -> {'usable' if not reasons else 'MANUAL REVIEW: ' + '; '.join(reasons)}")

# ---- decision matrix: weak hints never alarm; uncertainty -> AMBER, never RED
F = dict(edited=False, repeat=False, similar=False, mismatch=False, new_slip=False, unusable=False,
         prescriber="valid", meta_conflict=False, issuance="none")
ON, OFF = {"confirm_new_high_risk": True}, {"confirm_new_high_risk": False}
matrix = [
    (dict(F, edited=True), "routine", ON, "RED"),
    (dict(F, edited=True), "high", ON, "RED"),
    (dict(F, meta_conflict=True), "routine", ON, "AMBER"),    # provenance = supporting evidence, never RED
    (dict(F, meta_conflict=True), "high", ON, "AMBER"),
    (dict(F, prescriber="invalid"), "routine", ON, "AMBER"),
    (dict(F, prescriber="invalid"), "high", ON, "RED"),
    (dict(F, similar=True), "routine", ON, "AMBER"),          # A: possible rewrite -> confirm, never accuse
    (dict(F, similar=True), "high", ON, "AMBER"),
    (dict(F, repeat=True, mismatch=True), "routine", ON, "AMBER"),  # D: dose/drug differs from record
    (dict(F, repeat=True), "routine", ON, "GREEN"),
    (dict(F, repeat=True), "high", ON, "AMBER"),
    (dict(F, prescriber="unknown"), "routine", ON, "GREEN"),  # C: unknown doctor is not fraud
    (dict(F, prescriber="unknown"), "high", ON, "AMBER"),
    (dict(F, new_slip=True), "high", ON, "AMBER"),            # E: valid Regn No != authorship
    (dict(F, new_slip=True), "high", OFF, "GREEN"),
    (dict(F, new_slip=True), "routine", ON, "GREEN"),
    (dict(F, unusable=True), "routine", ON, "AMBER"),         # unreadable image -> manual review
    (dict(F, unusable=True), "high", OFF, "AMBER"),
    (dict(F, unusable=True, prescriber="invalid"), "high", ON, "RED"),
    (dict(F, prescriber="unverified"), "routine", ON, "GREEN"),  # stub/stale registry entry = unknown
    (dict(F, prescriber="unverified", new_slip=True), "high", OFF, "AMBER"),
    # clinic-signed issuance code
    (dict(F, new_slip=True, issuance="verified"), "high", ON, "GREEN"),      # signature = issuance confirmed
    (dict(F, new_slip=True, issuance="verified", prescriber="unverified"), "high", ON, "AMBER"),
    (dict(F, repeat=True, issuance="verified"), "high", ON, "AMBER"),        # a copied code is still a repeat
    (dict(F, mismatch=True, issuance="verified"), "high", ON, "AMBER"),      # drug not in the signed list
    (dict(F, edited=True, issuance="verified"), "high", ON, "RED"),
    (dict(F, issuance="invalid"), "routine", ON, "AMBER"),
    (dict(F, issuance="invalid"), "high", ON, "RED"),
]
bad = [(f, k, p, w, r.decide(f, k, p)) for f, k, p, w in matrix if r.decide(f, k, p) != w]
ok &= not bad
for f, k, p, w, got in bad:
    print(f"[FAIL] decide({ {x: y for x, y in f.items() if y not in (False, 'valid')} }, {k}, {p}) = {got}, want {w}")
print(f"[{'FAIL' if bad else 'PASS'}] decision matrix ({len(matrix)} rules)")

# ---- after a warning: every warning names an explicit step (never the generic fallback), and the
# quickest step that settles it - retake / counter / call
import itertools  # noqa: E402
fallback, combos = [], 0
for vals in itertools.product([False, True], repeat=7):
    for pres, iss, risk, pol in itertools.product(("valid", "unknown", "unverified", "invalid"),
                                                  ("none", "verified", "invalid"), ("routine", "high"), (ON, OFF)):
        f = dict(zip(("edited", "repeat", "similar", "mismatch", "new_slip", "unusable", "meta_conflict"), vals),
                 prescriber=pres, issuance=iss, doc_mismatch=False, drug_mismatch=vals[3])
        if r.decide(f, risk, pol) == "GREEN":
            continue
        combos += 1
        path, steps = r.next_step(f, risk, {}, pol)
        if not steps or any(s == r.ACTIONS[r.decide(f, risk, pol)] for s in steps):
            fallback.append((f, risk, pol))
ok &= not fallback
print(f"[{'FAIL' if fallback else 'PASS'}] every warning names its own step ({combos} warning combinations, "
      f"{len(fallback)} without)")
for f, risk, pol in fallback[:3]:
    print("       e.g.", {k: v for k, v in f.items() if v not in (False, "valid", "none")}, risk, pol)
paths = [
    (dict(F, unusable=True, new_slip=True), "high", "retake"),        # a better photo first, whatever else
    (dict(F, similar=True), "routine", "counter"),                    # look at the paper original
    (dict(F, similar=True), "high", "call"),
    (dict(F, repeat=True, mismatch=True, drug_mismatch=True), "routine", "counter"),   # sell as prescribed
    (dict(F, repeat=True), "high", "counter"),                        # ledger + H1 register settle it
    (dict(F, new_slip=True), "high", "call"),                         # only the clinic knows
    (dict(F, prescriber="unknown"), "high", "call"),
    (dict(F, meta_conflict=True), "routine", "call"),
    (dict(F, edited=True), "routine", "call"),
    (dict(F, repeat=True, prescriber="unverified"), "high", "call"),  # a call wins over a counter check
]
bad = [(f, k, w, r.next_step(f, k, {}, ON)[0]) for f, k, w in paths if r.next_step(f, k, {}, ON)[0] != w]
ok &= not bad
for f, k, w, got in bad:
    print(f"[FAIL] next_step({ {x: y for x, y in f.items() if y not in (False, 'valid', 'none')} }, {k}) = {got}, want {w}")
print(f"[{'FAIL' if bad else 'PASS'}] quickest valid step per warning ({len(paths)} rules)")

# ---- the sale is split: routine medicines with no issue go home now, only what needs confirmation waits
items = r.item_verdicts("Dolo 650, Pan 40, Alprax 0.25", dict(F, repeat=True), ON, "Dolo 650, Pan 40, Alprax 0.25")
split_ok = [(i["item"], i["verdict"]) for i in items] == [("Dolo 650", "GREEN"), ("Pan 40", "GREEN"),
                                                          ("Alprax 0.25", "AMBER")]
items = r.item_verdicts("Dolo 1000, Pan 40", dict(F, repeat=True, mismatch=True), ON, "Dolo 650, Pan 40")
split_ok &= [(i["item"], i["verdict"]) for i in items] == [("Dolo 1000", "AMBER"), ("Pan 40", "GREEN")]
items = r.item_verdicts("Dolo 650, Alprax 0.25", dict(F, edited=True), ON, "Dolo 650")
split_ok &= all(i["verdict"] == "RED" for i in items)           # an edited copy: nothing is handed over
items = r.item_verdicts("Dolo 650, Pan 40", dict(F, repeat=True, mismatch=True, doc_mismatch=True), ON, "Dolo 650, Pan 40")
split_ok &= all(i["verdict"] == "AMBER" for i in items)         # Regn No / date differs: the whole slip waits
ok &= split_ok
print(f"[{'PASS' if split_ok else 'FAIL'}] sale split per medicine (repeat H1, raised strength, edited copy, "
      f"document mismatch)")

checks = [r.drug_risk("Alprax 0.25, Dolo 650")[0] == "high", r.drug_risk("Dolo 650, Pan 40")[0] == "routine",
          r.drug_risk(None)[0] is None,
          r.drug_risk("Alprazolam 0.25, Dolo 650")[2] == ["Dolo 650"],   # unlisted names are reported
          r.prescriber("00000", "Example")[0] == "unverified",  # stub entry has no source
          r.prescriber("99999", None)[0] == "unverified",  # not in a PARTIAL registry: look it up, not "invalid"
          r.prescriber(None, None)[0] == "unknown",
          r.drug_diff("Dolo 1000, Pan 40", "Dolo 650, Pan 40") != [],      # strength changed
          r.drug_diff("Alprazolam 0.25", "Alprax 0.25") == [],             # brand vs molecule is not a change
          r.drug_diff("Dolo 650, Alprax 0.5", "Dolo 650") != [],           # drug added
          r.drug_diff("Dolo 650", "") == []]                                # nothing recorded -> no claim
ok &= all(checks)
print(f"[{'PASS' if all(checks) else 'FAIL'}] drug risk + drug comparison ({len(checks)} checks)")

# ---- speed at ledger scale
sus = r.paper_crop(fake)
t = time.perf_counter(); fs = r.features(sus); t_feat = time.perf_counter() - t
t = time.perf_counter(); r.compare(ref, sus, fref, fs); t_cmp = time.perf_counter() - t
t = time.perf_counter(); r.index_match(entry, fs, r.density(sus)); t_idx = time.perf_counter() - t
for n in (2_000, 20_000):
    fps = rng.normal(size=(n, 768)).astype(np.float32)
    fps /= np.linalg.norm(fps, axis=1, keepdims=True)
    t = time.perf_counter(); np.argsort(-(fps @ fs["fp"]))[:r.SHORTLIST]; t_rank = time.perf_counter() - t
    print(f"[INFO] ledger {n:>6}: features {t_feat:.2f}s + shortlist {t_rank * 1000:.1f}ms; then per candidate: "
          f"image compare {t_cmp:.2f}s or index match {t_idx:.2f}s")

sys.exit(0 if ok else 1)
