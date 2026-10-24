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

import cv2

import doctor_confirmation as dp
import slip_reader as ocr
import pdf_pages
import rxcheck as r

MAX_UPLOAD = 15 * 1024 * 1024
DEMO_USER = "Demo visitor"                # public demo only: everyone who opens the link is signed in as this
DEMO_DIR = Path(__file__).resolve().parent.parent / "demo"   # synthetic sample slips (tests/build_demo_samples.py)
SETUP_URL = "https://github.com/joshuajzenith-commits/rxche#reading-the-slip-ocr-a-typing-aid-not-a-check"
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


def new_case(data, suffix, origin, read=True, recorded=None):
    """A new case from an uploaded photo or PDF. recorded: a demo sample's recorded reading, replayed instead of
    OCR (the public deployment has no engine)."""
    if len(data) > MAX_UPLOAD:
        raise HttpError(413, "File is larger than 15 MB. Ask for a photo of the slip, or only its pages.")
    pdf = suffix == ".pdf" or pdf_pages.is_pdf(data)
    if not pdf and suffix not in r.IMG_EXT:
        raise HttpError(400, "Send a photo (.jpg, .png or .webp) or a PDF")
    cid = f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
    text = [None]
    if pdf:
        try:
            pages = pdf_pages.pages(data)
        except pdf_pages.PdfError as ex:
            raise HttpError(400, str(ex))
        path = uploads() / f"{cid}.jpg"   # page 1 is the case image: the checks, the evidence, the ledger
        path.write_bytes(pages[0][0])
        for i, (jpg, _) in enumerate(pages[1:], 2):
            page_path(cid, i - 1).write_bytes(jpg)
        text = [lines for _, lines in pages]
    else:
        path = uploads() / f"{cid}{suffix}"
        path.write_bytes(data)
    try:
        r.read_bgr(path)
    except ValueError:
        path.unlink()
        raise HttpError(400, "That file is not a readable image")
    # pre-check without drugs: shows routine/high-risk outcomes and decodes any clinic-signed code to prefill
    pre = r.public(r.run_check(path, draw=False))
    # reading the slip takes 5-15 s a page on a counter PC: it runs in the background and the form fills in when
    # it is done. Not for a retake (already decided) or a demo sample (synthetic slip, filled in for the visitor).
    # A PDF page with a text layer is read from its text, so it needs no OCR engine.
    if not read:
        why = "retake"
    elif origin.startswith("demo:"):
        why = None if recorded and ocr.enabled() else "demo"
    elif not ocr.enabled():
        why = "switched off"
    elif not ocr.available() and not all(text):
        why = "not installed"
    else:
        why = None
    save_case(cid, {"origin": origin, "created": r.now(), "by": r.actor(), "pre": pre, "final": None,
                    "hold": None, "recorded": None, "pages": len(text), "source": "pdf" if pdf else "photo",
                    "ocr": {"status": "off", "why": why} if why else {"status": "pending"}})
    if not why:
        pages_in = [{"path": page_path(cid, i, path), "lines": lines, "recorded": recorded if i == 0 else None}
                    for i, lines in enumerate(text)]
        # a replay takes a few seconds, like a reading, so the visitor sees the form fill in
        delay = min(4.0, (recorded.get("ms") or 3000) / 1000) if recorded else 0
        threading.Thread(target=read_case, args=(cid, pages_in, known_drug_names(), delay), daemon=True).start()
    return cid


def page_path(cid, i, first=None):
    """Page i (0 = the first) of a case: the case image itself, then '<cid>-p2.jpg' and so on for a PDF."""
    if i == 0:
        return first or case_files(cid)[0]
    return uploads() / f"{cid}-p{i + 1}.jpg"


def known_drug_names():
    """(names, strengths) this pharmacy knows: names from the drug list and from entries at this counter, and the
    strengths entered here for each name ({'dolo': {'650'}})."""
    table = r.drug_table() or r.EMPTY_TABLE
    names, strengths = set(table["brands"]) | set(table["molecules"]), {}
    for (drugs,) in r.db().execute("SELECT drugs FROM entries WHERE drugs != '' ORDER BY created_at DESC LIMIT 3000"):
        for item in re.split(r"[,;\n]", drugs or ""):
            m = SEEN_DRUG.match(item)
            if m:
                name = m.group(1).strip().lower()
                names.add(name)
                strengths.setdefault(name, set()).update(re.findall(r"\d+(?:\.\d+)?", item[m.end():]))
    return names, strengths


