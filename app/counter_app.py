"""rxcheck counter app - one decision, the reason for it, the evidence and the next action.

One PC runs the app (`python app/rxcheck.py serve --host 0.0.0.0 --cert ... --key ...`); every billing PC opens
it in a browser. All storage stays in the single SQLite file on that PC, so counters never disagree and
there is one audit trail. Staff sign in with their own PIN (`rxcheck staff add`), so the trail records who
actually acted - unlike RXCHECK_USER on the CLI, which is only a claim.

Standard library only on the server. Every page is complete HTML and works without JavaScript; the Vue bundle in
static/ (built from web/, committed, served from this PC - never a CDN) adds display controls, hotkeys, lookup
fields and the interactive audit log on top. Decisions and hold outcomes never depend on it.
"""
import csv
import hashlib
import html
import io
import json
import os
import re
import secrets
import ssl
import sys
import threading
import time
import traceback
from datetime import date, datetime, timedelta
from email import policy as email_policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, quote

import doctorping as dp
import ocr
import rxcheck as r

MAX_UPLOAD = 15 * 1024 * 1024
DEMO_USER = "Demo visitor"                # public demo only: everyone who opens the link is signed in as this
DEMO_DIR = Path(__file__).resolve().parent.parent / "demo"   # synthetic sample slips (tests/make_demo_samples.py)
MAX_DEMO_SESSIONS = 500                   # a crawler cannot fill memory with demo sessions
SESSION_IDLE = timedelta(minutes=30)      # shared counter PCs: re-enter the PIN after a break
SESSION_MAX = timedelta(hours=10)         # one shift
MAX_FAILURES, FAILURE_WINDOW = 5, timedelta(minutes=10)
UPLOAD_RETENTION_HOURS = 24               # images of finished cases are deleted after this (open holds keep theirs)
PHARMACIST_ONLY = {"confirmed", "dispensed-after-review"}
CASE_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")
STATIC = Path(__file__).resolve().parent.parent / "static"
STATIC_TYPES = {"app.js": "text/javascript; charset=utf-8", "app.css": "text/css; charset=utf-8"}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
       "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
LOOKUP_KINDS = ("drug", "regn", "doctor", "patient")
SEEN_DRUG = re.compile(r"\s*(?:(?:tab|cap|syp|inj|t|c)\.?\s+)?([a-z][a-z-]{2,}(?: [a-z][a-z-]{2,})?)", re.I)
LOOKUP_MAX = 8
AUDIT_PAGE = 50
OUTCOME_LABELS = {"confirmed": "Clinic confirmed", "refused": "Refused", "dispensed-after-review": "Dispensed after review",
                  "unreachable": "No answer - waiting for a call-back", "retaken": "Replaced by a new photo"}

VERDICT_UI = {
    "RED": ("HOLD", "Do not dispense until confirmed"),
    "AMBER": ("CONFIRM FIRST", "Needs confirmation before dispensing"),
    "GREEN": ("NO CONFLICT FOUND", "Dispense with your normal checks - this is not proof the slip is genuine"),
}


class HttpError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


DEMO = os.environ.get("RXCHECK_DEMO", "").lower() in ("1", "true", "yes")
DEMO_BANNER = ("DEMO DEPLOYMENT - research prototype. Do not upload real prescriptions or any patient data; "
               "it is not for dispensing decisions.")


class State:
    def __init__(self, inbox, backup_dir=None, backup_hours=4.0, tls=False, behind_proxy=False):
        self.inbox = Path(inbox) if inbox else None
        self.backup_dir, self.backup_hours = backup_dir, backup_hours
        self.behind_proxy = behind_proxy
        self.tls = tls or behind_proxy      # behind a cloud proxy the browser talks HTTPS: cookies stay Secure
        self.sessions, self.failures = {}, {}
        self.lock = threading.Lock()
        self.case_lock = threading.Lock()
        self.last_backup = None   # (when, folder, problems) or (when, None, [error])


STATE = None


def uploads():
    d = r.LEDGER_DIR / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


# ---------------------------------------------------------------- cases (one customer's prescription)

def case_files(cid):
    if not CASE_ID.match(cid or ""):
        raise HttpError(404, "No such case")
    imgs = [p for p in uploads().glob(cid + ".*") if p.suffix.lower() in r.IMG_EXT]
    meta = uploads() / f"{cid}.json"
    if not imgs or not meta.exists():
        raise HttpError(404, "This case is no longer available (finished cases are cleared after a day)")
    return imgs[0], meta


def load_case(cid):
    img, meta = case_files(cid)
    return img, json.loads(meta.read_text(encoding="utf-8"))


def save_case(cid, data):
    meta = uploads() / f"{cid}.json"
    tmp = meta.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(meta)


def new_case(data, suffix, origin, read=True):
    if len(data) > MAX_UPLOAD:
        raise HttpError(413, "Image is larger than 15 MB")
    if suffix not in r.IMG_EXT:
        raise HttpError(400, "Send a photo (.jpg, .png or .webp)")
    cid = f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
    path = uploads() / f"{cid}{suffix}"
    path.write_bytes(data)
    try:
        r.read_bgr(path)
    except ValueError:
        path.unlink()
        raise HttpError(400, "That file is not a readable image")
    # pre-check without drugs: shows routine/high-risk outcomes and decodes any clinic-signed code to prefill
    pre = r.public(r.run_check(path, draw=False))
    # reading the photo takes 5-15 s on a counter PC: it runs in the background and the form fills in when it is
    # done. Not for a retake (already decided) or a demo sample (synthetic slip, medicines filled in for the visitor)
    read = read and not origin.startswith("demo:") and ocr.available()
    save_case(cid, {"origin": origin, "created": r.now(), "by": r.actor(), "pre": pre, "final": None,
                    "hold": None, "recorded": None, "ocr": {"status": "pending"} if read else None})
    if read:
        threading.Thread(target=read_case, args=(cid, path, known_drug_names()), daemon=True).start()
    return cid


def known_drug_names():
    """Medicine names this pharmacy knows: the drug list, and names entered at this counter before."""
    table = r.drug_table() or r.EMPTY_TABLE
    names = set(table["brands"]) | set(table["molecules"])
    for (drugs,) in r.db().execute("SELECT drugs FROM entries WHERE drugs != '' ORDER BY created_at DESC LIMIT 3000"):
        for item in re.split(r"[,;\n]", drugs or ""):
            m = SEEN_DRUG.match(item)
            if m:
                names.add(m.group(1).strip().lower())
    return names


def read_case(cid, path, known):
    """Background: OCR the photo and store what it read on the case. A failure only means staff type as before."""
    res = ocr.read_slip(path, known, lambda text: r.drug_risk(text)[0] == "high")
    with STATE.case_lock:
        try:
            _, case = load_case(cid)
        except HttpError:
            return   # cleared meanwhile
        case["ocr"] = res
        save_case(cid, case)


def finalize(cid, img, f):
    """The counter check: decide, record, open a hold for anything held back, and store it on the case."""
    rx_date = r.iso_date(f.get("rx_date")) if f.get("rx_date") else None
    rep = r.run_check(img, f.get("drugs"), f.get("regn") or None, f.get("doctor") or None, rx_date)
    r.record_check(rep, f.get("drugs"), f.get("regn") or None, f.get("doctor") or None)
    worst = max(rep["verdicts"].values(), key=r.ORDER.index)
    hold = r.open_hold(rep) if worst != "GREEN" else None
    r.remember_patient(f.get("patient"))
    with STATE.case_lock:
        _, case = load_case(cid)
        case.update(final=r.public(rep), hold=hold, fields=f)
        save_case(cid, case)


def purge_uploads():
    """Delete images of finished cases after the retention period; never one an open hold refers to."""
    cutoff = time.time() - UPLOAD_RETENTION_HOURS * 3600
    held = {Path(row["image"]).stem for row in r.db().execute("SELECT image FROM holds WHERE status != 'closed'")}
    removed = 0
    for p in uploads().iterdir():
        if p.name.split(".")[0] not in held and p.stat().st_mtime < cutoff:
            p.unlink(missing_ok=True)
            removed += 1
    return removed


def hold_outcome(hid):
    row = r.db().execute("SELECT detail FROM audit WHERE action = 'resolve' AND ref = ? ORDER BY seq DESC LIMIT 1",
                         (hid,)).fetchone()
    return json.loads(row["detail"])["outcome"] if row else None


# ---------------------------------------------------------------- page parts

