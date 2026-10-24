"""Image transforms that simulate what happens to a prescription before it reaches the counter,
plus the forgeries used in tests and evaluation. Shared by test_robustness.py and eval/."""
import os
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

# the counter tests upload hundreds of slips; reading each photo (5-15 s) would only slow them down.
# tests/test_ocr.py switches it on where it is the thing under test.
os.environ.setdefault("RXCHECK_OCR", "off")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import rxcheck as r  # noqa: E402

rng = np.random.default_rng(0)
PRIVATE_PAIR = (ROOT / "samples" / "local" / "reference.jpg").is_file() and \
               (ROOT / "samples" / "local" / "modified.jpg").is_file()


def synthetic_samples(original=None):
    if original is None:
        original = np.full((2800, 1900, 3), 230, np.uint8)
        cv2.rectangle(original, (40, 40), (1860, 2760), (85, 85, 85), 8)
        cv2.putText(original, "SYNTHETIC TEST CLINIC", (250, 190), cv2.FONT_HERSHEY_SIMPLEX, 2.4,
                    (25, 25, 25), 5, cv2.LINE_AA)
        cv2.line(original, (180, 350), (1720, 350), (35, 35, 35), 5)
        for i, y in enumerate(range(650, 2470, 85)):
            if i == 8:
                continue
            cv2.putText(original, f"SAMPLE DATA {i:02d}  TEST ONLY", (220, y),
                        cv2.FONT_HERSHEY_SCRIPT_COMPLEX, 1.25, (40, 40, 40), 3, cv2.LINE_AA)
            cv2.line(original, (220, y + 18), (1450 + (i % 4) * 70, y + 18), (110, 110, 110), 2)
        cv2.line(original, (180, 2580), (1720, 2580), (35, 35, 35), 4)
        cv2.putText(original, "NOT A REAL PRESCRIPTION", (260, 2660), cv2.FONT_HERSHEY_SIMPLEX, 1.4,
                    (40, 40, 40), 4, cv2.LINE_AA)

    original = original.copy()
    suspect = original.copy()
    cv2.rectangle(suspect, (200, 720), (1650, 830), (230, 230, 230), -1)
    cv2.putText(suspect, "ALTERED SAMPLE LINE 99", (220, 810), cv2.FONT_HERSHEY_SCRIPT_COMPLEX, 1.25,
                (40, 40, 40), 3, cv2.LINE_AA)
    return original, suspect


def demo_slips():
    """The public demo's sample slips, {name: image}. Built from the synthetic generator only - never from the
    private pair in samples/local - so tests/test_deploy.py can regenerate and compare every image in demo/."""
    original, _ = synthetic_samples()
    return {"reference": original, "edited": added_line(original), "resent": whatsapp(original),
            "new": other_slip(7), "printed": printed_slip()}


def printed_slip():
    """A computer-printed synthetic slip for the demo's slip-reading sample. OpenCV's own font, so the image is
    byte-identical on every machine. Line 4 is smudged (as on a slip that got wet): the reader cannot read it
    clearly, which is the case the demo has to show."""
    W, H = 1240, 1754
    img = np.full((H, W, 3), 248, np.uint8)
    simplex, duplex = cv2.FONT_HERSHEY_SIMPLEX, cv2.FONT_HERSHEY_DUPLEX

    def put(canvas, text, x, y, scale=1.0, thick=2, font=simplex):
        cv2.putText(canvas, text, (x, y), font, scale, (25, 25, 25), thick, cv2.LINE_AA)

    put(img, "SYNTHETIC TEST CLINIC", 80, 110, 1.6, 3, duplex)
    put(img, "Demo Road, Sample Nagar - NOT A REAL PRESCRIPTION", 80, 160, 0.8)
    put(img, "Dr. Demo Sample, MBBS", 80, 240, 1.1)
    put(img, "Regn No: DEMO-1001", 80, 290)
    put(img, "Date: 20/10/2026", 860, 350)
    put(img, "Name: Sample Patient   Age: 34 / M", 80, 410)
    put(img, "Rx", 80, 500, 1.6, 3, duplex)
    put(img, "1. Tab. Dolo 650 mg   1-0-1  x 5 days", 120, 590, 1.1)
    put(img, "2. Tab. Pan 40 mg   1-0-0 before food", 120, 680, 1.1)
    put(img, "3. Tab. Alprax 0.25 mg   0-0-1", 120, 770, 1.1)
    put(img, "SYNTHETIC - FOR SOFTWARE TESTING ONLY", 80, 1680, 0.8)
    line = np.full_like(img, 248)
    put(line, "4. Tab. Metfornin 500 mg   1-0-1", 120, 860, 1.1)
    smudged = cv2.GaussianBlur(line[815:890, 100:800].astype(np.float32), (0, 0), 4.0) * 0.7 + 248 * 0.3
    img[815:890, 100:800] = np.clip(smudged, 0, 255).astype(np.uint8)
    return img


