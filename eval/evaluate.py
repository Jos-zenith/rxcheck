"""Evaluate the full rxcheck pipeline on a labelled manifest, in a throw-away ledger.

Metrics follow the counter's decision, not an "AI or not" score:
  RED   = do not dispense until confirmed   AMBER = needs confirmation   GREEN = no issues found

Safeguards against fooling ourselves (docs/field-validation.md):
  * results are broken down by origin (real / derived / synthetic) - derived and synthetic cases are not
    evidence of real-world accuracy, and the report says so;
  * a held-out split must not share a prescriber or pharmacy with the tuning split (checked);
  * the first held-out run locks the detection thresholds (eval/holdout_lock.json); any later change makes
    held-out results "contaminated", and the report says so.

Run:  python eval/evaluate.py [--no-confirm-new-high-risk] [--complete-registry] [--split tune|holdout|all]
                              [--manifest eval/field/manifest.csv --setup eval/field/setup.json]
"""
import argparse
import csv
import hashlib
import json
import math
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import rxcheck as r  # noqa: E402

LOCK = ROOT / "eval" / "holdout_lock.json"
# what a run must contain before its numbers may be called a field validation (docs/field-validation.md)
FIELD_MIN = {"genuine_real": 300, "fraud_real": 60, "prescribers": 20, "pharmacies": 3}
NOT_DETECTION = {"PIN_ITERATIONS", "REGISTRY_MAX_AGE_DAYS", "DRUG_LIST_MAX_AGE_DAYS", "OVERRIDE_ALERT"}


def wilson(k, n, z=1.96):
    """95% Wilson score interval - honest for small n, never outside [0, 1]."""
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def rate(k, n):
    lo, hi = wilson(k, n)
    return {"k": k, "n": n, "value": k / n if n else None, "ci95": [round(lo, 2), round(hi, 2)]}


def fmt(m):
    if not m["n"]:
        return "n/a"
    return f"{m['k']}/{m['n']} = {m['value']:.0%}  (95% CI {m['ci95'][0]:.0%}-{m['ci95'][1]:.0%})"


def thresholds():
    """Every numeric detection constant in rxcheck, and a hash of them: what the held-out lock protects."""
    number = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    t = {k: v for k, v in sorted(vars(r).items()) if k.isupper() and k not in NOT_DETECTION
         and (number(v) or (isinstance(v, tuple) and v and all(number(x) for x in v)))}
    return t, hashlib.sha256(json.dumps(t, sort_keys=True, default=str).encode()).hexdigest()[:16]


def leakage(rows):
    """Prescribers / pharmacies that appear in both splits - a held-out set must not share them."""
    out = []
    for col in ("prescriber", "pharmacy"):
        tune = {x[col] for x in rows if x.get("split") == "tune" and x.get(col)}
        hold = {x[col] for x in rows if x.get("split") == "holdout" and x.get(col)}
        out += [f"{col} '{v}' is in both tune and holdout" for v in sorted(tune & hold)]
    return out


def holdout_status(digest):
    """(independent, message). The first held-out evaluation locks the thresholds."""
    if not LOCK.exists():
        LOCK.write_text(json.dumps({"thresholds_sha": digest, "locked_on": r.now(), "runs": 1}, indent=2),
                        encoding="utf-8")
        return True, f"held-out set evaluated for the first time; thresholds locked ({digest})"
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    lock["runs"] = lock.get("runs", 0) + 1
    LOCK.write_text(json.dumps(lock, indent=2), encoding="utf-8")
    if lock["thresholds_sha"] != digest:
        return False, (f"CONTAMINATED: thresholds changed since the held-out set was first used "
                       f"({lock['thresholds_sha']} -> {digest}). These numbers are tuning results, not held-out ones.")
    return True, f"thresholds unchanged since lock on {lock['locked_on']} (run {lock['runs']})"


def use_temp_ledger(tmp, complete):
    r.LEDGER_DIR, r.DB, r.FEATURES = tmp, tmp / "rxcheck.db", tmp / "features"
    # registry fixture: a pharmacy that HAS verified this doctor - otherwise every high-risk slip would be AMBER
    # for "unverified prescriber" and the policy trade-off could not be measured. By default it lists only some
    # doctors (as any real pharmacy's will), so an invented Regn No is "unverified", not "invalid".
    reg = tmp / "registry.json"
    reg.write_text(json.dumps({"_coverage": "complete" if complete else "partial",
                               "_updated_on": r.date.today().isoformat(),   # a recent extract: "not listed" is a hard fact
                               "TEST-REGN-001": {"name": "Dr. Sample", "source": "evaluation fixture",
                                         "verified_on": r.date.today().isoformat()}}), encoding="utf-8")
    r.REGISTRY = reg


