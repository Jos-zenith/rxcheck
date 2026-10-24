"""Cloud deployment: what may enter the container image, first start on an empty disk, the first pharmacist
account, and the counter app behind a cloud host's HTTPS proxy.

Run:  python tests/test_deployment.py
"""
import fnmatch
import http.client
import json
import os
import time
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
os.environ.setdefault("RXCHECK_OCR", "off")   # uploads here test deployment, not the photo reading
import rxcheck as r  # noqa: E402
import counter_app as app  # noqa: E402

ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not cond else ""))


# ---- the image can only contain code and default data: never the real prescription in samples/ or ledger/
def docker_included(path, patterns):
    keep = True
    for pat in patterns:   # .dockerignore semantics: the last matching pattern wins, "!" re-includes
        neg = pat.startswith("!")
        if fnmatch.fnmatch(path, pat.lstrip("!")):
            keep = neg
    return keep


patterns = [l.strip() for l in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.startswith("#")]
tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
files = sorted(set(tracked) | {p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*") if p.is_file()
                               and ".git" not in p.parts})
inside = [f for f in files if docker_included(f, patterns)]
check(f"image build context is code + default data only ({', '.join(inside)})",
      set(inside) == {"requirements.txt", "requirements-ocr.txt", "app/rxcheck.py", "app/counter_app.py", "app/shared_index.py", "app/doctor_confirmation.py",
                      "app/slip_reader.py", "app/pdf_pages.py",
                      "docker-entrypoint.sh",
                      "data/drug_risk.json", "data/policy.json", "data/registry.json",
                      "static/app.js", "static/app.css",
                      "demo/samples.json", "demo/reference.jpg", "demo/edited.jpg", "demo/resent.jpg", "demo/new.jpg",
                      "demo/printed.jpg", "demo/slip-reader-scores.json"})
check("no image, ledger, report or key file can enter the image (only the synthetic demo slips)",
      not [f for f in inside if f.split("/")[0] in ("samples", "ledger", "inbox", "reports", "eval")
           or (f.endswith((".jpg", ".png")) and not f.startswith("demo/")) or f.endswith((".db", ".pem"))])
# every image in demo/ must be what the synthetic generator makes - a real photo can never ship as a "sample"
import cv2  # noqa: E402
import numpy as np  # noqa: E402
from synthetic_slips import demo_slips  # noqa: E402
made = demo_slips()
shipped = sorted(p.name for p in (ROOT / "demo").glob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))
diffs = {}
for name in shipped:
    img = cv2.imdecode(np.fromfile(str(ROOT / "demo" / name), np.uint8), cv2.IMREAD_COLOR)
    ref = made.get(Path(name).stem)
    diffs[name] = None if ref is None or img is None or img.shape != ref.shape else \
        float(np.abs(img.astype(np.int16) - ref.astype(np.int16)).mean())
check("demo/ images are exactly the synthetic generator's output (tests/build_demo_samples.py)",
      shipped == sorted(f"{k}.jpg" for k in made) and all(d is not None and d < 2.0 for d in diffs.values()), str(diffs))
dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
render = (ROOT / "render.yaml").read_text(encoding="utf-8")
check("Dockerfile: demo mode on, state on the disk, served behind the host's HTTPS proxy",
      "RXCHECK_DEMO=1" in dockerfile and "RXCHECK_HOME=/var/rxcheck" in dockerfile and '"--behind-proxy"' in dockerfile)
check("render.yaml: disk mounted at RXCHECK_HOME, health check on /healthz, admin PIN not in the file",
      "mountPath: /var/rxcheck" in render and "healthCheckPath: /healthz" in render
      and re.search(r"RXCHECK_ADMIN_PIN.*\n\s+sync: false", render) is not None)
check("entrypoint script has Unix line endings", b"\r\n" not in (ROOT / "docker-entrypoint.sh").read_bytes())

