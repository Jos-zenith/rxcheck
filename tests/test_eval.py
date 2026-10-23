"""Evaluation safeguards: a held-out set must be independent, and stays independent only while the
thresholds are frozen. No images needed.

Run:  python tests/test_eval.py
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))
import evaluate as ev  # noqa: E402

ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not cond else ""))


rows = [{"split": "tune", "prescriber": "dr-a", "pharmacy": "p1"},
        {"split": "holdout", "prescriber": "dr-b", "pharmacy": "p2"}]
check("independent splits: no leakage reported", ev.leakage(rows) == [])
leaky = rows + [{"split": "holdout", "prescriber": "dr-a", "pharmacy": "p3"}]
check("same prescriber in tune and holdout is caught", ev.leakage(leaky) == ["prescriber 'dr-a' is in both tune and holdout"])

with tempfile.TemporaryDirectory() as t:
    ev.LOCK = Path(t) / "holdout_lock.json"
    _, digest = ev.thresholds()
    first = ev.holdout_status(digest)
    again = ev.holdout_status(digest)
    check("first held-out run locks the thresholds; an unchanged re-run stays independent",
          first[0] and again[0] and json.loads(ev.LOCK.read_text())["runs"] == 2)
    ev.r.COPY_STRICT += 0.01          # "just one small tweak" after seeing held-out results
    _, changed = ev.thresholds()
    ev.r.COPY_STRICT -= 0.01
    status = ev.holdout_status(changed)
    check("changing a detection threshold afterwards marks the held-out results contaminated",
          not status[0] and "CONTAMINATED" in status[1])
    thr, _ = ev.thresholds()
    check("lock covers the detection constants, not unrelated settings",
          "COPY_STRICT" in thr and "INDEX_MIN_CORR" in thr and "PIN_ITERATIONS" not in thr)

synthetic = [{"origin": "derived", "truth": "genuine", "prescriber": "dr-a", "pharmacy": "lab"}] * 50
have, short = ev.field_readiness(synthetic)
check("derived cases never count toward a field validation", have["genuine_real"] == 0 and short)
real = ([{"origin": "real", "truth": "genuine", "prescriber": f"dr-{i % 25}", "pharmacy": f"p{i % 4}"}
         for i in range(300)] + [{"origin": "real", "truth": "fraud", "prescriber": "dr-1", "pharmacy": "p1"}] * 60)
check("enough real cases from enough prescribers and pharmacies -> field validation", ev.field_readiness(real)[1] == [])

sys.exit(0 if ok else 1)
