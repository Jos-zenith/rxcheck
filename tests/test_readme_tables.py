"""The README's decision table is the policy pharmacists read - it must match decide() exactly.

Run:  python tests/test_readme_tables.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import rxcheck as r  # noqa: E402

BASE = dict(edited=False, repeat=False, similar=False, mismatch=False, new_slip=False, unusable=False,
            prescriber="valid", meta_conflict=False, issuance="none")
# every row of the README table, mapped to the flags it describes (a new/renamed row must be added here)
ROWS = {
    "Copy of a recorded slip **with edits**": [dict(edited=True)],
    "Clinic-signed issuance code fails verification": [dict(issuance="invalid")],
    "Regn No invalid (not in a recent complete registry, or a verified entry names a different doctor)":
        [dict(prescriber="invalid")],
    "AI/provenance marker, or file dated before the Rx (supporting evidence only)": [dict(meta_conflict=True)],
    "**No clinic code**, but the prescriber's clinic signs every slip (`signs_all`)": [dict(code_missing=True)],
    "Image unusable for automated comparison": [dict(unusable=True)],
    "**Similar** to a recorded slip (possible rewrite)": [dict(similar=True)],
    "Requested drug/strength differs from the matched record": [dict(repeat=True, mismatch=True)],
    "Same slip / seen before (repeat)": [dict(repeat=True)],
    "Seen at **another pharmacy** (shared index)": [dict(repeat=True, shared=True)],
    "Shared index **unreachable**, slip new here": [dict(new_slip=True, shared_unreachable=True)],
    "Prescriber unknown or unverified (no Regn No, not in a partial registry, unchecked entry, registry unusable)":
        [dict(prescriber="unknown"), dict(prescriber="unverified")],
    "New slip, valid prescriber": [dict(new_slip=True)],
    "New slip, valid prescriber, **clinic-signed code verified**": [dict(new_slip=True, issuance="verified")],
}
VERDICT = re.compile(r"RED|AMBER|GREEN")


def table(md):
    lines = md.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("| Finding | Routine medicine | H1 / X / abuse-prone |"))
    rows = []
    for l in lines[start + 2:]:
        if not l.startswith("|"):
            break
        rows.append([c.strip() for c in l.strip("|").split("|")])
    return rows


def expected(cell, policy_on):
    if "confirm_new_high_risk" in cell:            # "**AMBER** if `confirm_new_high_risk` (default), else GREEN"
        return VERDICT.findall(cell)[0 if policy_on else -1]
    return VERDICT.findall(cell)[0]


ok = True
rows = table((ROOT / "README.md").read_text(encoding="utf-8"))
labels = [row[0] for row in rows]
for missing in set(ROWS) - set(labels):
    ok = False
    print(f"[FAIL] README table lacks row: {missing}")
for label, routine, high in rows:
    if label not in ROWS:
        ok = False
        print(f"[FAIL] README row not covered by this test: {label}")
        continue
    for flags in ROWS[label]:
        for pol_on in (True, False):
            pol = {"confirm_new_high_risk": pol_on}
            for risk, cell in (("routine", routine), ("high", high)):
                got, want = r.decide(dict(BASE, **flags), risk, pol), expected(cell, pol_on)
                if got != want:
                    ok = False
                    print(f"[FAIL] '{label}' {risk} policy={'on' if pol_on else 'off'}: README says {want}, code says {got}")
print(f"[{'PASS' if ok else 'FAIL'}] README decision table matches decide() ({len(rows)} rows x 2 risks x 2 policies)")

# the "after a warning" table is the procedure staff follow - it must match next_step() too
STEP_ROWS = {
    "Image unusable for automated comparison": [dict(unusable=True)],
    "Very similar to a recorded slip (possible rewrite)": [dict(similar=True)],
    "Medicine or strength differs from the recorded or signed prescription":
        [dict(mismatch=True, drug_mismatch=True), dict(repeat=True, mismatch=True, drug_mismatch=True)],
    "Regn No or date entered differs from the record": [dict(repeat=True, mismatch=True, doc_mismatch=True)],
    "Same slip presented again (repeat)": [dict(repeat=True)],
    "Seen at another pharmacy (shared index)": [dict(repeat=True, shared=True)],
    "Shared index unreachable, slip new here": [dict(new_slip=True, shared_unreachable=True)],
    "Prescriber unknown or unverified": [dict(prescriber="unknown"), dict(prescriber="unverified")],
    "New slip, valid prescriber (`confirm_new_high_risk`)": [dict(new_slip=True)],
    "AI/provenance marker, or file dated before the Rx": [dict(meta_conflict=True)],
    "No clinic code, but the clinic signs every slip": [dict(code_missing=True)],
    "Edited copy, invalid Regn No, or failed clinic signature (RED)":
        [dict(edited=True), dict(prescriber="invalid"), dict(issuance="invalid")],
}
lines = (ROOT / "README.md").read_text(encoding="utf-8").splitlines()
start = next(i for i, l in enumerate(lines) if l.startswith("| Warning | Routine medicine | High-risk medicine |"))
step_rows = []
for l in lines[start + 2:]:
    if not l.startswith("|"):
        break
    step_rows.append([c.strip() for c in l.strip("|").split("|")])
good = True
for missing in set(STEP_ROWS) - {x[0] for x in step_rows}:
    good = False
    print(f"[FAIL] README step table lacks row: {missing}")
POL = {"confirm_new_high_risk": True}
for label, routine, high in step_rows:
    if label not in STEP_ROWS:
        good = False
        print(f"[FAIL] README step row not covered by this test: {label}")
        continue
    for flags in STEP_ROWS[label]:
        for risk, cell in (("routine", routine), ("high", high)):
            got = r.next_step({**BASE, "doc_mismatch": False, "drug_mismatch": False, **flags}, risk, {}, POL)[0] or "-"
            if got != cell:
                good = False
                print(f"[FAIL] step for '{label}' ({risk}): README says {cell}, code says {got}")
ok &= good
print(f"[{'PASS' if good else 'FAIL'}] README 'after a warning' table matches next_step() ({len(step_rows)} rows x 2 risks)")

# no second, conflicting decision table elsewhere in the docs
for doc in ("docs/technical-overview.md",):
    text = (ROOT / doc).read_text(encoding="utf-8")
    if "| Document finding |" in text or "| Finding | Routine medicine |" in text:
        ok = False
        print(f"[FAIL] {doc} contains its own decision table - point to the README instead")
print(f"[{'PASS' if ok else 'FAIL'}] single source of truth for the decision policy")
sys.exit(0 if ok else 1)
