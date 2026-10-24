"""Storage safeguards: concurrent writers, append-only audit trail, JSON migration, backup.

Run:  python tests/test_database_and_backups.py
"""
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

from synthetic_slips import fixture_path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import rxcheck as r  # noqa: E402

WRITER = r'''
import sys; sys.path.insert(0, sys.argv[1])
from pathlib import Path
import numpy as np, rxcheck as r
r.LEDGER_DIR = Path(sys.argv[2]); r.DB = r.LEDGER_DIR / "rxcheck.db"; r.FEATURES = r.LEDGER_DIR / "features"
feats = {"pts": np.zeros((0, 2), np.float32), "resp": np.zeros(0, np.float32), "desc": np.zeros((0, 32), np.uint8),
         "fp": np.ones(768, np.float32), "shape": np.int32([10, 10])}
for i in range(int(sys.argv[4])):
    r.record_seen("x.jpg", feats, np.zeros((2, 2), np.uint8), f"{sys.argv[3]}{i:04d}".ljust(64, "0"))
'''


def use(tmp):
    r.LEDGER_DIR, r.DB, r.FEATURES = tmp, tmp / "rxcheck.db", tmp / "features"


ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not cond else ""))


def count(db_path, sql):
    con = sqlite3.connect(db_path)
    try:
        return con.execute(sql).fetchone()[0]
    finally:
        con.close()


with tempfile.TemporaryDirectory() as t:
    tmp = Path(t)
    use(tmp)
    r.db()   # create schema

    # 1. two processes (e.g. `watch` and a counter `check`) writing at the same time
    script = tmp / "writer.py"
    script.write_text(WRITER, encoding="utf-8")
    n = 40
    procs = [subprocess.Popen([sys.executable, str(script), str(ROOT / "app"), str(tmp), tag, str(n)],
                              stderr=subprocess.PIPE) for tag in ("a", "b")]
    errs = [p.communicate()[1].decode() for p in procs]
    rows = r.db().execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    audits = r.db().execute("SELECT COUNT(*) FROM audit WHERE action = 'index'").fetchone()[0]
    check(f"2 concurrent writers x {n}: {rows} entries, {audits} audit records, no errors",
          rows == 2 * n and audits == 2 * n and all(p.returncode == 0 for p in procs) and not any(errs))

    # 2. audit trail is append-only
    con = r.db()
    for stmt in ("UPDATE audit SET actor = 'someone-else'", "DELETE FROM audit"):
        try:
            with con:
                con.execute(stmt)
            check(f"audit rejects: {stmt}", False)
        except sqlite3.DatabaseError as ex:
            check(f"audit rejects: {stmt}  ({ex})", "append-only" in str(ex))

    # 3. an entry and its audit record are written together or not at all
    before = con.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
    try:
        r._insert_entry(con, {"id": "dup", "kind": "INVALID-KIND", "image": None, "sha256": "x", "date": "2025-01-15",
                              "regn_no": None, "doctor": None, "drugs": "", "note": "", "verdict": None,
                              "fp": np.ones(768, np.float32).tobytes(), "created_at": r.now(),
                              "created_by": "test"}, "index")
    except sqlite3.IntegrityError:
        pass
    after = con.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
    check("rejected write leaves no orphan audit record (transaction rolled back)", before == after)

    # 4. backup is a consistent, openable copy
    class A:
        dest = str(tmp / "backups")
    r.cmd_backup(A)
    snap = next((tmp / "backups").iterdir()) / "rxcheck.db"
    snap_con = sqlite3.connect(snap)
    copy_rows = snap_con.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    snap_con.close()
    check(f"backup opens and has all {copy_rows} entries", copy_rows == 2 * n)
    r.close_db()

with tempfile.TemporaryDirectory() as t:
    # 5. one-time migration from the earlier JSON ledger (entry + features + holds)
    tmp = Path(t)
    (tmp / "images").mkdir()
    shutil.copyfile(fixture_path(), tmp / "images" / "RX-TEST-001.jpg")
    (tmp / "ledger.json").write_text(json.dumps([{"id": "RX-TEST-001", "image": "images/RX-TEST-001.jpg",
                                                  "dispensed": "2025-01-15", "regn_no": "00000", "drugs": ""}]))
    (tmp / "holds.json").write_text(json.dumps([{"id": "H-old", "created": "2025-01-15T10:00:00", "image": "x.jpg",
                                                 "verdicts": {"routine": "RED"}, "findings": [], "status": "open"}]))
    use(tmp)
    con = r.db()
    e = con.execute("SELECT * FROM entries WHERE id = 'RX-TEST-001'").fetchone()
    h = con.execute("SELECT * FROM holds WHERE id = 'H-old'").fetchone()
    check("JSON ledger + holds migrated, originals renamed",
          e is not None and e["kind"] == "dispensed" and e["sha256"] and h is not None
          and (tmp / "ledger.json.migrated").exists() and not (tmp / "ledger.json").exists())
    r.close_db()