# Colours: every text/background pair meets WCAG AAA (7:1) in all four themes - standard and high contrast,
# light and dark (tests/test_a11y.py measures them). High contrast is chosen in the header (cookie rxui ->
# <html data-contrast>), or follows the OS "increase contrast" setting until someone chooses.
CSS = """
:root{--bg:#f6f7f4;--card:#fff;--ink:#1d2321;--muted:#414a46;--line:#c4cbc6;--accent:#174a39;--focus:#174a39;
--red:#8a1014;--red-bg:#fde8e8;--amber:#5a3700;--amber-bg:#fff2d6;--green:#14491a;--green-bg:#e3f4e4;--bw:1px}
@media (prefers-color-scheme:dark){:root{--bg:#141716;--card:#1d2120;--ink:#e9ecea;--muted:#b8c1bc;--line:#444c48;
--accent:#8fd4b6;--focus:#8fd4b6;--red:#ffa59d;--red-bg:#3a1416;--amber:#ffcf8a;--amber-bg:#352710;--green:#b3dfb5;
--green-bg:#142a17}}
@media (prefers-contrast:more){html:not([data-contrast=std]){--bg:#fff;--card:#fff;--ink:#000;--muted:#1f1f1f;--line:#000;
--accent:#00382a;--focus:#000;--red:#6e0000;--red-bg:#fff0f0;--amber:#3d2600;--amber-bg:#fff6e0;--green:#003d0a;
--green-bg:#effaef;--bw:2px}}
@media (prefers-contrast:more) and (prefers-color-scheme:dark){html:not([data-contrast=std]){--bg:#000;--card:#000;
--ink:#fff;--muted:#ebebeb;--line:#fff;--accent:#9effd2;--focus:#fff;--red:#ffbdb8;--red-bg:#1f0000;--amber:#ffdf9e;
--amber-bg:#1f1400;--green:#c2ffc6;--green-bg:#001f05;--bw:2px}}
html[data-contrast=high]{--bg:#fff;--card:#fff;--ink:#000;--muted:#1f1f1f;--line:#000;--accent:#00382a;--focus:#000;
--red:#6e0000;--red-bg:#fff0f0;--amber:#3d2600;--amber-bg:#fff6e0;--green:#003d0a;--green-bg:#effaef;--bw:2px}
@media (prefers-color-scheme:dark){html[data-contrast=high]{--bg:#000;--card:#000;--ink:#fff;--muted:#ebebeb;--line:#fff;
--accent:#9effd2;--focus:#fff;--red:#ffbdb8;--red-bg:#1f0000;--amber:#ffdf9e;--amber-bg:#1f1400;--green:#c2ffc6;
--green-bg:#001f05;--bw:2px}}
html[data-text="1"]{font-size:115%}html[data-text="2"]{font-size:130%}html[data-text="3"]{font-size:150%}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:1rem/1.5 system-ui,"Segoe UI",Roboto,sans-serif}
:focus-visible{outline:3px solid var(--focus);outline-offset:2px}
.skip{position:absolute;left:8px;top:-60px;padding:8px 14px;background:var(--card);color:var(--ink);border:2px solid var(--ink);border-radius:8px;z-index:30}
.skip:focus{top:8px}
html[data-contrast=high] a{text-decoration-thickness:2px}html[data-contrast=high] .card,html[data-contrast=high] input,
html[data-contrast=high] select,html[data-contrast=high] textarea{border-width:2px;border-color:var(--ink)}
html[data-contrast=high] button{border-width:2px}
a{color:var(--accent);text-underline-offset:3px}
header{position:sticky;top:0;z-index:10;display:flex;gap:10px 18px;align-items:center;padding:10px 20px;background:var(--card);border-bottom:var(--bw) solid var(--line);flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:10px;color:var(--ink);text-decoration:none;font-weight:800;font-size:1.1rem;line-height:1.1}
.brand small{display:block;font-size:.7rem;font-weight:700;color:var(--muted);letter-spacing:.1em;text-transform:uppercase}
.mark{display:inline-grid;place-items:center;width:36px;height:36px;border-radius:10px;background:var(--accent);color:var(--card);font-weight:900;font-size:.95rem;font-style:italic}
header nav{display:flex;gap:4px;flex:1 1 auto;flex-wrap:wrap}
header nav a{padding:8px 14px;border-radius:9px;color:var(--ink);text-decoration:none;font-weight:600}
header nav a:hover{background:var(--bg)}header nav a[aria-current=page]{background:var(--accent);color:var(--card)}
.who{display:flex;align-items:center;gap:10px;line-height:1.15}.who b{display:block}
.who .role{display:block;font-size:.8rem;color:var(--muted);text-transform:capitalize}
.avatar{display:inline-grid;place-items:center;width:36px;height:36px;border-radius:50%;background:var(--bg);border:var(--bw) solid var(--line);font-weight:800;font-size:.85rem}
main{max-width:1200px;margin:0 auto;padding:20px}
.card{background:var(--card);border:var(--bw) solid var(--line);border-radius:14px;padding:20px;margin:0 0 16px;box-shadow:0 1px 3px #00000012}
h1{font-size:1.45rem;margin:0 0 6px;letter-spacing:-.01em}h2{font-size:1.05rem;margin:0 0 12px}h3{font-size:1rem;margin:0 0 8px}
.card>h2{font-size:.78rem;text-transform:uppercase;letter-spacing:.09em;color:var(--muted)}
.home{display:grid;grid-template-columns:minmax(0,1fr);gap:0 16px;align-items:start}.side>.card{margin-bottom:16px}
.decision{display:grid;grid-template-columns:minmax(0,1fr);gap:0 20px;align-items:start}
@media (min-width:1000px){.home{grid-template-columns:minmax(0,1.35fr) minmax(0,1fr)}
.decision{grid-template-columns:minmax(0,1.1fr) minmax(0,1fr)}.d-side{position:sticky;top:76px}}
label{display:block;font-weight:600;margin:14px 0 6px}
input[type=text],input[type=date],input[type=password],textarea,select{width:100%;padding:11px 12px;border:var(--bw) solid var(--muted);border-radius:10px;background:var(--bg);color:var(--ink);font:inherit}
button,.button-link{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:44px;font:inherit;font-weight:700;padding:10px 18px;border-radius:10px;border:2px solid var(--accent);background:var(--accent);color:var(--card);cursor:pointer;text-decoration:none}
button.secondary{background:var(--card);color:var(--accent)}
button.danger{background:var(--red);border-color:var(--red);color:var(--card)}
button.small{min-height:34px;padding:5px 12px;font-size:.9rem}button.big{width:100%;min-height:54px;font-size:1.1rem}
.actions{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0 4px}form.inline{display:inline}
.row{display:flex;gap:12px;flex-wrap:wrap}.row>*{flex:1 1 200px}
input[type=file]{width:100%;padding:30px 16px;border:2px dashed var(--muted);border-radius:14px;background:var(--bg);color:var(--ink);font:inherit;cursor:pointer}
input[type=file]::file-selector-button{font:inherit;font-weight:700;margin-right:14px;padding:10px 16px;border-radius:10px;border:2px solid var(--accent);background:var(--card);color:var(--accent);cursor:pointer}
.drop{display:flex;flex-direction:column;gap:2px;margin:18px 0 8px;font-weight:400}.drop-title{font-weight:700;font-size:1.05rem}
.hero h1{font-size:1.7rem}.js-only{display:none}html.js .js-only{display:inline}
html.rx-drag input[type=file][data-shortcut=upload]{border-color:var(--accent);border-style:solid;background:var(--card)}
.verdict{display:grid;grid-template-columns:auto 1fr;gap:18px;align-items:start;border-radius:16px;padding:22px 24px;margin:0 0 14px;border:2px solid;border-left-width:12px}
.verdict .icon{display:grid;place-items:center;width:64px;height:64px;border-radius:50%;font-size:2rem;font-weight:900;line-height:1}
.verdict .word{font-size:2.4rem;font-weight:900;letter-spacing:.02em;line-height:1.05}.verdict .sub{font-weight:700;font-size:1.05rem;margin-top:2px}
.verdict .why{color:var(--ink);font-size:1.1rem;font-weight:600;margin:12px 0 0}
.v-RED{background:var(--red-bg);color:var(--red);border-color:var(--red)}.v-AMBER{background:var(--amber-bg);color:var(--amber);border-color:var(--amber)}
.v-GREEN{background:var(--green-bg);color:var(--green);border-color:var(--green)}
.v-RED .icon{background:var(--red);color:var(--red-bg)}.v-AMBER .icon{background:var(--amber);color:var(--amber-bg)}
.v-GREEN .icon{background:var(--green);color:var(--green-bg)}
.pill{display:inline-block;padding:2px 10px;border-radius:99px;font-weight:700;font-size:.85rem;border:1px solid}
.split{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:0 0 14px}
.split p{margin:0;padding:12px 16px;border-radius:12px;border:2px solid;font-size:1.05rem;font-weight:600}
.split b{display:block;font-size:.75rem;text-transform:uppercase;letter-spacing:.09em}
.give{background:var(--green-bg);color:var(--green);border-color:var(--green)}.wait{background:var(--amber-bg);color:var(--amber);border-color:var(--amber)}
.next{margin:0 0 14px;padding:14px 18px;border-radius:12px;background:var(--card);border:var(--bw) solid var(--line);border-left:6px solid var(--accent);font-size:1.02rem}
.next ul{margin:6px 0 0;padding-left:20px}.path-call{border-left-color:var(--red)}.path-counter,.path-retake{border-left-color:var(--green)}
.checked-by{margin:4px 0 2px}
.doctor{margin:14px 0 4px;padding:14px 16px;border-radius:12px;background:var(--bg);border:var(--bw) solid var(--line)}
.doctor input[type=text],.doctor input[type=date]{background:var(--card)}
img.evidence{display:block;width:100%;max-height:72vh;object-fit:contain;border:var(--bw) solid var(--line);border-radius:10px;background:#fff;margin:0 0 12px}
.evidence-card p{margin:0 0 8px}
.muted{color:var(--muted)}.warn{color:var(--red);font-weight:600}ul{padding-left:20px}li{margin:4px 0}
.notice{margin:0 0 16px;padding:12px 16px;border-radius:12px;border:2px solid;font-weight:600}.notice a{color:inherit}
.notice-red{background:var(--red-bg);color:var(--red);border-color:var(--red)}.notice-amber{background:var(--amber-bg);color:var(--amber);border-color:var(--amber)}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:10px 8px;border-bottom:var(--bw) solid var(--line);vertical-align:top}
th{font-size:.78rem;text-transform:uppercase;letter-spacing:.07em;color:var(--muted)}tr:hover td{background:var(--bg)}
.issues{border:2px solid var(--amber);background:var(--amber-bg);color:var(--ink);border-radius:10px;padding:10px 14px;margin-top:14px}
.issues ul{list-style:none;padding-left:0}details.override{margin-top:12px}
.ocr{margin:14px 0 4px;padding:10px 14px;border-radius:12px;background:var(--bg);border:var(--bw) solid var(--line)}
.ocr p{margin:4px 0}.ocr-list{list-style:none;padding-left:0;margin:8px 0 2px}
.ocr-list li{display:flex;flex-wrap:wrap;gap:6px 12px;align-items:center;margin:8px 0}
img.crop{display:block;max-width:100%;max-height:72px;border:var(--bw) solid var(--line);border-radius:6px;background:#fff}
.issues img.crop{margin:4px 0 6px 28px}
.today{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}
.today div{padding:10px 12px;border-radius:10px;background:var(--card);border:var(--bw) solid var(--line);color:var(--muted);font-size:.86rem;line-height:1.25}
.today b{display:block;font-size:1.7rem;color:var(--ink);line-height:1.1;margin-bottom:2px}
.today .t-green b{color:var(--green)}.today .t-amber b{color:var(--amber)}.today .t-red b{color:var(--red)}
details summary{cursor:pointer;font-weight:600}
.demo{margin:0;padding:10px 20px;background:var(--red-bg);color:var(--red);font-weight:700;border-bottom:2px solid var(--red)}
kbd[data-keys]{margin-left:8px;font-size:.85em;opacity:.95}.hint{color:var(--muted);font-size:.92rem;margin:6px 0 0}
.activity{list-style:none;padding:0;margin:0 0 8px}.activity li{margin:6px 0}
.timeline{margin:12px 0 0;padding-left:22px;color:var(--ink)}.timeline li{margin:4px 0}button.is-sending{cursor:progress}
.login{max-width:440px;margin:48px auto;padding:32px}.login button{width:100%;min-height:50px}
.big-mark{width:52px;height:52px;font-size:1.3rem;border-radius:14px;margin-bottom:14px}
label.tick{font-weight:400;display:flex;gap:8px;align-items:flex-start}
.samples{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
.sample{display:flex;flex-direction:column;gap:6px;align-items:flex-start;padding:14px;border-radius:12px;background:var(--bg);border:var(--bw) solid var(--line)}
.sample button{margin-top:auto}.demo-try{border:2px solid var(--accent)}
.say{margin:10px 0 0;padding:8px 12px;border-radius:8px;background:var(--bg);color:var(--ink)}
@media (max-width:600px){main{padding:12px}header{padding:8px 12px}.who>span:not(.avatar){display:none}
.who{order:1;margin-left:auto}header nav{order:2;flex-basis:100%}#rx-display{order:3}
.verdict{grid-template-columns:1fr;gap:10px;padding:16px;border-left-width:8px}.verdict .icon{width:46px;height:46px;font-size:1.5rem}
.verdict .word{font-size:1.8rem}.split{grid-template-columns:1fr}.today{grid-template-columns:repeat(2,minmax(0,1fr))}
.actions button{flex:1 1 100%}}
"""


_REQ = threading.local()   # per-request display preferences, read by page()


def ui_attrs():
    """<html> attributes from this PC's display choice (cookie rxui, set by the header controls), so the
    first paint already has the chosen text size and contrast."""
    m = re.fullmatch(r"t([0-3])-(auto|std|hc)", getattr(_REQ, "ui", None) or "")
    if not m:
        return ""
    contrast = {"std": ' data-contrast="std"', "hc": ' data-contrast="high"'}.get(m[2], "")
    return f' data-text="{m[1]}"{contrast}'


def page(title, body, user=None, refresh=None):
    nav = ""
    if user:
        here = getattr(_REQ, "path", "") or ""
        tabs = [("/", "Counter"), ("/holds", "Holds")] + ([("/audit", "Audit log")] if user["role"] == "pharmacist" else [])

        def tab(href, label):
            on = here == href or (href == "/" and here.startswith("/case/")) or (href != "/" and here.startswith(href))
            return f'<a href="{href}"' + (' aria-current="page"' if on else "") + f'>{label}</a>'
        initials = "".join(w[0] for w in str(user["name"]).split()[:2]).upper() or "?"
        nav = (f'<nav aria-label="Main">{"".join(tab(h, l) for h, l in tabs)}</nav>'
               f'<div class="who"><span class="avatar" aria-hidden="true">{esc(initials)}</span>'
               f'<span><b>{esc(user["name"])}</b><span class="role">{esc(user["role"])}</span></span>'
               + ("" if user["name"] == DEMO_USER else   # the public demo visitor has nothing to sign out of
                  f'<form class="inline" method="post" action="/logout">{csrf_field(user)}'
                  f'<button class="secondary small" type="submit">Sign out</button></form>') + '</div>')
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    banner = f'<p class="demo" role="alert">{esc(DEMO_BANNER)}</p>' if DEMO else ""
    enhance = ""
    if (STATIC / "app.js").is_file():
        enhance = ('<link rel="stylesheet" href="/static/app.css">'
                   '<script type="module" src="/static/app.js"></script>')
    return (f'<!doctype html><html lang="en"{ui_attrs()}><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">{meta}'
            f'<title>{esc(title)} - rxcheck</title><link rel="stylesheet" href="/app.css">{enhance}</head><body>'
            f'<a class="skip" href="#main">Skip to main content</a>'
            f'<header><a class="brand" href="/"><span class="mark" aria-hidden="true">Rx</span>'
            f'<span>rxcheck<small>counter</small></span></a>{nav}<div id="rx-display"></div></header>{banner}'
            f'<main id="main" tabindex="-1">{body}</main></body></html>')