def load_samples():
    if PRIVATE_PAIR:
        return (r.read_bgr(ROOT / "samples" / "local" / "reference.jpg"),
                r.read_bgr(ROOT / "samples" / "local" / "modified.jpg"))
    original = r.read_bgr(fixture_path())
    return synthetic_samples(original)


def fixture_path():
    if PRIVATE_PAIR:
        return ROOT / "samples" / "local" / "reference.jpg"
    path = Path(tempfile.gettempdir()) / "rxcheck-public-test-fixture-v2.jpg"
    if not path.exists():
        cv2.imencode(".jpg", synthetic_samples()[0], [cv2.IMWRITE_JPEG_QUALITY, 95])[1].tofile(str(path))
    return path


# ---- what happens to a genuine slip before it reaches the counter

def whatsapp(img, angle=3.0, width=900, quality=45, gain=0.85):
    h, w = img.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    img = cv2.warpAffine(img, M, (w, h), borderValue=(60, 80, 110))
    img = cv2.convertScaleAbs(img, alpha=gain, beta=0)
    img = cv2.resize(img, (width, int(h * width / w)), interpolation=cv2.INTER_AREA)
    return cv2.imdecode(cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])[1], cv2.IMREAD_COLOR)


def photocopy(img):
    g = cv2.convertScaleAbs(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), alpha=1.6, beta=-90)
    g = np.clip(g + rng.normal(0, 12, g.shape), 0, 255).astype(np.uint8)
    return cv2.cvtColor(g, cv2.COLOR_GRAY2BGR)


def dim_shop_light(img):
    noisy = img.astype(np.float32) * 0.45 + rng.normal(0, 8, img.shape)
    return np.clip(noisy, 0, 255).astype(np.uint8)


def side_shadow(img):
    h, w = img.shape[:2]
    return (img * np.tile(np.linspace(0.45, 1, w), (h, 1))[..., None]).astype(np.uint8)


def angled_photo(img):
    h, w = img.shape[:2]
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = np.float32([[0.06 * w, 0.03 * h], [0.95 * w, 0], [w, h], [0.02 * w, 0.97 * h]])
    return cv2.warpPerspective(img, cv2.getPerspectiveTransform(src, dst), (w, h), borderValue=(60, 80, 110))


def shaky(img):
    return cv2.GaussianBlur(img, (0, 0), 2.5)


def cropped(img):
    h, w = img.shape[:2]
    return img[int(0.06 * h):int(0.97 * h), int(0.04 * w):int(0.98 * w)]


def jpeg(img, q):
    return cv2.imdecode(cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])[1], cv2.IMREAD_COLOR)


def forwarded(img, times=5, first_gain=0.85):
    """Photographed once, then forwarded `times-1` more times: each forward only resizes and recompresses."""
    img = whatsapp(img, gain=first_gain)
    for i in range(times - 1):
        img = whatsapp(img, angle=0, width=int(img.shape[1] * 0.95), quality=45 - 3 * i, gain=1.0)
    return img


def screenshot(img):
    """Gallery screenshot on a 1080x2400 phone: letterboxed on black, status bar and toolbar, re-shared."""
    H, W = 2400, 1080
    s = min(W / img.shape[1], (H - 300) / img.shape[0])
    im = cv2.resize(img, (int(img.shape[1] * s), int(img.shape[0] * s)), interpolation=cv2.INTER_AREA)
    canvas = np.zeros((H, W, 3), np.uint8)
    y, x = (H - im.shape[0]) // 2, (W - im.shape[1]) // 2
    canvas[y:y + im.shape[0], x:x + im.shape[1]] = im
    cv2.rectangle(canvas, (0, 0), (W, 90), (30, 30, 30), -1)
    cv2.putText(canvas, "10:42   4G   87%", (30, 62), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (230, 230, 230), 2)
    cv2.rectangle(canvas, (0, H - 160), (W, H), (30, 30, 30), -1)
    return jpeg(canvas, 80)