def read_case(cid, pages, known, delay=0):
    """Background: read the slip and store what it read on the case. A failure only means staff type as before."""
    time.sleep(delay)
    names, strengths = known
    res = ocr.read_pages(pages, names, lambda text: r.drug_risk(text)[0] == "high", strengths=strengths)
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
    _, case = load_case(cid)
    r.record_check(rep, f.get("drugs"), f.get("regn") or None, f.get("doctor") or None,
                   {"case": cid, "rx_date": f.get("rx_date") or None, "source": case.get("source", "photo"),
                    "pages": case.get("pages", 1)})
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
        case = re.sub(r"-p\d+$", "", p.name.split(".")[0])   # a PDF's later pages go with their case
        if case not in held and p.stat().st_mtime < cutoff:
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
header{position:sticky;top:12px;z-index:10;display:flex;gap:8px 14px;align-items:center;flex-wrap:wrap;
max-width:1160px;margin:16px auto 0;padding:8px 10px 8px 12px;border-radius:999px;
background:color-mix(in srgb,var(--card) 90%,transparent);border:var(--bw) solid var(--line);
box-shadow:0 10px 30px #0000001f,0 1px 2px #00000014;-webkit-backdrop-filter:blur(12px);backdrop-filter:blur(12px)}
@media (max-width:1200px){header{margin-left:20px;margin-right:20px}}
html[data-contrast=high] header{background:var(--card);border:2px solid var(--ink);box-shadow:none}
.brand{display:flex;align-items:center;gap:10px;color:var(--ink);text-decoration:none;font-weight:800;font-size:1.05rem;line-height:1.1}
.brand small{display:block;font-size:.66rem;font-weight:700;color:var(--muted);letter-spacing:.14em;text-transform:uppercase}
.mark{display:inline-grid;place-items:center;width:40px;height:40px;border-radius:50%;background:var(--accent);color:var(--card);font-weight:900;font-size:.95rem;font-style:italic}
header nav{display:flex;gap:2px;flex:1 1 auto;justify-content:center;flex-wrap:wrap}
header nav a{position:relative;display:inline-flex;align-items:center;min-height:44px;padding:0 18px;border-radius:999px;
color:var(--accent);text-decoration:none;font-weight:700;font-size:.84rem;letter-spacing:.14em;text-transform:uppercase}
header nav a:hover{background:color-mix(in srgb,var(--accent) 10%,transparent)}
header nav a[aria-current=page]::after{content:"";position:absolute;left:50%;bottom:2px;width:7px;height:7px;
margin-left:-3.5px;border-radius:50%;background:var(--accent)}
.who{display:flex;align-items:center;gap:10px;line-height:1.15;font-size:.92rem}.who b{display:block}
.who .role{display:block;font-size:.76rem;color:var(--muted);text-transform:capitalize}
.avatar{display:inline-grid;place-items:center;width:40px;height:40px;border-radius:50%;background:var(--bg);border:var(--bw) solid var(--line);font-weight:800;font-size:.85rem}
button.round,.rx-display button{display:inline-grid;place-items:center;width:40px;height:40px;min-height:40px;padding:0;
border-radius:50%;border:var(--bw) solid var(--line);background:transparent;color:var(--accent);font-weight:800;font-size:.9rem}
button.round:hover,.rx-display button:hover:not(:disabled):not([aria-pressed=true]){border-color:var(--accent);background:color-mix(in srgb,var(--accent) 10%,transparent)}
#rx-display{padding-left:12px;border-left:var(--bw) solid var(--line)}#rx-display:empty{display:none}
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
.hero h1{font-size:1.7rem}.js-only{display:none}html.js .js-only{display:inline}html.js .nojs-only{display:none}
.upload-status{font-weight:600;margin:10px 0 0}.upload-status:empty{display:none}
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
.issues img.crop{margin:4px 0 6px 28px;max-width:calc(100% - 28px)}
.s-quiet{background:var(--bg);color:var(--ink);border-color:var(--line)}.ocr .pill{margin-right:6px}
.reading-status .pill{margin-right:0}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.status-row{display:flex;flex-wrap:wrap;gap:6px 8px;align-items:center;margin:8px 0}
.hero-head{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;justify-content:space-between;margin-bottom:6px}
.hero-head h1,.hero-head h2{margin:0}
.chip{display:inline-flex;align-items:center;gap:4px;padding:2px 10px;border-radius:99px;font-size:.85rem;font-weight:600;
border:1px solid var(--line);background:var(--bg);color:var(--ink)}
.chip.warn{border-color:var(--amber);background:var(--amber-bg);color:var(--amber)}
.chip.ok{border-color:var(--green);background:var(--green-bg);color:var(--green)}
.chip.src{border-color:var(--accent);color:var(--accent);background:var(--card)}
label .chip{margin-left:6px;vertical-align:1px}
details.tip{display:inline-block;position:relative}
details.tip>summary{list-style:none;display:inline-grid;place-items:center;width:24px;height:24px;border-radius:50%;
border:var(--bw) solid var(--muted);color:var(--muted);font:italic 700 .8rem/1 Georgia,serif;cursor:pointer}
details.tip>summary::-webkit-details-marker{display:none}
details.tip[open]>summary{background:var(--ink);color:var(--card);border-color:var(--ink)}
.tip-body{position:absolute;z-index:15;left:0;top:30px;width:min(340px,80vw);padding:10px 12px;border-radius:10px;
background:var(--card);color:var(--ink);border:var(--bw) solid var(--line);box-shadow:0 6px 18px #00000030;font-size:.92rem;
font-weight:400;line-height:1.45}
.drop{align-items:center;text-align:center;padding:6px 0}.drop-icon{color:var(--accent)}
.demo-note{margin:10px 0 0;padding:10px 14px;border-radius:10px;background:var(--bg);border:var(--bw) solid var(--line);
border-left:4px solid var(--accent);font-size:.94rem}
.demo-try{margin-top:18px;padding-top:12px;border-top:var(--bw) solid var(--line)}
.demo-try h2{font-size:1rem;margin:0}.demo-try .status-row{margin:0 0 8px}
.samples{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,240px),1fr));gap:8px}
.sample{margin:0}
.sample-btn{display:flex;align-items:center;justify-content:flex-start;gap:10px;width:100%;height:100%;min-height:44px;
padding:6px 10px 6px 6px;border-radius:10px;border:var(--bw) solid var(--line);background:var(--bg);color:var(--ink);
text-align:left;font-weight:600;font-size:.88rem}
.sample-btn:hover{border-color:var(--accent)}.sample-btn img{flex:none;width:48px;height:48px;object-fit:cover;
object-position:top;border-radius:6px;border:var(--bw) solid var(--line);background:#fff}
.sample-text{display:flex;flex-direction:column;align-items:flex-start;gap:3px;line-height:1.25}
.sample-text .pill{font-size:.72rem;padding:0 7px}
ul.chips{list-style:none;padding:0;display:flex;flex-wrap:wrap;gap:6px;margin:4px 0 12px}
ul.chips li{margin:0;padding:3px 10px;border-radius:99px;font-size:.88rem;font-weight:600;border:1px solid var(--line)}
ul.chips li.yes{border-color:var(--green);color:var(--green);background:var(--green-bg)}
ul.chips li.no{color:var(--muted)}
.stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}
.stats div{padding:10px 12px;border-radius:10px;border:var(--bw) solid var(--line);color:var(--muted);font-size:.86rem}
.stats b{display:block;font-size:1.6rem;color:var(--ink);line-height:1.15}.stats .t-green b{color:var(--green)}
details.more{margin:10px 0 0}details.more>summary{color:var(--accent)}
.spin{width:16px;height:16px;border-radius:50%;border:2px solid var(--line);border-top-color:var(--accent);
animation:rx-spin .8s linear infinite}@keyframes rx-spin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.spin{animation:none}}
.read-as{display:block;font-size:.85rem}
.issues h2{display:flex;align-items:center;gap:8px;font-size:1rem;margin:0}.issues .count{display:inline-grid;
place-items:center;min-width:24px;height:24px;border-radius:99px;background:var(--amber);color:var(--amber-bg);font-size:.85rem}
.issues .hint{margin:2px 0 6px}
details.chk{border-top:var(--bw) solid var(--line);padding:2px 0}details.chk:first-of-type{border-top:0}
details.chk>summary{display:grid;grid-template-columns:28px auto auto 1fr;gap:4px 10px;align-items:center;padding:8px 0;
list-style:none}details.chk>summary::-webkit-details-marker{display:none}
.chk .ico{display:inline-grid;place-items:center;width:26px;height:26px;border-radius:50%;font-weight:900;border:1px solid}
.chk .area{font-weight:700}.chk .find{grid-column:2/-1;color:var(--muted);font-size:.94rem}
.chk-body{padding:0 0 8px 38px}
@media (min-width:700px){.chk .find{grid-column:auto;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
details.chk[open] .find{white-space:normal}}
.verdict .why .tip{margin-left:8px;vertical-align:2px}.verdict .why .tip-body{color:var(--ink)}
tr.late td,tr.late th{background:var(--amber-bg)}.hold-meta{margin:0 0 4px}
.checks h3{display:flex;gap:10px;align-items:center;margin:0 0 4px}.checks p{margin:4px 0}
.checks ul.ev{margin:4px 0;color:var(--muted);font-size:.94rem}.checks .unknown{color:var(--muted);font-size:.94rem}table.compact td,table.compact th{padding:6px 8px}
table.compact caption{text-align:left;margin-bottom:6px}
tbody th{text-transform:none;letter-spacing:0;font-size:inherit;color:var(--ink);font-weight:600}
.demo-about h3{margin:20px 0 8px}.scroll{overflow-x:auto}
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
.say{margin:10px 0 0;padding:8px 12px;border-radius:8px;background:var(--bg);color:var(--ink)}
.home{align-items:stretch}.home-col{display:flex;flex-direction:column;min-width:0}
.home-col>.card{margin-bottom:16px}.home-col>.card:last-child{flex:1 1 auto}
.card-head{display:flex;justify-content:space-between;align-items:baseline;gap:8px;margin-bottom:10px}
.card-head h2{margin:0;font-size:.78rem;text-transform:uppercase;letter-spacing:.09em;color:var(--muted);display:flex;gap:8px;align-items:center}
.card-head a{font-size:.88rem;font-weight:600}
.card-head .count{display:inline-grid;place-items:center;min-width:22px;height:22px;padding:0 6px;border-radius:99px;
background:var(--amber);color:var(--amber-bg);font-size:.78rem;letter-spacing:0}
.trend{margin-top:16px;padding-top:14px;border-top:var(--bw) solid var(--line)}
.trend-head{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:6px 12px;margin-bottom:6px}
.trend-head h3{margin:0;font-size:.9rem}
.legend{display:flex;gap:12px;flex-wrap:wrap;font-size:.8rem;color:var(--muted)}
.legend span{display:inline-flex;align-items:center;gap:5px}.sw{display:inline-block;width:10px;height:10px;border-radius:3px}
.s-GREEN{background:#0ca30c}.s-AMBER{background:#fab219}.s-RED{background:#d03b3b}
.plot{position:relative;display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:6px}
.tcol{position:relative;display:flex;flex-direction:column;align-items:stretch}
.area{height:116px;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;
border-bottom:var(--bw) solid var(--line)}
.bar{display:block;max-width:100%}
.cap{font-size:.78rem;font-weight:700;color:var(--ink);line-height:1;margin-bottom:4px}
.day{text-align:center;font-size:.74rem;color:var(--muted);padding-top:4px}.is-today .day{color:var(--ink);font-weight:700}
.plot:hover .tcol:not(:hover) .bar{opacity:.4}
.tcol:hover::after{content:attr(data-tip);position:absolute;bottom:calc(100% + 4px);left:50%;transform:translateX(-50%);
z-index:5;white-space:nowrap;padding:5px 9px;border-radius:7px;background:var(--ink);color:var(--card);font-size:.78rem;
font-weight:600;pointer-events:none}
.tcol:nth-child(-n+2):hover::after{left:0;transform:none}.tcol:nth-child(n+6):hover::after{left:auto;right:0;transform:none}
.trend-empty{position:absolute;inset:0 0 24px;display:grid;place-items:center;margin:0;padding:0 12px;text-align:center;
color:var(--muted);font-size:.88rem}
.insights{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:14px 0 0}
.insights div{padding:10px 12px;border-radius:10px;background:var(--bg);border:var(--bw) solid var(--line)}
.insights dt{font-size:.76rem;color:var(--muted);font-weight:600;line-height:1.25}
.insights dd{margin:4px 0 0;font-size:.82rem;color:var(--muted)}.insights dd b{font-size:1.2rem;color:var(--ink);margin-right:3px}
.feed{list-style:none;padding:0;margin:0 0 14px}
.feed li{display:grid;grid-template-columns:7.5em minmax(0,1fr) auto;gap:4px 12px;align-items:start;margin:0;padding:10px 0;
border-top:var(--bw) solid var(--line)}.feed li:first-child{border-top:0;padding-top:0}
.feed .pill{justify-self:start;white-space:nowrap;margin-top:1px}.feed-main b{margin-right:6px}
.feed-why{display:block;font-size:.86rem;margin-top:1px}
.feed-when{text-align:right;font-size:.82rem;line-height:1.3;white-space:nowrap}.feed-when b,.feed-when span{display:block}
.feed-empty,.all-clear{margin:0;color:var(--muted)}.all-clear span{color:var(--green);font-weight:900;margin-right:4px}
.feed-card{display:flex;flex-direction:column}.feed-card .activity{margin:auto 0 0;padding-top:10px;border-top:var(--bw) solid var(--line);font-size:.9rem}
.hold-list{list-style:none;padding:0;margin:0}
.hold-list li{display:grid;grid-template-columns:7.5em minmax(0,1fr);gap:10px;align-items:start;margin:0;padding:9px 0;
border-top:var(--bw) solid var(--line)}.hold-list li:first-child{border-top:0;padding-top:0}
.hold-list .pill{justify-self:start;white-space:nowrap}.hold-list li div{display:flex;flex-direction:column;font-size:.9rem;min-width:0}
.hold-list li.late span:last-child{color:var(--red);font-weight:700}
.facts{margin:0}.facts>div{display:grid;grid-template-columns:minmax(0,10.5em) minmax(0,1fr);gap:4px 12px;padding:9px 0;
border-top:var(--bw) solid var(--line)}.facts>div:first-child{border-top:0;padding-top:0}
.facts dt{display:flex;gap:6px;align-items:center;font-weight:600;font-size:.9rem}.facts dd{margin:0;font-size:.9rem;color:var(--muted)}
.facts dd b{color:var(--ink)}.facts details.tip>summary{width:20px;height:20px;font-size:.7rem}
.record>p:last-child{margin:12px 0 0;font-size:.88rem}
.steps{list-style:none;padding:0;margin:0;display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,230px),1fr));gap:12px}
.steps li{display:flex;gap:12px;margin:0;padding:14px;border-radius:12px;background:var(--bg);border:var(--bw) solid var(--line)}
.step-n{flex:none;display:grid;place-items:center;width:30px;height:30px;border-radius:50%;background:var(--accent);
color:var(--card);font-weight:800}.steps p{margin:4px 0 0;font-size:.9rem;color:var(--muted)}
@media (max-width:860px){header{border-radius:26px;padding:8px 10px}.who{margin-left:auto}
header nav{order:5;flex-basis:100%;border-top:var(--bw) solid var(--line);padding-top:4px}}
@media (max-width:600px){main{padding:12px}header{margin:10px 12px 0;top:8px}.who>span:not(.avatar){display:none}
.brand-name{display:none}header{column-gap:6px}.who{gap:6px}#rx-display{padding-left:0;border-left:0}
header nav a{padding:0 12px;letter-spacing:.1em}
.verdict{grid-template-columns:1fr;gap:10px;padding:16px;border-left-width:8px}.verdict .icon{width:46px;height:46px;font-size:1.5rem}
.verdict .word{font-size:1.8rem}.split{grid-template-columns:1fr}.today{grid-template-columns:repeat(2,minmax(0,1fr))}
.actions button{flex:1 1 100%}
.feed li{grid-template-columns:minmax(0,1fr)}.feed-when{text-align:left}.feed-when b,.feed-when span{display:inline;margin-right:6px}.insights{grid-template-columns:1fr}.facts>div{grid-template-columns:1fr}.hold-list li{grid-template-columns:1fr}}
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
                  f'<button class="round" type="submit" aria-label="Sign out" title="Sign out">{SIGN_OUT_ICON}</button>'
                  f'</form>') + '</div>')
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    banner = f'<p class="demo" role="alert">{esc(DEMO_BANNER)}</p>' if DEMO else ""
    enhance = ""
    if (STATIC / "app.js").is_file():
        enhance = ('<link rel="stylesheet" href="/static/app.css">'
                   '<script type="module" src="/static/app.js"></script>')
    return (f'<!doctype html><html lang="en"{ui_attrs()}><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">{meta}'
            f'<title>{esc(title)} - rxcheck</title><link rel="stylesheet" href="/app.css">{enhance}</head><body>'
            f'<a class="skip" href="#main">Skip to main content</a>{banner}'
            f'<header><a class="brand" href="/"><span class="mark" aria-hidden="true">Rx</span>'
            f'<span class="brand-name">rxcheck<small>counter</small></span></a>{nav}<div id="rx-display"></div></header>'
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


def tip(label, body):
    """An explanation on demand: an (i) that opens in place. Native <details>: keyboard and screen reader friendly."""
    return (f'<details class="tip"><summary><span aria-hidden="true">i</span><span class="sr">{esc(label)}</span>'
            f'</summary><div class="tip-body">{body}</div></details>')


def chip(text, kind=""):
    return f'<span class="chip{" " + kind if kind else ""}">{text}</span>'


UPLOAD_ICON = ('<svg class="drop-icon" viewBox="0 0 24 24" aria-hidden="true" width="40" height="40"><path fill="none" '
               'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" d="M12 16V4m0 0-4 '
               '4m4-4 4 4M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/></svg>')
SIGN_OUT_ICON = ('<svg viewBox="0 0 24 24" aria-hidden="true" width="18" height="18"><path fill="none" stroke="currentColor" '
                 'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 '
                 '2h-3M10 16l-4-4 4-4M6 12h10"/></svg>')


def verdict_block(verdict, reason, extra=""):
    word, sub = VERDICT_UI[verdict]
    reason = plain(reason)
    head, _, rest = reason.partition(" - ")   # the finding first; its explanation on demand
    icon = {"RED": "&#x2715;", "AMBER": "!", "GREEN": "&#x2713;"}[verdict]
    return (f'<section class="verdict v-{verdict}" role="status"><span class="icon" aria-hidden="true">{icon}</span>'
            f'<div><div class="word">{word}</div><div class="sub">{esc(sub)}</div>'
            f'<p class="why">{esc(head)}{tip("More about this", esc(rest)) if rest else ""}</p></div></section>{extra}')


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


def today_panel(user=None, trend=False):
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
            f'<div class="t-red"><b>{t["overrides"]}</b>overrides</div></div>{alert}'
            f'{trend_chart() + week_insights() if trend else ""}</section>')


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


