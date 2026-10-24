"""Authenticated doctor confirmation: the doctor, not the pharmacist, says "yes, I issued this".

Until now "Clinic confirmed" was recorded on the pharmacist's word. This module replaces the word with evidence:

  enrolment   The doctor's contact comes from the council register (the registry entry's sourced `phone`), never
              from the pharmacy and never from the slip. A one-time code is sent to that number; the doctor proves
              possession and registers a device key. Nobody else can enrol a doctor: a squatter's request still
              sends the code to the official number.
  ping        For a held high-risk prescription the pharmacy asks the service. The doctor's phone receives ONLY:
              patient initials and age, the medicines, the prescription date and a pseudonymous pharmacy id - plus a
              one-time code. At most 3 pending pings per doctor per hour, 30 per pharmacy per day.
  answer      Two channels, both authenticated:
              app tap   the doctor's device signs (request, decision, summary) with the key registered at enrolment;
              read-out  the doctor reads the one-time code to the pharmacist, who enters it. The code is bound to that
                        request and that pharmacy, single use, 3 tries, 15 minutes. It can approve, never deny.
              A denial is only possible through the app, and a denied request can no longer be approved.
  receipt     The service signs {request, doctor, hash of exactly what was asked, decision, channel, pharmacy,
              time}. The pharmacy verifies the signature with the service's public key, checks that the hash is the
              prescription of THIS hold, and records the receipt in its audit trail. A receipt for another prescription,
              another pharmacy, a tampered one or one older than 24 hours is worthless.

The service is SIMULATED: a folder (service.db, keys, a mock phone outbox). The pharmacy-side client only ever sees
request / verify_otp / status and a masked contact hint; enrolment, device keys and the outbox are the doctor's side.
Not built: a real SMS/app channel, doctor-initiated contact change, key rotation, and the counter-app screens (the
CLI and `resolve_hold` carry the whole flow). See README "Doctor confirmation".
"""
import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from pathlib import Path

import rxcheck as r

OTP_DIGITS = 6
REQUEST_TTL = 900            # a ping is open for 15 minutes
ENROL_TTL = 900
ENROL_MAX_TRIES = 5
OTP_MAX_TRIES = 3
MAX_PENDING_PER_DOCTOR_HOUR = 3
MAX_REQUESTS_PER_NODE_DAY = 30
RECEIPT_TTL = 86400
REQUEST_STATES = ("pending", "approved", "denied", "expired", "locked")
PATIENT_RE = re.compile(r"^[A-Z]{1,4}(, \d{1,3})?$")


def _clock():
    return time.time()


class Unreachable(Exception):
    """The service could not be reached."""


class Refused(Exception):
    """The service refused (message is safe to show)."""


# ---------------------------------------------------------------- crypto helpers (Ed25519, raw keys as base64)

def _b64(b):
    return base64.b64encode(b).decode()


def new_keypair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    k = Ed25519PrivateKey.generate()
    raw = serialization.Encoding.Raw
    return (_b64(k.private_bytes(raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())),
            _b64(k.public_key().public_bytes(raw, serialization.PublicFormat.Raw)))


def sign(priv_b64, message):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    return _b64(Ed25519PrivateKey.from_private_bytes(base64.b64decode(priv_b64)).sign(message))


def verify_sig(pub_b64, message, sig_b64):
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(pub_b64)).verify(base64.b64decode(sig_b64), message)
        return True
    except (InvalidSignature, ValueError):
        return False


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


# ---------------------------------------------------------------- what exactly is being asked

def mask_patient(text):
    """'Ravi Kumar, 34' -> 'RK, 34': the doctor's phone gets initials and age, never a name."""
    if PATIENT_RE.match((text or "").strip()):      # already initials (and age): keep as typed
        return text.strip()
    words = re.findall(r"[A-Za-z]+", text or "")
    age = re.findall(r"\d{1,3}", text or "")
    ini = "".join(w[0].upper() for w in words)[:4]
    return ini + (f", {age[-1]}" if age and ini else "") if ini else ""


def drugs_norm(drugs):
    """Canonical medicine list ('alprazolam 0.25; dolo 650'): the form that is hashed and compared."""
    return "; ".join(f"{mol} {'/'.join(nums)}".strip() for mol, nums in sorted(r.parse_drugs(drugs).items()))


def summary_sha(regn, patient, drugs_n, rx_date, slip_sha):
    return hashlib.sha256(canonical({"regn": str(regn), "patient": patient, "drugs": drugs_n,
                                      "date": str(rx_date or ""), "slip": slip_sha or ""})).hexdigest()