def apply_setup(setup):
    ids = {}
    for s in setup:
        img = ROOT / s["image"]
        if s["kind"] == "dispensed":
            ids[s["id"]] = r.add_entry(img, s["id"], s["date"], s["regn"] or None, s["doctor"] or None, s["drugs"])["id"]
        else:
            sheet = r.paper_crop(r.read_bgr(img))
            ids[s["id"]] = r.record_seen(img, r.features(sheet), r.density(sheet), r.sha256(img), s["drugs"],
                                         s["regn"] or None, s["doctor"] or None, when=s["date"])
    return ids


def metrics_for(results):
    fraud = [x for x in results if x["truth"] == "fraud"]
    genuine = [x for x in results if x["truth"] == "genuine"]
    red = [x for x in results if x["verdict"] == "RED"]
    copies = [x for x in results if x["copy_of"]]
    new = [x for x in results if not x["copy_of"]]
    return {
        "fraud recall (RED)": rate(sum(x["verdict"] == "RED" for x in fraud), len(fraud)),
        "fraud stopped before dispensing (RED or AMBER)": rate(sum(x["verdict"] != "GREEN" for x in fraud), len(fraud)),
        "false-positive rate (genuine marked RED)": rate(sum(x["verdict"] == "RED" for x in genuine), len(genuine)),
        "precision of RED": rate(sum(x["truth"] == "fraud" for x in red), len(red)),
        "genuine needing confirmation (AMBER)": rate(sum(x["verdict"] == "AMBER" for x in genuine), len(genuine)),
        "unverifiable rate, all cases (AMBER)": rate(sum(x["verdict"] == "AMBER" for x in results), len(results)),
        "duplicate detection recall": rate(sum(x["matched"] == x["copy_of"] for x in copies), len(copies)),
        "false duplicate rate (new slips matched)": rate(sum(bool(x["matched"]) for x in new), len(new)),
        # what the warnings cost the counter: a call is slow and may go unanswered; the others take seconds
        "genuine needing a call to the clinic": rate(sum(x["path"] == "call" for x in genuine), len(genuine)),
        "genuine settled at the counter or by a retake": rate(sum(x["path"] in ("counter", "retake") for x in genuine),
                                                              len(genuine)),
        "fraud whose step is a call": rate(sum(x["path"] == "call" for x in fraud), len(fraud)),
    }