# ---------------------------------------------------------------- home page panels: what the counter has been doing

WORD = {"GREEN": "No conflict", "AMBER": "Confirm first", "RED": "Hold"}
PATH_SHORT = {"retake": "Retake the photo", "counter": "Counter check", "call": "Call the clinic"}


def worst_of(d):
    return max((d.get("verdicts") or {"x": "GREEN"}).values(), key=r.ORDER.index)


def ago(at):
    """'just now', '12 min ago', '3 h ago', else the date and time."""
    try:
        s = (datetime.now() - datetime.fromisoformat(at)).total_seconds()
    except (TypeError, ValueError):
        return at or ""
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 12 * 3600:
        return f"{int(s // 3600)} h ago"
    return when(at)


def week_counts(days=7):
    """[(date, {GREEN, AMBER, RED})] for the last `days` days, today last. A slip checked twice counts once."""
    start = date.today() - timedelta(days=days - 1)
    slips = {}
    for x in r.db().execute("SELECT at, ref, detail FROM audit WHERE action = 'check' AND at >= ? ORDER BY seq",
                            (start.isoformat(),)):
        slips[(x["at"][:10], x["ref"])] = worst_of(detail_of(x))
    out = [(start + timedelta(days=i), {"GREEN": 0, "AMBER": 0, "RED": 0}) for i in range(days)]
    by_day = {d.isoformat(): c for d, c in out}
    for (day, _), v in slips.items():
        if day in by_day:
            by_day[day][v] += 1
    return out


BAR_FILL = {"GREEN": "#0ca30c", "AMBER": "#fab219", "RED": "#d03b3b"}   # status fills, labelled in the legend


def bar_svg(c, top, h=92, w=28):
    """One day's column as SVG (the page's CSP allows no inline styles): green at the base, then amber, then red,
    2px apart, the top end rounded."""
    keys = [k for k in ("GREEN", "AMBER", "RED") if c[k]]
    if not top or not keys:
        return ""
    hs = [max(c[k] / top * h, 3) for k in keys]
    total = sum(hs) + 2 * (len(hs) - 1)
    y, parts = total, []
    for i, (k, sh) in enumerate(zip(keys, hs)):
        y -= sh
        if i == len(keys) - 1:   # the top segment: rounded data end
            rr = min(4, sh)
            parts.append(f'<path fill="{BAR_FILL[k]}" d="M0 {y + sh:.1f}V{y + rr:.1f}Q0 {y:.1f} {rr} {y:.1f}H{w - rr}'
                         f'Q{w} {y:.1f} {w} {y + rr:.1f}V{y + sh:.1f}Z"/>')
        else:
            parts.append(f'<rect fill="{BAR_FILL[k]}" x="0" y="{y:.1f}" width="{w}" height="{sh:.1f}"/>')
        y -= 2
    return (f'<svg class="bar" viewBox="0 0 {w} {total:.1f}" width="{w}" height="{total:.0f}" aria-hidden="true">'
            f'{"".join(parts)}</svg>')


def trend_chart():
    """Checks per day for the last week, stacked by verdict; a table for screen readers."""
    days = week_counts()
    top = max((sum(c.values()) for _, c in days), default=0)
    cols, rows = [], []
    for d, c in days:
        n = sum(c.values())
        label = "Today" if d == date.today() else f"{d:%a}"
        tipline = f"{d:%a %d %b}: {n} check{'s' if n != 1 else ''}" + (
            " - " + ", ".join(f"{c[k]} {WORD[k].lower()}" for k in ("GREEN", "AMBER", "RED") if c[k]) if n else "")
        cols.append(f'<div class="tcol{" is-today" if d == date.today() else ""}" data-tip="{esc(tipline)}">'
                    f'<div class="area"><span class="cap">{n or ""}</span>{bar_svg(c, top)}</div>'
                    f'<span class="day">{label}</span></div>')
        rows.append(f'<tr><th scope="row">{d:%a %d %b}</th><td>{n}</td><td>{c["GREEN"]}</td><td>{c["AMBER"]}</td>'
                    f'<td>{c["RED"]}</td></tr>')
    empty = "" if top else (f'<p class="trend-empty">No checks in the last 7 days'
                            f'{" - open a sample slip and it lands here" if DEMO else ""}.</p>')
    legend = "".join(f'<span><i class="sw s-{k}" aria-hidden="true"></i>{WORD[k]}</span>' for k in ("GREEN", "AMBER", "RED"))
    return (f'<div class="trend"><div class="trend-head"><h3>Checks, last 7 days</h3><div class="legend">{legend}</div></div>'
            f'<div class="plot" aria-hidden="true">{"".join(cols)}{empty}</div>'
            f'<div class="sr"><table><caption>Checks per day, last 7 days</caption><thead><tr><th scope="col">Day</th>'
            f'<th scope="col">Checks</th><th scope="col">No conflict</th><th scope="col">Confirm first</th>'
            f'<th scope="col">Hold</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div></div>')


def week_insights():
    """Three numbers that say whether warnings cost the counter too much, or are being waved through."""
    w = r.workload(7)
    n, p = w["slips"], w["paths"]
    quick = w["green"] + p["retake"] + p["counter"]
    share = f"{round(100 * quick / n)}%" if n else "-"
    med = w["median_minutes_to_close"]
    return (f'<dl class="insights">'
            f'<div><dt>Settled without a call</dt><dd><b>{share}</b>{f" {quick} of {n}" if n else " no checks yet"}</dd></div>'
            f'<div><dt>Time to close a hold</dt><dd>{f"<b>{med:g} min</b> median" if med is not None else "<b>-</b> none closed yet"}</dd></div>'
            f'<div><dt>Overrides</dt><dd><b>{w["overrides"]}</b> of {w["closed"]} closed hold{"s" if w["closed"] != 1 else ""}'
            f'</dd></div></dl>')


