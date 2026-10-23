"""Registry, drug-risk list and policy: broken or missing data makes a check more cautious - never crashes it,
never relaxes it. Imports are validated, all-or-nothing, versioned and audited.

Run:  python tests/test_data.py
"""
import json
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import rxcheck as r  # noqa: E402
from scenarios import fixture_path, whatsapp  # noqa: E402
import cv2  # noqa: E402

ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not cond else ""))


def write(p, text):
    Path(p).write_text(text if isinstance(text, str) else json.dumps(text), encoding="utf-8")


today = date.today().isoformat()
GOOD_DRUGS = {"_verified": True, "_source": "test", "_verified_on": today,
              "molecules": {"alprazolam": "high", "paracetamol": "routine"}, "brands": {"alprax": "alprazolam",
                                                                                      "dolo": "paracetamol"}}

with tempfile.TemporaryDirectory() as t:
    tmp = Path(t)
    r.LEDGER_DIR, r.DB, r.FEATURES, r.REPORTS = tmp, tmp / "rxcheck.db", tmp / "features", tmp / "reports"
    r.REGISTRY, r.DRUG_RISK, r.POLICY = tmp / "registry.json", tmp / "drug_risk.json", tmp / "policy.json"

    # ---- drug-risk list
    write(r.DRUG_RISK, GOOD_DRUGS)
    check("valid list: routine stays routine", r.drug_risk("Dolo 650")[0] == "routine")
    check("valid, recently verified list counts as verified", r.data_status()["drug_risk"]["verified"])
    write(r.DRUG_RISK, dict(GOOD_DRUGS, _verified_on=(date.today() - timedelta(days=400)).isoformat()))
    check("list verified over 180 days ago is reported as not verified", not r.data_status()["drug_risk"]["verified"])
    r.DRUG_RISK.unlink()
    check("missing list: every medicine treated as high-risk", r.drug_risk("Dolo 650")[0] == "high")
    write(r.DRUG_RISK, '{"molecules": {"alprazolam": "high",')
    st = r.data_status()["drug_risk"]
    check("corrupt list: unusable, high-risk, reason given", not st["usable"] and r.drug_risk("Dolo 650")[0] == "high"
          and "not valid JSON" in st["problems"][0])
    write(r.DRUG_RISK, {"molecules": {}})
    check("empty list: unusable", not r.data_status()["drug_risk"]["usable"])
    write(r.DRUG_RISK, {"molecules": {"alprazolam": "HIGH!", "paracetamol": "routine"}, "brands": {"xyz": "nothing"}})
    st = r.data_status()["drug_risk"]
    check("unknown tier -> treated as high, reported", r.drug_risk("Alprazolam 0.25")[0] == "high"
          and any("treated as high" in p for p in st["problems"]))
    check("brand mapped to a missing molecule -> ignored, reported", any("xyz" in p for p in st["problems"]))
    write(r.DRUG_RISK, GOOD_DRUGS)

    # ---- registry
    for text, what in (("{not json", "corrupt"), ('["a list"]', "wrong shape")):
        write(r.REGISTRY, text)
        status, _, msg = r.prescriber("00000", "Dr X")
        check(f"{what} registry -> 'unverified' with a reason (not 'invalid', no crash)",
              status == "unverified" and "registry unusable" in msg, msg)
    r.REGISTRY.unlink()
    check("missing registry -> unverified", r.prescriber("00000", None)[0] == "unverified")
    write(r.REGISTRY, {"00000": {"name": "Dr. A", "source": "council", "verified_on": "12/08/2026"},
                       "11111": {"council": "no name"}})
    status, _, _ = r.prescriber("00000", "Dr. A")
    st = r.data_status()["registry"]
    check("malformed verified_on -> unverified, reported, no crash",
          status == "unverified" and any("verified_on" in p for p in st["problems"]))
    check("entry without a name is dropped and reported", any("11111" in p for p in st["problems"]))
    write(r.REGISTRY, {"_coverage": "partial", "00000": {"name": "Dr. A", "source": "council", "verified_on": today},
                       "22222": {"name": "Dr. B"}})
    check("verified entry -> valid", r.prescriber("00000", "Dr. A")[0] == "valid")
    check("not in a PARTIAL registry -> unverified (look it up), not invalid", r.prescriber("99999", None)[0] == "unverified")
    check("verified entry, other doctor's name -> invalid", r.prescriber("00000", "Dr. Z")[0] == "invalid")
    check("unchecked entry, other doctor's name -> unverified (our data may be wrong)",
          r.prescriber("22222", "Dr. Z")[0] == "unverified")
    # a contradiction is RED only when it is a hard fact; registry data goes stale
    write(r.REGISTRY, {"00000": {"name": "Dr. Mira Sample", "source": "council", "verified_on": today}})
    for written, want in (("Dr. M. Sample", "valid"), ("Sample Mira", "valid"),
                          ("Dr. Mira Sampl", "valid"), ("Mira S", "valid"),
                          ("Dr. R. Sample", "unverified"), ("Dr. Ramesh Kumar", "invalid")):
        got = r.prescriber("00000", written)[0]
        check(f"verified entry, slip says '{written}' -> {want}", got == want, got)
    complete = {"_coverage": "complete", "_source": "full council extract", "_updated_on": today,
                "00000": {"name": "Dr. A", "source": "council", "verified_on": today}}
    write(r.REGISTRY, complete)
    check("not in a recent COMPLETE registry -> invalid", r.prescriber("99999", None)[0] == "invalid")
    for when, what in (((date.today() - timedelta(days=200)).isoformat(), "old"), (None, "undated")):
        write(r.REGISTRY, dict(complete, _updated_on=when))
        status, _, msg = r.prescriber("99999", None)
        check(f"not in an {what} complete extract -> unverified (a new doctor may be missing)",
              status == "unverified" and "registered since" in msg, msg)

    # ---- Regn No written differently: council prefixes, separators, case, zero padding
    for a, b, want in (("TN/73816", "tn 73816", "same"), ("TN-73816", "TN.73816", "same"), ("73816", "TN/73816", "format"),
                       ("TN/073816", "73816", "format"), ("KA 73816", "TN 73816", "different"),
                       ("73816", "73818", "different"), ("TN/73816", "TN/73818", "different")):
        check(f"Regn No '{a}' vs '{b}' -> {want}", r.regn_relation(a, b) == want, r.regn_relation(a, b))
    write(r.REGISTRY, {"_coverage": "complete", "_source": "full council extract", "_updated_on": today,
                       "TN/73816": {"name": "Dr. A", "source": "council", "verified_on": today},
                       "KA/55555": {"name": "Dr. K", "source": "council", "verified_on": today}})
    for typed in ("TN/73816", "tn 73816", "TN-73816", "TN73816"):
        check(f"registry 'TN/73816', typed '{typed}' -> valid", r.prescriber(typed, "Dr. A")[0] == "valid")
    for typed in ("73816", "TN/073816"):
        status, rec, msg = r.prescriber(typed, "Dr. A")
        check(f"complete registry, typed '{typed}' (prefix / padding differs) -> unverified with a look-up, not invalid",
              status == "unverified" and rec is None and "TN/73816" in msg, msg)
    check("complete registry, same digits under ANOTHER council -> still invalid", r.prescriber("TN/55555", "Dr. K")[0] == "invalid")
    write(r.REGISTRY, {"00000": {"name": "Dr. A", "source": "council", "verified_on": today, "phone": "999"}})
    _, rec, _ = r.prescriber("00000", None)
    check("phone without phone_source is never used for call-backs", r.trusted(rec, "phone") is None)

    # ---- policy
    write(r.POLICY, "{broken")
    check("corrupt policy -> cautious default", r.policy()["confirm_new_high_risk"] is True)
    write(r.POLICY, {"confirm_new_high_risk": "no"})
    check("mistyped policy value -> cautious default, reported",
          r.policy()["confirm_new_high_risk"] is True and not r.data_status()["policy"]["usable"])
    write(r.POLICY, {"confirm_new_high_risk": False})
    check("valid policy value is used", r.policy()["confirm_new_high_risk"] is False)
    r.POLICY.unlink()

    # ---- audited, all-or-nothing imports
    write(r.REGISTRY, {"_note": "kept", "00000": {"name": "Dr. A"}})
    before = r.REGISTRY.read_text(encoding="utf-8")
    write(tmp / "bad.csv", "regn_no,name,verified_on,phone\n12345,Dr. B,2026-10-01,\n,Dr. C,2026-10-01,\n"
                           "67890,Dr. D,01/10/2026,\n24680,Dr. E,2026-10-01,0000000000\n")
    try:
        r.import_registry(tmp / "bad.csv", "TN Medical Council register, checked by test")
        check("bad registry CSV rejected", False)
    except ValueError as ex:
        msg = str(ex)
        check("bad registry CSV rejected as a whole, every problem listed",
              all(s in msg for s in ("line 3", "line 4", "line 5")) and r.REGISTRY.read_text(encoding="utf-8") == before,
              msg)
    write(tmp / "twins.csv", f"regn_no,name,verified_on\nTN/123,Dr. B,{today}\ntn-123,Dr. B,{today}\n")
    try:
        r.import_registry(tmp / "twins.csv", "test")
        check("same Regn No written two ways in one import -> rejected", False)
    except ValueError as ex:
        check("same Regn No written two ways in one import -> rejected", "appears twice (as TN/123)" in str(ex), str(ex))
    write(tmp / "good.csv", "regn_no,name,council,verified_on,phone,phone_source\n"
                            "12345,Dr. B,TNMC,2026-10-01,0000000000,clinic visit 2026-10-01\n"
                            f"00000,Dr. A,TNMC,{today},,\n")
    try:
        r.import_registry(tmp / "good.csv", "")
        check("import without --source refused", False)
    except ValueError:
        check("import without --source refused", True)
    n = r.import_registry(tmp / "good.csv", "TN Medical Council register, checked by test")
    data = json.loads(r.REGISTRY.read_text(encoding="utf-8"))
    check("good registry CSV merged; earlier entries and note kept", n == 2 and data["_note"] == "kept"
          and r.prescriber("00000", "Dr. A")[0] == "valid" and r.trusted(data["12345"], "phone") == "0000000000")
    hist = list((tmp / "history").glob("registry-*.json"))
    row = r.db().execute("SELECT * FROM audit WHERE action = 'data-import'").fetchone()
    check("previous registry kept in data/history; import audited with both hashes",
          len(hist) == 1 and hist[0].read_text(encoding="utf-8") == before and row
          and json.loads(row["detail"])["old_sha"] != json.loads(row["detail"])["new_sha"])

    write(tmp / "drugs_bad.csv", "molecule,tier,brands\nalprazolam,high,alprax;restyl\nparacetamol,maybe,dolo\n")
    try:
        r.import_drugs(tmp / "drugs_bad.csv", "CDSCO Schedule H1 (test)", "test pharmacist")
        check("bad drug CSV rejected", False)
    except ValueError as ex:
        check("bad drug CSV rejected (tier must be ndps/high/routine)", "line 3" in str(ex))
    write(tmp / "drugs.csv", "molecule,tier,brands,schedule\nalprazolam,high,alprax;restyl,H1\n"
                             "paracetamol,routine,dolo;calpol,\n")
    m, b = r.import_drugs(tmp / "drugs.csv", "CDSCO Schedule H1 (test)", "test pharmacist")
    st = r.data_status()["drug_risk"]
    check("good drug CSV replaces the list, marked verified with source and reviewer",
          (m, b) == (2, 4) and st["verified"] and r.drug_risk("Restyl 0.5")[0] == "high"
          and r.drug_risk("Calpol 500")[0] == "routine")
    write(tmp / "drugs3.csv", "molecule,tier,brands,schedule\nalprazolam,ndps,alprax,NDPS\n"
                              "pregabalin,high,lyrica,H1\nparacetamol,routine,dolo,\n")
    r.import_drugs(tmp / "drugs3.csv", "NDPS + CDSCO lists (test)", "test pharmacist")
    check("NDPS tier: high-risk for the verdict, and fails closed (no override)",
          r.drug_risk("Alprax 0.25")[0] == "high" and r.fail_closed("Alprax 0.25, Lyrica 75, Dolo 650") == ["Alprax 0.25"])
    check("  NDPS medicine without a strength is flagged like any high-risk one",
          any("without a strength" in e["issue"] for e in r.review_drugs("Alprax")))
    r.DRUG_RISK.write_text("{broken", encoding="utf-8")
    check("  drug list unusable: nothing can be ruled out, every medicine fails closed",
          r.fail_closed("Dolo 650, Lyrica 75") == ["Dolo 650", "Lyrica 75"])
    r.import_drugs(tmp / "drugs.csv", "CDSCO Schedule H1 (test)", "test pharmacist")

    # ---- entry review (typed today, OCR-prefilled later): what must be confirmed against the slip
    issues = r.review_drugs("Alprx .25, Alprax, Dolo 650.0, Dolo 650")
    text = " | ".join(f"{i['item']}: {i['issue']}" for i in issues)
    check("look-alike name flagged with the high-risk suggestion", "did you mean 'alprax' (HIGH-RISK)" in text, text)
    check("leading-dot strength flagged", "starts with '.'" in text, text)
    check("high-risk medicine without strength flagged", "without a strength" in text, text)
    check("trailing-zero strength flagged", "trailing zero" in text, text)
    check("a clean entry raises nothing", r.review_drugs("Alprax 0.25, Dolo 650") == [])

    # ---- every report names the data it was decided with; a broken list shows in the reason
    write(r.DRUG_RISK, "{broken")
    rep = r.run_check(fixture_path(), "Dolo 650", draw=False)
    check("report carries data-file hashes", set(rep["data"]) == {"registry", "drug_risk", "policy"})
    check("broken drug list -> routine medicine handled as high-risk, reason says why",
          rep["risk"] == "high" and rep["verdicts"]["high"] == "AMBER" and "Drug-risk list unusable" in rep["reason"],
          rep["reason"])

    # ---- a genuine re-sent copy: the Regn No typed with a prefix is not an edit; different digits are
    r.add_entry(fixture_path(), "RX-FMT-1", today, "73816", "Dr. A", "Dolo 650")
    resent = tmp / "resent.jpg"
    cv2.imencode(".jpg", whatsapp(r.read_bgr(fixture_path())))[1].tofile(str(resent))
    rep = r.run_check(resent, "Dolo 650", regn="TN/73816", draw=False)
    check("re-sent copy, Regn No '73816' typed as 'TN/73816' -> not an edit, noted",
          (rep["match"] or {}).get("identity") == "copy" and not rep["flags"]["edited"]
          and any("written differently" in n for n in rep["notes"]), rep["reason"])
    rep = r.run_check(resent, "Dolo 650", regn="73818", draw=False)
    check("re-sent copy, Regn No '73818' for recorded '73816' -> still an edit (RED)",
          rep["flags"]["edited"] and "RED" in rep["verdicts"].values(), rep["reason"])
    r.close_db()

sys.exit(0 if ok else 1)