def field_readiness(results):
    real = [x for x in results if x.get("origin") == "real"]
    have = {"genuine_real": sum(x["truth"] == "genuine" for x in real),
            "fraud_real": sum(x["truth"] == "fraud" for x in real),
            "prescribers": len({x.get("prescriber") for x in real if x.get("prescriber")}),
            "pharmacies": len({x.get("pharmacy") for x in real if x.get("pharmacy")})}
    short = [f"{k} {have[k]}/{v}" for k, v in FIELD_MIN.items() if have[k] < v]
    return have, short


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-confirm-new-high-risk", action="store_true")
    ap.add_argument("--complete-registry", action="store_true",
                    help="assume an authoritative COMPLETE registry: unlisted Regn Nos become invalid")
    ap.add_argument("--split", default="all", choices=["tune", "holdout", "all"])
    ap.add_argument("--manifest", default=str(ROOT / "eval" / "manifest.csv"))
    ap.add_argument("--setup", default=str(ROOT / "eval" / "setup.json"))
    ap.add_argument("--out")
    a = ap.parse_args()
    if a.no_confirm_new_high_risk:   # show the trade-off: fewer calls, more fraud through
        r.policy = lambda: {"confirm_new_high_risk": False}

    all_rows = list(csv.DictReader(open(a.manifest, encoding="utf-8")))
    leaks = leakage(all_rows)
    if leaks and a.split != "tune":
        sys.exit("held-out set is not independent:\n  " + "\n  ".join(leaks))
    rows = [x for x in all_rows if a.split == "all" or x.get("split", "tune") == a.split]
    if not rows:
        sys.exit(f"no '{a.split}' cases in {a.manifest}")
    thr, digest = thresholds()
    holdout_note = None
    if a.split == "holdout":
        independent, holdout_note = holdout_status(digest)

    setup = json.loads(Path(a.setup).read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        use_temp_ledger(Path(tmp), a.complete_registry)
        ids = apply_setup(setup)
        seen_id = ids.get("SEEN-ROUTINE")
        r.run_check(ROOT / rows[0]["image"], draw=False)   # warm-up (OpenCV thread pools) - not timed
        results = []
        for row in rows:
            rep = r.run_check(ROOT / row["image"], row["drugs"] or None, row["regn"] or None,
                              row["doctor"] or None, draw=False)
            verdict = next(iter(rep["verdicts"].values())) if len(rep["verdicts"]) == 1 else rep["verdicts"]["high"]
            m = rep["match"] or {}
            results.append({**row, "copy_of": seen_id if row["copy_of"] == "SEEN" else row["copy_of"],
                            "verdict": verdict, "seconds": rep["seconds"],
                            "matched": m.get("ledger_id", "") if m.get("identity") in ("copy", "seen") else "",
                            "identity": "unusable" if rep["unusable"] else m.get("identity", "none"),
                            "path": rep["path"] or "", "held": rep["held"], "hand_over": rep["hand_over"],
                            "reason": rep["reason"]})
        r.close_db()

    secs = np.array([x["seconds"] for x in results])
    metrics = metrics_for(results)
    by_origin = {o: metrics_for([x for x in results if x.get("origin") == o])
                 for o in ("real", "derived", "synthetic") if any(x.get("origin") == o for x in results)}
    latency = {"median_s": round(float(np.median(secs)), 2), "p95_s": round(float(np.percentile(secs, 95)), 2),
               "max_s": round(float(secs.max()), 2)}
    have, short = field_readiness(results)

    print(f"\n{'id':4} {'truth':8} {'origin':9} {'verdict':7} {'step':8} {'match':9} {'secs':>5}  scenario")
    for x in results:
        flag = "  <- missed" if x["truth"] == "fraud" and x["verdict"] == "GREEN" else \
               "  <- false alarm" if x["truth"] == "genuine" and x["verdict"] == "RED" else ""
        print(f"{x['id']:4} {x['truth']:8} {x.get('origin', '?'):9} {x['verdict']:7} {x['path']:8} {x['identity']:9} "
              f"{x['seconds']:5.2f}  {x['scenario']}{flag}")
    genuine = sum(x["truth"] == "genuine" for x in results)
    print(f"\n{len(results)} cases ({a.split}): {genuine} genuine, {len(results) - genuine} fraud;  "
          f"registry: {'complete' if a.complete_registry else 'partial'};  thresholds {digest}")
    if holdout_note:
        print(f"  HOLD-OUT: {holdout_note}")
    for k, m in metrics.items():
        print(f"  {k:48} {fmt(m)}")
    print(f"  {'latency (pipeline only, after warm-up)':48} median {latency['median_s']}s, "
          f"p95 {latency['p95_s']}s, max {latency['max_s']}s")
    for o, ms in by_origin.items():
        keys = ("fraud stopped before dispensing (RED or AMBER)", "false-positive rate (genuine marked RED)",
                "genuine needing confirmation (AMBER)")
        print(f"\n  origin = {o}:")
        for k in keys:
            print(f"    {k:46} {fmt(ms[k])}")
    if short:
        print(f"\n  NOT A FIELD VALIDATION - real cases below the minimum: {', '.join(short)}.\n"
              f"  Derived and synthetic cases show how the pipeline behaves, not how accurate it is.")
    print()

    out = Path(a.out) if a.out else ROOT / "eval" / ("results_policy_off.json" if a.no_confirm_new_high_risk
                                                      else "results.json")
    out.write_text(json.dumps({"policy": r.policy(), "registry": "complete" if a.complete_registry else "partial",
                               "split": a.split, "thresholds_sha": digest, "thresholds": thr,
                               "holdout": holdout_note, "field_validation": {"have": have, "minimum": FIELD_MIN,
                                                                             "short": short},
                               "metrics": metrics, "by_origin": by_origin, "latency": latency, "cases": results},
                              indent=2, default=str), encoding="utf-8")
    print(f"  saved {out}")


if __name__ == "__main__":
    main()
