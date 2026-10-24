"""Score the slip reader on the labelled set (samples/ocr/, from build_ocr_set.py) and on real slips from the field
study (eval/field/ocr/labels.json, if present).

The number that matters most is SILENT ERRORS: a value filled in wrong that would go through without a tick if
staff sent it unchanged. Wrong values that need a tick are caught at the counter (if staff look); silent ones
only by the staff member noticing. The target is zero.

Each slip is read once and scored for two counters:
  new          - only the drug list is on file, no strength dispensed yet (a counter on its first day)
  established  - every medicine in the set has been dispensed before, at the strengths on the slips (weeks of use)

Run:  python eval/ocr/score.py [--only printed,pdf-text]      (writes samples/ocr/results.json)
Synthetic results describe the reader on synthetic slips; they are not evidence of accuracy at a real counter.
"""
import json
import re
import statistics
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "app"))
import ocr  # noqa: E402
import pdfpages  # noqa: E402
import rxcheck as r  # noqa: E402

SET = ROOT / "samples" / "ocr"
REAL = ROOT / "eval" / "field" / "ocr"


def norm(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def page_lines(path, tmp):
    """[[line, ...] per page] - the text layer for a PDF page that has one, otherwise OCR."""
    if path.suffix.lower() == ".pdf":
        out = []
        for i, (jpg, lines) in enumerate(pdfpages.pages(path.read_bytes())):
            if lines:
                segs, src = [(tuple(x["box"]), x["text"], x["conf"]) for x in lines], "text"
            else:
                p = Path(tmp) / f"{path.stem}-{i}.jpg"
                p.write_bytes(jpg)
                segs, src = ocr.read_segments(ocr.load(p)), "ocr"
            out.append([{**ln, "page": i, "src": src} for ln in ocr.group_lines(segs)])
        return out
    return [[{**ln, "page": 0, "src": "ocr"} for ln in ocr.group_lines(ocr.read_segments(ocr.load(path)))]]


def score(res, expect):
    """Per slip: medicines and header fields - correct, shown as unclear, wrong (and silently wrong), missed."""
    want = [norm(x) for x in expect.get("drugs", [])]
    filled = {norm(d["text"]): d for d in res["drugs"]}
    unclear = {norm(u["text"]) for u in res["unclear"]}
    s = {"med_expected": len(want), "med_filled_ok": sum(w in filled for w in want),
         "med_unclear_ok": sum(w in unclear and w not in filled for w in want),
         "med_wrong": sum(1 for t in filled if t not in want),
         "med_silent": sum(1 for t, d in filled.items() if t not in want and not d["confirm"]),
         "hdr_expected": 0, "hdr_ok": 0, "hdr_wrong": 0, "hdr_silent": 0,
         "ticks": len(ocr.confirmations({**res, "status": "done"}, ocr.values({**res, "status": "done"})))}
    s["med_missed"] = s["med_expected"] - s["med_filled_ok"] - s["med_unclear_ok"]
    for key in ("regn", "doctor", "rx_date"):
        if not expect.get(key):
            continue
        s["hdr_expected"] += 1
        got = res.get(key)
        if not got:
            continue
        if norm(got["value"]).replace(" ", "") == norm(expect[key]).replace(" ", ""):
            s["hdr_ok"] += 1
        else:
            s["hdr_wrong"] += 1
            s["hdr_silent"] += not got["confirm"]
    return s


def items():
    m = json.loads((SET / "manifest.json").read_text(encoding="utf-8")) if (SET / "manifest.json").is_file() else None
    for it in (m or {}).get("items", []):
        yield SET / it["file"], it
    labels = REAL / "labels.json"
    if labels.is_file():
        for it in json.loads(labels.read_text(encoding="utf-8")).get("items", []):
            yield REAL / it["file"], {**it, "kind": "real-" + it.get("kind", "slip"), "synthetic": False}


def main():
    only = None
    if "--only" in sys.argv:
        only = set(sys.argv[sys.argv.index("--only") + 1].split(","))
    if not ocr.available():
        sys.exit("the OCR engine is not installed (see requirements-ocr.txt)")
    table = r.drug_table() or r.EMPTY_TABLE
    listed = set(table["brands"]) | set(table["molecules"])
    todo = [(p, it) for p, it in items() if not only or it["kind"] in only]
    if not todo:
        sys.exit("no slips: run python eval/ocr/build_ocr_set.py first")
    everything, seen = set(listed), {}
    for _, it in todo:   # the established counter has dispensed every medicine in the set, at its strength
        for d in it["expect"].get("drugs", []):
            m = re.match(r"^(.*?)(?:\s+([\d.]+).*)?$", d)
            everything.add(norm(m.group(1)))
            if m.group(2):
                seen.setdefault(norm(m.group(1)), set()).add(m.group(2))
    is_high = lambda t: r.drug_risk(t)[0] == "high"   # noqa: E731
    today = date.today()
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for n, (path, it) in enumerate(todo, 1):
            t0 = time.time()
            try:
                lines = [ln for pg in page_lines(path, tmp) for ln in pg]
                err = None
            except Exception as ex:
                lines, err = [], f"{type(ex).__name__}: {ex}"
            secs = time.time() - t0
            row = {"id": it["id"], "kind": it["kind"], "synthetic": it.get("synthetic", True), "seconds": round(secs, 2),
                   "error": err}
            for name, known, strengths in (("new", listed, {}), ("established", everything, seen)):
                res = ocr.parse(lines, known, is_high, today, strengths)
                row[name] = score(res, it["expect"])
                if name == "established":
                    row["read"] = {"drugs": [d["text"] for d in res["drugs"]], "unclear": [u["text"] for u in res["unclear"]],
                                   **{k: res[k]["value"] for k in ("regn", "doctor", "rx_date") if res.get(k)}}
            rows.append(row)
            print(f"  {n}/{len(todo)} {it['id']}: {secs:.1f} s", flush=True)
    (SET / "results.json").write_text(json.dumps({"scored_on": today.isoformat(), "rows": rows}, indent=1),
                                      encoding="utf-8")
    report(rows)


def report(rows):
    kinds = []
    for x in rows:
        if x["kind"] not in kinds:
            kinds.append(x["kind"])
    for counter in ("new", "established"):
        print(f"\n{counter} counter ({'drug list only on file' if counter == 'new' else 'every medicine on file'})")
        print(f"{'kind':<18}{'slips':>6}{'meds':>6}{'filled ok':>11}{'unclear':>9}{'missed':>8}{'wrong':>7}"
              f"{'SILENT':>8}{'header ok':>11}{'hdr wrong':>10}{'hdr SILENT':>11}{'ticks/slip':>11}{'s/slip':>8}")
        for k in kinds + ["ALL"]:
            rs = [x for x in rows if k == "ALL" or x["kind"] == k]
            t = {f: sum(x[counter][f] for x in rs) for f in rs[0][counter]}
            pct = lambda a, b: f"{100 * a / b:.0f}%" if b else "-"   # noqa: E731
            print(f"{k:<18}{len(rs):>6}{t['med_expected']:>6}{pct(t['med_filled_ok'], t['med_expected']):>11}"
                  f"{t['med_unclear_ok']:>9}{t['med_missed']:>8}{t['med_wrong']:>7}{t['med_silent']:>8}"
                  f"{pct(t['hdr_ok'], t['hdr_expected']):>11}{t['hdr_wrong']:>10}{t['hdr_silent']:>11}"
                  f"{t['ticks'] / len(rs):>11.1f}{statistics.median(x['seconds'] for x in rs):>8.1f}")
    print("\nfilled ok = medicines filled in exactly right; unclear = right but shown, not filled; wrong = filled in "
          "but not on the slip as written; SILENT = wrong and no tick needed.")
    if any(x["kind"] == "handwriting-font" for x in rows):
        print("handwriting-font rows are typed handwriting-style fonts, not handwriting: not a handwriting score.")
    if not any(not x["synthetic"] for x in rows):
        print("No real slips scored: add consented ones to eval/field/ocr/ (see eval/ocr/labels-template.json).")


if __name__ == "__main__":
    main()