def _digits(phone):
    return re.sub(r"\D", "", phone or "")[-10:]


def _hint(phone):
    return "******" + _digits(phone)[-2:]


# ---------------------------------------------------------------- the service (simulated: a folder)

SCHEMA = """
CREATE TABLE IF NOT EXISTS enrolments (
  regn TEXT PRIMARY KEY, name TEXT NOT NULL, contact TEXT NOT NULL, status TEXT NOT NULL,
  otp_hash TEXT, otp_expires REAL, otp_tries INTEGER NOT NULL DEFAULT 0, device_pub TEXT,
  created REAL NOT NULL, activated REAL);
CREATE TABLE IF NOT EXISTS requests (
  id TEXT PRIMARY KEY, regn TEXT NOT NULL, node TEXT NOT NULL, summary_sha TEXT NOT NULL,
  patient TEXT NOT NULL, drugs TEXT NOT NULL, rx_date TEXT,
  status TEXT NOT NULL, via TEXT, otp_hash TEXT NOT NULL, tries INTEGER NOT NULL DEFAULT 0,
  created REAL NOT NULL, expires REAL NOT NULL, decided REAL, receipt TEXT, sig TEXT);
CREATE INDEX IF NOT EXISTS requests_regn ON requests(regn, created);
CREATE INDEX IF NOT EXISTS requests_node ON requests(node, created);
"""


