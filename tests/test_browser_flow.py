"""The slip reader in a real browser: photo upload -> background read -> fields filled by the page script ->
crops shown -> ticks required -> decision. Also a PDF upload and the 'OCR disabled' state.

Run:  python tests/test_browser_flow.py
Needs Playwright (pip install playwright==1.64.1) and an installed Edge or Chrome - no browser download: it
drives the one on the PC. Skipped, not failed, when neither is there. The OCR engine is a stand-in with fixed
readings, so the test is about the page and the server, not about reading accuracy (eval/score_slip_reader.py).
"""
import json
import os
import sys
import tempfile
import threading
import time
from datetime import date, timedelta
from pathlib import Path

import cv2

from synthetic_slips import r, other_slip
import synthetic_slips as pdfs

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import counter_app as app  # noqa: E402
import slip_reader as ocr  # noqa: E402

try:
    from playwright.sync_api import sync_playwright, Error as PwError
except ImportError:
    print("[SKIP] browser test: playwright not installed (pip install playwright==1.64.1)")
    sys.exit(0)

ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not cond else ""))


day = (date.today() - timedelta(days=3)).strftime("%d/%m/%Y")
SEGMENTS = [((40, 40, 700, 80), "Dr. Asha Example, MBBS", 0.99), ((40, 90, 400, 120), "Regn No: TN-45821", 0.98),
            ((500, 130, 760, 160), f"Date: {day}", 0.99), ((60, 300, 500, 340), "1. Tab. Dolo 650 mg 1-0-1", 0.99),
            ((60, 360, 500, 400), "2. Tab. Alprax 0.25 mg 0-0-1", 0.98),
            ((60, 420, 500, 460), "3. Tab. Metfornin 500", 0.91)]


def fake_segments(img):
    time.sleep(2.5)   # a counter PC takes 5-15 s; long enough here for staff to type into a field first
    return list(SEGMENTS)


ocr.available, ocr.read_segments = (lambda: True), fake_segments
os.environ["RXCHECK_OCR"] = "on"


def launch(p):
    for channel in ("msedge", "chrome"):
        try:
            return p.chromium.launch(channel=channel, headless=True), channel
        except PwError:
            continue
    return None, None