def recent_checks(user, n=6, extra=""):
    """The last few checks, newest first: verdict, medicines, the step it asked for, who and when."""
    seen, items = set(), []
    for x in r.db().execute("SELECT at, actor, ref, detail FROM audit WHERE action = 'check' ORDER BY seq DESC LIMIT 60"):
        if x["ref"] in seen:
            continue
        seen.add(x["ref"])
        d = detail_of(x)
        v = worst_of(d)
        drugs = ((d.get("entered") or {}).get("drugs") or "").strip()
        risk, hits, _ = r.drug_risk(drugs) if drugs else (None, [], [])
        what = (esc(drugs if len(drugs) <= 60 else drugs[:57] + "...") if drugs
                else '<span class="muted">pre-check, medicines not entered yet</span>')
        flag = chip("high-risk", "warn") if risk == "high" else ""
        step = "Hand over" if v == "GREEN" else PATH_SHORT.get(d.get("path"), "Confirm before dispensing")
        why = plain(d.get("reason") or "") if v != "GREEN" else "No conflict with the slips and lists on record"
        items.append(f'<li><span class="pill v-{v}">{WORD[v]}</span><div class="feed-main"><b>{what}</b>{flag}'
                     f'<span class="muted feed-why">{esc(step)} &middot; {esc(why if len(why) <= 110 else why[:107] + "...")}'
                     f'</span></div><span class="feed-when"><b>{esc(x["actor"])}</b><span class="muted">{esc(ago(x["at"]))}'
                     f'</span></span></li>')
        if len(items) >= n:
            break
    a = r.last_activity()
    o = a["override"]
    if a["check"]:
        c = a["check"]
        last_check = f'<b>{esc(c["actor"])}</b>, {esc(when(c["at"]))} - {esc(VERDICT_UI[worst_of(detail_of(c))][0])}'
    else:
        last_check = "none yet"
    if o:
        note = detail_of(o).get("note") or ""
        override = (f'<b>{esc(o["actor"])}</b>, {esc(when(o["at"]))} - hold {esc(o["ref"])}'
                    + (f': "{esc(note)}"' if note else ""))
    else:
        override = "none recorded"
    body = (f'<ul class="feed">{"".join(items)}</ul>' if items else
            '<p class="feed-empty">Nothing checked yet. Every check lands here with its verdict, the medicines, '
            'the step it asked for, and who did it.</p>')
    link = '<a href="/audit">Full audit log</a>' if user["role"] == "pharmacist" else ""
    return (f'<section class="card feed-card" aria-labelledby="recent-h"><div class="card-head"><h2 id="recent-h">Recent '
            f'checks</h2>{link}</div>{body}<ul class="activity"><li>Last check: {last_check}</li>'
            f'<li>Last override: {override}</li>{f"<li>{esc(extra)}</li>" if extra else ""}</ul></section>')


def holds_panel():
    """Holds still open, oldest first, with how long they have waited and what happens next."""
    rows = r.db().execute("SELECT * FROM holds WHERE status != 'closed' ORDER BY created").fetchall()
    items = []
    for h in rows[:4]:
        worst = max(json.loads(h["verdicts"]).values(), key=r.ORDER.index)
        st = hold_state(h["id"])
        due, nxt = follow_up(st, h["status"], r.hold_info(h["id"])[0] or "call")
        late = due is not None and datetime.now() >= due
        items.append(f'<li{" class=late" if late else ""}><span class="pill v-{worst}">{WORD[worst]}</span>'
                     f'<div><b>{esc(h["id"])}</b><span class="muted">{esc(ago(h["created"]))}'
                     f'{" &middot; " + esc(st["owner"]) if st["owner"] else ""}</span><span>{esc(nxt)}</span></div></li>')
    more = f'<p class="hint">and {len(rows) - 4} more</p>' if len(rows) > 4 else ""
    body = (f'<ul class="hold-list">{"".join(items)}</ul>{more}' if items else
            '<p class="all-clear"><span aria-hidden="true">&#x2713;</span> Nothing on hold. A warning that needs a '
            'call or a check waits here, with its owner and when to try again.</p>')
    return (f'<section class="card" aria-labelledby="holds-h"><div class="card-head"><h2 id="holds-h">On hold'
            f'{f" <span class=count>{len(rows)}</span>" if rows else ""}</h2><a href="/holds">All holds</a></div>'
            f'{body}</section>')


def record_panel(backup=""):
    """What the checks compare against, and whether the record itself is safe: the counter's safety net."""
    con = r.db()
    kinds = dict(con.execute("SELECT kind, COUNT(*) FROM entries GROUP BY kind").fetchall())
    n_audit = con.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
    table = r.drug_table()
    tiers = {}
    for t in (table or {}).get("molecules", {}).values():
        tiers[t] = tiers.get(t, 0) + 1
    raw, _, _ = r.read_data(r.DRUG_RISK)
    sample = not (isinstance(raw, dict) and raw.get("_verified"))
    reg, _, _ = r.registry()
    pol = r.policy()
    drugs = (f'{tiers.get("ndps", 0)} NDPS / Schedule X, {tiers.get("high", 0)} other high-risk'
             if table else '<span class="warn">drug list unusable - everything is treated as high-risk</span>')
    rows = [
        ("Slips on record", f'<b>{kinds.get("dispensed", 0)}</b> dispensed, <b>{kinds.get("seen", 0)}</b> seen',
         "A copy, an edit or a re-send of any of these is recognised, by fingerprint - no image is kept for it."),
        ("Medicines watched", drugs + (f' {chip("sample list", "warn")}' if table and sample else ""),
         "High-risk medicines are held for confirmation; NDPS / Schedule X items are never released on an override."),
        ("Prescribers listed", f'<b>{len(reg)}</b> in the local registry',
         "A Regn No is checked against this list. A valid number proves the doctor exists, not that they wrote the slip."),
        ("New high-risk slips", "confirmed with the prescriber" if pol.get("confirm_new_high_risk", True)
         else '<span class="warn">not confirmed (policy off)</span>',
         "A high-risk prescription seen for the first time needs the prescriber's confirmation (data/policy.json)."),
        ("Audit trail", f'<b>{n_audit}</b> entries, append-only and hash-chained',
         "Every check, hold, override and sign-in, with who and when. Records cannot be edited or deleted."),
    ]
    items = "".join(f'<div><dt>{k}{tip(k, esc(why))}</dt><dd>{v}</dd></div>' for k, v, why in rows)
    return (f'<section class="card record" aria-labelledby="record-h"><h2 id="record-h">What every check compares '
            f'against</h2><dl class="facts">{items}</dl>{backup}</section>')


def how_it_works():
    """Public demo: the four questions behind every verdict, so a visitor knows what the samples show."""
    steps = (
        ("The image", "Seen before? An edited copy? Already dispensed at another pharmacy? Compared with every slip "
                      "on record by fingerprint, in about a second."),
        ("The medicines", "Each item is tiered: NDPS / Schedule X, other high-risk (H1), or routine. Routine items "
                          "with no issue can go home now; only the rest waits."),
        ("The prescriber", "Regn No against the local registry, the name on the slip against the listed name, and a "
                           "clinic-signed code when the slip carries one."),
        ("The decision", "No conflict, Confirm first or Hold - with the quickest step that settles it: retake the "
                         "photo, check at the counter, or call the clinic."),
    )
    lis = "".join(f'<li><span class="step-n" aria-hidden="true">{i}</span><div><b>{t}</b><p>{b}</p></div></li>'
                  for i, (t, b) in enumerate(steps, 1))
    return (f'<section class="card how" aria-labelledby="how-h"><h2 id="how-h">How a check decides</h2>'
            f'<ol class="steps">{lis}</ol></section>')


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
    status = "" if 'class="warn"' in backup else backup   # a backup problem is already the alert at the top
    inbox_card = (f'<section class="card" aria-labelledby="inbox-h"><h2 id="inbox-h">Received on WhatsApp</h2>{inbox}'
                  f'</section>') if rows else ""
    inbox_line = "WhatsApp inbox: nothing received in the last 24 hours" if STATE.inbox and not rows else ""
    return page("Counter", f"""{alerts}<div class="home"><div class="home-col"><section class="card hero" id="check"><div class="hero-head"><h1>Check a prescription</h1>{reading_status()}</div>
{demo_upload_note()}<form method="post" action="/upload" enctype="multipart/form-data" data-primary>{csrf_field(user)}
<label for="image" class="drop">{UPLOAD_ICON}<span class="drop-title">Photo or PDF of the slip</span>
<span class="muted"><span class="js-only">Drop, paste or choose - it starts by itself</span><span class="nojs-only">Choose a file, then Analyse</span> {kbd("Mod+U")}</span></label><input type="file" id="image" name="image" accept="image/*,application/pdf"
 required data-shortcut="upload" aria-keyshortcuts="Control+U Meta+U">
<p class="upload-status" role="status" aria-live="polite" data-upload-status></p>
<p class="nojs-only"><button type="submit" class="big" aria-keyshortcuts="Control+Enter Meta+Enter">Analyse prescription</button>{kbd("Mod+Enter")}</p></form>{demo_panel(user)}</section>{inbox_card}{recent_checks(user, extra=inbox_line)}</div>
<div class="home-col side">{today_panel(user, trend=True)}{holds_panel()}{record_panel(status)}</div></div>
{how_it_works() if DEMO else ""}{demo_about() if DEMO and demo_ready() else ""}""", user, refresh=60)


def reading_status():
    """Next to the photo field: whether this counter reads the slip. A badge; the why on demand."""
    if not ocr.enabled():
        state, label, cls = "off", "Slip reading off", "s-quiet"
        why = "Switched off on this counter (RXCHECK_OCR=off). Staff type the medicines from the slip."
    elif ocr.available():
        state, label, cls = "on", "Slip reading on", "v-GREEN"
        why = ("Reads the slip in 5-15 s while you work and fills in what it can. Anything uncertain is marked for a "
               "tick against the slip. Nothing leaves this PC.")
    elif DEMO:
        state, label, cls = "missing", "Slip reading: samples only", "s-quiet"
        why = ("This public demo has no OCR engine, so your own photos are not read. The Printed slip sample replays a "
               "reading recorded with the engine on a PC.")
    else:
        state, label, cls = "missing", "Slip reading not installed", "s-quiet"
        why = "Staff type the medicines. To install: requirements-ocr.txt."
    return (f'<div class="status-row reading-status" data-ocr-status="{state}"><span class="pill {cls}">{label}</span>'
            f'{tip("About slip reading", esc(why))}</div>')


def demo_upload_note():
    """Public demo without the OCR engine: what happens to the visitor's own photo, said before they upload it."""
    if not (DEMO and ocr.enabled() and not ocr.available()):
        return ""
    return ('<p class="demo-note"><b>On this demo, your own photo gets the image checks, and you type the '
            'medicines.</b> To see a slip read for you, open the <i>Printed slip</i> sample below.</p>')


def ocr_results():
    """demo/slip-reader-scores.json (eval/score_slip_reader.py): the slip reader on the synthetic labelled set."""
    try:
        data = json.loads((DEMO_DIR / "slip-reader-scores.json").read_text(encoding="utf-8"))
        return data if isinstance(data.get("rows"), list) and data["rows"] else None
    except (OSError, ValueError, AttributeError):
        return None