def screen_recapture(img):
    """Slip shown on a customer's phone screen and photographed by the pharmacist: pixel-grid structure
    resampled by the camera (moire), glare, bezel, perspective, slight blur."""
    disp = cv2.resize(img, (720, int(img.shape[0] * 720 / img.shape[1])), interpolation=cv2.INTER_AREA).astype(np.float32)
    grid = np.ones(disp.shape[:2], np.float32)
    grid[::3, :] *= 0.80
    grid[:, ::3] *= 0.85
    big = cv2.resize(disp * grid[..., None], None, fx=2.2, fy=2.2, interpolation=cv2.INTER_LINEAR)
    h, w = big.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    glare = 70 * np.exp(-(((xx - 0.7 * w) / (0.25 * w)) ** 2 + ((yy - 0.3 * h) / (0.2 * h)) ** 2))
    big = np.clip(big + glare[..., None], 0, 255)
    pad = 120
    canvas = np.full((h + 2 * pad, w + 2 * pad, 3), 20, np.float32)
    canvas[pad:pad + h, pad:pad + w] = big
    out = cv2.GaussianBlur(angled_photo(canvas.astype(np.uint8)), (0, 0), 1.2)
    return jpeg(out, 70)


# ---- images too damaged to compare - must go to manual review, never be treated as a new slip

def tiny(img):
    return cv2.resize(img, (260, int(img.shape[0] * 260 / img.shape[1])), interpolation=cv2.INTER_AREA)


def heavy_blur(img):
    return cv2.GaussianBlur(img, (0, 0), 7)


def blown_glare(img):
    h, w = img.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    g = 400 * np.exp(-(((xx - 0.5 * w) / (0.35 * w)) ** 2 + ((yy - 0.55 * h) / (0.3 * h)) ** 2))
    return np.clip(img.astype(np.float32) + g[..., None], 0, 255).astype(np.uint8)


def near_black(img):
    return np.clip(img.astype(np.float32) * 0.12 + rng.normal(0, 6, img.shape), 0, 255).astype(np.uint8)


def glary_screen(img):
    """Phone-screen photo with the screen glare blowing out the writing."""
    return jpeg(cv2.GaussianBlur(blown_glare(screen_recapture(img)), (0, 0), 3), 70)