def kbd(keys):
    """A shortcut hint, hidden until the script that makes the key work has loaded."""
    return f'<kbd data-keys="{esc(keys)}" hidden>{esc(keys.replace("Mod", "Ctrl"))}</kbd>'


def when(at):
    """'today 14:32' or '2025-01-15 14:32' from an audit timestamp."""
    try:
        t = datetime.fromisoformat(at)
    except (TypeError, ValueError):
        return at or ""
    return f"today {t:%H:%M}" if t.date() == date.today() else f"{t:%Y-%m-%d %H:%M}"


def detail_of(row):
    try:
        d = json.loads(row["detail"] or "{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def csrf_field(user):
    return f'<input type="hidden" name="csrf" value="{esc(user["csrf"])}">' if user else ""


# What staff say while a slip is checked: neutral, never an accusation in front of a queue - a failed check is
# not proof of fraud, and a RED said out loud starts arguments.
SAY_TO_CUSTOMER = {
    "call": "“I need to confirm this prescription with the clinic - it takes a few minutes. You can wait, or "
            "take the other items now.”",
    "counter": "“One moment, I am checking the prescription.”",
    "retake": "“Could you show me the paper slip, or send a clearer photo?”",
}


def plain(reason):
    """Counter wording for a headline: the stroke-match percentage belongs in 'All checks', not in front of the
    pharmacist, who needs to know where to look instead."""
    return re.sub(r" - strokes match \d+% at 1px", " - the changed areas are marked in the evidence", reason or "")


def verdict_block(verdict, reason, extra=""):
    word, sub = VERDICT_UI[verdict]
    reason = plain(reason)
    icon = {"RED": "&#x2715;", "AMBER": "!", "GREEN": "&#x2713;"}[verdict]
    return (f'<section class="verdict v-{verdict}" role="status"><span class="icon" aria-hidden="true">{icon}</span>'
            f'<div><div class="word">{word}</div><div class="sub">{esc(sub)}</div>'
            f'<p class="why">{esc(reason)}</p></div></section>{extra}')


def backup_line():
    st = STATE.last_backup
    if not STATE.backup_dir:
        return '<p class="warn">Scheduled backups are OFF - start the app with --backup-dir (another disk or USB).</p>'
    if st is None:
        return '<p class="muted">First backup runs shortly after start.</p>'
    when, folder, problems = st
    age = (datetime.now() - when).total_seconds() / 3600
    if problems or age > 2 * STATE.backup_hours:
        return f'<p class="warn">Backup problem: {esc("; ".join(problems) or "last backup is overdue")}</p>'
    return f'<p class="muted">Last backup {when:%H:%M}, verified.</p>'


# ---------------------------------------------------------------- pages

def login_page(error=""):
    err = f'<p class="warn">{esc(error)}</p>' if error else ""
    if DEMO and demo_ready():
        err = ('<p><a class="button-link" href="/">Open the public demo - no account needed</a></p>'
               '<p class="muted">Pharmacist accounts sign in below.</p>') + err
    return page("Sign in", f"""<section class="card login"><span class="mark big-mark" aria-hidden="true">Rx</span>
<h1>Sign in to the counter</h1><p class="muted">Prescription checks for this pharmacy.</p>{err}<form method="post" action="/login">
<label for="name">Name</label><input type="text" id="name" name="name" autocomplete="username" required autofocus>
<label for="pin">PIN</label><input type="password" id="pin" name="pin" inputmode="numeric" autocomplete="current-password" required>
<p><button type="submit">Sign in</button></p></form>
<p class="muted">Each person signs in with their own PIN so the record shows who checked and who approved.</p></section>""")


def today_panel(user=None):
    """Today's warning load, and a standing alert when warnings are being waved through. Alerts that name a
    person are shown to pharmacists only."""
    t, fortnight = r.workload(1), r.workload(14)
    p = t["paths"]
    alert = ""
    if fortnight["override_alert"]:
        alert = (f'<p class="warn">{fortnight["overrides"]} of {fortnight["closed"]} held prescriptions in the last 14 days '
                 f'were dispensed without the step that was asked for. Review them together: either the warnings '
                 f'do not fit this counter, or they are being waved through.</p>')
    for name in (fortnight["staff_alerts"] if user and user["role"] == "pharmacist" else []):
        v = fortnight["overrides_by"][name]
        alert += (f'<p class="warn">{esc(name)} dispensed {v["overrides"]} of the {v["closed"]} held prescriptions they '
                  f'closed in the last 14 days without the step that was asked for. Review those cases together '
                  f'(Audit log, overrides only).</p>')
    return (f'<section class="card" aria-labelledby="today-h"><h2 id="today-h">Today</h2><div class="today">'
            f'<div><b>{t["slips"]}</b>checked</div><div class="t-green"><b>{t["green"]}</b>no issue</div>'
            f'<div><b>{p["retake"] + p["counter"]}</b>settled at the counter</div>'
            f'<div class="t-amber"><b>{p["call"]}</b>needed a call</div>'
            f'<div class="t-amber"><b>{t["waiting_for_callback"]}</b>waiting for a call-back</div>'
            f'<div class="t-red"><b>{t["overrides"]}</b>overrides</div></div>{alert}</section>')


def activity_panel(user):
    """Who did the last check and the last override - visible to everyone at the counter."""
    a = r.last_activity()
    c, o = a["check"], a["override"]
    if c:
        v = detail_of(c).get("verdicts") or {"x": "GREEN"}
        word = VERDICT_UI[max(v.values(), key=r.ORDER.index)][0]
        check = f'<b>{esc(c["actor"])}</b>, {esc(when(c["at"]))} - {esc(word)}'
    else:
        check = "none yet"
    if o:
        note = detail_of(o).get("note") or ""
        override = (f'<b>{esc(o["actor"])}</b>, {esc(when(o["at"]))} - hold {esc(o["ref"])}'
                    + (f': "{esc(note)}"' if note else ""))
    else:
        override = "none recorded"
    link = '<p><a href="/audit">Open the audit log</a></p>' if user["role"] == "pharmacist" else ""
    return (f'<section class="card" aria-labelledby="activity-h"><h2 id="activity-h">Last activity</h2>'
            f'<ul class="activity"><li>Last check: {check}</li><li>Last override: {override}</li></ul>{link}</section>')


def home_page(user):
    rows = []
    if STATE.inbox and STATE.inbox.exists():
        cutoff = time.time() - 24 * 3600
        imgs = sorted((p for p in STATE.inbox.iterdir() if p.suffix.lower() in r.IMG_EXT and p.stat().st_mtime > cutoff),
                      key=lambda p: -p.stat().st_mtime)[:12]
        for p in imgs:
            rep = r.load_json(r.REPORTS / f"{p.stem}_report.json", None)
            head = r.headline(rep) if rep else "not pre-checked yet"
            if rep and rep.get("path") == "retake":
                head = "Unreadable - ask the customer now for a clear photo of the paper slip"
            rows.append(f'<tr><td>{datetime.fromtimestamp(p.stat().st_mtime):%H:%M}</td><td>{esc(p.name)}</td>'
                        f'<td>{esc(head)}</td><td><form method="post" action="/inbox/take">{csrf_field(user)}'
                        f'<input type="hidden" name="name" value="{esc(p.name)}"><button type="submit">Open</button>'
                        f'</form></td></tr>')
    inbox = ("<table><tr><th>Arrived</th><th>Image</th><th>Pre-check</th><th></th></tr>" + "".join(rows) + "</table>"
             if rows else '<p class="muted">No prescriptions received in the last 24 hours.</p>')
    n_holds = r.db().execute("SELECT COUNT(*) FROM holds WHERE status != 'closed'").fetchone()[0]
    holds = (f'<p class="notice notice-amber"><a href="/holds"><b>{n_holds} open hold{"s" if n_holds != 1 else ""}</b></a> '
             f'waiting for a call-back or decision.</p>' if n_holds else "")
    backup = backup_line()
    alerts = holds + (backup.replace('<p class="warn">', '<p class="notice notice-red">') if 'class="warn"' in backup else "")
    status = "" if 'class="warn"' in backup else f'<section class="card">{backup}</section>'
    return page("Counter", f"""{alerts}{demo_panel(user)}<div class="home"><section class="card hero" id="check"><h1>Check a prescription</h1>
<p class="muted">Photo of the paper slip, or the image the customer forwarded. Takes about two seconds.</p>
<form method="post" action="/upload" enctype="multipart/form-data" data-primary>{csrf_field(user)}
<label for="image" class="drop"><span class="drop-title">Prescription photo {kbd("Mod+U")}</span>
<span class="muted">Choose a file or drop it here<span class="js-only">, or paste it with Ctrl+V - the check starts by
 itself</span></span></label><input type="file" id="image" name="image" accept="image/*"
 required data-shortcut="upload" aria-keyshortcuts="Control+U Meta+U">
<p><button type="submit" class="big" aria-keyshortcuts="Control+Enter Meta+Enter">Check prescription</button>{kbd("Mod+Enter")}</p></form></section>
<div class="side">{today_panel(user)}{activity_panel(user)}</div></div>
<section class="card"><h2>Received on WhatsApp</h2>{inbox}</section>{status}""", user, refresh=60)


def demo_panel(user):
    """Public demo: one-click synthetic sample slips, so a visitor sees every verdict without a prescription."""
    meta = demo_samples() if DEMO and demo_ready() else None
    if not meta:
        return ""
    ref = meta["reference"]
    tiles = "".join(
        f'<form method="post" action="/demo/try" class="sample">{csrf_field(user)}'
        f'<input type="hidden" name="key" value="{esc(s["key"])}">'
        f'<span class="pill v-{esc(s.get("expect", "GREEN"))}">{esc(s.get("expect", ""))}</span>'
        f'<b>{esc(s["label"])}</b><span class="muted">{esc(s.get("what", ""))}</span>'
        f'<button type="submit" class="secondary">Check this sample</button></form>'
        for s in meta["samples"] if (DEMO_DIR / s["image"]).is_file())
    return (f'<section class="card demo-try" aria-labelledby="demo-h"><h2 id="demo-h">Try it with a sample slip</h2>'
            f'<p>This public demo has one slip on record: <b>{esc(ref["id"])}</b>, dispensed {esc(ref["dispensed"])}. '
            f'Each sample below is a synthetic test image - pick one and the medicines are filled in for you.</p>'
            f'<div class="samples">{tiles}</div></section>')


def entry_form(user, cid, case, fields=None, issues=None, error=""):
    pre = case["pre"]
    iss = pre.get("issuance") or {}
    f = dict(fields or case.get("prefill") or {})
    signed = iss.get("status") == "verified" and iss.get("fields")
    if signed and not fields:
        f = {"drugs": signed["drugs"], "regn": signed["regn"], "rx_date": signed["date"],
             "patient": signed.get("patient", "")}
    read = case.get("ocr") or {}
    if not fields and not signed:   # the photo reading fills only what is still empty; a signed code wins
        for k, v in ocr.values(read).items():
            f[k] = f.get(k) or v
    note = ""
    if signed:
        note = ('<p class="muted">Filled in from the clinic-signed code on the slip. Remove any medicine the customer '
                'is not buying now.</p>')
    elif iss.get("status") == "invalid":
        note = f'<p class="warn">{esc(iss["message"])}</p>'
    issue_html = ""
    if issues:
        items = "".join(f'<li><label class="tick">'
                        f'<input type="checkbox" name="confirm" value="{i}" required> <span><b>{esc(x["item"])}</b>: '
                        f'{esc(x["issue"])}</span></label>{crop_img(cid, x.get("crop"), x["item"])}</li>'
                        for i, x in enumerate(issues))
        issue_html = (f'<div class="issues"><b>Check these against the slip before continuing.</b> Correct the entry '
                      f'above, or tick to confirm it is exactly what the slip says.<ul>{items}</ul></div>')
    v = pre["verdicts"]
    if v["routine"] == v["high"]:
        pre_line = f"Image check: {VERDICT_UI[v['routine']][0]}."
    else:
        pre_line = (f"Image check: {VERDICT_UI[v['routine']][0]} for routine medicines, "
                    f"{VERDICT_UI[v['high']][0]} for high-risk ones.")
    if max(v.values(), key=r.ORDER.index) != "GREEN":
        pre_line += " " + plain(pre.get("reason", ""))
    err = f'<p class="warn">{esc(error)}</p>' if error else ""
    return page("Enter medicines", f"""<section class="card"><h1>What is the customer buying?</h1>
<p class="muted">{esc(pre_line)}</p>{note}{err}{"" if signed else ocr_panel(cid, read)}
<form method="post" action="/case/{cid}/check" data-primary>{csrf_field(user)}
<label for="drugs">Medicines and strengths being sold (as on the slip)</label>
<div data-lookup="drug" data-multi><input type="text" id="drugs" name="drugs" value="{esc(f.get("drugs"))}"
 placeholder="e.g. Alprax 0.25, Dolo 650" required aria-describedby="drugs-hint"></div>
<p class="hint" id="drugs-hint">Suggestions give the medicine name only - type the strength from the slip.</p>
<div class="row"><div><label for="regn">Doctor's Regn No (from the slip)</label>
<div data-lookup="regn"><input type="text" id="regn" name="regn" value="{esc(f.get("regn"))}"
 aria-describedby="prescriber-hint"></div></div>
<div><label for="doctor">Doctor's name</label><div data-lookup="doctor"><input type="text" id="doctor" name="doctor"
 value="{esc(f.get("doctor"))}" aria-describedby="prescriber-hint"></div></div>
<div><label for="rx_date">Date on the slip</label><input type="date" id="rx_date" name="rx_date" value="{esc(f.get("rx_date"))}"></div></div>
<p class="hint" id="prescriber-hint">Type the Regn No and the name exactly as printed on the slip. Choosing a
 suggestion fills only that field - never the other one, so the check can still compare them.</p>
<label for="patient">Patient name (optional)</label>
<div data-lookup="patient"><input type="text" id="patient" name="patient" value="{esc(f.get("patient"))}"
 autocomplete="off"></div>
{issue_html}<p><button type="submit" aria-keyshortcuts="Control+Enter Meta+Enter">Get decision</button>{kbd("Mod+Enter")}</p>
</form></section>
<section class="card"><h2>The slip</h2><img class="evidence" src="/img/{cid}" alt="Prescription image as received"></section>""", user)


def crop_img(cid, n, what):
    if not isinstance(n, int):
        return ""
    return f'<img class="crop" src="/case/{cid}/crop/{n}" alt="The part of the slip read as {esc(what)}">'


def ocr_panel(cid, read):
    """What the photo reading found: filled into empty fields, or shown for staff to type. '' when OCR is off."""
    st = (read or {}).get("status")
    if st == "pending":
        return (f'<div class="ocr" id="ocr" role="status" data-ocr-pending="/case/{cid}/ocr"><p><b>Reading the photo</b> '
                f'to fill in what it can (5-15 s, on this PC). You can start typing: nothing you type is replaced.</p>'
                f'<p class="hint">Without JavaScript, <a href="/case/{cid}">reload this page</a> before typing.</p></div>')
    if st == "failed":
        return ('<div class="ocr" id="ocr" role="status"><p class="hint">The photo could not be read automatically. '
                'Type the medicines from the slip.</p></div>')
    if st != "done":
        return ""
    found = [d["text"] for d in read.get("drugs", [])]
    found += [f'{label} {read[k]["value"]}' for k, label in (("regn", "Regn No"), ("doctor", "doctor"),
                                                             ("rx_date", "date")) if read.get(k)]
    head = (f'<p><b>Read from the photo:</b> {esc(", ".join(found))}. Filled into empty fields only - check each one '
            f'against the slip; anything that needs a tick is asked when you continue.</p>' if found else
            '<p><b>Nothing could be filled in from the photo.</b> Type the medicines from the slip.</p>')
    unclear = "".join(f'<li>{crop_img(cid, u.get("crop"), u["text"])}<span>Read as <b>{esc(u["text"])}</b>: '
                      f'{esc(u["why"])}. Not filled in - type it if it is being sold.</span></li>'
                      for u in read.get("unclear", []))
    unclear = f'<ul class="ocr-list">{unclear}</ul>' if unclear else ""
    return f'<div class="ocr" id="ocr" role="status">{head}{unclear}</div>'


def decision_page(user, cid, case, error="", notice=""):
    rep = case["final"]
    worst = max(rep["verdicts"].values(), key=r.ORDER.index)
    iss = rep.get("issuance") or {}
    iss_line = ("Issuance verified: " + iss["message"] if iss.get("status") == "verified"
                else "Issuance code FAILED: " + iss.get("message", "") if iss.get("status") == "invalid"
                else "Issuance not verified - the slip has no clinic-signed code.")
    split = ""
    if rep.get("hand_over") and rep.get("held"):
        split = (f'<div class="split"><p class="give"><b>Hand over now:</b> {esc(", ".join(rep["hand_over"]))}</p>'
                 f'<p class="wait"><b>Hold:</b> {esc(", ".join(rep["held"]))}</p></div>')
    if rep.get("path"):
        phone = f" Registry phone: {rep['callback']}." if rep.get("callback") and rep["path"] == "call" else ""
        steps = "".join(f"<li>{esc(x)}</li>" for x in rep["steps"])
        say = SAY_TO_CUSTOMER.get(rep["path"], "")
        todo = (f'<div class="next path-{rep["path"]}"><b>{esc(r.PATH_LABELS[rep["path"]])}</b>{esc(phone)}'
                f'<ul>{steps}</ul>' + (f'<p class="say"><b>Tell the customer:</b> {esc(say)}</p>' if say else "")
                + '</div>')
    else:
        todo = f'<p class="next"><b>Next:</b> {esc(r.ACTIONS[worst])}</p>'
    err = f'<p class="warn">{esc(error)}</p>' if error else ""
    patient = (case.get("fields") or {}).get("patient")
    by = (f'<p class="checked-by">Checked by <b>{esc(rep.get("actor"))}</b>, {esc(when(rep.get("checked_at")))}'
          + (f' · patient {esc(patient)}' if patient else "") + '.</p>')
    parts = [verdict_block(worst, rep["reason"], split + todo + err), by, f'<p class="muted">{esc(iss_line)}</p>']

    # hold actions come before the evidence: they are what the pharmacist does next
    hid = case.get("hold")
    if hid:
        row = r.db().execute("SELECT status FROM holds WHERE id = ?", (hid,)).fetchone()
        status = row["status"] if row else "closed"
        if status != "closed":
            path = rep.get("path") or "call"
            parts.append(hold_form(user, cid, hid, path, status,
                                   doctor_section(user, cid, case, hid, notice) if path == "call" else "", worst))
        else:
            parts.append(f'<section class="card"><p>Hold {esc(hid)} closed: {esc(hold_outcome(hid) or "")}.</p>'
                         f'{hold_history(hid)}</section>')

    # evidence: the comparison with the recorded slip if there is one, else the slip itself
    m = rep.get("match") or {}
    if m.get("report_png"):
        png = Path(m["report_png"])
        close = png.with_name(png.stem + "_closeups.png")
        ev = ""
        if close.exists():
            ev += (f'<p>Changed areas (recorded slip above, this image below):</p>'
                   f'<img class="evidence" src="/evidence/{quote(close.name)}" alt="Close-ups of changed areas">')
        ev += (f'<p>Side by side with {esc(m["ledger_id"])}:</p>'
               f'<img class="evidence" src="/evidence/{quote(png.name)}" alt="Comparison with the recorded slip">')
    else:
        ev = f'<img class="evidence" src="/img/{cid}" alt="Prescription image as received">'
    evidence = f'<section class="card evidence-card"><h2>Evidence</h2>{ev}</section>'

    # high-risk dispensing goes into the image ledger (alongside the H1 register) - offered only once it is
    # allowed: a GREEN check, or a hold a pharmacist closed as confirmed / dispensed after review
    can_record = worst == "GREEN" or hold_outcome(hid) in PHARMACIST_ONLY
    if rep.get("risk") == "high" and user["role"] == "pharmacist" and (case.get("recorded") or can_record):
        if case.get("recorded"):
            parts.append(f'<section class="card"><p>Recorded in the ledger as {esc(case["recorded"])}.</p></section>')
        else:
            parts.append(f'<section class="card"><h2>After dispensing</h2><form method="post" action="/case/{cid}/record">'
                         f'{csrf_field(user)}<p>Record this slip in the ledger so a re-used or edited copy is caught '
                         f'next time (same moment as the H1 register entry).</p>'
                         f'<button type="submit">Record as dispensed</button></form></section>')

    details = "".join(f"<li>{esc(x)}</li>" for x in rep["findings"]) + \
        "".join(f'<li class="muted">{esc(x)}</li>' for x in rep["notes"])
    parts.append(f'<section class="card"><details><summary>All checks ({len(rep["findings"])} findings, '
                 f'{len(rep["notes"])} notes)</summary><ul>{details}</ul><p class="muted">Checked {esc(rep["checked_at"])} '
                 f'by {esc(rep.get("actor"))} in {rep["seconds"]}s. Data versions: '
                 f'{esc(", ".join(f"{k} {v}" for k, v in (rep.get("data") or {}).items()))}</p></details></section>')
    parts.append(f'<p><a class="button-link" href="/" data-shortcut="next" aria-keyshortcuts="Control+U Meta+U">'
                 f'Next customer</a>{kbd("Mod+U")}</p>')
    return page(VERDICT_UI[worst][0], f'<div class="decision"><div class="d-main">{"".join(parts)}</div>'
                                      f'<aside class="d-side" aria-label="Evidence">{evidence}</aside></div>', user)


DOCTOR_PRIVACY = ("The doctor's phone receives the patient's initials and age, the held medicines, the prescription "
                  "date and this pharmacy's anonymous id - never the patient's name or the slip.")


def doctor_section(user, cid, case, hid, notice=""):
    """Ask the prescriber for a signed answer. Any signed-in person sees the state; only a pharmacist asks or
    enters the code the doctor read out (the same people who may record 'Clinic confirmed')."""
    ph = user["role"] == "pharmacist"
    said = dp.hold_decision(hid)   # a denial is shown even if the service is no longer connected
    if not dp.config() and not (said and said["decision"] == "denied"):
        return ('<p class="muted">Doctor confirmation is not connected on this counter (rxcheck doctor trust).</p>'
                if ph else "")
    asks = dp.asks(hid)
    pending = [a for a in asks if a["status"] == "pending"]
    f = case.get("fields") or {}
    hidden = f'{csrf_field(user)}<input type="hidden" name="case" value="{esc(cid)}">'
    if notice:
        out = f'<p role="status"><b>{esc(notice)}</b></p>'
    else:
        out = ""
    if said and said["decision"] == "denied":
        out += ('<p><span class="pill v-RED">Doctor DENIED</span> The prescriber says they did not issue this '
                f'prescription (answered {"in the app" if said["via"] == "app" else "by code"}; signed receipt '
                'verified). Do not dispense. Refuse politely - do not accuse.</p>')
    elif said:
        out += ('<p><span class="pill v-GREEN">Doctor confirmed</span> '
                f'({"in the app" if said["via"] == "app" else "read-out code"}; signed receipt verified). '
                '"Clinic confirmed" below is recorded as authenticated.</p>')
    elif pending:
        out += (f'<p>Asked {esc(when(pending[-1]["asked_at"]))}. The doctor can approve or deny in the app, or read '
                f'you a code. The request stays open for 15 minutes.</p>'
                f'<form method="post" action="/hold/{esc(hid)}/doctor/status">{hidden}'
                f'<button type="submit" class="secondary">Check for the doctor\'s answer</button></form>')
        if ph:
            out += (f'<form method="post" action="/hold/{esc(hid)}/doctor/otp">{hidden}'
                    f'<label for="dcode">Code the doctor read out</label>'
                    f'<input type="text" id="dcode" name="code" inputmode="numeric" autocomplete="off" '
                    f'pattern="[0-9]{{6}}" maxlength="6" required>'
                    f'<p><button type="submit">Enter code</button></p></form>')
    elif ph:
        held = ", ".join(case["final"].get("held") or []) or f.get("drugs") or ""
        if asks:
            out += f'<p class="muted">The last request ended without an answer ({esc(asks[-1]["status"])}).</p>'
        out += (f'<form method="post" action="/hold/{esc(hid)}/doctor/ask">{hidden}'
                f'<p>Ask the prescriber to confirm: <b>{esc(held)}</b></p>'
                f'<label for="dregn">Registration number</label>'
                f'<input type="text" id="dregn" name="regn" value="{esc(f.get("regn"))}" required maxlength="40">'
                f'<label for="dpatient">Patient: initials and age, for example RK, 34</label>'
                f'<input type="text" id="dpatient" name="patient" value="{esc(dp.mask_patient(f.get("patient")))}" '
                f'required maxlength="12" autocomplete="off">'
                f'<label for="ddate">Date on the slip</label>'
                f'<input type="date" id="ddate" name="rx_date" value="{esc(f.get("rx_date") or date.today().isoformat())}">'
                f'<p class="muted">{esc(DOCTOR_PRIVACY)}</p>'
                f'<p><button type="submit">Ask the doctor</button></p></form>')
    else:
        out += '<p class="muted">A pharmacist can ask the prescriber for a signed confirmation.</p>'
    return f'<div class="doctor"><h3>Ask the doctor</h3>{out}</div>'


DOCTOR_NOTICES = {
    "approved": "The doctor confirmed (signed receipt verified). 'Clinic confirmed' below now records an authenticated confirmation.",
    "denied": "The doctor DENIED issuing this prescription. Refuse it; do not dispense.",
    "pending": "No answer yet. The request stays open for 15 minutes.",
    "expired": "The request expired without an answer. Ask again, or call the clinic on the registry number.",
    "locked": "Locked after 3 wrong codes. Ask again, or call the clinic on the registry number.",
    "not_enrolled": "This doctor has not enrolled for confirmations. Call the clinic on the registry number instead.",
    "rate_limited": "Too many requests for this doctor or pharmacy just now. Call the clinic on the registry number instead.",
    "unreachable": "The doctor service could not be reached. Nothing was sent or approved: call the clinic on the registry number.",
    "not_configured": "Doctor confirmation is not connected on this counter.",
}


def doctor_notice(res):
    if res["status"] == "wrong_otp":
        return f"Wrong code - {res.get('tries_left', 0)} tries left."
    if res["status"] == "pending" and res.get("contact_hint"):
        return (f"Sent to the doctor's registered phone ({res['contact_hint']}). The doctor can approve or deny in the "
                f"app, or read you a code to enter below.")
    if res["status"] == "refused":
        return f"The request was refused: {res.get('detail', 'not accepted')}"
    return DOCTOR_NOTICES.get(res["status"], "Something went wrong - call the clinic on the registry number.")


def hold_history(hid):
    """Who opened the hold and who recorded each outcome, oldest first - from the audit trail."""
    rows = r.db().execute("SELECT at, actor, action, detail FROM audit WHERE ref = ? AND action IN ('hold', 'resolve', 'doctor-ask', "
                          "'doctor-receipt') "
                          "ORDER BY seq", (hid,)).fetchall()
    items = []
    for x in rows:
        d = detail_of(x)
        if x["action"] == "hold":
            what = "Hold opened"
        elif x["action"] == "doctor-ask":
            what = f"Doctor asked (message to {d.get('contact') or 'the registered phone'})"
        elif x["action"] == "doctor-receipt":
            what = (f"Doctor {str(d.get('decision', '')).upper()} via "
                    f"{'the app' if d.get('via') == 'app' else 'a read-out code'} (signed receipt)")
        else:
            what = OUTCOME_LABELS.get(d.get("outcome"), d.get("outcome") or "Outcome")
            if d.get("override"):
                what += " - OVERRIDE"
            if d.get("outcome") == "confirmed":
                what += (f" - AUTHENTICATED by the doctor ({'app' if d.get('via') == 'app' else 'read-out code'})"
                         if d.get("authenticated") else " - on the pharmacist's word")
            if d.get("reason") and d["reason"] in r.REVIEW_REASONS and d["reason"] != "other":
                what += f" ({r.REVIEW_REASONS[d['reason']]})"
            if d.get("note"):
                what += f': "{d["note"]}"'
        items.append(f'<li>{esc(what)} - <b>{esc(x["actor"])}</b>, {esc(when(x["at"]))}</li>')
    return f'<ol class="timeline" aria-label="History of hold {esc(hid)}">{"".join(items)}</ol>' if items else ""


def hold_form(user, cid, hid, path, status, doctor_html="", verdict=""):
    """Outcome buttons for this hold's path. The quick, legitimate resolutions come first; dispensing
    without the step that was asked for is folded away, labelled as an override, and needs a note.
    On a RED hold or after the doctor's denial, Refuse is the main button and anything that dispenses is secondary."""
    ph = user["role"] == "pharmacist"
    btn = lambda outcome, label, cls="": (f'<button type="submit" name="outcome" value="{outcome}"'
                                          + (f' class="{cls}"' if cls else "") + f'>{esc(label)}</button>')
    said = (dp.hold_decision(hid) or {}).get("decision")
    denied = said == "denied"   # the prescriber said no: Refuse is the only way out, so offer nothing else
    refuse_first = denied or verdict == "RED"
    fields, buttons, extra = "", [], ""
    if path == "retake":
        extra = (f'<form method="post" action="/case/{cid}/retake" enctype="multipart/form-data" data-primary>'
                 f'{csrf_field(user)}<label for="retake">New photo of the paper slip {kbd("Mod+U")}</label>'
                 f'<input type="file" id="retake" name="image" accept="image/*" required data-shortcut="upload" '
                 f'aria-keyshortcuts="Control+U Meta+U">'
                 f'<p><button type="submit">Check the new photo</button></p></form>')
    elif path == "counter" and ph:
        _, settled_by = r.hold_info(hid)   # only the checks that settle THIS warning; no default
        opts = '<option value="">Choose what you checked</option>' + "".join(
            f'<option value="{k}">{esc(r.REVIEW_REASONS[k])}</option>' for k in settled_by)
        fields = (f'<label for="reason">What did you check?</label><select id="reason" name="reason" required>{opts}</select>')
        buttons.append(btn("dispensed-after-review", "Checked - dispense", "secondary" if refuse_first else ""))
    elif path == "call" and not denied:
        if ph:
            buttons.append(btn("confirmed", "Clinic confirmed (doctor's signed receipt)" if said == "approved"
                               else "Clinic confirmed", "secondary" if refuse_first else ""))
        if status == "open":
            buttons.append(btn("unreachable", "No answer - call back later", "secondary"))
    refuse = btn("refused", "Refuse", "danger" if refuse_first else "secondary")
    actions = [refuse] + buttons if refuse_first else buttons + [refuse]
    form_main = (f'<form method="post" action="/hold/{esc(hid)}/resolve">{csrf_field(user)}'
                 f'<input type="hidden" name="case" value="{cid}">'
                 f'<label for="note">Note (who you spoke to, number called, what you saw - no patient names or health details)</label>'
                 f'<input type="text" id="note" name="note">{fields}<div class="actions">{"".join(actions)}</div></form>')
    override = ""
    no_override = r.hold_fail_closed(hid)
    if denied:
        override = ""
    elif ph and no_override:
        override = (f'<p class="muted">No override: {esc(", ".join(no_override))} is dispensed only when the clinic '
                    f'confirms or a counter check settles the warning (NDPS / Schedule X, or the drug list is '
                    f'unusable). If the clinic cannot be reached, hand over the other items; the customer can return.</p>')
    elif ph:
        override = (f'<details class="override"><summary>Dispense without this step (override)</summary>'
                    f'<form method="post" action="/hold/{esc(hid)}/resolve">{csrf_field(user)}'
                    f'<input type="hidden" name="case" value="{cid}"><input type="hidden" name="reason" value="other">'
                    f'<p class="muted">Recorded under your name, kept permanently in the audit trail and counted in '
                    f'the override rate. Say why, without patient names or health details.</p>'
                    f'<label for="onote">Reason</label><input type="text" id="onote" name="note" required minlength="10">'
                    f'<p>{btn("dispensed-after-review", "Dispense anyway", "secondary")}</p></form></details>')
    only = "" if ph else '<p class="muted">A pharmacist records "confirmed", "checked - dispense" and overrides.</p>'
    waiting = '<p class="muted">Waiting for a call-back.</p>' if status == "waiting" else ""
    return (f'<section class="card"><h2>Hold {esc(hid)}</h2>{waiting}{extra}{doctor_html}{form_main}{override}{only}'
            f'{hold_history(hid)}</section>')


def holds_page(user):
    rows = r.db().execute("SELECT * FROM holds WHERE status != 'closed' ORDER BY created").fetchall()
    out = []
    for h in rows:
        worst = max(json.loads(h["verdicts"]).values(), key=r.ORDER.index)
        findings = json.loads(h["findings"])
        cid = Path(h["image"]).stem
        link = f'<a href="/case/{cid}">open</a>' if CASE_ID.match(cid) else esc(Path(h["image"]).name)
        age = int((datetime.now() - datetime.fromisoformat(h["created"])).total_seconds() // 60)
        why = findings[0] if findings else ""
        if not why and CASE_ID.match(cid):   # a new-slip hold has no finding, only the decision's reason
            try:
                why = (load_case(cid)[1].get("final") or {}).get("reason") or ""
            except Exception:
                why = ""
        waited = f"{age} min" if age < 120 else f"{age // 60} h"
        out.append(f'<tr><td>{waited}</td><td><span class="pill v-{worst}">{worst}</span></td><td>{esc(h["status"])}</td>'
                   f'<td>{esc(plain(why))}</td><td>{link}</td></tr>')
    body = ("<table><tr><th>Waiting</th><th>Verdict</th><th>Status</th><th>Reason</th><th></th></tr>" + "".join(out)
            + "</table>") if out else '<p class="muted">No open holds.</p>'
    return page("Holds", f'<section class="card"><h1>Open holds (oldest first)</h1>{body}</section>{activity_panel(user)}'
                         f'<p><a href="/" data-shortcut="next">Back to the counter</a></p>', user, refresh=60)


# ---------------------------------------------------------------- audit log (pharmacists: compliance review)

ACTION_LABELS = {
    "check": "Check", "entry-confirmed": "Entry confirmed against the slip", "ocr-use": "Photo reading used",
    "hold": "Hold opened",
    "resolve": "Hold outcome", "doctor-ask": "Doctor asked", "doctor-receipt": "Doctor's answer", "login": "Signed in", "login-failed": "Wrong PIN", "login-locked": "Sign-in locked",
    "add": "Recorded in the ledger", "index": "Indexed (no image)", "index-update": "Index updated",
    "migrate": "Migrated", "staff-add": "Staff added", "staff-disable": "Staff disabled", "staff-pin": "PIN changed",
    "backup": "Backup", "restore": "Restore", "data-import": "Data import", "audit-export": "Audit log exported",
}
AUDIT_GROUPS = {   # value -> (label, actions); "overrides" is a filter on resolve rows
    "checks": ("Checks and entry confirmations", ("check", "entry-confirmed", "ocr-use")),
    "holds": ("Holds and their outcomes", ("hold", "resolve", "doctor-ask", "doctor-receipt")),
    "overrides": ("Overrides only", ("resolve",)),
    "signin": ("Sign-ins and wrong PINs", ("login", "login-failed", "login-locked")),
    "ledger": ("Ledger and index records", ("add", "index", "index-update", "migrate")),
    "admin": ("Staff, data, backups and exports", ("staff-add", "staff-disable", "staff-pin", "backup", "restore",
                                                    "data-import", "audit-export")),
}


def audit_summary(row):
    """One readable line for an audit row; the raw detail stays available next to it."""
    d, a = detail_of(row), row["action"]
    if a == "check":
        v = d.get("verdicts") or {"x": "GREEN"}
        return f'{VERDICT_UI[max(v.values(), key=r.ORDER.index)][0]}: {d.get("reason") or "no issue found"}'
    if a == "doctor-ask":
        return f'request {d.get("request")} to {d.get("contact") or "the registered phone"} (Regn No {d.get("regn")})'
    if a == "doctor-receipt":
        return f'{str(d.get("decision", "")).upper()} via {d.get("via")} (request {d.get("request")})'
    if a == "resolve":
        s = OUTCOME_LABELS.get(d.get("outcome"), d.get("outcome") or "")
        if d.get("outcome") == "confirmed":
            s += " - authenticated by the doctor" if d.get("authenticated") else " - on the pharmacist's word"
        if d.get("reason") in r.REVIEW_REASONS and d["reason"] != "other":
            s += f" ({r.REVIEW_REASONS[d['reason']]})"
        return s + (f' - "{d["note"]}"' if d.get("note") else "")
    if a == "hold":
        return f'{r.PATH_LABELS.get(d.get("path"), "Hold")}' + (f' - held: {", ".join(d["held"])}' if d.get("held") else "")
    if a == "entry-confirmed":
        return "; ".join(f'{i.get("item")}: {i.get("issue")}' for i in d.get("issues", []))
    if a == "ocr-use":
        return (f'{d.get("offered", 0)} value(s) filled in from the photo, {d.get("kept", 0)} kept, '
                f'{d.get("changed", 0)} corrected; {d.get("unclear", 0)} left for staff to type')
    if a.startswith("login"):
        return f'from {d.get("ip", "?")}'
    if a in ("add", "index", "index-update"):
        return ", ".join(f"{k} {v}" for k, v in d.items() if v and k in ("drugs", "regn_no", "verdict", "date"))
    return json.dumps(d, ensure_ascii=False)[:160] if d else ""


def audit_query(q, before=None, limit=None):
    """Rows matching the filters (actor, group, from, to, q), newest first; (rows, seq to continue before)."""
    limit = limit or AUDIT_PAGE
    where, args = [], []
    if q.get("actor"):
        where.append("actor = ?")
        args.append(q["actor"])
    group = AUDIT_GROUPS.get(q.get("group") or "")
    if group:
        where.append(f"action IN ({', '.join('?' * len(group[1]))})")
        args += group[1]
        if q.get("group") == "overrides":
            where.append("json_extract(detail, '$.override') = 1")
    for key, op in (("from", ">="), ("to", "<=")):
        d = r.iso_date(q.get(key)) if q.get(key) else None
        if d:
            where.append(f"substr(at, 1, 10) {op} ?")
            args.append(d.isoformat())
    if q.get("q"):
        like = "%" + q["q"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(ref LIKE ? ESCAPE '\\' OR detail LIKE ? ESCAPE '\\' OR actor LIKE ? ESCAPE '\\')")
        args += [like, like, like]
    if before:
        where.append("seq < ?")
        args.append(before)
    sql = "SELECT * FROM audit" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY seq DESC LIMIT ?"
    rows = [dict(x) for x in r.db().execute(sql, (*args, limit + 1))]
    return rows[:limit], (rows[limit - 1]["seq"] if len(rows) > limit else None)


def audit_json(row):
    d = detail_of(row)
    return {"seq": row["seq"], "at": row["at"], "actor": row["actor"], "action": row["action"],
            "label": ACTION_LABELS.get(row["action"], row["action"]), "ref": row["ref"],
            "summary": audit_summary(row), "override": bool(d.get("override")), "detail": d or None}


def csv_cell(v):
    """Spreadsheets run a cell that starts with = + - @ as a formula: free-text notes must never do that."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def audit_page(user):
    actors = [x[0] for x in r.db().execute("SELECT DISTINCT actor FROM audit ORDER BY actor COLLATE NOCASE")]
    groups = [{"value": k, "label": v[0]} for k, v in AUDIT_GROUPS.items()]
    rows, _ = audit_query({})
    fallback = "".join(f'<tr><td>{esc(x["at"].replace("T", " "))}</td><th scope="row">{esc(x["actor"])}</th>'
                       f'<td>{esc(ACTION_LABELS.get(x["action"], x["action"]))}</td>'
                       f'<td>{esc(audit_summary(x))}{" · " + esc(x["ref"]) if x["ref"] else ""}</td></tr>' for x in rows)
    table = (f'<table><caption class="muted">The latest {len(rows)} entries. Download the CSV for the full log.</caption>'
             f'<thead><tr><th scope="col">When</th><th scope="col">Who</th><th scope="col">Event</th>'
             f'<th scope="col">What happened</th></tr></thead><tbody>{fallback}</tbody></table>')
    return page("Audit log", f"""<section class="card"><h1>Audit log</h1>
<p class="muted">Every check, hold, outcome, sign-in, staff and data change, newest first. The log is append-only:
entries cannot be edited or deleted. Each export is itself recorded.</p>
<div id="rx-audit" data-staff="{esc(json.dumps(actors))}" data-groups="{esc(json.dumps(groups))}">
<p><a href="/audit.csv" download>Download the full log as CSV</a></p>{table}</div></section>
<p><a href="/" data-shortcut="next">Back to the counter</a></p>""", user)


# ---------------------------------------------------------------- lookups (suggestions for the entry form)

def _rank(label, q):
    s = label.lower()
    return 0 if s.startswith(q) else 1 if any(w.startswith(q) for w in re.split(r"[\s.,-]+", s)) else 2


def _matches(cands, q):
    """cands: {key: item}; prefix matches first, then word-start, then anywhere; most used first within each."""
    ql = q.lower()
    hits = [it for it in cands.values() if ql in it["label"].lower()]
    hits.sort(key=lambda it: (_rank(it["label"], ql), -it.get("_n", 0), it["label"].lower()))
    return [{k: v for k, v in it.items() if k != "_n"} for it in hits[:LOOKUP_MAX]]


def _doctor_key(name):
    return re.sub(r"[^a-z]", "", re.sub(r"^\s*dr\.?\s+", "", (name or "").lower()))


def lookup(kind, q):
    """Suggestions from this pharmacy's own data: drug list and previous entries, the prescriber registry and
    previously recorded prescribers, and patient names typed here before."""
    q = re.sub(r"\s+", " ", q or "").strip()[:60]
    if kind not in LOOKUP_KINDS or len(q) < (1 if kind == "regn" else 2):
        return []
    con = r.db()
    cands = {}
    if kind == "drug":
        table = r.drug_table() or r.EMPTY_TABLE
        mols, brands = table["molecules"], table["brands"]

        def drug_item(name, n=0):
            mol = brands.get(name.lower(), name.lower())
            high = mols.get(mol) in r.HIGH_TIERS
            detail = (f"{mol} · " if mol != name.lower() else "") + (
                      "high-risk, NDPS / Schedule X: no override" if mols.get(mol) == "ndps" else
                      "high-risk: needs the H1 / X checks" if high else
                      "on this counter's list" if mol in mols else "entered here before")
            return {"value": name, "label": name, "detail": detail, "_n": n,
                    **({"tag": "high", "tagText": "High-risk"} if high else {})}

        for b in brands:
            cands[b] = drug_item(b.title())
        for m in mols:
            cands.setdefault(m, drug_item(m.title()))
        counts = {}
        for (drugs,) in con.execute("SELECT drugs FROM entries WHERE drugs != '' ORDER BY created_at DESC LIMIT 3000"):
            for item in re.split(r"[,;\n]", drugs or ""):
                m = SEEN_DRUG.match(item)
                if m:
                    name = m.group(1).strip()
                    counts.setdefault(name.lower(), [name, 0])[1] += 1
        for key, (name, n) in counts.items():
            if key in cands:
                cands[key]["_n"] = n
            else:
                cands[key] = drug_item(name, n)
    elif kind in ("regn", "doctor"):
        entries, _, _ = r.registry()
        for regn, rec in entries.items():
            ok = bool(r.trusted(rec, "source"))
            tag = {"tag": "verified", "tagText": "Registry: verified"} if ok else \
                  {"tag": "unverified", "tagText": "Registry: not verified"}
            if kind == "regn":
                cands[regn] = {"value": regn, "label": regn, "detail": rec.get("name") or "", **tag}
            else:
                cands[_doctor_key(rec.get("name"))] = {"value": rec.get("name") or "", "label": rec.get("name") or "",
                                                       "detail": f"Registry lists Regn No {regn}", **tag}
        for row in con.execute("SELECT regn_no, doctor, COUNT(*) AS n FROM entries WHERE regn_no IS NOT NULL "
                               "OR doctor IS NOT NULL GROUP BY regn_no, doctor"):
            if kind == "regn" and row["regn_no"]:
                it = cands.setdefault(row["regn_no"], {"value": row["regn_no"], "label": row["regn_no"],
                                                       "detail": row["doctor"] or "", "tag": "unverified",
                                                       "tagText": "Not in the registry"})
                it["_n"] = it.get("_n", 0) + row["n"]
            elif kind == "doctor" and row["doctor"]:
                key = _doctor_key(row["doctor"])
                if key and key not in cands:
                    cands[key] = {"value": row["doctor"], "label": row["doctor"], "tag": "unverified",
                                  "tagText": "Not in the registry",
                                  "detail": f"On slips here with Regn No {row['regn_no']}" if row["regn_no"] else ""}
                if key in cands:
                    cands[key]["_n"] = cands[key].get("_n", 0) + row["n"]
        if kind == "regn":   # numbers match from the start only: "12" should not offer 12345
            cands = {k: v for k, v in cands.items() if k.lower().startswith(q.lower())}
    else:
        for p in r.find_patients(q, LOOKUP_MAX):
            cands[p["name"].lower()] = {"value": p["name"], "label": p["name"],
                                        "detail": f'Last seen {p["last_seen"]} · {p["visits"]} visit'
                                                  f'{"s" if p["visits"] != 1 else ""}'}
    return _matches(cands, q)


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "rxcheck"
    sys_version = ""

    def log_message(self, fmt, *args):   # the audit trail is the record; keep the console quiet
        pass

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    # -- plumbing
    def send(self, code, body, ctype="text/html; charset=utf-8", headers=(), cache="no-store"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        for k, v in list(headers) + self.demo_cookie():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def demo_cookie(self):
        """The Set-Cookie of a demo session opened on this request (public demo only), else nothing."""
        tok = getattr(self, "_demo_token", None)
        if not tok:
            return []
        secure = "; Secure" if STATE.tls else ""
        return [("Set-Cookie", f"rxs={tok}; HttpOnly; SameSite=Strict; Path=/{secure}")]

    def demo_session(self):
        """Public demo: sign the visitor in as DEMO_USER without a PIN. Never used unless RXCHECK_DEMO is on."""
        tok, t = secrets.token_urlsafe(32), datetime.now()
        with STATE.lock:
            demo = sorted((s["last"], k) for k, s in STATE.sessions.items() if s["name"] == DEMO_USER)
            for _, k in demo[:max(0, len(demo) - MAX_DEMO_SESSIONS + 1)]:
                STATE.sessions.pop(k, None)
            STATE.sessions[tok] = {"name": DEMO_USER, "role": "pharmacist", "csrf": secrets.token_urlsafe(24),
                                   "start": t, "last": t}
            s = dict(STATE.sessions[tok], token=tok)
        self._demo_token = tok
        return s

    def send_json(self, code, obj):
        self.send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def send_static(self, name):
        """The Vue bundle: public code, no data. Revalidated by ETag so a busy counter does not re-download it."""
        p = STATIC / name
        if name not in STATIC_TYPES or not p.is_file():
            raise HttpError(404, "Not found")
        data = p.read_bytes()
        tag = '"' + hashlib.sha256(data).hexdigest()[:20] + '"'
        if self.headers.get("If-None-Match") == tag:
            self.send_response(304)
            self.send_header("ETag", tag)
            self.end_headers()
            return
        self.send(200, data, STATIC_TYPES[name], [("ETag", tag)], cache="no-cache")

    def query(self, name):
        return (parse_qs(self.path.partition("?")[2]).get(name) or [""])[0].strip()

    def redirect(self, to, headers=()):
        self.send_response(303)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        for k, v in list(headers) + self.demo_cookie():
            self.send_header(k, v)
        self.end_headers()

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_UPLOAD + 100_000:
            raise HttpError(413, "Upload too large")
        return self.rfile.read(n)

    def form(self):
        ctype = self.headers.get("Content-Type", "")
        raw = self.body()
        if ctype.startswith("multipart/form-data"):
            msg = BytesParser(policy=email_policy.HTTP).parsebytes(
                b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + raw)
            fields, files = {}, {}
            for part in msg.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if part.get_filename() is not None:
                    files[name] = (part.get_filename(), part.get_payload(decode=True) or b"")
                else:
                    fields[name] = [(part.get_payload(decode=True) or b"").decode("utf-8", "replace")]
            return fields, files
        return parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True), {}

    def client_ip(self):
        """Behind a proxy, the address it appended last (a client can forge earlier entries, not that one)."""
        if STATE.behind_proxy:
            fwd = [x.strip() for x in (self.headers.get("X-Forwarded-For") or "").split(",") if x.strip()]
            if fwd:
                return fwd[-1]
        return self.client_address[0]

    def cookie(self, name):
        for c in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = c.strip().partition("=")
            if k == name:
                return v
        return None

    def session(self):
        tok = self.cookie("rxs")
        if not tok:
            return None
        with STATE.lock:
            s = STATE.sessions.get(tok)
            t = datetime.now()
            if not s or t - s["last"] > SESSION_IDLE or t - s["start"] > SESSION_MAX:
                STATE.sessions.pop(tok, None)
                return None
            row = r.db().execute("SELECT active FROM staff WHERE name = ?", (s["name"],)).fetchone()
            if not row or not row["active"]:          # disabled mid-shift: out immediately
                STATE.sessions.pop(tok, None)
                return None
            s["last"] = t
            return dict(s, token=tok)

    def dispatch(self, method):
        path = self.path.split("?")[0]
        api = path.startswith("/api/")
        try:
            self.user, self._demo_token = None, None
            _REQ.ui = self.cookie("rxui")
            _REQ.path = path
            if method == "GET" and path == "/app.css":
                return self.send(200, CSS, "text/css; charset=utf-8")
            if method == "GET" and path.startswith("/static/"):
                return self.send_static(path[len("/static/"):])
            if method == "GET" and path == "/healthz":   # for the host's health checks: no data, no sign-in
                r.db().execute("SELECT 1").fetchone()
                return self.send(200, "ok", "text/plain; charset=utf-8")
            if path == "/login":
                return self.login(method)
            self.user = self.session()
            if self.user is None and DEMO and method == "GET" and not api and demo_ready():
                self.user = self.demo_session()   # public demo: open the link, use the counter - no account
            if self.user is None:
                if method == "POST" or api:
                    raise HttpError(401, "Signed out - sign in again")
                return self.redirect("/login")
            r.set_actor(self.user["name"])
            if method == "POST":
                self.fields, self.files = self.form()
                if not secrets.compare_digest((self.fields.get("csrf") or [""])[0], self.user["csrf"]):
                    raise HttpError(403, "This form has expired - go back, reload the page and try again")
            self.route(method, path)
        except HttpError as ex:
            if api:
                return self.send_json(ex.code, {"error": ex.message})
            self.send(ex.code, page("Problem", f'<section class="card"><h1>{esc(ex.message)}</h1>'
                                               f'<p><a href="/">Back to the counter</a></p></section>', self.user))
        except Exception:
            traceback.print_exc()
            if api:
                return self.send_json(500, {"error": "Something went wrong"})
            self.send(500, page("Error", '<section class="card"><h1>Something went wrong</h1><p>The check was not '
                                         'completed. Treat this prescription as unchecked and confirm it manually.</p>'
                                         '<p><a href="/">Back to the counter</a></p></section>', self.user))
        finally:
            r.set_actor(None)
            r.close_thread_db()

    def field(self, name, default=""):
        return (self.fields.get(name) or [default])[0].strip()

    # -- routes
    def login(self, method):
        if method == "GET":
            return self.send(200, login_page())
        fields = parse_qs(self.body().decode("utf-8", "replace"))
        name, pin = (fields.get("name") or [""])[0].strip(), (fields.get("pin") or [""])[0]
        key = name.lower()   # per name, not per IP: on the internet an attacker can rotate addresses
        with STATE.lock:
            recent = [t for t in STATE.failures.get(key, []) if datetime.now() - t < FAILURE_WINDOW]
            STATE.failures[key] = recent
        if len(recent) >= MAX_FAILURES:
            r.set_actor(name or "?")
            con = r.db()
            with con:
                r.audit(con, "login-locked", name, {"ip": self.client_ip()})
            return self.send(429, login_page("Too many wrong PINs. Wait 10 minutes or ask the pharmacist."))
        user = r.authenticate(name, pin)
        r.set_actor(name or "?")
        con = r.db()
        if not user:
            with STATE.lock:
                STATE.failures.setdefault(key, []).append(datetime.now())
            with con:
                r.audit(con, "login-failed", name, {"ip": self.client_ip()})
            return self.send(401, login_page("Name or PIN not recognised."))
        tok = secrets.token_urlsafe(32)
        with STATE.lock:
            STATE.failures.pop(key, None)
            STATE.sessions[tok] = {"name": user["name"], "role": user["role"], "csrf": secrets.token_urlsafe(24),
                                   "start": datetime.now(), "last": datetime.now()}
        with con:
            r.audit(con, "login", user["name"], {"ip": self.client_ip()})
        secure = "; Secure" if STATE.tls else ""
        self.redirect("/", [("Set-Cookie", f"rxs={tok}; HttpOnly; SameSite=Strict; Path=/{secure}")])

    def route(self, method, path):
        u = self.user
        parts = [p for p in path.split("/") if p]
        if method == "GET" and not parts:
            return self.send(200, home_page(u))
        if method == "GET" and parts == ["holds"]:
            return self.send(200, holds_page(u))
        if method == "GET" and parts == ["api", "lookup"]:
            return self.send_json(200, {"items": lookup(self.query("kind"), self.query("q"))})
        if method == "GET" and parts in (["audit"], ["audit.csv"], ["api", "audit"]):
            if u["role"] != "pharmacist":
                raise HttpError(403, "Only a pharmacist can open the audit log")
            return self.audit_route(parts)
        if method == "POST" and parts == ["logout"]:
            with STATE.lock:
                STATE.sessions.pop(u["token"], None)
            return self.redirect("/login", [("Set-Cookie", "rxs=; Max-Age=0; Path=/")])
        if method == "POST" and parts == ["upload"]:
            name, data = self.files.get("image") or ("", b"")
            if not data:
                raise HttpError(400, "Choose a photo of the prescription first")
            cid = new_case(data, Path(name).suffix.lower() or ".jpg", "upload")
            return self.redirect(f"/case/{cid}")
        if method == "POST" and parts == ["demo", "try"] and DEMO and demo_ready():
            key = self.field("key")
            sample = next((s for s in (demo_samples() or {}).get("samples", []) if s.get("key") == key), None)
            src = DEMO_DIR / sample["image"] if sample else None
            if not src or Path(sample["image"]).name != sample["image"] or not src.is_file():
                raise HttpError(404, "That sample is not available")
            cid = new_case(src.read_bytes(), src.suffix.lower(), f"demo:{key}")
            with STATE.case_lock:
                _, case = load_case(cid)
                case["prefill"] = {k: str(v) for k, v in (sample.get("fields") or {}).items()}
                save_case(cid, case)
            return self.redirect(f"/case/{cid}")
        if method == "POST" and parts == ["inbox", "take"]:
            name = self.field("name")
            src = (STATE.inbox / name) if STATE.inbox else None
            if not src or Path(name).name != name or not src.is_file():
                raise HttpError(404, "That image is no longer in the inbox")
            cid = new_case(src.read_bytes(), src.suffix.lower(), f"inbox:{name}")
            return self.redirect(f"/case/{cid}")
        if len(parts) == 2 and parts[0] == "img" and method == "GET":
            img, _ = case_files(parts[1])
            ctype = {".png": "image/png", ".webp": "image/webp"}.get(img.suffix.lower(), "image/jpeg")
            return self.send(200, img.read_bytes(), ctype)
        if len(parts) == 2 and parts[0] == "evidence" and method == "GET":
            name = parts[1]
            p = r.REPORTS / name
            if not re.fullmatch(r"[\w.-]+\.png", name) or not p.is_file():
                raise HttpError(404, "Evidence image not found")
            return self.send(200, p.read_bytes(), "image/png")
        if len(parts) >= 2 and parts[0] == "case":
            return self.case_route(method, parts[1], parts[2:])
        if len(parts) == 3 and parts[0] == "hold" and parts[2] == "resolve" and method == "POST":
            return self.resolve(parts[1])
        if len(parts) == 4 and parts[0] == "hold" and parts[2] == "doctor" and method == "POST":
            return self.doctor(parts[1], parts[3])
        raise HttpError(404, "Page not found")

    def audit_route(self, parts):
        if parts == ["audit"]:
            return self.send(200, audit_page(self.user))
        filters = {k: self.query(k)[:80] for k in ("actor", "group", "from", "to", "q")}
        if parts == ["api", "audit"]:
            before = self.query("before")
            rows, nxt = audit_query(filters, int(before) if before.isdigit() else None)
            return self.send_json(200, {"rows": [audit_json(x) for x in rows], "next": nxt})
        rows, _ = audit_query(filters, limit=100_000)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["seq", "at", "actor", "action", "event", "ref", "summary", "override", "detail"])
        for x in rows:
            w.writerow([csv_cell(v) for v in (x["seq"], x["at"], x["actor"], x["action"],
                                             ACTION_LABELS.get(x["action"], x["action"]), x["ref"], audit_summary(x),
                                             "yes" if detail_of(x).get("override") else "", x["detail"])])
        con = r.db()
        with con:   # an export of the trail is itself part of the trail
            r.audit(con, "audit-export", None, {"filters": {k: v for k, v in filters.items() if v}, "rows": len(rows)})
        name = f"rxcheck-audit-{datetime.now():%Y%m%d-%H%M}.csv"
        return self.send(200, "﻿" + buf.getvalue(), "text/csv; charset=utf-8",
                         [("Content-Disposition", f'attachment; filename="{name}"')])

    def case_route(self, method, cid, rest):
        u = self.user
        img, case = load_case(cid)
        if method == "GET" and not rest:
            if case["final"]:
                return self.send(200, decision_page(u, cid, case))
            return self.send(200, entry_form(u, cid, case))
        if method == "GET" and rest == ["ocr"]:
            read = case.get("ocr") or {}
            return self.send_json(200, {"status": read.get("status") or "off", "values": ocr.values(read),
                                        "panel": ocr_panel(cid, read)})
        if method == "GET" and len(rest) == 2 and rest[0] == "crop" and rest[1].isdigit():
            crops = (case.get("ocr") or {}).get("crops") or []
            if int(rest[1]) >= len(crops):
                raise HttpError(404, "No such part of the slip")
            try:
                return self.send(200, ocr.crop_jpeg(img, crops[int(rest[1])]), "image/jpeg")
            except ValueError:
                raise HttpError(404, "No such part of the slip")
        if method == "POST" and rest == ["check"]:
            if case["final"]:
                return self.redirect(f"/case/{cid}")
            f = {k: self.field(k) for k in ("drugs", "regn", "doctor", "rx_date", "patient")}
            f["patient"] = re.sub(r"\s+", " ", f["patient"])[:80]
            if not f["drugs"]:
                return self.send(400, entry_form(u, cid, case, f, error="Enter the medicines being sold."))
            rx_date = r.iso_date(f["rx_date"]) if f["rx_date"] else None
            if f["rx_date"] and not rx_date:
                return self.send(400, entry_form(u, cid, case, f, error="Date must be a valid date."))
            # what was read from the photo and sent unchanged is ticked like any doubtful entry
            issues = r.review_drugs(f["drugs"]) + ocr.confirmations(case.get("ocr"), f)
            confirmed = set(self.fields.get("confirm", []))
            if issues and confirmed != {str(i) for i in range(len(issues))}:
                return self.send(200, entry_form(u, cid, case, f, issues))
            use = ocr.usage(case.get("ocr"), f)
            if issues or use:
                con = r.db()
                with con:
                    if issues:
                        r.audit(con, "entry-confirmed", cid, {"issues": [{k: v for k, v in i.items() if k != "crop"}
                                                                         for i in issues]})
                    if use:
                        r.audit(con, "ocr-use", cid, use)
            finalize(cid, img, f)
            return self.redirect(f"/case/{cid}")
        if method == "POST" and rest == ["retake"]:
            if not case.get("final") or case["final"].get("path") != "retake":
                raise HttpError(400, "This prescription does not need a new photo")
            name, data = self.files.get("image") or ("", b"")
            if not data:
                raise HttpError(400, "Choose the new photo first")
            new = new_case(data, Path(name).suffix.lower() or ".jpg", f"retake of {cid}", read=False)
            new_img, _ = case_files(new)
            finalize(new, new_img, case.get("fields") or {})
            if case.get("hold"):
                try:
                    r.resolve_hold(case["hold"], "retaken", f"new photo checked as case {new}")
                except ValueError:
                    pass   # already closed by someone else
            return self.redirect(f"/case/{new}")
        if method == "POST" and rest == ["record"]:
            if u["role"] != "pharmacist":
                raise HttpError(403, "Only a pharmacist can record a dispensed high-risk prescription")
            rep = case.get("final")
            if not rep or rep.get("risk") != "high":
                raise HttpError(400, "Only checked high-risk prescriptions are recorded")
            worst = max(rep["verdicts"].values(), key=r.ORDER.index)
            if worst != "GREEN" and hold_outcome(case.get("hold")) not in PHARMACIST_ONLY:
                raise HttpError(409, "Resolve the hold first (confirmed / dispensed after review)")
            if case.get("recorded"):
                return self.redirect(f"/case/{cid}")
            f = case.get("fields") or {}
            eid = f"RX-{cid}"
            r.add_entry(img, eid, date.today().isoformat(), f.get("regn") or None, f.get("doctor") or None,
                        f.get("drugs") or "", note=f"counter app case {cid}")
            with STATE.case_lock:
                _, case = load_case(cid)
                case["recorded"] = eid
                save_case(cid, case)
            return self.redirect(f"/case/{cid}")
        raise HttpError(404, "Page not found")

    def doctor(self, hid, what):
        """Ask the prescriber (pharmacist), enter the code they read out (pharmacist), or look for their answer."""
        if what not in ("ask", "otp", "status"):
            raise HttpError(404, "Page not found")
        if what != "status" and self.user["role"] != "pharmacist":
            raise HttpError(403, "Only a pharmacist can ask the doctor or enter the doctor's code")
        cid = self.field("case")
        _, case = load_case(cid)
        row = r.db().execute("SELECT status FROM holds WHERE id = ?", (hid,)).fetchone()
        if case.get("hold") != hid or not case.get("final"):
            raise HttpError(400, "That hold does not belong to this case")
        if not row or row["status"] == "closed":
            raise HttpError(409, "This hold is already closed")
        notice = {"ask": self.doctor_ask, "otp": self.doctor_otp, "status": self.doctor_status}[what](case, hid)
        return self.send(200, decision_page(self.user, cid, case, notice=notice))

    def doctor_pending(self, hid):
        return [a for a in dp.asks(hid) if a["status"] == "pending"]

    def doctor_ask(self, case, hid):
        pending = self.doctor_pending(hid)
        if pending and dp.poll(pending[-1]["request_id"])["status"] == "pending":
            return "Already asked - waiting for the doctor's answer."
        regn, raw_date = self.field("regn")[:40], self.field("rx_date")
        patient = dp.mask_patient(self.field("patient")[:40])
        rx_date = r.iso_date(raw_date) if raw_date else None
        if not regn:
            return "Enter the doctor's registration number."
        if not patient:
            return "Enter the patient's initials and age, for example RK, 34."
        if raw_date and not rx_date:
            return "The date must be a valid date."
        drugs = ", ".join(case["final"].get("held") or []) or (case.get("fields") or {}).get("drugs") or ""
        if not drugs:
            return "There are no medicines to ask about."
        return doctor_notice(dp.ask(hid, regn, drugs, patient, rx_date.isoformat() if rx_date else ""))

    def doctor_otp(self, case, hid):
        code = self.field("code")
        if not re.fullmatch(r"\d{6}", code):
            return "The code is 6 digits."
        pending = self.doctor_pending(hid)
        if not pending:
            return "There is no open request for this hold - ask the doctor first."
        return doctor_notice(dp.submit_otp(pending[-1]["request_id"], code))

    def doctor_status(self, case, hid):
        pending = self.doctor_pending(hid)
        if not pending:
            return "There is no open request for this hold."
        return doctor_notice(dp.poll(pending[-1]["request_id"]))

    def resolve(self, hid):
        outcome, note, cid = self.field("outcome"), self.field("note"), self.field("case")
        reason = self.field("reason") or None
        if outcome not in r.RESOLUTIONS or outcome == "retaken":
            raise HttpError(400, "Unknown outcome")
        if outcome in PHARMACIST_ONLY and self.user["role"] != "pharmacist":
            raise HttpError(403, "Only a pharmacist can record that outcome")
        try:
            r.resolve_hold(hid, outcome, note, reason)
        except ValueError as ex:
            if CASE_ID.match(cid or ""):
                _, case = load_case(cid)
                if case.get("final"):
                    return self.send(409, decision_page(self.user, cid, case, error=str(ex)))
            raise HttpError(409, str(ex))
        return self.redirect(f"/case/{cid}" if CASE_ID.match(cid) else "/holds")


# ---------------------------------------------------------------- scheduled backups

def backup_loop(stop):
    while not stop.wait(60 if STATE.last_backup is None else STATE.backup_hours * 3600):
        try:
            r.set_actor("scheduled-backup")
            dest, problems = r.cmd_backup(SimpleNamespace(dest=STATE.backup_dir, keep=42))
            STATE.last_backup = (datetime.now(), dest, problems)
            purge_uploads()
            r.purge_patients()
        except Exception as ex:   # a full or missing backup disk must be visible, not fatal
            STATE.last_backup = (datetime.now(), None, [f"backup failed: {ex}"])
        finally:
            r.close_thread_db()


def make_server(host="127.0.0.1", port=8765, inbox=None, backup_dir=None, backup_hours=4.0, cert=None, key=None,
                behind_proxy=False):
    global STATE
    STATE = State(inbox, backup_dir, backup_hours, tls=bool(cert), behind_proxy=behind_proxy)
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    if cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(cert, key)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    stop = threading.Event()
    if backup_dir:
        threading.Thread(target=backup_loop, args=(stop,), daemon=True).start()
    srv.stop_event = stop
    return srv


def bootstrap_admin():
    """A cloud host has no terminal for `staff add`: create the first pharmacist from RXCHECK_ADMIN_NAME and
    RXCHECK_ADMIN_PIN - only while no pharmacist exists, so changing or leaving the variables later does nothing."""
    n = r.db().execute("SELECT COUNT(*) FROM staff WHERE active = 1 AND role = 'pharmacist' AND name != ?",
                       (DEMO_USER,)).fetchone()[0]
    name, pin = os.environ.get("RXCHECK_ADMIN_NAME", "").strip(), os.environ.get("RXCHECK_ADMIN_PIN", "").strip()
    if n or not (name and pin):
        return n > 0
    r.set_actor("bootstrap")
    r.add_staff(name, "pharmacist", pin)   # ValueError if the PIN is not 6+ digits
    r.set_actor(None)
    print(f"created pharmacist account '{name}' from RXCHECK_ADMIN_NAME / RXCHECK_ADMIN_PIN")
    return True


_DEMO_READY = False


def demo_ready():
    return _DEMO_READY


def demo_samples():
    """The public demo's synthetic sample slips (demo/samples.json): {'reference': {...}, 'samples': [...]}."""
    try:
        meta = json.loads((DEMO_DIR / "samples.json").read_text(encoding="utf-8"))
        return meta if isinstance(meta.get("samples"), list) and isinstance(meta.get("reference"), dict) else None
    except (OSError, ValueError, AttributeError):
        return None


def prepare_demo():
    """Public demo (RXCHECK_DEMO=1): a 'Demo visitor' pharmacist that needs no PIN (its PIN is random and never
    shown), and the synthetic reference slip recorded as already dispensed, so the samples show every verdict."""
    global _DEMO_READY
    con = r.db()
    r.set_actor("bootstrap")
    try:
        if not con.execute("SELECT 1 FROM staff WHERE name = ?", (DEMO_USER,)).fetchone():
            r.add_staff(DEMO_USER, "pharmacist", str(secrets.randbelow(10 ** 12)).zfill(12))
        meta = demo_samples()
        ref = (meta or {}).get("reference") or {}
        if ref.get("image") and (DEMO_DIR / ref["image"]).is_file() and \
                not con.execute("SELECT 1 FROM entries WHERE id = ?", (ref["id"],)).fetchone():
            r.add_entry(DEMO_DIR / ref["image"], ref["id"], ref["dispensed"], ref.get("regn"), ref.get("doctor"),
                        ref.get("drugs") or "", note="public demo: synthetic reference slip")
    finally:
        r.set_actor(None)
    _DEMO_READY = True


def serve(host, port, inbox=None, backup_dir=None, backup_hours=4.0, cert=None, key=None, behind_proxy=False):
    try:
        have_admin = bootstrap_admin()
    except ValueError as ex:
        sys.exit(f"RXCHECK_ADMIN_PIN rejected: {ex}")
    if DEMO:
        prepare_demo()
        print(f"DEMO mode: no sign-in needed - visitors use the counter as '{DEMO_USER}'"
              + ("" if have_admin else " (no pharmacist account; set RXCHECK_ADMIN_NAME / _PIN to add one)"))
    elif not have_admin:
        sys.exit("No pharmacist account yet. Create one first:  python app/rxcheck.py staff add NAME --role pharmacist\n"
                 "(on a cloud host: set RXCHECK_ADMIN_NAME and RXCHECK_ADMIN_PIN)")
    problems = r.verify_store()
    if problems:
        print("WARNING - ledger problems (run `rxcheck verify`):\n  " + "\n  ".join(problems[:5]))
    if host not in ("127.0.0.1", "localhost") and not cert and not behind_proxy:
        print("WARNING - serving the LAN without TLS: PINs and prescriptions cross the network unencrypted. "
              "Use --cert/--key.")
    srv = make_server(host, port, inbox, backup_dir, backup_hours, cert, key, behind_proxy)
    purge_uploads()
    r.purge_patients()
    scheme = "https" if cert else "http"
    if DEMO:
        print("DEMO mode: every page carries a 'do not upload real prescriptions' banner")
    print(f"rxcheck counter app on {scheme}://{host}:{port}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop_event.set()
        srv.server_close()