def demo_about():
    """Public demo: what is in it, how the reader scored, how to run it - as chips and figures, details on demand."""
    res = ocr_results()
    figures = table = ""
    if res:
        rows = {x["kind"]: x for x in res["rows"]}
        silent = sum(x["silent"] + x["silent_established"] for x in res["rows"])
        printed, every = rows.get("printed") or {}, rows.get("ALL") or {}
        figures = (f'<div class="stats"><div class="t-green"><b>{silent}</b>silent errors</div>'
                   f'<div><b>{printed.get("established_pct", "-")}%</b>printed slips read right</div>'
                   f'<div><b>{every.get("slips", "-")}</b>synthetic test slips</div></div>'
                   f'<p class="hint">Synthetic slips - not real-world accuracy. Real handwriting is mostly not read.</p>')
        body = "".join(f'<tr><th scope="row">{esc(x["label"])}</th><td>{x["slips"]}</td><td>{x["new_pct"]}%</td>'
                       f'<td>{x["established_pct"]}%</td><td>{x["silent"] + x["silent_established"]}</td></tr>'
                       for x in res["rows"])
        table = (f'<details class="more"><summary>Scores by slip type</summary><div class="scroll"><table class="compact">'
                 f'<thead><tr><th scope="col">Slips</th><th scope="col">n</th><th scope="col">New counter</th>'
                 f'<th scope="col">Established</th><th scope="col">Silent errors</th></tr></thead><tbody>{body}</tbody>'
                 f'</table></div><p class="hint">Scored {esc(res.get("scored_on") or "")}. Medicines filled in exactly '
                 f'right with only the drug list on file (new) or every medicine dispensed before (established). '
                 f'Silent error = filled in wrong with no tick asked. Synthetic slips, not evidence of accuracy on real '
                 f'prescriptions; the handwriting row is typed handwriting-style fonts.</p></details>')
    yes = ("Image checks", "Slip reading (replay)", "Holds and audit")
    no = ("Live OCR of your photos", "Real doctors and registry", "Interaction or allergy checks")
    chips = ("".join(f'<li class="yes"><span aria-hidden="true">&#x2713;</span> {x}</li>' for x in yes)
             + "".join(f'<li class="no"><span aria-hidden="true">&#x2715;</span> <span class="sr">Not included:</span>'
                       f' {x}</li>' for x in no))
    return (f'<section class="card demo-about" aria-labelledby="about-h"><h2 id="about-h">This demo</h2>'
            f'<ul class="chips">{chips}</ul>{figures}{table}'
            f'<p class="hint">To run it on your own PC with live slip reading, see the '
            f'<a href="{SETUP_URL}" target="_blank" rel="noopener">setup notes</a>.</p></section>')