def rewrite(img, amp, scale, seed=1):
    """Stand-in for the same doctor rewriting the same prescription: same words and layout, every stroke
    displaced/reshaped by a smooth random field (max `amp` px). Real rewrites differ more than this."""
    g = np.random.default_rng(seed)
    p = r.paper_crop(img).copy()
    H = p.shape[0]
    y0, y1 = int(0.22 * H), int(0.93 * H)
    body = p[y0:y1]
    bh, bw = body.shape[:2]

    def field():
        f = cv2.GaussianBlur(g.normal(size=(bh, bw)).astype(np.float32), (0, 0), scale)
        return f / (np.abs(f).max() + 1e-9) * amp
    xs, ys = np.meshgrid(np.arange(bw, dtype=np.float32), np.arange(bh, dtype=np.float32))
    p[y0:y1] = cv2.remap(body, xs + field(), ys + field(), cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    return p


def op_chit(img):
    """Plain-paper slip with no printed letterhead (like a govt OP chit or small clinic): the handwriting
    moved to the top of a blank sheet, photographed on a dark counter."""
    p = r.paper_crop(img)
    h = p.shape[0]
    paper = np.median(p[int(0.3 * h):int(0.9 * h)].reshape(-1, 3), axis=0).astype(np.uint8)
    out = np.empty_like(p)
    out[:] = paper
    body = p[int(0.20 * h):int(0.90 * h)]
    out[int(0.03 * h):int(0.03 * h) + body.shape[0]] = body
    return cv2.copyMakeBorder(out, 80, 80, 80, 80, cv2.BORDER_CONSTANT, value=(50, 60, 80))


def op_chit_redated(img):
    """OP chit with a figure added next to the date at the very top of the page."""
    p = op_chit(img)
    cv2.putText(p, "+10", (80 + 1240, 80 + 150), cv2.FONT_HERSHEY_SCRIPT_SIMPLEX, 1.4, (110, 40, 30), 3)
    return p


# ---- different prescriptions on the same letterhead (stand-ins: real strokes, different layout)

def body_op(img, op):
    p = r.paper_crop(img).copy()
    h = p.shape[0]
    y0, y1 = int(0.24 * h), int(0.90 * h)
    p[y0:y1] = op(p[y0:y1])
    return p


def mirrored_body(img):
    return body_op(img, lambda b: b[:, ::-1])


def flipped_body(img):
    return body_op(img, lambda b: b[::-1])


# ---- forgeries

def added_line(img):
    """Hand-added drug line in the Rx block (like adding a benzodiazepine to a genuine slip)."""
    p = r.paper_crop(img).copy()
    cv2.putText(p, "T. ALPRAX 0.5  x30", (640, 1325), cv2.FONT_HERSHEY_SCRIPT_SIMPLEX, 1.3, (110, 40, 30), 2)
    return p


OTHER_LINES = ["SAMPLE PATIENT 01", "SAMPLE TEST LINE 01", "SAMPLE DATA",
               "SAMPLE ITEM A     1-1-1  x 3 days", "SAMPLE ITEM B     1-0-0  x 5 days",
               "SAMPLE NOTE 01", "SAMPLE NOTE 02", "SAMPLE REVIEW LINE"]
THIRD_LINES = ["SAMPLE PATIENT 02", "SAMPLE TEST LINE 02", "SAMPLE DATA 03", "SAMPLE DATA 04",
               "SAMPLE ITEM C     1-0-1", "SAMPLE ITEM D     1-0-0", "SAMPLE DURATION", "SAMPLE ADVICE"]


def other_slip(seed=0, lines=OTHER_LINES):
    """A genuinely different routine prescription (different patient, words and hand) on plain paper,
    photographed on a dark counter. Not derived from the reference sample."""
    g = np.random.default_rng(seed)
    h, w = 1900, 1400
    paper = np.empty((h, w, 3), np.uint8)
    paper[:] = (198, 204, 210)
    y = 160
    for i, t in enumerate(lines):
        x = int(90 + g.integers(0, 120))
        cv2.putText(paper, t, (x, y), cv2.FONT_HERSHEY_SCRIPT_COMPLEX, float(g.uniform(1.6, 2.0)),
                    (120, 50, 40), 3, cv2.LINE_AA)
        y += int(g.integers(150, 210))
    cv2.putText(paper, "SAMPLE CLINICIAN", (980, 1760), cv2.FONT_HERSHEY_SCRIPT_SIMPLEX, 2.2,
                (120, 50, 40), 3, cv2.LINE_AA)
    return whatsapp(cv2.copyMakeBorder(paper, 90, 90, 90, 90, cv2.BORDER_CONSTANT, value=(50, 60, 80)), angle=2)


TAMIL_FONTS = ("C:/Windows/Fonts/Nirmala.ttc", "/mnt/c/Windows/Fonts/Nirmala.ttc",   # Windows; Windows from WSL
               "/usr/share/fonts/truetype/noto/NotoSansTamil-Regular.ttf",            # Debian/Ubuntu fonts-noto
               "/usr/share/fonts/truetype/lohit-tamil/Lohit-Tamil.ttf")


def tamil_font(size):
    for path in TAMIL_FONTS:
        if Path(path).exists():
            from PIL import ImageFont
            return ImageFont.truetype(path, size)
    raise FileNotFoundError(f"no Tamil font found - install one of: {', '.join(TAMIL_FONTS)}")


def tamil_line(img):
    """Hand-added dosage instruction in Tamil ("morning 1 - night 1") in the Rx block."""
    from PIL import Image, ImageDraw
    p = r.paper_crop(img).copy()
    pil = Image.fromarray(cv2.cvtColor(p, cv2.COLOR_BGR2RGB))
    font = tamil_font(44)
    ImageDraw.Draw(pil).text((640, 1295), "காலை 1 - இரவு 1", font=font, fill=(30, 40, 110))
    return cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR)


def pasted_lower(img):
    """Real handwriting cut-and-pasted 170px lower on the page."""
    return body_op(img, lambda b: np.roll(b, 170, axis=0))