with tempfile.TemporaryDirectory() as t:
    tmp = Path(t)
    # ---- first start on an empty disk
    r.HOME = tmp
    r.LEDGER_DIR, r.DB, r.FEATURES, r.REPORTS = tmp / "ledger", tmp / "ledger" / "rxcheck.db", tmp / "ledger" / "features", tmp / "reports"
    r.REGISTRY, r.DRUG_RISK, r.POLICY = (tmp / "data" / n for n in ("registry.json", "drug_risk.json", "policy.json"))
    first = r.seed_home()
    (tmp / "data" / "policy.json").write_text('{"confirm_new_high_risk": false}', encoding="utf-8")
    again = r.seed_home()
    check("empty disk seeded with the default data files once; later edits are never overwritten",
          sorted(first) == ["drug_risk.json", "policy.json", "registry.json"] and again == []
          and r.policy()["confirm_new_high_risk"] is False)

    # ---- the first pharmacist comes from environment variables, once
    os.environ.update(RXCHECK_ADMIN_NAME="admin", RXCHECK_ADMIN_PIN="12345")
    try:
        app.bootstrap_admin()
        check("a PIN shorter than 6 digits is refused", False)
    except ValueError:
        check("a PIN shorter than 6 digits is refused", True)
    os.environ["RXCHECK_ADMIN_PIN"] = "246813"
    created = app.bootstrap_admin()
    os.environ.update(RXCHECK_ADMIN_NAME="someone-else", RXCHECK_ADMIN_PIN="999999")
    app.bootstrap_admin()
    names = [x[0] for x in r.db().execute("SELECT name FROM staff")]
    check("first pharmacist created from the environment; changing the variables later creates nobody",
          created and names == ["admin"])
    for k in ("RXCHECK_ADMIN_NAME", "RXCHECK_ADMIN_PIN"):
        os.environ.pop(k)
    r.close_db()

    # ---- behind the cloud host's HTTPS proxy, in demo mode
    app.DEMO = True
    srv = app.make_server("127.0.0.1", 0, inbox=tmp / "inbox", behind_proxy=True)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def req(method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        c.request(method, path, body=body, headers=headers or {})
        res = c.getresponse()
        data = res.read().decode("utf-8", "replace")
        c.close()
        return res.status, res.getheader("Set-Cookie") or "", data

    s, _, body = req("GET", "/healthz")
    check("/healthz answers without sign-in and reveals nothing", s == 200 and body == "ok")
    s, _, body = req("GET", "/login")
    check("demo banner on every page", "DEMO DEPLOYMENT" in body and "Do not upload real prescriptions" in body)
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    s, cookie, _ = req("POST", "/login", urlencode({"name": "admin", "pin": "246813"}),
                       dict(form, **{"X-Forwarded-For": "6.6.6.6, 203.0.113.9"}))
    check("session cookie is Secure, HttpOnly, SameSite=Strict behind the HTTPS proxy",
          s == 303 and all(x in cookie for x in ("Secure", "HttpOnly", "SameSite=Strict")), cookie)
    ip = json.loads(r.db().execute("SELECT detail FROM audit WHERE action = 'login'").fetchone()[0])["ip"]
    check("audit records the address the proxy saw, not a forged earlier entry", ip == "203.0.113.9", ip)
    statuses = [req("POST", "/login", urlencode({"name": "admin", "pin": "000000"}),
                    dict(form, **{"X-Forwarded-For": f"198.51.100.{i}"}))[0] for i in range(6)]
    check("PIN guessing from rotating addresses is still locked out after 5 tries", statuses == [401] * 5 + [429],
          statuses)

    # ---- public demo: open the link and use the counter - no account; one-click synthetic sample slips
    def req2(method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
        c.request(method, path, body=body, headers=headers or {})
        res = c.getresponse()
        data = res.read().decode("utf-8", "replace")
        c.close()
        return res.status, res.getheader("Location") or "", res.getheader("Set-Cookie") or "", data

    s, loc, _, _ = req2("GET", "/")
    check("demo mode without the demo prepared still asks for sign-in", s == 303 and loc == "/login")
    app.prepare_demo()
    app.prepare_demo()   # a restart: nothing duplicated
    n_demo = r.db().execute("SELECT COUNT(*) FROM staff WHERE name = ?", (app.DEMO_USER,)).fetchone()[0]
    n_ref = r.db().execute("SELECT COUNT(*) FROM entries WHERE id = 'RX-DEMO-0812'").fetchone()[0]
    check("demo prepared once: one 'Demo visitor' account, the synthetic reference slip on record", (n_demo, n_ref) == (1, 1))
    s, _, cookie, body = req2("GET", "/")
    tok = re.search(r"rxs=([^;]+)", cookie)
    check("opening the link signs the visitor in as 'Demo visitor' - no PIN, cookie Secure/HttpOnly/Strict",
          s == 200 and tok is not None and "Demo visitor" in body and "Try it with a sample slip" in body
          and all(x in cookie for x in ("Secure", "HttpOnly", "SameSite=Strict")), cookie)
    s, _, _, _ = req2("POST", "/login", urlencode({"name": app.DEMO_USER, "pin": "000000000000"}), form)
    check("nobody can sign in as 'Demo visitor' with a PIN", s == 401)
    h = {"Cookie": f"rxs={tok.group(1)}", **form}
    csrf = re.search(r'name="csrf" value="([^"]+)"', body).group(1)
    s, _, _, _ = req2("POST", "/demo/try", urlencode({"key": "edited"}), h)
    check("a sample still needs the form token (CSRF)", s == 403)
    s, loc, _, _ = req2("POST", "/demo/try", urlencode({"key": "edited", "csrf": csrf}), h)
    cid = loc.rsplit("/", 1)[-1]
    s, _, _, body = req2("GET", loc, headers=h)
    check("a sample opens as a case with its medicines filled in", s == 200 and 'value="Alprax 0.5"' in body, loc)
    s, loc2, _, _ = req2("POST", f"/case/{cid}/check", urlencode({"csrf": csrf, "drugs": "Alprax 0.5", "regn": "DEMO-1001",
                                                                  "doctor": "Dr. Demo Sample"}), h)
    s, _, _, body = req2("GET", loc2 or f"/case/{cid}", headers=h)
    check("the edited sample is caught as an edited copy of the recorded slip (RED)",
          'class="verdict v-RED"' in body and "RX-DEMO-0812" in body)
    say = re.search(r'<p class="say"><b>Tell the customer:</b>(.*?)</p>', body)
    check("  ...with a neutral line for the customer: no accusation in front of the queue",
          say is not None and not re.search(r"fraud|fake|forg|edit|copy|police", say.group(1), re.I),
          say.group(1) if say else "no line")
    # ---- the printed sample: the cloud has no OCR engine; the recorded reading is replayed through the live pipeline
    import slip_reader as ocr
    os.environ["RXCHECK_OCR"] = "on"
    real_available, ocr.available = ocr.available, (lambda: False)
    s, _, _, body = req2("GET", "/", headers=h)
    check("home page: 'Slip reading not installed', and says the Printed slip replays a recorded reading",
          'data-ocr-status="missing"' in body and "Slip reading not installed" in body and "replays a reading" in body
          and "takes about two seconds." in body)
    check("  'What this demo includes' and the reader's scores on the synthetic set, with their caveat",
          "What this demo includes" in body and "How the slip reader scored" in body and "Silent errors" in body
          and "not evidence of accuracy on real prescriptions" in body and "python app/slip_reader.py slip.jpg" in body)
    s, loc, _, _ = req2("POST", "/demo/try", urlencode({"key": "printed", "csrf": csrf}), h)
    pcid = loc.rsplit("/", 1)[-1]
    s, _, _, body = req2("GET", loc, headers=h)
    check("printed sample opens 'Reading', nothing filled in yet", 'data-ocr-state="reading"' in body
          and 'value="Dolo' not in body)
    for _ in range(100):
        s, _, _, txt = req2("GET", f"/case/{pcid}/ocr", headers=h)
        data = json.loads(txt)
        if data["status"] != "pending":
            break
        time.sleep(0.1)
    printed = next(x for x in app.demo_samples()["samples"] if x["key"] == "printed")
    check("  the recorded reading fills in what the tile promises, without an engine", data["status"] == "done"
          and data["values"] == printed["after_reading"], data.get("values"))
    check("  ...says it is a recorded reading; the smudged line is shown as read, not filled in, with its cut-out",
          "Recorded reading." in data["panel"] and "Metformin5oomg" in data["panel"]
          and "Metformin" not in data["values"]["drugs"] and "/crop/" in data["panel"]
          and 'data-ocr-state="confirm"' in data["panel"], data["panel"][:400])
    fields = dict(printed["after_reading"], csrf=csrf)
    s, _, _, body = req2("POST", f"/case/{pcid}/check", urlencode(fields), h)
    n = body.count('name="confirm"')
    check("  sent as filled in -> 'Check blocked' until each doubtful reading is ticked", 'data-ocr-state="blocked"' in body
          and n >= 2, n)
    s, loc3, _, _ = req2("POST", f"/case/{pcid}/check", urlencode({**fields, "confirm": [str(i) for i in range(n)]},
                                                                  doseq=True), h)
    s, _, _, body = req2("GET", loc3 or f"/case/{pcid}", headers=h)
    check("  ...ticked -> the decision the tile promises (AMBER: a new slip with a high-risk medicine)",
          f'class="verdict v-{printed["expect"]}"' in body)
    ocr.available = real_available
    os.environ["RXCHECK_OCR"] = "off"
    got = {}
    for smp in app.demo_samples()["samples"]:
        f = {**smp.get("after_reading", {}), **smp["fields"]}
        rep = r.run_check(app.DEMO_DIR / smp["image"], f["drugs"], f.get("regn"), f.get("doctor"), draw=False)
        got[smp["key"]] = (max(rep["verdicts"].values(), key=r.ORDER.index), smp["expect"])
    check("every demo sample gives the verdict its tile promises", all(a == b for a, b in got.values()), str(got))
    s, _, _, _ = req2("POST", "/demo/try", urlencode({"key": "../ledger/rxcheck.db", "csrf": csrf}), h)
    check("only the listed samples can be opened", s == 404)
    srv.shutdown()
    srv.server_close()
    app.DEMO = False
    r.close_db()

sys.exit(0 if ok else 1)