def demo_panel(user):
    """Public demo: one-click synthetic sample slips - a thumbnail, the verdict it gives, its name."""
    meta = demo_samples() if DEMO and demo_ready() else None
    if not meta:
        return ""
    tiles = "".join(
        f'<form method="post" action="/demo/try" class="sample">{csrf_field(user)}'
        f'<input type="hidden" name="key" value="{esc(s["key"])}"><button type="submit" class="sample-btn">'
        f'<img src="/demo/thumb/{esc(s["key"])}" alt="" width="48" height="48" loading="lazy">'
        f'<span class="sample-text"><b>{esc(s["label"])}</b>'
        f'<span class="pill v-{esc(s.get("expect", "GREEN"))}">{esc(s.get("expect", ""))}</span></span></button></form>'
        for s in meta["samples"] if (DEMO_DIR / s["image"]).is_file())
    return (f'<section class="demo-try" aria-labelledby="demo-h"><div class="status-row"><h2 id="demo-h">Try a sample '
            f'slip</h2>{tip("About the samples", "Synthetic test images. One slip is on record as dispensed (" + esc(meta["reference"]["id"]) + "), so copies of it are recognised. The Printed slip is read by OCR (a recorded reading); for the others the medicines are filled in for you.")}</div>'
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
        note = (f'<div class="status-row">{chip("Filled from the clinic-signed code", "ok")}'
                f'{tip("About the signed code", "Remove any medicine the customer is not buying now.")}</div>')
    elif iss.get("status") == "invalid":
        note = f'<p class="warn">{esc(iss["message"])}</p>'
    issue_html = ""
    if issues:
        items = "".join(f'<li><label class="tick"><input type="checkbox" name="confirm" value="{i}" required>'
                        f'<span><b>{esc(x["item"])}</b> {chip(esc(x["issue"]), "warn")}</span></label>'
                        f'{crop_img(cid, x.get("crop"), x["item"])}</li>' for i, x in enumerate(issues))
        issue_html = (f'<div class="issues" role="group" aria-labelledby="issues-h"><h2 id="issues-h">Tick against the '
                      f'slip <span class="count">{len(issues)}</span></h2><p class="hint">Or correct the entry above.</p>'
                      f'<ul>{items}</ul></div>')
    v = pre["verdicts"]
    tier = lambda k: VERDICT_UI[v[k]][0].capitalize()   # noqa: E731
    pre_chips = (f'<span class="pill v-{v["routine"]}">Image: {tier("routine")}</span>' if v["routine"] == v["high"] else
                 f'<span class="pill v-{v["routine"]}">Routine: {tier("routine")}</span>'
                 f'<span class="pill v-{v["high"]}">High-risk: {tier("high")}</span>')
    if max(v.values(), key=r.ORDER.index) != "GREEN":
        pre_chips += tip("Why", esc(plain(pre.get("reason", ""))))
    pre_line = f'<div class="status-row pre-check">{pre_chips}</div>'
    err = f'<p class="warn">{esc(error)}</p>' if error else ""
    # which fields the reading filled in, and which of those will need a tick: shown on the fields themselves
    from_slip, to_tick = set(), set()
    if not fields and not signed and read.get("status") == "done":
        vals, to_tick = field_marks(read)
        from_slip = {k for k in vals if f.get(k) == vals[k]}
    mark = lambda k: (chip("check", "warn") if k in to_tick else chip("from slip", "src") if k in from_slip else "")  # noqa: E731
    return page("Enter medicines", f"""<div class="decision"><div><section class="card"><h1>What is the customer buying?</h1>
{pre_line}{note}{err}{"" if signed else ocr_panel(cid, read, issues)}{pdf_note(case)}
<form method="post" action="/case/{cid}/check" data-primary>{csrf_field(user)}
<label for="drugs">Medicines and strengths being sold {mark("drugs")}</label>
<div data-lookup="drug" data-multi><input type="text" id="drugs" name="drugs" value="{esc(f.get("drugs"))}"
 placeholder="e.g. Alprax 0.25, Dolo 650" required aria-describedby="drugs-hint"></div>
<p class="hint" id="drugs-hint">Suggestions give the name only; the strength comes from the slip.</p>
<div class="row"><div><label for="regn">Doctor's Regn No {mark("regn")}</label>
<div data-lookup="regn"><input type="text" id="regn" name="regn" value="{esc(f.get("regn"))}"
 aria-describedby="prescriber-hint"></div></div>
<div><label for="doctor">Doctor's name {mark("doctor")}</label><div data-lookup="doctor"><input type="text" id="doctor" name="doctor"
 value="{esc(f.get("doctor"))}" aria-describedby="prescriber-hint"></div></div>
<div><label for="rx_date">Date on the slip {mark("rx_date")}</label><input type="date" id="rx_date" name="rx_date" value="{esc(f.get("rx_date"))}"></div></div>
<p class="hint" id="prescriber-hint">As printed on the slip. A suggestion fills only its own field.</p>
<label for="patient">Patient name (optional)</label>
<div data-lookup="patient"><input type="text" id="patient" name="patient" value="{esc(f.get("patient"))}"
 autocomplete="off"></div>
{issue_html}<p><button type="submit" aria-keyshortcuts="Control+Enter Meta+Enter">Get decision</button>{kbd("Mod+Enter")}</p>
</form></section>
</div><div class="d-side"><section class="card"><h2>The slip</h2><img class="evidence" src="/img/{cid}" alt="Prescription image as received">{more_pages(cid, case)}</section></div></div>""", user)


def field_marks(read):
    """(values the reading fills in, the fields among them that will need a tick if sent unchanged)."""
    vals, ticks = ocr.values(read), set()
    for c in ocr.confirmations(read, vals):
        if any(c["item"] == d["text"] for d in read.get("drugs", [])):
            ticks.add("drugs")
        ticks.update(k for k in ("regn", "doctor", "rx_date") if (read.get(k) or {}).get("value") == c["item"])
    return vals, ticks


def pdf_note(case):
    n = case.get("pages") or 1
    if case.get("source") != "pdf":
        return ""
    return (f'<div class="status-row">{chip(f"PDF &middot; {n} page" + ("s" if n != 1 else ""))}'
            f'{tip("About PDFs", "PDF, " + str(n) + " page" + ("s" if n != 1 else "") + ". The image checks (seen before, edited copy) used page 1 only; the text in a PDF says what the file says, not who issued it.")}</div>')


def more_pages(cid, case):
    n = case.get("pages") or 1
    return "".join(f'<p class="muted">Page {i}</p><img class="evidence" src="/case/{cid}/page/{i}" '
                   f'alt="Page {i} of the PDF">' for i in range(2, n + 1))


def crop_img(cid, n, what):
    if not isinstance(n, int):
        return ""
    return f'<img class="crop" src="/case/{cid}/crop/{n}" alt="The part of the slip read as {esc(what)}">'


OCR_STATES = {   # state -> (label, pill class). One of these is always shown, so staff know what the form holds
    "reading": ("Reading", "s-quiet"),
    "ready": ("Ready", "v-GREEN"),
    "confirm": ("Needs confirmation", "v-AMBER"),
    "blocked": ("Check blocked", "v-RED"),
    "failed": ("Could not read", "s-quiet"),
    "disabled": ("OCR disabled", "s-quiet"),
}
OFF_REASONS = {
    "switched off": "Photo reading is switched off on this counter (RXCHECK_OCR=off).",
    "not installed": "Photo reading is not installed on this counter (see requirements-ocr.txt).",
    "demo": "Sample slips are filled in for you; the photo is not read.",
}


def ocr_state(read, issues=None):
    """reading | ready | confirm | blocked | failed | disabled. 'blocked' = the form was sent and some entries
    still need a tick; 'confirm' = what was filled in will need ticks if it is sent unchanged."""
    st = (read or {}).get("status")
    if issues:
        return "blocked"
    if st == "pending":
        return "reading"
    if st == "failed":
        return "failed"
    if st == "done":
        return "confirm" if ocr.confirmations(read, ocr.values(read)) else "ready"
    return "disabled"


def ocr_panel(cid, read, issues=None):
    """The reading's state as a badge and counts; unclear lines as cut-outs; the explanation on demand."""
    read = read or {}
    state = ocr_state(read, issues)
    label, cls = OCR_STATES[state]
    if state == "confirm":
        label += f" ({len(ocr.confirmations(read, ocr.values(read)))})"
    badge = f'<span class="pill {cls}" data-ocr-chip>{esc(label)}</span>'
    attrs = f'class="ocr" id="ocr" role="status" data-ocr-state="{state}"'
    if state == "reading":
        return (f'<div {attrs} data-ocr-pending="/case/{cid}/ocr"><div class="status-row"><span class="spin" '
                f'aria-hidden="true"></span>{badge}<span class="muted">Reading the slip - type meanwhile, nothing you type '
                f'is replaced</span></div><noscript><p><a href="/case/{cid}">Reload when read</a></p></noscript></div>')
    if state == "failed":
        return f'<div {attrs}><div class="status-row">{badge}<span class="muted">Type from the slip</span></div></div>'
    if state == "disabled" or (state == "blocked" and read.get("status") != "done"):
        why = OFF_REASONS.get(read.get("why"), "The slip was not read for this check.")
        extra = chip(f"{len(issues)} to tick", "warn") if state == "blocked" else ""
        return f'<div {attrs}><div class="status-row">{badge}{extra}{tip("Why", esc(why))}</div></div>'
    n_fill = len(read.get("drugs", [])) + sum(1 for k in ("regn", "doctor", "rx_date") if read.get(k))
    n_tick = len(issues) if state == "blocked" else len(ocr.confirmations(read, ocr.values(read)))
    unclear = read.get("unclear", [])
    pages = read.get("pages") or 1
    counts = (chip(f"{n_fill} filled", "src") + (chip(f"{n_tick} to tick", "warn") if n_tick else "")
              + (chip(f"{len(unclear)} unclear") if unclear else "") + (chip(f"{pages} pages") if pages > 1 else ""))
    replay = "recorded" in (read.get("engine") or "")
    about = ("<b>Recorded reading.</b> This public demo has no OCR engine, so this sample replays what the "
             "OCR engine read from this same image on a PC. What is filled in, what needs a tick and the check run here, "
             "live. " if replay else "") + ("Read from the slip and filled into empty fields only. Marked fields need a "
                                           "tick against the slip when you continue.")
    unread = (chip(f'page {", ".join(str(p) for p in read["unread"])} not read', "warn") if read.get("unread") else "")
    head = (f'<div class="status-row">{badge}{counts}{chip("Replay") if replay else ""}{unread}'
            f'{tip("Read from the slip", about)}</div>')
    if not n_fill:
        head += '<p class="hint">Nothing could be filled in - type from the slip.</p>'
    # an unclear line: its cut-out, and what the engine made of it, typos and all
    items = "".join(f'<li>{crop_img(cid, u.get("crop"), u["text"])}<span>{chip("not filled - type it", "warn")}'
                    f'<span class="muted read-as">read as &ldquo;{esc(u.get("raw") or u["text"])}&rdquo;</span></span></li>'
                    for u in unclear)
    return f'<div {attrs}>{head}{f"<ul class=ocr-list>{items}</ul>" if items else ""}</div>'


# ---------------------------------------------------------------- what was checked, area by area
# The verdict is one word; these are the separate questions behind it. An edited image, a re-sent image and a
# high-risk medicine are different signals and are shown apart, each with what is NOT known.

IMAGE_LINES = ("IMAGE UNUSABLE", "COPY of", "SAME SLIP", "VERY SIMILAR", "SEEN BEFORE", "Exact same image file",
               "SEEN AT ANOTHER PHARMACY", "Differs from the slip at the other", "Not seen before", "Not seen at other",
               "Shared index")
FILE_LINES = ("Generator/AI", "C2PA", "No EXIF", "No camera data", "Image file is dated", "Saved at JPEG")
PRESCRIBER_LINES = ("ISSUANCE CODE FAILED", "Regn No", "Signed ", "NO CLINIC CODE", "Prescription date", "QR code on")
MEDICINE_LINES = ("High-risk", "Not in drug-risk list", "Drug-risk list", "Confirm entry", "Not in the clinic-signed",
                  "Differs from record of")
CHECK_STATUS = {"ok": ("OK", "v-GREEN"), "finding": ("Finding", "v-RED"), "unverified": ("Not verified", "v-AMBER"),
                "seen": ("Seen before", "v-AMBER"), "new": ("Not seen before", "s-quiet"),
                "unchecked": ("Not checked", "s-quiet"), "high": ("High-risk", "v-AMBER")}


def explain(rep):
    """[{'area', 'status', 'finding', 'evidence': [...], 'unknown'}] for the image, the prescriber and issuance, and
    the medicines - from the check's own flags and lines, nothing inferred beyond them."""
    f, m = rep["flags"], rep.get("match") or {}
    lines = rep["findings"] + rep["notes"]
    pick = lambda prefixes: [x for x in lines if x.startswith(prefixes)]   # noqa: E731
    img, file_notes = pick(IMAGE_LINES), pick(FILE_LINES)
    if f.get("unusable"):
        image = ("finding", img[0] if img else "The image could not be used for comparison",
                 "Nothing about the slip itself was compared. Retake the photo of the paper slip.")
    elif f.get("edited"):
        image = ("finding", img[0], "Who changed it, and why. A doctor may correct a slip by hand; then the clinic can "
                                    "say so. An edit is a reason to confirm, not proof of fraud.")
    elif m.get("identity") == "similar":
        image = ("unverified", img[0], "Whether the doctor rewrote the slip or someone redrew a copy: only the clinic can "
                                       "tell.")
    elif m.get("identity") == "copy" or (f.get("repeat") and m.get("identity") != "seen" and img):
        image = ("seen", img[0], "Whether this repeat is allowed: the prescription's repeats and the H1 register decide.")
    elif m.get("identity") == "seen" or any(x.startswith(("SEEN BEFORE", "SEEN AT ANOTHER", "Exact same")) for x in img):
        image = ("seen", img[0], "No image was kept for the earlier check, so changes since then cannot be located.")
    else:
        image = ("new", "No record of this slip here to compare with",
                 "Only slips checked here (and at member pharmacies, if joined) are compared: a slip used anywhere else "
                 "looks new. A new slip is not a verified one.")
    image_ev = [x for x in img if x != image[1]] + (["File: " + "; ".join(file_notes) + " (supporting only)"]
                                                    if file_notes else [])

    iss = (rep.get("issuance") or {}).get("status", "none")
    pres = f.get("prescriber")
    if iss == "invalid" or pres == "invalid" or f.get("code_missing"):
        p_status = "finding"
    elif iss == "verified" and pres == "valid":
        p_status = "ok"
    elif pres == "unknown" and iss != "verified":
        p_status = "unchecked"
    elif pres == "valid":
        p_status = "ok"
    else:
        p_status = "unverified"
    p_lines = pick(PRESCRIBER_LINES)
    p_find = next((x for x in rep["findings"] if x in p_lines), None) or (
        "Clinic-signed code verified" if iss == "verified" else rep.get("prescriber_message") or "Regn No not entered")
    unknown = [] if iss == "verified" else ["Who wrote this slip: a Regn No in the registry proves the doctor exists, "
                                            "not that they wrote it. Only a clinic-signed code or the doctor's own answer "
                                            "settles that."]
    if pres == "unverified":
        unknown.append("'Not verified' is not 'invalid': the local registry lists only some doctors.")
    prescriber = (p_status, p_find, " ".join(unknown) or "Nothing: the clinic's signature covers who issued it.")
    p_ev = [x for x in p_lines + [(rep.get("issuance") or {}).get("message") or ""] if x and x != p_find]

    items = rep.get("items") or []
    no_override = set(rep.get("fail_closed") or [])
    med_lines = pick(MEDICINE_LINES)
    differs = [x for x in rep["findings"] if x.startswith(("Differs from record of", "Not in the clinic-signed"))]
    tiers = [f'{i["item"]}: ' + ("high-risk, NDPS / Schedule X - no override" if i["item"] in no_override
                                 else "high-risk" if i["risk"] == "high" else "routine") for i in items]
    if differs:
        med = ("finding", differs[0])
    elif any(i["risk"] == "high" for i in items):
        med = ("high", "; ".join(tiers))
    elif items:
        med = ("ok", "; ".join(tiers))
    else:
        med = ("unchecked", "No medicines entered yet")
    med_unknown = ("Whether the medicine suits this patient: no interaction, allergy or dose checks. Names not on the "
                   "drug list count as routine unless they look like a controlled medicine.")
    med_ev = [x for x in differs[1:] + med_lines if not x.startswith("Differs") or x not in differs[:1]]
    if differs:
        med_ev = tiers + med_ev
    return [{"area": "Image", "status": image[0], "finding": image[1], "evidence": image_ev, "unknown": image[2]},
            {"area": "Prescriber and issuance", "status": prescriber[0], "finding": prescriber[1], "evidence": p_ev,
             "unknown": prescriber[2]},
            {"area": "Medicines", "status": med[0], "finding": med[1], "evidence": med_ev, "unknown": med_unknown}]


CHECK_ICON = {"ok": "&#x2713;", "finding": "&#x2715;", "unverified": "!", "seen": "&#x21bb;", "new": "&ndash;",
              "unchecked": "&ndash;", "high": "!"}


def explain_card(rep):
    """Three rows - image, prescriber and issuance, medicines: an icon, a status and the finding at a glance;
    the evidence and what is not known when a row is opened (open already when it is a finding)."""
    rows = []
    for a in explain(rep):
        label, cls = CHECK_STATUS[a["status"]]
        ev = "".join(f"<li>{esc(plain(x))}</li>" for x in a["evidence"])
        rows.append(f'<details class="chk" data-check="{esc(a["area"])}" data-status="{a["status"]}"'
                    f'{" open" if a["status"] == "finding" else ""}><summary><span class="ico {cls}" aria-hidden="true">'
                    f'{CHECK_ICON[a["status"]]}</span><span class="area">{esc(a["area"])}</span>'
                    f'<span class="pill {cls}">{label}</span><span class="find">{esc(plain(a["finding"]))}</span></summary>'
                    f'<div class="chk-body">' + (f'<ul class="ev">{ev}</ul>' if ev else "")
                    + f'<p class="unknown"><b>Not known:</b> {esc(a["unknown"])}</p></div></details>')
    return (f'<section class="card checks" aria-labelledby="checks-h"><h2 id="checks-h">What was checked</h2>'
            f'{"".join(rows)}</section>')


def decision_page(user, cid, case, error="", notice=""):
    rep = case["final"]
    worst = max(rep["verdicts"].values(), key=r.ORDER.index)
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
    parts = [verdict_block(worst, rep["reason"], split + todo + err), by, explain_card(rep)]

    # hold actions come before the evidence: they are what the pharmacist does next
    hid = case.get("hold")
    if hid:
        row = r.db().execute("SELECT status FROM holds WHERE id = ?", (hid,)).fetchone()
        status = row["status"] if row else "closed"
        if status != "closed":
            path = rep.get("path") or "call"
            parts.append(hold_meta(user, cid, hid, status, path))
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
        ev = f'<img class="evidence" src="/img/{cid}" alt="Prescription image as received">{more_pages(cid, case)}'
    evidence = f'<section class="card evidence-card"><h2>Evidence</h2>{pdf_note(case)}{ev}</section>'

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
        return (f'<div class="status-row">{chip("Doctor app: not connected")}'
                f'{tip("Doctor confirmation", "Doctor confirmation is not connected on this counter (rxcheck doctor trust). Call the clinic on the registry number.")}</div>'
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


FOLLOW_UP_MIN = 120   # an unanswered call: try again after this long (the hold stays 'waiting', never auto-approved)


def hold_state(hid):
    """Owner, attempts, next follow-up and outcome of a hold - all from the audit trail."""
    rows = r.db().execute("SELECT at, actor, action, detail FROM audit WHERE ref = ? AND action IN "
                          "('hold', 'hold-assign', 'resolve', 'doctor-ask', 'doctor-receipt') ORDER BY seq",
                          (hid,)).fetchall()
    st = {"opened_by": None, "owner": None, "tries": 0, "last_try": None, "outcome": None, "closed_by": None,
          "closed_at": None, "doctor": None}
    for x in rows:
        d = detail_of(x)
        if x["action"] == "hold":
            st["opened_by"] = st["owner"] = x["actor"]
        elif x["action"] == "hold-assign":
            st["owner"] = x["actor"]
        elif x["action"] == "doctor-ask":
            st["tries"] += 1
            st["last_try"] = x["at"]
        elif x["action"] == "doctor-receipt":
            st["doctor"] = d.get("decision")
        elif x["action"] == "resolve":
            if d.get("outcome") == "unreachable":
                st["tries"] += 1
                st["last_try"] = x["at"]
            else:
                st["outcome"], st["closed_by"], st["closed_at"] = d.get("outcome"), x["actor"], x["at"]
    return st


def follow_up(st, status, path):
    """(when, text): what happens next with this hold, and by when."""
    if status == "closed":
        return None, ""
    if path != "call":
        return None, {"retake": "retake the photo now", "counter": "check at the counter now"}.get(path, "now")
    if not st["last_try"]:
        return None, "call the clinic now (registry number)"
    due = datetime.fromisoformat(st["last_try"]) + timedelta(minutes=FOLLOW_UP_MIN)
    late = datetime.now() >= due
    return due, (f"OVERDUE since {due:%H:%M}" if late else f"call again at {due:%H:%M}") + \
        f" ({st['tries']} attempt{'s' if st['tries'] != 1 else ''} so far)"


def hold_meta(user, cid, hid, status, path):
    """Owner and next step as chips, with a button to take the hold."""
    st = hold_state(hid)
    due, nxt = follow_up(st, status, path)
    late = due is not None and datetime.now() >= due
    take = ("" if st["owner"] == user["name"] or status == "closed" else
            f'<form class="inline" method="post" action="/hold/{hid}/take">{csrf_field(user)}'
            f'<input type="hidden" name="case" value="{esc(cid)}">'
            f'<button type="submit" class="secondary small">Take this hold</button></form>')
    return (f'<div class="status-row hold-meta">{chip("Owner: <b>" + esc(st["owner"] or "-") + "</b>")}'
            + (chip("Next: " + esc(nxt), "warn" if late or "now" in nxt else "") if nxt else "") + f'{take}</div>')


def hold_history(hid):
    """Who opened the hold and who recorded each outcome, oldest first - from the audit trail."""
    rows = r.db().execute("SELECT at, actor, action, detail FROM audit WHERE ref = ? AND action IN ('hold', 'hold-assign', "
                          "'resolve', 'doctor-ask', 'doctor-receipt') ORDER BY seq", (hid,)).fetchall()
    items = []
    for x in rows:
        d = detail_of(x)
        if x["action"] == "hold":
            what = "Hold opened"
        elif x["action"] == "hold-assign":
            what = "Took the hold"
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
                 f'<label for="note">Note</label><input type="text" id="note" name="note" '
                 f'placeholder="Who you spoke to, number called - no patient details">'
                 f'{fields}<div class="actions">{"".join(actions)}</div></form>')
    override = ""
    no_override = r.hold_fail_closed(hid)
    if denied:
        override = ""
    elif ph and no_override:
        override = (f'<div class="status-row">{chip("No override: " + esc(", ".join(no_override)), "warn")}'
                    f'{tip("Why no override", "NDPS / Schedule X (or the drug list is unusable): dispensed only when the clinic confirms or a counter check settles the warning. If the clinic cannot be reached, hand over the other items; the customer can return.")}</div>')
    elif ph:
        override = (f'<details class="override"><summary>Dispense without this step (override)</summary>'
                    f'<form method="post" action="/hold/{esc(hid)}/resolve">{csrf_field(user)}'
                    f'<input type="hidden" name="case" value="{cid}"><input type="hidden" name="reason" value="other">'
                    f'<p class="muted">Recorded under your name, kept permanently in the audit trail and counted in '
                    f'the override rate. Say why, without patient names or health details.</p>'
                    f'<label for="onote">Reason</label><input type="text" id="onote" name="note" required minlength="10">'
                    f'<p>{btn("dispensed-after-review", "Dispense anyway", "secondary")}</p></form></details>')
    only = "" if ph else f'<div class="status-row">{chip("Pharmacist decides")}{tip("Who decides", "A pharmacist records confirmed, checked - dispense and overrides.")}</div>'
    waiting = f'<div class="status-row">{chip("Waiting for a call-back", "warn")}</div>' if status == "waiting" else ""
    return (f'<section class="card"><h2>Hold {esc(hid)}</h2>{waiting}{extra}{doctor_html}{form_main}{override}{only}'
            f'{hold_history(hid)}</section>')


def hold_reason(h, cid):
    findings = json.loads(h["findings"])
    why = findings[0] if findings else ""
    if not why and CASE_ID.match(cid):   # a new-slip hold has no finding, only the decision's reason
        try:
            why = (load_case(cid)[1].get("final") or {}).get("reason") or ""
        except Exception:
            why = ""
    return why


def holds_page(user):
    """Open holds, oldest first, each with who owns it, what was tried and what happens next; then the holds closed
    in the last day with their outcome."""
    out = []
    for h in r.db().execute("SELECT * FROM holds WHERE status != 'closed' ORDER BY created").fetchall():
        worst = max(json.loads(h["verdicts"]).values(), key=r.ORDER.index)
        cid = Path(h["image"]).stem
        link = f'<a href="/case/{cid}">open</a>' if CASE_ID.match(cid) else esc(Path(h["image"]).name)
        st = hold_state(h["id"])
        due, nxt = follow_up(st, h["status"], r.hold_info(h["id"])[0] or "call")
        late = due is not None and datetime.now() >= due
        out.append(f'<tr{" class=late" if late else ""}><th scope="row">{esc(h["id"])}<br><span class="muted">'
                   f'{esc(when(h["created"]))}</span></th><td><span class="pill v-{worst}">{worst}</span> '
                   f'{esc(plain(hold_reason(h, cid)))}</td><td>{esc(st["owner"] or "-")}</td>'
                   f'<td>{esc(nxt)}</td><td>{link}</td></tr>')
    open_t = ('<table><thead><tr><th scope="col">Hold, opened</th><th scope="col">Why it is held</th>'
              '<th scope="col">Owner</th><th scope="col">Next step</th><th scope="col"></th></tr></thead><tbody>'
              + "".join(out) + "</tbody></table>") if out else '<p class="muted">No open holds.</p>'
    since = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
    done = []
    for h in r.db().execute("SELECT * FROM holds WHERE status = 'closed' ORDER BY created DESC").fetchall():
        st = hold_state(h["id"])
        if not st["closed_at"] or st["closed_at"] < since:
            continue
        cid = Path(h["image"]).stem
        done.append(f'<tr><th scope="row">{esc(h["id"])}</th><td>{esc(plain(hold_reason(h, cid)))}</td>'
                    f'<td>{esc(OUTCOME_LABELS.get(st["outcome"], st["outcome"] or ""))}</td>'
                    f'<td>{esc(st["closed_by"] or "")}, {esc(when(st["closed_at"]))}</td>'
                    f'<td>{f"<a href=/case/{cid}>open</a>" if CASE_ID.match(cid) else ""}</td></tr>')
    closed_t = ('<h2>Closed in the last 24 hours</h2><table><thead><tr><th scope="col">Hold</th><th scope="col">Why it was '
                'held</th><th scope="col">Outcome</th><th scope="col">By, when</th><th scope="col"></th></tr></thead>'
                '<tbody>' + "".join(done) + '</tbody></table>') if done else ""
    return page("Holds", f'<section class="card"><h1>Open holds (oldest first)</h1>'
                         f'<p class="muted">An unanswered call is tried again after {FOLLOW_UP_MIN // 60} hours; overdue '
                         f'holds are marked. Anyone can take a hold; the history shows who did what.</p>{open_t}</section>'
                         f'{f"<section class=card>{closed_t}</section>" if closed_t else ""}{activity_panel(user)}'
                         f'<p><a href="/" data-shortcut="next">Back to the counter</a></p>', user, refresh=60)


# ---------------------------------------------------------------- audit log (pharmacists: compliance review)

ACTION_LABELS = {
    "check": "Check", "entry-confirmed": "Entry confirmed against the slip", "ocr-use": "Photo reading used",
    "hold": "Hold opened", "hold-assign": "Hold taken",
    "resolve": "Hold outcome", "doctor-ask": "Doctor asked", "doctor-receipt": "Doctor's answer", "login": "Signed in", "login-failed": "Wrong PIN", "login-locked": "Sign-in locked",
    "add": "Recorded in the ledger", "index": "Indexed (no image)", "index-update": "Index updated",
    "migrate": "Migrated", "staff-add": "Staff added", "staff-disable": "Staff disabled", "staff-pin": "PIN changed",
    "backup": "Backup", "restore": "Restore", "data-import": "Data import", "audit-export": "Audit log exported",
}
AUDIT_GROUPS = {   # value -> (label, actions); "overrides" is a filter on resolve rows
    "checks": ("Checks and entry confirmations", ("check", "entry-confirmed", "ocr-use")),
    "holds": ("Holds and their outcomes", ("hold", "hold-assign", "resolve", "doctor-ask", "doctor-receipt")),
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


OCR_REPORT_DAYS = (7, 14, 30, 90)


def ocr_rows(days):
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    return [(row["at"], row["actor"], row["ref"], json.loads(row["detail"] or "{}")) for row in r.db().execute(
        "SELECT at, actor, ref, detail FROM audit WHERE action = 'ocr-use' AND at >= ? ORDER BY seq", (since,))]


def pct(a, b):
    return f"{100 * a / b:.0f}%" if b else "-"


def ocr_report_page(user, days):
    """Pilot figures for the photo reading: acceptance and correction per field and confidence band, from the
    audit trail (counts only). This is what FILL_MIN / CONFIRM_ABOVE in app/slip_reader.py should be set from."""
    rep = ocr.report([d for *_, d in ocr_rows(days)])
    few = 30
    rows = "".join(
        f'<tr><th scope="row">{esc({"medicine": "Medicine", "regn": "Regn No", "doctor": "Doctor", "rx_date": "Date"}.get(t["field"], t["field"]))}</th>'
        f'<td>{esc(t["band"])}</td><td>{t["filled"]}</td><td>{t["kept"]} ({pct(t["kept"], t["filled"])})</td>'
        f'<td>{t["filled"] - t["kept"]} ({pct(t["filled"] - t["kept"], t["filled"])})</td><td>{t["ticked"]}</td>'
        f'<td>{t["unclear"]}</td><td>{t["unclear_right"]} ({pct(t["unclear_right"], t["unclear"])})</td>'
        f'<td>{"few - not yet a basis for a threshold" if t["filled"] + t["unclear"] < few else ""}</td></tr>'
        for t in rep["table"])
    table = (f'<table><thead><tr><th scope="col">Field</th><th scope="col">Confidence</th>'
             f'<th scope="col">Filled in</th><th scope="col">Kept as read (acceptance)</th>'
             f'<th scope="col">Changed by staff (correction)</th><th scope="col">Needed a tick</th>'
             f'<th scope="col">Unclear, not filled in</th><th scope="col">...staff typed what was read</th>'
             f'<th scope="col"></th></tr></thead><tbody>{rows}</tbody></table>' if rows else
             '<p class="muted">No photo readings recorded in this period.</p>')
    choose = " · ".join(f'<a href="/ocr-report?days={d}"' + (' aria-current="page"' if d == days else "") + f'>{d} days</a>'
                        for d in OCR_REPORT_DAYS)
    median = f'{rep["median_ms"] / 1000:.1f} s' if rep["median_ms"] else "-"
    return page("Photo reading report", f"""<section class="card"><h1>Photo reading: pilot figures</h1>
<p class="muted">Last {days} days: {choose}. From the audit trail, counts only - no slip text, no patient.</p>
<div class="today"><div><b>{rep["checks"]}</b>checks with the slip read</div>
<div class="t-green"><b>{rep["done"]}</b>read before the form was sent</div>
<div class="t-amber"><b>{rep["pending"]}</b>sent while still reading</div>
<div class="t-red"><b>{rep["failed"]}</b>could not be read</div>
<div><b>{median}</b>median reading time</div></div>
<h2>Acceptance by field and confidence</h2>{table}
<p class="hint">Acceptance = filled-in values staff sent unchanged; correction = values they changed or removed. A
low acceptance in a band means values there should not be filled in, or should need a tick (FILL_MIN and
CONFIRM_ABOVE in app/slip_reader.py). Unclear readings that staff typed exactly as read suggest a band could be filled in.
Small counts mean little.</p>
<p><a href="/ocr-report.csv?days={days}" download>Download one row per reading as CSV</a></p></section>
<p><a href="/audit">Audit log</a> · <a href="/" data-shortcut="next">Back to the counter</a></p>""", user)


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
<p><a href="/audit.csv" download>Download the full log as CSV</a> · <a href="/ocr-report">Photo reading report</a></p>{table}</div></section>
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
        if method == "GET" and parts in (["ocr-report"], ["ocr-report.csv"]):
            if u["role"] != "pharmacist":
                raise HttpError(403, "Only a pharmacist can open the photo reading report")
            q = self.query("days")
            days = int(q) if q.isdigit() and 1 <= int(q) <= 366 else 14
            if parts == ["ocr-report"]:
                return self.send(200, ocr_report_page(u, days))
            return self.ocr_csv(days)
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
        if method == "GET" and len(parts) == 3 and parts[:2] == ["demo", "thumb"] and DEMO and demo_ready():
            sample = next((x for x in (demo_samples() or {}).get("samples", []) if x.get("key") == parts[2]), None)
            src = DEMO_DIR / sample["image"] if sample and Path(sample["image"]).name == sample["image"] else None
            if not src or not src.is_file():
                raise HttpError(404, "No such sample")
            img = r.read_bgr(src)
            cv = r.cv2   # the engine's OpenCV; the server itself stays standard library
            img = cv.resize(img, (360, int(img.shape[0] * 360 / img.shape[1])), interpolation=cv.INTER_AREA)
            return self.send(200, cv.imencode(".jpg", img, [cv.IMWRITE_JPEG_QUALITY, 80])[1].tobytes(), "image/jpeg",
                             cache="private, max-age=3600")
        if method == "POST" and parts == ["demo", "try"] and DEMO and demo_ready():
            key = self.field("key")
            sample = next((s for s in (demo_samples() or {}).get("samples", []) if s.get("key") == key), None)
            src = DEMO_DIR / sample["image"] if sample else None
            if not src or Path(sample["image"]).name != sample["image"] or not src.is_file():
                raise HttpError(404, "That sample is not available")
            cid = new_case(src.read_bytes(), src.suffix.lower(), f"demo:{key}", recorded=sample.get("reading"))
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
        if len(parts) == 3 and parts[0] == "hold" and parts[2] == "take" and method == "POST":
            row = r.db().execute("SELECT status FROM holds WHERE id = ?", (parts[1],)).fetchone()
            if not row:
                raise HttpError(404, "No such hold")
            if row["status"] == "closed":
                raise HttpError(409, "This hold is already closed")
            con = r.db()
            with con:
                r.audit(con, "hold-assign", parts[1], {"owner": u["name"]})
            back = self.field("case")
            return self.redirect(f"/case/{back}" if CASE_ID.match(back or "") else "/holds")
        if len(parts) == 4 and parts[0] == "hold" and parts[2] == "doctor" and method == "POST":
            return self.doctor(parts[1], parts[3])
        raise HttpError(404, "Page not found")

    def ocr_csv(self, days):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["at", "actor", "case", "status", "engine", "read_ms", "field", "confidence_band", "name_on_file",
                    "high_risk", "filled_in", "needed_tick", "kept_as_read"])
        n = 0
        yn = {True: "yes", False: "no", None: ""}
        for at, actor, ref, d in ocr_rows(days):
            for x in d.get("readings") or [None]:
                x = x or {}
                w.writerow([csv_cell(v) for v in (at, actor, ref, d.get("status", "done"), d.get("engine"), d.get("ms"),
                                                  x.get("field"), x.get("band"), yn[x.get("known")], yn[x.get("high")],
                                                  yn[x.get("filled")], yn[x.get("tick")], yn[x.get("kept")])])
                n += 1
        con = r.db()
        with con:
            r.audit(con, "audit-export", None, {"what": "ocr-report", "days": days, "rows": n})
        name = f"rxcheck-ocr-{datetime.now():%Y%m%d-%H%M}.csv"
        return self.send(200, "﻿" + buf.getvalue(), "text/csv; charset=utf-8",
                         [("Content-Disposition", f'attachment; filename="{name}"')])

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
                                        "ticks": sorted(field_marks(read)[1]), "panel": ocr_panel(cid, read)})
        if method == "GET" and len(rest) == 2 and rest[0] == "crop" and rest[1].isdigit():
            crops = (case.get("ocr") or {}).get("crops") or []
            if int(rest[1]) >= len(crops):
                raise HttpError(404, "No such part of the slip")
            c = crops[int(rest[1])]
            try:
                return self.send(200, ocr.crop_jpeg(page_path(cid, c["page"], img), c["box"]), "image/jpeg")
            except (ValueError, OSError):
                raise HttpError(404, "No such part of the slip")
        if method == "GET" and len(rest) == 2 and rest[0] == "page" and rest[1].isdigit():
            n = int(rest[1])
            p = page_path(cid, n - 1, img) if 1 <= n <= (case.get("pages") or 1) else None
            if not p or not p.is_file():
                raise HttpError(404, "No such page")
            return self.send(200, p.read_bytes(), "image/jpeg")
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
                    if use:   # what the reader filled in (no patient), beside the counts: what was read vs sent
                        r.audit(con, "ocr-use", cid, {**use, "read": ocr.values(case.get("ocr"))})
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