class Service:
    def __init__(self, path):
        self.path = Path(path)

    def _con(self):
        if (self.path / "OFFLINE").exists():       # simulates the service / connection being down
            raise Unreachable("doctor service unreachable (simulated: OFFLINE marker)")
        try:
            self.path.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(self.path / "service.db", timeout=30)
            con.row_factory = sqlite3.Row
            con.executescript(SCHEMA)
            return con
        except (OSError, sqlite3.Error) as ex:
            raise Unreachable(f"doctor service unreachable: {ex}")

    # -- keys
    def init(self):
        if (self.path / "service.key").exists():
            raise Refused("service already initialised")
        self.path.mkdir(parents=True, exist_ok=True)
        priv, pub = new_keypair()
        (self.path / "service.key").write_text(priv, encoding="utf-8")
        (self.path / "service.pub").write_text(pub, encoding="utf-8")
        (self.path / "service.secret").write_text(_b64(secrets.token_bytes(32)), encoding="utf-8")
        self._con().close()
        return pub

    def pub(self):
        return (self.path / "service.pub").read_text(encoding="utf-8").strip()

    def _hmac(self, *parts):
        secret = base64.b64decode((self.path / "service.secret").read_text(encoding="utf-8"))
        return hmac.new(secret, "|".join(parts).encode(), hashlib.sha256).hexdigest()

    # -- mock phone: what the doctor's contact receives
    def _send(self, contact, message):
        out = self.path / "outbox"
        out.mkdir(exist_ok=True)
        with open(out / f"{_digits(contact)}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({**message, "at": int(_clock())}) + "\n")

    def inbox(self, contact):
        p = self.path / "outbox" / f"{_digits(contact)}.jsonl"
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    # -- enrolment (the doctor's side)
    def enrol_start(self, regn, council_entry):
        """The contact is the council register's sourced phone; the caller cannot choose it."""
        contact = r.trusted(council_entry, "phone")
        if not contact or not council_entry.get("name"):
            raise Refused("the council register has no sourced contact for this registration number")
        con = self._con()
        row = con.execute("SELECT status FROM enrolments WHERE regn = ?", (regn,)).fetchone()
        if row and row["status"] == "active":
            raise Refused("already enrolled: a contact change must be confirmed from the enrolled contact")
        otp = f"{secrets.randbelow(10 ** OTP_DIGITS):0{OTP_DIGITS}d}"
        with con:
            con.execute("INSERT OR REPLACE INTO enrolments (regn, name, contact, status, otp_hash, otp_expires, otp_tries,"
                        " created) VALUES (?, ?, ?, 'pending', ?, ?, 0, ?)",
                        (regn, council_entry["name"], contact, self._hmac("enrol", regn, otp), _clock() + ENROL_TTL, _clock()))
        self._send(contact, {"type": "enrol-otp", "regn": regn, "otp": otp,
                             "text": f"Enrolment code {otp} for doctor confirmations (registration {regn})."})
        con.close()
        return {"contact_hint": _hint(contact)}

    def enrol_confirm(self, regn, otp, device_pub):
        con = self._con()
        row = con.execute("SELECT * FROM enrolments WHERE regn = ? AND status = 'pending'", (regn,)).fetchone()
        if not row or _clock() > (row["otp_expires"] or 0) or row["otp_tries"] >= ENROL_MAX_TRIES:
            raise Refused("no open enrolment (expired or locked): start again")
        ok = hmac.compare_digest(row["otp_hash"], self._hmac("enrol", regn, str(otp)))
        with con:
            if not ok:
                con.execute("UPDATE enrolments SET otp_tries = otp_tries + 1 WHERE regn = ?", (regn,))
            else:
                con.execute("UPDATE enrolments SET status = 'active', otp_hash = NULL, device_pub = ?, activated = ?"
                            " WHERE regn = ?", (device_pub, _clock(), regn))
        con.close()
        if not ok:
            raise Refused("wrong code")
        return {"status": "active"}

    # -- the pharmacy's API
    def request(self, node, regn, patient, drugs, drugs_n, rx_date, slip_sha):
        if not PATIENT_RE.match(patient or ""):
            raise Refused("patient must be initials and age only (e.g. 'RK, 34')")
        con = self._con()
        enrol = con.execute("SELECT * FROM enrolments WHERE regn = ? AND status = 'active'", (regn,)).fetchone()
        if not enrol:
            con.close()
            return {"status": "not_enrolled"}
        now = _clock()
        pending = con.execute("SELECT COUNT(*) FROM requests WHERE regn = ? AND created > ?", (regn, now - 3600)).fetchone()[0]
        daily = con.execute("SELECT COUNT(*) FROM requests WHERE node = ? AND created > ?", (node, now - 86400)).fetchone()[0]
        if pending >= MAX_PENDING_PER_DOCTOR_HOUR or daily >= MAX_REQUESTS_PER_NODE_DAY:
            con.close()
            return {"status": "rate_limited"}
        rid = secrets.token_hex(5)
        otp = f"{secrets.randbelow(10 ** OTP_DIGITS):0{OTP_DIGITS}d}"
        sha = summary_sha(regn, patient, drugs_n, rx_date, slip_sha)
        with con:
            con.execute("INSERT INTO requests (id, regn, node, summary_sha, patient, drugs, rx_date, status, otp_hash,"
                        " created, expires) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                        (rid, regn, node, sha, patient, drugs, rx_date, self._hmac("req", rid, node, otp), now, now + REQUEST_TTL))
        self._send(enrol["contact"], {
            "type": "confirm-request", "request_id": rid, "otp": otp,
            "from": f"pharmacy {node[:4]} (pseudonymous)", "patient": patient, "drugs": drugs, "rx_date": rx_date,
            "text": f"Did you issue a prescription for {patient}: {drugs}, dated {rx_date}? Approve or deny in the app, "
                    f"or read code {otp} to the pharmacist to approve."})
        con.close()
        return {"status": "pending", "request_id": rid, "expires_at": int(now + REQUEST_TTL),
                "contact_hint": _hint(enrol["contact"]), "channels": ["app", "otp"]}

    def _expire(self, con, row):
        if row["status"] == "pending" and _clock() > row["expires"]:
            with con:
                con.execute("UPDATE requests SET status = 'expired' WHERE id = ?", (row["id"],))
            return "expired"
        return row["status"]

    def _decide(self, con, row, decision, via):
        now = int(_clock())
        body = {"v": 1, "request_id": row["id"], "regn": row["regn"], "summary_sha": row["summary_sha"],
                "decision": decision, "via": via, "node": row["node"], "decided_at": now, "valid_until": now + RECEIPT_TTL}
        receipt = canonical(body)
        priv = (self.path / "service.key").read_text(encoding="utf-8")
        with con:
            con.execute("UPDATE requests SET status = ?, via = ?, decided = ?, receipt = ?, sig = ? WHERE id = ?",
                        (decision, via, now, receipt.decode(), sign(priv, receipt), row["id"]))

    def _view(self, con, rid, node):
        row = con.execute("SELECT * FROM requests WHERE id = ? AND node = ?", (rid, node)).fetchone()
        if not row:
            raise Refused("unknown request")
        status = self._expire(con, row)
        row = con.execute("SELECT * FROM requests WHERE id = ?", (rid,)).fetchone()
        out = {"status": status}
        if row["receipt"]:
            out["receipt"], out["sig"] = row["receipt"], row["sig"]
        return out

    def status(self, node, request_id):
        con = self._con()
        out = self._view(con, request_id, node)
        con.close()
        return out

    def verify_otp(self, node, request_id, code):
        """The code the doctor read out. Approves only; bound to this request and this pharmacy."""
        con = self._con()
        row = con.execute("SELECT * FROM requests WHERE id = ? AND node = ?", (request_id, node)).fetchone()
        if not row:
            con.close()
            raise Refused("unknown request")
        status = self._expire(con, row)
        if status != "pending":
            out = self._view(con, request_id, node)
            con.close()
            return out
        if row["tries"] >= OTP_MAX_TRIES:
            with con:
                con.execute("UPDATE requests SET status = 'locked' WHERE id = ?", (request_id,))
            con.close()
            return {"status": "locked"}
        if hmac.compare_digest(row["otp_hash"], self._hmac("req", request_id, node, str(code))):
            self._decide(con, row, "approved", "otp")
        else:
            with con:
                con.execute("UPDATE requests SET tries = tries + 1 WHERE id = ?", (request_id,))
            left = OTP_MAX_TRIES - row["tries"] - 1
            if left <= 0:
                with con:
                    con.execute("UPDATE requests SET status = 'locked' WHERE id = ?", (request_id,))
                con.close()
                return {"status": "locked"}
            con.close()
            return {"status": "wrong_otp", "tries_left": left}
        out = self._view(con, request_id, node)
        con.close()
        return out

    # -- the doctor's device
    def respond(self, request_id, decision, signature):
        """App tap: the device signs 'request|decision|summary' with the key registered at enrolment."""
        if decision not in ("approved", "denied"):
            raise Refused("decision must be approved or denied")
        con = self._con()
        row = con.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        if not row:
            raise Refused("unknown request")
        if self._expire(con, row) != "pending":
            raise Refused("request is no longer open")
        enrol = con.execute("SELECT device_pub FROM enrolments WHERE regn = ? AND status = 'active'", (row["regn"],)).fetchone()
        msg = f"{request_id}|{decision}|{row['summary_sha']}".encode()
        if not enrol or not verify_sig(enrol["device_pub"], msg, signature):
            raise Refused("signature does not match the doctor's enrolled device")
        self._decide(con, row, decision, "app")
        con.close()
        return {"status": decision}


def device_sign(device_priv, request_id, decision, summary_sha_):
    """What the doctor's app does on a tap (mock phone side)."""
    return sign(device_priv, f"{request_id}|{decision}|{summary_sha_}".encode())


# ---------------------------------------------------------------- the pharmacy's side

CONFIG_FILE = "doctorsvc.json"
CLIENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS doctor_asks (
  request_id TEXT PRIMARY KEY, hold_id TEXT NOT NULL, regn TEXT NOT NULL, summary_sha TEXT NOT NULL,
  asked_at TEXT NOT NULL, status TEXT NOT NULL, via TEXT, receipt TEXT, sig TEXT);
CREATE INDEX IF NOT EXISTS doctor_asks_hold ON doctor_asks(hold_id);
"""


def config():
    p = r.HOME / "data" / CONFIG_FILE
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return {"service": d["service"], "pub": d["pub"], "node": d["node"]}
    except (OSError, ValueError, KeyError):
        return None


def trust(service_dir):
    """Trust a doctor-confirmation service: store its PUBLIC key and a pseudonymous node id for this pharmacy."""
    cfg = config()
    node = cfg["node"] if cfg else secrets.token_hex(4)
    r.save_json(r.HOME / "data" / CONFIG_FILE, {"service": str(service_dir), "pub": Service(service_dir).pub(), "node": node})
    return node


def _con():
    con = r.db()
    con.executescript(CLIENT_SCHEMA)
    return con


def ask(hold_id, regn, drugs, patient, rx_date, slip_sha=None):
    """Ping the enrolled doctor about the prescription this hold concerns. There is deliberately no contact
    parameter: the service uses the number from enrolment, so neither the slip nor the pharmacist can redirect it."""
    cfg = config()
    if not cfg:
        return {"status": "not_configured"}
    if not slip_sha:
        row = r.db().execute("SELECT image FROM holds WHERE id = ?", (hold_id,)).fetchone()
        slip_sha = r.sha256(row["image"]) if row and row["image"] and Path(row["image"]).is_file() else ""
    who, norm = mask_patient(patient), drugs_norm(drugs)
    try:
        res = Service(cfg["service"]).request(cfg["node"], str(regn), who, drugs, norm, str(rx_date or ""), slip_sha)
    except Unreachable as ex:
        return {"status": "unreachable", "detail": str(ex)}
    except Refused as ex:
        return {"status": "refused", "detail": str(ex)}
    if res["status"] == "pending":
        con = _con()
        with con:
            con.execute("INSERT INTO doctor_asks VALUES (?, ?, ?, ?, ?, 'pending', NULL, NULL, NULL)",
                        (res["request_id"], hold_id, str(regn), summary_sha(regn, who, norm, rx_date, slip_sha), r.now()))
            r.audit(con, "doctor-ask", hold_id, {"request": res["request_id"], "regn": str(regn),
                                                 "contact": res["contact_hint"]})
    return res


def _record(request_id, res):
    """Keep what the service answered (a verified receipt, or just the status)."""
    con = _con()
    row = con.execute("SELECT * FROM doctor_asks WHERE request_id = ?", (request_id,)).fetchone()
    if not row:
        return
    decided_before = row["receipt"] is not None
    with con:
        con.execute("UPDATE doctor_asks SET status = ?, via = ?, receipt = ?, sig = ? WHERE request_id = ?",
                    (res["status"] if res["status"] in REQUEST_STATES else row["status"],   # a wrong code is not a state
                     json.loads(res["receipt"])["via"] if res.get("receipt") else row["via"],
                     res.get("receipt") or row["receipt"], res.get("sig") or row["sig"], request_id))
        if res.get("receipt") and not decided_before:
            r.audit(con, "doctor-receipt", row["hold_id"], {"request": request_id, "decision": res["status"],
                                                            "via": json.loads(res["receipt"])["via"]})


def poll(request_id):
    cfg = config()
    try:
        res = Service(cfg["service"]).status(cfg["node"], request_id)
    except Unreachable as ex:
        return {"status": "unreachable", "detail": str(ex)}
    except Refused as ex:
        return {"status": "refused", "detail": str(ex)}
    _record(request_id, res)
    return {"status": res["status"]}


def submit_otp(request_id, code):
    cfg = config()
    try:
        res = Service(cfg["service"]).verify_otp(cfg["node"], request_id, str(code).strip())
    except Unreachable as ex:
        return {"status": "unreachable", "detail": str(ex)}
    except Refused as ex:
        return {"status": "refused", "detail": str(ex)}
    _record(request_id, res)
    return {k: v for k, v in res.items() if k not in ("receipt", "sig")}


def _verified(row, cfg):
    """Receipt -> {'decision', 'via', 'request_id'} only if the signature holds, it is for THIS pharmacy and THIS
    prescription, and it is fresh. Anything else is as good as no receipt."""
    if not (row["receipt"] and row["sig"] and cfg):
        return None
    if not verify_sig(cfg["pub"], row["receipt"].encode(), row["sig"]):
        return None
    body = json.loads(row["receipt"])
    if body.get("node") != cfg["node"] or body.get("summary_sha") != row["summary_sha"] \
            or body.get("request_id") != row["request_id"] or _clock() > body.get("valid_until", 0):
        return None
    return {"decision": body["decision"], "via": body["via"], "request_id": row["request_id"]}


def hold_decision(hold_id):
    """What the doctor said about this hold: {'decision': 'approved'|'denied', ...} or None. A denial wins.

    A denial is final. It is read from the append-only audit trail, so it does not expire after RECEIPT_TTL
    and does not vanish if the service config is removed. Trusting it without re-checking the signature is
    safe because a denial can only make the pharmacy stricter. An approval counts only while its receipt
    verifies and is fresh."""
    for (det,) in r.db().execute("SELECT detail FROM audit WHERE action = 'doctor-receipt' AND ref = ? ORDER BY seq",
                                 (hold_id,)):
        d = json.loads(det or "{}")
        if d.get("decision") == "denied":
            return {"decision": "denied", "via": d.get("via"), "request_id": d.get("request")}
    cfg = config()
    if not cfg:
        return None
    rows = _con().execute("SELECT * FROM doctor_asks WHERE hold_id = ? AND receipt IS NOT NULL ORDER BY asked_at", (hold_id,)).fetchall()
    found = [v for v in (_verified(x, cfg) for x in rows) if v]
    return next((v for v in found if v["decision"] == "denied"), next(iter(found), None))


def asks(hold_id):
    """This hold's pings, oldest first: [{'request_id', 'regn', 'asked_at', 'status', 'via'}]."""
    return [dict(x) for x in _con().execute(
        "SELECT request_id, regn, asked_at, status, via FROM doctor_asks WHERE hold_id = ? ORDER BY asked_at, rowid", (hold_id,))]
