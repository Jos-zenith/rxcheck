"""Shared (cross-pharmacy) index: notice a slip that another pharmacy has already seen, without sharing prescriptions.

The local ledger and index only know slips THIS pharmacy handled, so a clone of a slip dispensed across town looks
new. This module adds a network of member pharmacies and a hub. Three rules keep patient data out of it:

  1. The hub never holds anything readable. Per slip it stores a 256-bit keyed locality-sensitive signature of the
     coarse ink layout, an opaque token, a date and 'dispensed'/'seen'. No image, Regn No, doctor, patient or
     medicine. The signature is a random +-1 projection of the 24x32 ink-density grid; the projection is derived
     from a network key the hub operator does not hold, so the hub cannot even test a candidate image against it.
  2. Matching is two-stage. Stage 1 (hub): signatures within a Hamming radius -> a few candidate (node, token)
     pairs. Stage 2 (the pharmacy that owns the candidate): the asking pharmacy sends its COMPACT index entry (the
     same unreadable keypoints + 1/8-scale density map the local index keeps), the owner compares it locally and
     answers only match / no match, plus on a match the date, 'dispensed' or 'seen', and keyed tags of the
     medicines recorded. Stage 1 alone is never a verdict: layouts of different slips can collide.
  3. Not knowing is never lenient. If the hub (or a candidate's owner) cannot be reached, the slip is "new, but not
     checked against other pharmacies": high-risk medicines wait for confirmation even if policy would skip it,
     and `sync` re-checks those slips when the connection returns, reporting a match instead of dropping it.

This is a SIMULATION of the network: the hub is a folder (hub.db) and a peer is another RXCHECK_HOME reached by a
subprocess that speaks JSON on stdin/stdout. Replacing both with an HTTPS service is a transport change - the
messages below are the protocol. Not built: authenticated membership beyond a shared-key MAC, key rotation
(a pharmacy that leaves still knows the key), rate limits, and DPDP consent text. See README "Shared index".
"""
import base64
import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

import rxcheck as r

SIG_BITS = 256
FP_DIM = 24 * 32
HAMMING_MAX = 72          # stage 1 radius: same slip, 17 transformations: 0-43 bits; unrelated slips: 46-175
MAX_CANDIDATES = 5        # candidates sent to stage 2 per check: each one is a peer request
MIN_CORR = r.INDEX_MIN_CORR   # stage 2 uses the local index's own "seen before" threshold
MSG_MAX_BYTES = 400_000   # a compact entry is ~110 KB; refuse anything bigger
MSG_MAX_AGE_S = 600
PEER_TIMEOUT_S = 30
KINDS = {"dispensed": "D", "seen": "S"}
_POP = np.array([bin(i).count("1") for i in range(256)], np.uint8)


class Unreachable(Exception):
    """Hub or peer could not be reached (or refused the request)."""


# ---------------------------------------------------------------- configuration

def config_path():
    return r.HOME / "data" / "shared.json"


def config():
    """{'node', 'key', 'hub'} when this pharmacy has joined a network, else None (everything here is then a no-op)."""
    p = config_path()
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return {"node": d["node"], "key": base64.b64decode(d["key_b64"]), "hub": d["hub"]}
    except (OSError, ValueError, KeyError):
        return None


def new_network_key(out):
    key = secrets.token_bytes(32)
    Path(out).write_text(base64.b64encode(key).decode() + "\n", encoding="utf-8")
    return key


def join(hub_dir, key_file):
    """Join the network: a random pseudonymous node id (not the pharmacy's name) and the shared key."""
    key = base64.b64decode(Path(key_file).read_text(encoding="utf-8").strip())
    if len(key) != 32:
        raise ValueError("network key must be 32 bytes")
    node = secrets.token_hex(4)
    Hub(hub_dir).register(node, r.HOME)
    r.save_json(config_path(), {"node": node, "key_b64": base64.b64encode(key).decode(), "hub": str(hub_dir)})
    return node


# ---------------------------------------------------------------- keyed signature and tags