with tempfile.TemporaryDirectory() as t, sync_playwright() as pw:
    browser, channel = launch(pw)
    if not browser:
        print("[SKIP] browser test: no Edge or Chrome installed")
        sys.exit(0)
    print(f"(browser: {channel} {browser.version})")
    tmp = Path(t)
    r.LEDGER_DIR, r.DB, r.FEATURES, r.REPORTS = tmp, tmp / "rxcheck.db", tmp / "features", tmp / "reports"
    r.REGISTRY = tmp / "registry.json"
    r.REGISTRY.write_text("{}", encoding="utf-8")
    r.add_staff("pharmacist-a", "pharmacist", "111111")
    r.close_db()
    srv = app.make_server("127.0.0.1", 0, inbox=tmp / "inbox")
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    slip = tmp / "slip.jpg"
    cv2.imwrite(str(slip), other_slip(seed=7))
    errors = []
    page = browser.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "Failed to load resource" not in m.text
            else None)
    # every failed request, by address (the browser's own favicon request aside: the app serves none)
    page.on("response", lambda res: errors.append(f"HTTP {res.status} {res.url}")
            if res.status >= 400 and not res.url.endswith("/favicon.ico") else None)
    try:
        page.goto(f"{base}/login")
        page.fill("input[name=name]", "pharmacist-a")
        page.fill("input[name=pin]", "111111")
        page.click("button[type=submit]")
        page.wait_for_url(f"{base}/")

        # ---- photo: choosing it starts the check (quickcheck.js), the form opens while the slip is read
        page.set_input_files("#image", str(slip))
        page.wait_for_url(f"{base}/case/*")
        cid = page.url.rsplit("/", 1)[1]
        check("upload -> the form opens at once, state 'Reading'", page.locator('[data-ocr-state="reading"]').count() == 1
              and page.input_value("#drugs") == "")
        page.fill("#doctor", "Dr. Typed By Staff")   # typed before the reading finishes
        page.keyboard.press("Escape")
        page.wait_for_selector('[data-ocr-state="confirm"]', timeout=20000)
        check("reading done -> state 'Needs confirmation', fields filled by the page script",
              page.input_value("#drugs") == "Dolo 650, Alprax 0.25" and page.input_value("#regn") == "TN-45821"
              and page.input_value("#rx_date") == (date.today() - timedelta(days=3)).isoformat(),
              [page.input_value(f"#{k}") for k in ("drugs", "regn", "rx_date")])
        check("  what staff typed before was not replaced", page.input_value("#doctor") == "Dr. Typed By Staff")
        check("  filling a field does not pop up its suggestion list",
              page.locator(".rx-lookup__list:visible").count() == 0)
        check("  'Metfornin' is shown with its crop, loaded; not filled in",
              "Metfornin 500" in page.inner_text("#ocr") and "Metfornin" not in page.input_value("#drugs")
              and page.eval_on_selector("#ocr img.crop", "i => i.complete && i.naturalWidth > 0"))
        check("  the fields say where their value came from: 'check' or 'from slip' chips; the typed one has none",
              page.get_attribute("#ocr", "role") == "status"
              and page.inner_text('label[for="drugs"] .chip') == "check"
              and page.inner_text('label[for="regn"] .chip') == "check"
              and page.locator('label[for="doctor"] .chip').count() == 0)

        # ---- sent unchanged: blocked until each reading is ticked against its crop
        page.click("text=Get decision")
        page.wait_for_selector('[data-ocr-state="blocked"]')
        boxes = page.locator('input[name="confirm"]')
        check("sent -> state 'Check blocked', one tick per doubtful reading (name not on file, high-risk, Regn No)",
              boxes.count() == 3 and "3 to tick" in page.inner_text("#ocr"), boxes.count())
        try:
            page.wait_for_function("() => { const is = [...document.querySelectorAll('.issues img.crop')];"
                                   " return is.length === 3 && is.every(i => i.complete && i.naturalWidth > 0) }",
                                   timeout=10000)
            loaded = True
        except PwError:
            loaded = False
        check("  each item beside its crop, loaded", loaded)
        url = page.url
        page.click("text=Get decision")
        page.wait_for_timeout(500)
        check("  without the ticks the browser does not send the form", page.url == url
              and page.locator('[data-ocr-state="blocked"]').count() == 1
              and not page.eval_on_selector("form[data-primary]", "f => f.checkValidity()"))
        for i in range(boxes.count()):
            boxes.nth(i).check()
        page.click("text=Get decision")
        page.wait_for_url(f"{base}/case/{cid}")
        check("  all ticked -> the decision", page.locator(".verdict").count() == 1)
        use = json.loads(r.db().execute("SELECT detail FROM audit WHERE ref = ? AND action = 'ocr-use'",
                                        (cid,)).fetchone()[0])
        check("  audit: reading used, the typed doctor not counted as a reading kept",
              use["status"] == "done" and use["offered"] == 5 and use["changed"] == 1, use)

        # ---- a PDF: chosen like a photo, its text read without OCR
        pdf = tmp / "rx.pdf"
        pdf.write_bytes(pdfs.text_pdf([[(60, 60, 20, "SYNTHETIC TEST CLINIC"), (60, 118, 12, "Regn No: TN-45821")],
                                           [(60, 80, 12, "1. Tab. Dolo 650 mg 1-0-1")]]))
        page.goto(f"{base}/")
        page.set_input_files("#image", str(pdf))
        page.wait_for_url(f"{base}/case/*")
        page.wait_for_selector('[data-ocr-state="confirm"], [data-ocr-state="ready"]', timeout=20000)
        check("PDF upload: read from its text, page 2 shown, the form says the checks used page 1",
              page.input_value("#drugs") == "Dolo 650" and page.locator("img[src$='/page/2']").count() == 1
              and "PDF, 2 pages" in page.content())

        # ---- switched off
        os.environ["RXCHECK_OCR"] = "off"
        page.goto(f"{base}/")
        page.set_input_files("#image", str(slip))
        page.wait_for_url(f"{base}/case/*")
        check("switched off -> state 'OCR disabled', nothing filled", page.locator('[data-ocr-state="disabled"]').count() == 1
              and "OCR disabled" in page.inner_text("#ocr") and page.input_value("#drugs") == "")
        check("no script errors on any page", not errors, errors)
    except Exception as ex:
        check(f"browser flow ran to the end ({type(ex).__name__}: {str(ex)[:200]})", False)
        page.screenshot(path=str(ROOT / "samples" / "browser-failure.png"))
    finally:
        browser.close()
        srv.shutdown()
        r.close_db()

print("\nALL PASS" if ok else "\nSOME CHECKS FAILED")
sys.exit(0 if ok else 1)