with tempfile.TemporaryDirectory() as t:
    # 6. a writer killed mid-write (power cut, crash) leaves a consistent database
    tmp = Path(t)
    use(tmp)
    r.db()
    r.close_db()
    script = tmp / "writer.py"
    script.write_text(WRITER, encoding="utf-8")
    p = subprocess.Popen([sys.executable, str(script), str(ROOT / "app"), str(tmp), "k", "5000"], stderr=subprocess.PIPE)
    deadline = time.time() + 60
    while time.time() < deadline:   # kill it once it is busy writing
        time.sleep(0.2)
        try:
            n = count(tmp / "rxcheck.db", "SELECT COUNT(*) FROM entries")
        except sqlite3.OperationalError:
            continue
        if n >= 20:
            break
    p.kill()
    p.wait()
    con = r.db()
    n_e = con.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    n_a = con.execute("SELECT COUNT(*) FROM audit WHERE action = 'index'").fetchone()[0]
    problems = r.verify_store()
    check(f"writer killed after {n_e} entries: every entry has its audit record, verify healthy",
          0 < n_e < 5000 and n_e == n_a and not problems, problems[:3])

    # 7. verify notices damage and tampering
    img = tmp / "slip.jpg"
    shutil.copyfile(fixture_path(), img)
    r.add_entry(img, "RX-T1", "2026-10-01")
    class B:
        dest, keep = str(tmp / "backups"), None
    backup, bproblems = r.cmd_backup(B)
    check("backup of a healthy ledger verifies clean", not bproblems, bproblems)
    with open(tmp / "images" / "RX-T1.jpg", "ab") as f:
        f.write(b"tampered")
    check("verify: changed ledger image detected", any("has changed" in x for x in r.verify_store()))
    (tmp / "images" / "RX-T1.jpg").unlink()
    check("verify: missing ledger image detected", any("missing" in x for x in r.verify_store()))
    with con:
        con.execute("DROP TRIGGER audit_append_only_d")
    check("verify: removed audit protection detected", any("trigger" in x for x in r.verify_store()))

    # 8. restore: damaged live database replaced by the verified backup, old files kept aside
    r.record_seen("late.jpg", r.compact(r.features(r.paper_crop(r.read_bgr(img))),
                                         r.density(r.paper_crop(r.read_bgr(img)))), np.zeros((2, 2), np.uint8),
                  "z" * 64)
    r.close_db()
    with open(tmp / "rxcheck.db", "r+b") as f:   # disk damage: overwrite part of the file
        f.seek(4096)
        f.write(b"\x00" * 8192)
    for x in ("rxcheck.db-wal", "rxcheck.db-shm"):
        (tmp / x).unlink(missing_ok=True)
    damaged = r.verify_store()
    check("verify: damaged database file detected", bool(damaged), "no problem reported")
    aside, after = r.restore_store(backup)
    n_back = count(backup / "rxcheck.db", "SELECT COUNT(*) FROM entries")
    n_now = r.db().execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    check(f"restore: backup ({n_back} entries) back in place, verifies healthy, damaged files kept in {aside.name}",
          not after and n_now == n_back and (aside / "rxcheck.db").exists(), after[:3])
    check("restore is in the audit trail",
          r.db().execute("SELECT COUNT(*) FROM audit WHERE action = 'restore'").fetchone()[0] == 1)
    bad_backup = tmp / "bad-backup"
    shutil.copytree(backup, bad_backup)
    (bad_backup / "images" / "RX-T1.jpg").unlink()
    try:
        r.restore_store(bad_backup)
        check("restore refuses a backup that fails verification", False)
    except ValueError:
        check("restore refuses a backup that fails verification", True)
    _, bproblems = r.cmd_backup(B)
    check("backup after a restore: compared with the backup restored from, not the ones it superseded",
          not bproblems, bproblems)
    r.close_db()