def _projection(key):
    """256 x 768 matrix of +-1 derived from the network key (HMAC counter mode): the same on every platform."""
    need = SIG_BITS * FP_DIM
    blocks = (need // 256) + 1
    raw = b"".join(hmac.new(key, b"rxcheck-lsh-v1|" + i.to_bytes(4, "big"), hashlib.sha256).digest()
                   for i in range(blocks))
    bits = np.unpackbits(np.frombuffer(raw, np.uint8))[:need].reshape(SIG_BITS, FP_DIM)
    return bits.astype(np.float32) * 2 - 1


_PROJ_CACHE = {}


def signature(key, fp):
    """256-bit SimHash of the layout fingerprint: Hamming distance ~ 256 * angle / pi, so the same slip photographed
    twice lands within ~25 bits and unrelated slips near 128."""
    if key not in _PROJ_CACHE:
        _PROJ_CACHE[key] = _projection(key)
    v = np.asarray(fp, np.float32).ravel()
    return np.packbits((_PROJ_CACHE[key] @ v) > 0).tobytes()


def token(key, node, entry_id):
    return hmac.new(key, f"token|{node}|{entry_id}".encode(), hashlib.sha256).hexdigest()[:24]


def _tag(key, text):
    return hmac.new(key, b"drug|" + text.encode(), hashlib.sha256).hexdigest()[:12]


def drug_tags(key, drugs):
    """{tag(molecule): [tag(molecule|strength), ...]} - lets the other side say 'a medicine was added' or 'the
    strength differs' without the medicine names crossing the wire in clear."""
    return {_tag(key, mol): [_tag(key, f"{mol}|{n}") for n in nums] for mol, nums in r.parse_drugs(drugs).items()}


def drug_changes(key, requested, tags):
    """Differences between what is requested now and the tags returned for the slip as dispensed elsewhere."""
    out = []
    for mol, nums in r.parse_drugs(requested).items():
        rec = tags.get(_tag(key, mol))
        if rec is None:
            out.append(f"'{mol}' was not on the slip as recorded at the other pharmacy")
        elif nums and rec and sorted(_tag(key, f"{mol}|{n}") for n in nums) != sorted(rec):
            out.append(f"'{mol}' strength differs from the slip as recorded at the other pharmacy")
    return out


# ---------------------------------------------------------------- hub (simulated: a folder)

HUB_SCHEMA = """
CREATE TABLE IF NOT EXISTS idx (node TEXT NOT NULL, token TEXT NOT NULL, sig BLOB NOT NULL, date TEXT NOT NULL,
  kind TEXT NOT NULL, PRIMARY KEY (node, token));
CREATE INDEX IF NOT EXISTS idx_date ON idx(date);
CREATE TABLE IF NOT EXISTS members (node TEXT PRIMARY KEY, home TEXT NOT NULL, joined TEXT NOT NULL);
"""


class Hub:
    """What the hub operator sees and stores. Deliberately small: see the module docstring."""

    def __init__(self, path):
        self.path = Path(path)

    def _con(self):
        if (self.path / "OFFLINE").exists():       # simulates the hub / connection being down
            raise Unreachable("hub unreachable (simulated: OFFLINE marker)")
        try:
            self.path.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(self.path / "hub.db", timeout=30)
            con.executescript(HUB_SCHEMA)
            return con
        except (OSError, sqlite3.Error) as ex:
            raise Unreachable(f"hub unreachable: {ex}")

    def register(self, node, home):
        con = self._con()
        with con:
            con.execute("INSERT OR REPLACE INTO members VALUES (?, ?, ?)", (node, str(home), r.now()))
        con.close()

    def home_of(self, node):
        """Where a peer can be reached (a simulation detail: in production, its service URL)."""
        con = self._con()
        row = con.execute("SELECT home FROM members WHERE node = ?", (node,)).fetchone()
        con.close()
        if not row:
            raise Unreachable(f"unknown node {node}")
        return row[0]

    def publish(self, node, tok, sig, when, kind):
        con = self._con()
        with con:
            con.execute("INSERT OR REPLACE INTO idx VALUES (?, ?, ?, ?, ?)", (node, tok, sig, when, kind))
        con.close()

    def query(self, node, sig, since, max_dist=HAMMING_MAX, limit=MAX_CANDIDATES):
        """Other pharmacies' entries within `max_dist` bits, nearest first. One XOR + popcount over the table."""
        con = self._con()
        rows = con.execute("SELECT node, token, sig, date, kind FROM idx WHERE node != ? AND date >= ?",
                           (node, since)).fetchall()
        total = con.execute("SELECT COUNT(*) FROM idx WHERE node != ?", (node,)).fetchone()[0]
        con.close()
        if not rows:
            return [], total
        m = np.frombuffer(b"".join(x[2] for x in rows), np.uint8).reshape(len(rows), SIG_BITS // 8)
        dist = _POP[np.bitwise_xor(m, np.frombuffer(sig, np.uint8))].sum(1)
        order = [i for i in np.argsort(dist, kind="stable") if dist[i] <= max_dist][:limit]
        return [{"node": rows[i][0], "token": rows[i][1], "date": rows[i][3], "kind": rows[i][4],
                 "dist": int(dist[i])} for i in order], total

    def stats(self):
        con = self._con()
        n = con.execute("SELECT COUNT(*) FROM idx").fetchone()[0]
        nodes = con.execute("SELECT COUNT(*) FROM members").fetchone()[0]
        con.close()
        return {"entries": n, "members": nodes}


# ---------------------------------------------------------------- local bookkeeping

SHARED_SCHEMA = """
CREATE TABLE IF NOT EXISTS shared_map (
  token TEXT PRIMARY KEY, entry_id TEXT NOT NULL UNIQUE, published INTEGER NOT NULL DEFAULT 0,
  unchecked INTEGER NOT NULL DEFAULT 0);
"""


def _con():
    con = r.db()
    con.executescript(SHARED_SCHEMA)
    return con


def publish(entry_id, kind, fp, when):
    """Called when a slip is recorded locally. Never raises: a down hub leaves the entry unpublished for `sync`."""
    cfg = config()
    if not cfg:
        return None
    con = _con()
    tok = token(cfg["key"], cfg["node"], entry_id)
    con.execute("INSERT OR IGNORE INTO shared_map (token, entry_id) VALUES (?, ?)", (tok, entry_id))
    con.commit()
    try:
        Hub(cfg["hub"]).publish(cfg["node"], tok, signature(cfg["key"], fp), when, KINDS[kind])
    except (Unreachable, sqlite3.Error):
        return False
    with con:
        con.execute("UPDATE shared_map SET published = 1 WHERE token = ?", (tok,))
        r.audit(con, "shared-publish", entry_id, {"token": tok})
    return True


def mark_unchecked(entry_id):
    """This slip was recorded while the shared index could not be asked: `sync` must ask later."""
    if config():
        con = _con()
        with con:
            con.execute("UPDATE shared_map SET unchecked = 1 WHERE entry_id = ?", (entry_id,))


# ---------------------------------------------------------------- messages between pharmacies

def compact_of(e):
    """The compact index form of a local entry: seen-entries already are; image-ledger entries derive it."""
    have = r.ledger_features(e)
    if "dens" not in have:
        have = r.compact(have, r.density(r.paper_crop(r.read_bgr(r.LEDGER_DIR / e["image"]))))
    return have


def pack_compact(c):
    """A compact index entry as bytes: what stage 2 sends to ONE candidate's owner (never to the hub)."""
    buf = io.BytesIO()
    np.savez_compressed(buf, pts=c["pts"], resp=c["resp"], desc=c["desc"], fp=c["fp"],
                        shape=c["shape"], dens=c["dens"])
    return base64.b64encode(buf.getvalue()).decode()


def unpack_compact(s):
    z = np.load(io.BytesIO(base64.b64decode(s)), allow_pickle=False)
    return {k: z[k] for k in z.files}


def _mac(key, node, ts, body):
    return hmac.new(key, f"req|{node}|{ts}|".encode() + body, hashlib.sha256).hexdigest()


def make_request(cfg, tok, compact):
    body = json.dumps({"token": tok, "compact": pack_compact(compact)}, separators=(",", ":"))
    ts = int(time.time())
    return {"from": cfg["node"], "ts": ts, "body": body, "mac": _mac(cfg["key"], cfg["node"], ts, body.encode())}


def answer_request(req):
    """Runs at the OWNER of the candidate. Authenticates the asker, compares locally, discloses the minimum."""
    cfg = config()
    if not cfg:
        return {"error": "not a member"}
    body = req.get("body", "")
    if len(body) > MSG_MAX_BYTES:
        return {"error": "message too large"}
    if abs(time.time() - int(req.get("ts", 0))) > MSG_MAX_AGE_S or \
            not hmac.compare_digest(str(req.get("mac", "")), _mac(cfg["key"], req.get("from", ""), req["ts"], body.encode())):
        return {"error": "not authenticated"}
    msg = json.loads(body)
    con = _con()
    row = con.execute("SELECT entry_id FROM shared_map WHERE token = ?", (msg["token"],)).fetchone()
    result = {"match": False}
    if row:
        e = con.execute("SELECT * FROM entries WHERE id = ?", (row[0],)).fetchone()
        if e is not None:
            e = dict(e)
            have = compact_of(e)
            q = unpack_compact(msg["compact"])
            corr = r.index_match(have, q, q["dens"])
            if corr >= MIN_CORR:
                result = {"match": True, "corr": round(corr, 2), "date": e["date"],
                          "kind": "dispensed" if e["kind"] == "dispensed" else "seen",
                          "drug_tags": drug_tags(cfg["key"], e["drugs"])}
    with con:   # accountability: who asked, about which entry, what was disclosed
        r.audit(con, "shared-answer", row[0] if row else None,
                {"asked_by": req.get("from"), "match": result["match"]})
    return result


def call_peer(cfg, hub, node, req):
    """Simulated transport: the peer is another RXCHECK_HOME, run as its own process."""
    try:
        home = hub.home_of(node)
        env = dict(os.environ, RXCHECK_HOME=home)
        p = subprocess.run([sys.executable, str(Path(r.__file__).resolve()), "shared", "answer"],
                           input=json.dumps(req), capture_output=True, text=True, timeout=PEER_TIMEOUT_S, env=env)
        out = json.loads(p.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as ex:
        raise Unreachable(f"peer {node}: {ex}")
    if "error" in out:
        raise Unreachable(f"peer {node}: {out['error']}")
    return out


# ---------------------------------------------------------------- the lookup a check performs

def lookup(feats, dens, drugs, window_days=365, transport=None):
    """Ask the network whether another pharmacy has seen this slip. Returns a dict with status
    'off' | 'clear' | 'match' | 'unreachable' (plus detail). Cheap when off (one missing file)."""
    cfg = config()
    if not cfg:
        return {"status": "off"}
    transport = transport or call_peer     # resolved now, so tests and other transports can replace it
    since = (date.today() - timedelta(days=window_days)).isoformat()
    hub = Hub(cfg["hub"])
    try:
        cands, total = hub.query(cfg["node"], signature(cfg["key"], feats["fp"]), since)
    except Unreachable as ex:
        return {"status": "unreachable", "detail": str(ex)}
    if not cands:
        return {"status": "clear", "searched": total}
    compact = r.compact(feats, dens)
    hits, failed = [], []
    for c in cands:
        try:
            resp = transport(cfg, hub, c["node"], make_request(cfg, c["token"], compact))
        except Unreachable as ex:
            failed.append(str(ex))
            continue
        if resp.get("match"):
            hits.append({**resp, "node": c["node"], "dist": c["dist"]})
    if hits:
        best = max(hits, key=lambda h: (h["kind"] == "dispensed", h["corr"]))
        changes = drug_changes(cfg["key"], drugs, best["drug_tags"]) if drugs else []
        return {"status": "match", "date": best["date"], "kind": best["kind"], "corr": best["corr"],
                "pharmacies": len({h["node"] for h in hits}), "changes": changes, "tags": best["drug_tags"]}
    if failed:   # a candidate we could not verify is not the same as no candidate
        return {"status": "unreachable", "detail": "; ".join(failed)}
    return {"status": "clear", "searched": total}


def public_result(res):
    """The part of a lookup result that goes into the report (the tags stay in memory)."""
    return {k: v for k, v in res.items() if k != "tags"}


def item_changed(res, item):
    """Does this one medicine differ from the slip as recorded at the other pharmacy?"""
    cfg = config()
    return bool(cfg and res.get("tags") is not None and drug_changes(cfg["key"], item, res["tags"]))


# ---------------------------------------------------------------- sync after a connection returns

def sync(transport=None):
    """Publish what could not be published, then re-check slips that were checked while the network was
    unreachable. A match found now is REPORTED: audit trail plus a hold, never silently dropped."""
    cfg = config()
    if not cfg:
        return {"enabled": False}
    con = _con()
    out = {"enabled": True, "published": 0, "rechecked": 0, "late_matches": [], "still_offline": False}
    try:
        for row in con.execute("SELECT token, entry_id FROM shared_map WHERE published = 0").fetchall():
            e = con.execute("SELECT kind, date, fp FROM entries WHERE id = ?", (row[1],)).fetchone()
            if e is None:
                continue
            Hub(cfg["hub"]).publish(cfg["node"], row[0], signature(cfg["key"], np.frombuffer(e["fp"], np.float32)),
                                    e["date"], KINDS[e["kind"]])
            with con:
                con.execute("UPDATE shared_map SET published = 1 WHERE token = ?", (row[0],))
                r.audit(con, "shared-publish", row[1], {"token": row[0], "late": True})
            out["published"] += 1
        for row in con.execute("SELECT entry_id FROM shared_map WHERE unchecked = 1").fetchall():
            e = dict(con.execute("SELECT * FROM entries WHERE id = ?", (row[0],)).fetchone())
            have = compact_of(e)
            res = lookup(have, have["dens"], e["drugs"], transport=transport)
            if res["status"] == "unreachable":
                out["still_offline"] = True
                continue
            with con:
                con.execute("UPDATE shared_map SET unchecked = 0 WHERE entry_id = ?", (row[0],))
            out["rechecked"] += 1
            if res["status"] == "match":
                late_match(e, res)
                out["late_matches"].append({"entry": e["id"], "date": res["date"], "kind": res["kind"]})
    except Unreachable:
        out["still_offline"] = True
    return out


def late_match(e, res):
    """A slip we cleared while offline turns out to have been seen elsewhere: say so, loudly."""
    text = (f"LATE MATCH: slip {e['id']} (checked here {e['date']} while the shared index was unreachable) was "
            f"{'dispensed' if res['kind'] == 'dispensed' else 'checked'} at another pharmacy on {res['date']}")
    flags = {"edited": False, "repeat": True, "similar": False, "mismatch": bool(res["changes"]), "doc_mismatch": False,
             "drug_mismatch": bool(res["changes"]), "new_slip": False, "unusable": False, "prescriber": "unknown",
             "meta_conflict": False, "issuance": "none", "shared": True}
    rep = {"image": None, "sha256": e["sha256"], "verdicts": {"routine": "AMBER", "high": "AMBER"},
           "findings": [text] + res["changes"], "path": "call", "held": [], "flags": flags, "fail_closed": []}
    hid = r.open_hold(rep)
    con = r.db()
    with con:
        r.audit(con, "shared-late-match", e["id"], {"hold": hid, "date": res["date"], "kind": res["kind"]})
    return hid


def status():
    cfg = config()
    if not cfg:
        return {"enabled": False}
    con = _con()
    own = {"published": con.execute("SELECT COUNT(*) FROM shared_map WHERE published = 1").fetchone()[0],
           "unpublished": con.execute("SELECT COUNT(*) FROM shared_map WHERE published = 0").fetchone()[0],
           "unchecked": con.execute("SELECT COUNT(*) FROM shared_map WHERE unchecked = 1").fetchone()[0]}
    try:
        hub = Hub(cfg["hub"]).stats()
    except Unreachable as ex:
        hub = {"error": str(ex)}
    return {"enabled": True, "node": cfg["node"], "hub": cfg["hub"], "own": own, "network": hub}