with tempfile.TemporaryDirectory() as t:
    # 9. hash-chained audit trail: tamper-evident, also against someone who rewrites the whole file
    tmp = Path(t)
    use(tmp)
    con = r.db()
    for i in range(5):
        with con:
            r.audit(con, "test", f"T{i}", {"n": i})
    check("audit records are hash-chained; a clean trail verifies", not r.verify_store()
          and con.execute("SELECT COUNT(*) FROM audit_chain").fetchone()[0]
          == con.execute("SELECT COUNT(*) FROM audit").fetchone()[0])

    class B2:
        dest, keep = str(tmp / "backups"), None
    first, bproblems = r.cmd_backup(B2)
    with con:
        r.audit(con, "test", "T5", {"n": 5})
    _, bproblems2 = r.cmd_backup(B2)
    check("each backup checks that the trail still contains the previous backup's", not bproblems and not bproblems2,
          bproblems + bproblems2)

    with con:   # someone with the file drops the protection and edits history
        for trig in ("audit_append_only_u", "audit_append_only_d", "audit_chain_append_only_u",
                     "audit_chain_append_only_d"):
            con.execute(f"DROP TRIGGER {trig}")
        con.execute("UPDATE audit SET detail = '{\"n\": 99}' WHERE ref = 'T2'")
    check("verify: an edited audit detail is detected", any("detail was changed" in p for p in r.verify_store()))
    with con:
        con.execute("UPDATE audit SET detail = '{\"n\": 2}' WHERE ref = 'T2'")
        con.execute("UPDATE audit SET actor = 'someone-else' WHERE ref = 'T3'")
    check("verify: a changed actor breaks the hash chain", any("chain broken at record" in p for p in r.verify_store()))

    # a determined editor recomputes the whole chain: the live file looks consistent on its own...
    with con:
        con.execute("DELETE FROM audit_chain")
        prev = r.CHAIN_GENESIS
        for x in con.execute("SELECT * FROM audit ORDER BY seq").fetchall():
            d = r._detail_sha(x["detail"])
            prev = r._chain_hash(prev, x["seq"], x["at"], x["actor"], x["action"], x["ref"], d)
            con.execute("INSERT INTO audit_chain VALUES (?, ?, ?)", (x["seq"], d, prev))
    con.executescript(r.SCHEMA)   # triggers back in place
    check("a fully recomputed chain passes on its own (why an outside copy matters)", not r.verify_store())
    diff = r.chain_extends(first / "rxcheck.db", con)
    check("...but differs from an earlier backup: the rewrite is detected", diff and "history was changed" in diff, diff)
    _, bproblems3 = r.cmd_backup(B2)
    check("  and the next scheduled backup reports it", any("history was changed" in p for p in bproblems3), bproblems3)
    r.close_db()

with tempfile.TemporaryDirectory() as t:
    # 10. a ledger written before hash chaining is chained once, on first start; never again afterwards
    tmp = Path(t)
    old = sqlite3.connect(tmp / "rxcheck.db")
    old.executescript(r.SCHEMA.split("CREATE TABLE IF NOT EXISTS audit_chain")[0])
    old.executemany("INSERT INTO audit (at, actor, action, ref, detail) VALUES (?, ?, ?, ?, ?)",
                    [("2025-01-15T10:00:00", "pharmacist-a", "check", f"x{i}", "{}") for i in range(3)])
    old.commit()
    old.close()
    check("pre-chain ledger verifies (nothing to compare yet)", not r.verify_store(tmp))
    use(tmp)
    con = r.db()
    n = con.execute("SELECT COUNT(*) FROM audit_chain").fetchone()[0]
    check("first start chains the existing records and says so", n == 4 and con.execute(
        "SELECT COUNT(*) FROM audit WHERE action = 'chain-start'").fetchone()[0] == 1 and not r.verify_store())
    with con:
        con.execute("DROP TRIGGER audit_chain_append_only_d")
        con.execute("DELETE FROM audit_chain")
    r.close_db()
    con = r.db()   # restart: an emptied chain must not be silently rebuilt
    check("an emptied chain is not rebuilt on restart; verify reports the unchained records",
          con.execute("SELECT COUNT(*) FROM audit_chain").fetchone()[0] == 0
          and any("not in the hash chain" in p for p in r.verify_store()))
    with con:      # override rate per person: one pharmacist waving warnings through stands out
        for i in range(10):
            r.set_actor("staff-a" if i < 5 else "pharmacist-a")
            r.audit(con, "resolve", f"H-{i}", {"outcome": "dispensed-after-review", "override": i < 4})
        r.set_actor(None)
    w = r.workload(1)
    check("override alert per person: names only the one who overrides most of their holds",
          w["staff_alerts"] == ["staff-a"] and w["overrides_by"]["pharmacist-a"] == {"overrides": 0, "closed": 5}, w)
    r.close_db()

sys.exit(0 if ok else 1)
