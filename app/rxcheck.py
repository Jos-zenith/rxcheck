#!/usr/bin/env python3
"""rxcheck - prescription verification triage for a busy pharmacy counter (prototype).

It does not try to answer "is this image AI-generated?" from pixels - nobody can do that
reliably in a second. It answers "can I dispense this now, or must I confirm first?" using
facts a forger cannot control:

  1. Re-use check   - is this the same slip as one seen or dispensed before?
                      * image ledger (high-risk sales): stroke-level comparison, edits boxed
                      * no-image index (every checked slip): "seen before" only, never RED
  2. Prescriber     - does the Regn No exist and belong to that doctor (local registry)?
  3. Drug risk      - routine medicine vs abuse-prone / Schedule H1 / X  (data/drug_risk.json)
  4. Consistency    - drugs/strengths now requested vs what was recorded for the matched slip
  5. Issuance code  - a QR code signed by a participating clinic: proves that clinic issued exactly this
                      prescription (the only check here that speaks to authorship). Optional.
  6. File metadata  - supporting evidence only: an AI/provenance marker asks for confirmation, never RED.

Images the tool cannot read are never treated as new slips - they go to manual review.
Unresolved RED/AMBER cases go to a hold queue (`holds`, `resolve`). Storage is one SQLite file with
an append-only audit trail (`log`), online `backup`, `verify` and `restore`. Broken or missing data
files never crash a check: they make it more cautious and say why (`data status`).
Everything runs offline; only the confirmation call to the clinic needs a phone.

Usage:
  python app/rxcheck.py add IMAGE --id RX-EXAMPLE-001 --dispensed 2025-01-15 --regn REGN-EXAMPLE --doctor "Dr. Example" --drugs "Alprax 0.25"
  python app/rxcheck.py check IMAGE [--drugs "Alprax 0.25, Dolo 650"] [--regn REGN-EXAMPLE] [--doctor "Dr. Example"] [--rx-date 2025-01-15]
  python app/rxcheck.py watch   inbox/      # pre-check every image that lands in the folder (e.g. WhatsApp downloads)
  python app/rxcheck.py serve   [--host 0.0.0.0]   # counter app for one or more billing PCs (browser)
  python app/rxcheck.py staff   add NAME --role pharmacist|staff | list | disable NAME
  python app/rxcheck.py holds               # open holds, oldest first
  python app/rxcheck.py resolve H-... --outcome confirmed|refused|unreachable|dispensed-after-review [--note ...]
  python app/rxcheck.py data    status | import-registry CSV --source ... | import-drugs CSV --source ... --verified-by ...
  python app/rxcheck.py clinic  keygen KEY.pem | issue KEY.pem --regn ... --date ... --drugs ... --out QR.png
  python app/rxcheck.py log | backup D:/rxcheck-backups | verify | restore D:/rxcheck-backups/rxcheck-backup-...

The verdict is a triage signal for the pharmacist, not proof of fraud.
"""
import argparse
import base64
import csv
import difflib
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
# everything that changes at run time lives under one directory: the repo by default, or RXCHECK_HOME
# (a persistent disk when deployed - the container image itself holds only code and default data files)
HOME = Path(os.environ.get("RXCHECK_HOME") or ROOT)
LEDGER_DIR = HOME / "ledger"
DB = LEDGER_DIR / "rxcheck.db"
FEATURES = LEDGER_DIR / "features"
REGISTRY = HOME / "data" / "registry.json"
DRUG_RISK = HOME / "data" / "drug_risk.json"
POLICY = HOME / "data" / "policy.json"
REPORTS = HOME / "reports"
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}

WORK_W = 1400          # paper width (px) everything is normalised to
BODY_Y = (0.22, 0.93)  # handwritten band on a letterhead slip (below letterhead, above footer)
PLAIN_Y = (0.03, 0.97)  # handwritten band on plain paper / OP chits (no printed rules found)
CLONE_AT = 0.80        # lenient agreement (ink within 4px) above which a slip is at least "similar"
COPY_STRICT = 0.92     # strict agreement (ink within 1px) needed to call it a copy ...
COPY_NCC = 0.88        # ... together with tile correlation; real copies >= 0.93, 1mm-displaced rewrites <= 0.69
MAX_EDIT_FRAC = 0.25   # edits may cover this much of the handwriting and still be "edits" (forgeries <= 0.18,
                       # simulated rewrites >= 0.32 in tests); beyond it the whole page is judged
TOL_PX = 4             # stroke position tolerance for "similar"
EDIT_TOL_PX = 2        # tighter tolerance for locating edits
MIN_CHANGE_PX = 60     # Ignore low-level render noise below this threshold.
FP_MIN = 0.50          # fingerprint similarity needed to be shortlisted
SHORTLIST = 5          # slips that get the precise (slow) comparison
INDEX_KP = 1500        # keypoints kept per no-image index entry (strongest, handwritten band only)
INDEX_MIN_CORR = 0.75  # layout correlation for an index entry to count as "seen before"
                       # (copies incl. AI/5x forwarded/screen >= 0.88; different slips ~0.0 in tests)
INDEX_MIN_INLIERS = 12      # keypoint inliers an alignment model needs before its layout is compared

ORB = cv2.ORB_create(8000)
CLAHE = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))


# ---------------------------------------------------------------- images

def read_bgr(path):
    # np.fromfile + imdecode copes with spaces / non-ASCII Windows paths
    img = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"cannot read image: {path}")
    return img


def paper_crop(img):
    """Crop to the largest bright region (the sheet), dropping table/background. Skipped when the
    paper already fills the frame (scanner apps, screenshots) - otherwise Otsu would split the
    paper's own shading and crop away part of the slip."""
    sc = min(1.0, 600 / max(img.shape[:2]))   # the sheet outline only needs a coarse mask
    small = cv2.resize(img, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA)
    g = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (9, 9), 0)
    _, m = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((11, 11), np.uint8))
    k = max(2, int(0.01 * min(m.shape)))
    border = np.concatenate([m[:k].ravel(), m[-k:].ravel(), m[:, :k].ravel(), m[:, -k:].ravel()])
    # real background (table, cloth) is <=0.33 of paper brightness in tests; paper shading alone is >=0.67
    dark, bright = g[m == 0], g[m > 0]
    contrast = dark.mean() / bright.mean() if dark.size and bright.size else 1.0
    has_background = np.mean(border == 0) > 0.5 and contrast < 0.5
    cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if cnts and has_background:
        x, y, w, h = cv2.boundingRect(max(cnts, key=cv2.contourArea))
        if w * h > 0.3 * m.shape[0] * m.shape[1]:
            x, y, w, h = (int(round(v / sc)) for v in (x, y, w, h))
            img = img[y:y + h, x:x + w]
    s = WORK_W / img.shape[1]
    return cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)


def gray(img):
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


INK_BELOW = 0.80    # darker than 80% of the local paper = ink
BLANK_ABOVE = 0.92  # within 8% of the local paper = clearly blank


def paper_level(g):
    """Local paper brightness: strokes removed by a max filter at full resolution, then smoothed at
    quarter resolution (paper brightness varies slowly, so this is ~4x cheaper and equivalent)."""
    g = cv2.GaussianBlur(g, (3, 3), 0)  # so sensor/photocopy noise peaks don't lift it
    small = cv2.resize(cv2.dilate(g, np.ones((15, 15), np.uint8)), None, fx=0.25, fy=0.25,
                       interpolation=cv2.INTER_AREA)
    small = cv2.GaussianBlur(small.astype(np.float32), (0, 0), 2)
    return cv2.resize(small, (g.shape[1], g.shape[0]), interpolation=cv2.INTER_LINEAR)


def paper_ratio(g, level=None):
    """Each pixel's brightness relative to the local paper (1.0 = blank paper). Ratios, not grey-level
    gaps, so dim light, shadow and flash do not change the result."""
    level = paper_level(g) if level is None else level
    return cv2.GaussianBlur(g, (3, 3), 0).astype(np.float32) / (level + 1.0)


def paper_mask(g, level=None):
    """Where the image is paper rather than table/cloth left in the corners of a tilted photo
    (background texture would otherwise read as 'ink' on both images and fake agreement)."""
    p = paper_level(g) if level is None else level
    return (p > 0.6 * np.median(p)).astype(np.uint8) * 255


def ink(g, ratio=None):
    """Binary mask of pen/print strokes."""
    m = ((paper_ratio(g) if ratio is None else ratio) < INK_BELOW).astype(np.uint8) * 255
    return cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))


def blank_near(ratio, r):
    """True where there is no ink-ish pixel within r px - i.e. a stroke here would be genuinely new."""
    return (cv2.erode(ratio, disk(r)) >= BLANK_ABOVE).astype(np.uint8) * 255


def sharpness(g):
    return float(cv2.Laplacian(g, cv2.CV_32F).var())


def match_blur(a, b):
    """Blur the sharper of two views of the same slip until it is no sharper than the other, so a
    shaky or recompressed photo is not read as 'strokes missing'."""
    sa, sb = sharpness(a), sharpness(b)
    sharp, soft, swap = (a, b, False) if sa > sb else (b, a, True)
    target = min(sa, sb)
    for sigma in (0.8, 1.2, 1.6, 2.0, 2.5, 3.0, 3.5):
        if sharpness(sharp) <= target * 1.1:
            break
        sharp = cv2.GaussianBlur(a if not swap else b, (0, 0), sigma)
    return (soft, sharp) if swap else (sharp, soft)


def disk(r):
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))


def zones(sheet):
    """Handwritten band (top, bottom) as page fractions. A printed letterhead is recognised by its long
    ruled line near the top; plain paper and many OP chits have none, so almost the whole page counts
    (otherwise an edit near the top of an OP chit would be dismissed as 'letterhead')."""
    g = gray(sheet)
    m = ink(g) & paper_mask(g)
    h, w = m.shape
    lines = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((1, int(0.4 * w)), np.uint8))
    rows = np.where((lines > 0).sum(1) > 0.4 * w)[0] / h
    top = BODY_Y[0] if np.any((rows > 0.03) & (rows < 0.35)) else PLAIN_Y[0]
    bottom = BODY_Y[1] if np.any((rows > 0.85) & (rows < 0.98)) else PLAIN_Y[1]
    return top, bottom


def fingerprint(sheet):
    """Coarse layout of the handwriting (24x32 ink-density grid). Cheap to compare, survives
    compression, blur, shadows and small crops; different slips score low."""
    h = sheet.shape[0]
    body = ink(gray(sheet))[int(BODY_Y[0] * h):int(BODY_Y[1] * h)].astype(np.float32)
    g = cv2.GaussianBlur(cv2.resize(body, (24, 32), interpolation=cv2.INTER_AREA), (3, 3), 0).ravel()
    g -= g.mean()
    return g / (np.linalg.norm(g) + 1e-9)


def features(sheet):
    # contrast-equalised so a dark photo still yields keypoints (ORB's detector uses a fixed threshold)
    k, d = ORB.detectAndCompute(CLAHE.apply(gray(sheet)), None)
    return {"pts": np.float32([p.pt for p in k]).reshape(-1, 2),
            "resp": np.float32([p.response for p in k]),
            "desc": d if d is not None else np.zeros((0, 32), np.uint8),
            "fp": fingerprint(sheet), "shape": np.int32(sheet.shape[:2])}


DENS_SCALE = 1 / 8     # index density map: ~175px wide for a slip - words become blobs, not readable


def density(sheet):
    """Ink density at 1/8 resolution: the layout of the writing, too coarse to read."""
    return cv2.resize(ink(gray(sheet)), None, fx=DENS_SCALE, fy=DENS_SCALE, interpolation=cv2.INTER_AREA)


def compact(feats, dens):
    """What the no-image index keeps: the strongest keypoints (for alignment) and a coarse ink-density
    map (for the decision). Enough to recognise the same slip again; not an image of the prescription."""
    h = feats["shape"][0]
    y = feats["pts"][:, 1] / h
    idx = np.where((y > BODY_Y[0]) & (y < BODY_Y[1]))[0]
    idx = idx[np.argsort(-feats["resp"][idx])][:INDEX_KP]
    return {"pts": feats["pts"][idx], "resp": feats["resp"][idx], "desc": feats["desc"][idx],
            "fp": feats["fp"], "shape": feats["shape"], "dens": dens}


def match_points(fr, fs):
    """RANSAC homography suspect -> reference; returns (H, inlier points in reference coords)."""
    if len(fr["desc"]) < 15 or len(fs["desc"]) < 15:
        return None, np.zeros((0, 2), np.float32)
    pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(fs["desc"], fr["desc"], k=2)
    good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < 0.75 * p[1].distance]
    if len(good) < 15:
        return None, np.zeros((0, 2), np.float32)
    src = fs["pts"][[m.queryIdx for m in good]]
    dst = fr["pts"][[m.trainIdx for m in good]]
    H, mask = cv2.findHomography(src, dst, cv2.RANSAC, 4.0)
    if H is None:
        return None, np.zeros((0, 2), np.float32)
    return H, dst[mask.ravel().astype(bool)]


def align(fr, fs):
    H, inl = match_points(fr, fs)
    return H, len(inl)


def refine(ref, sus, H, scale=0.25):
    """Sub-pixel refinement of the keypoint homography (ECC on lighting-normalised images). A global
    homography can line a true copy up exactly but cannot make a rewrite match stroke-for-stroke."""
    S = np.diag([scale, scale, 1.0])
    small = lambda img: cv2.resize(paper_ratio(gray(img)), None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    W = (S @ np.linalg.inv(H) @ np.linalg.inv(S)).astype(np.float32)   # ECC maps reference -> suspect coords
    try:
        _, W = cv2.findTransformECC(small(ref), small(sus), W, cv2.MOTION_HOMOGRAPHY,
                                    (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 25, 1e-5), None, 5)
    except cv2.error:
        return H   # did not converge - keep the keypoint estimate
    Hn = np.linalg.inv(np.linalg.inv(S) @ W.astype(np.float64) @ S)
    return Hn / Hn[2, 2]


def index_match(fe, fs, sus_dens):
    """Correlation of the coarse ink layout in the handwritten band after keypoint alignment.
    1.0 = same layout; different slips score near 0. Says "seen before", never "edited".
    Two alignment models are tried and the better one kept. A full homography is needed for perspective
    (photos at an angle, of a phone screen), but fitted to a dozen inliers it overfits, and the OpenCV
    builds overfit differently: one heavily forwarded copy scored 0.88 on Windows and 0.75 on Linux. A
    similarity (rotation, scale, shift) cannot overfit that way and scored 0.97 on both. Each model must
    reach the same inlier minimum, which different slips do not (they score 0.0 under both)."""
    if len(fe["desc"]) < 15 or len(fs["desc"]) < 15:
        return 0.0
    pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(fs["desc"], fe["desc"], k=2)
    good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < 0.75 * p[1].distance]
    if len(good) < 15:
        return 0.0
    src = fs["pts"][[m.queryIdx for m in good]]
    dst = fe["pts"][[m.trainIdx for m in good]]
    models = []
    H, mask = cv2.findHomography(src, dst, cv2.RANSAC, 4.0)
    if H is not None and mask.sum() >= INDEX_MIN_INLIERS:
        models.append(H)
    A, mask = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=4.0, refineIters=20)
    if A is not None and mask is not None and mask.sum() >= INDEX_MIN_INLIERS:
        models.append(np.vstack([A, [0.0, 0.0, 1.0]]))
    return max((_layout_corr(fe["dens"], sus_dens, M) for M in models), default=0.0)


def _layout_corr(ref_dens, sus_dens, H):
    S = np.diag([DENS_SCALE, DENS_SCALE, 1.0])
    ref = ref_dens.astype(np.float32)
    h, w = ref.shape
    M = S @ H @ np.linalg.inv(S)
    warped = cv2.warpPerspective(sus_dens.astype(np.float32), M, (w, h), borderValue=0)
    valid = cv2.warpPerspective(np.ones_like(sus_dens, np.float32), M, (w, h)) > 0.99
    valid[:int(BODY_Y[0] * h)] = False
    valid[int(BODY_Y[1] * h):] = False
    a, b = cv2.GaussianBlur(ref, (0, 0), 1)[valid], cv2.GaussianBlur(warped, (0, 0), 1)[valid]
    if a.size < 100 or a.std() < 1e-3 or b.std() < 1e-3:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def tile_ncc(rr, rs, inkmask, valid, T=64, S=2):
    """Median correlation of the paper-ratio maps over inked tiles, allowing +-S px local shift.
    A copy reproduces every stroke (high); a rewrite by the same hand does not (low)."""
    H, W = rr.shape
    out = []
    for y in range(S, H - T - S, T):
        for x in range(S, W - T - S, T):
            if valid[y:y + T, x:x + T].mean() < 250 or inkmask[y:y + T, x:x + T].mean() < 255 * 0.03:
                continue
            out.append(float(cv2.matchTemplate(rs[y - S:y + T + S, x - S:x + T + S], rr[y:y + T, x:x + T],
                                               cv2.TM_CCOEFF_NORMED).max()))
    return float(np.median(out)) if len(out) >= 5 else None


def compare(ref, sus, fr=None, fs=None):
    """Align suspect onto reference. Returns identity ('copy' | 'similar' | 'different'), scores,
    changed regions and masks."""
    H, inliers = align(fr or features(ref), fs or features(sus))
    if H is None or inliers < 25:
        return None
    H = refine(ref, sus, H)
    h, w = ref.shape[:2]
    warped =cv2.warpPerspective(sus, H, (w, h), borderValue=(255, 255, 255))
    valid = cv2.warpPerspective(np.full(sus.shape[:2], 255, np.uint8), H, (w, h))
    valid = cv2.erode(valid, disk(12))
    margin = np.zeros((h, w), np.uint8)
    margin[int(0.01 * h):int(0.99 * h), int(0.03 * w):int(0.97 * w)] = 255
    valid &= margin

    gr, gs = match_blur(gray(ref), gray(warped))
    lr, ls = paper_level(gr), paper_level(gs)
    valid &= cv2.erode(paper_mask(gr, lr) & paper_mask(gs, ls), disk(10))
    rr, rs = paper_ratio(gr, lr), paper_ratio(gs, ls)
    ir, isu = ink(gr, rr) & valid, ink(gs, rs) & valid

    top, bottom = zones(ref)
    body = np.zeros((h, w), np.uint8)
    body[int(top * h):int(bottom * h), :] = 255

    # edits are located with a tighter tolerance: an "8" overwritten as "10" stays within 4px of the old ink
    missing = ir & blank_near(rs, EDIT_TOL_PX)   # strokes on the recorded slip that are gone
    added = isu & blank_near(rr, EDIT_TOL_PX)    # strokes that are new on the suspect
    diff = cv2.morphologyEx(missing | added, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    blobs = cv2.dilate(diff, disk(14))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(blobs)
    regions = []
    edited = np.zeros((h, w), np.uint8)
    for i in range(1, n):
        x, y, bw, bh, _ = stats[i]
        strength = int(np.count_nonzero(diff[y:y + bh, x:x + bw]))
        if strength < MIN_CHANGE_PX:
            continue
        cy = (y + bh / 2) / h
        zone = "letterhead" if cy < top else "footer" if cy > bottom else "handwritten"
        regions.append({"box": [int(x), int(y), int(bw), int(bh)], "zone": zone, "strength": strength})
        edited[max(0, y - 20):y + bh + 20, max(0, x - 20):x + bw + 20] = 255
    regions.sort(key=lambda r: -r["strength"])

    # identity is judged on the UNCHANGED part of the page, so an added line does not hide the copy -
    # but only when the changes are small; a rewrite "changes" almost everything and keeps full scoring
    ink_body = (ir | isu) & body
    edited_frac = np.count_nonzero(ink_body & edited) / max(1, np.count_nonzero(ink_body))
    idb = body & ~edited if edited_frac <= MAX_EDIT_FRAC else body

    def frac(a, other_ratio, r, region):
        n = np.count_nonzero(a & region)
        return np.count_nonzero(a & ~blank_near(other_ratio, r) & region) / n if n else 0.0
    # lenient: ink has ink-ish within 4px (survives blur/compression; also true for close rewrites)
    agreement = min(frac(ir, rs, TOL_PX, body), frac(isu, rr, TOL_PX, body))
    # strict: within 1px, plus tile correlation - only a copy reproduces every stroke this closely
    strict = min(frac(ir, rs, 1, idb), frac(isu, rr, 1, idb))
    ncc = tile_ncc(rr, rs, ir & idb, valid)
    if agreement >= CLONE_AT and strict >= COPY_STRICT and (ncc is None or ncc >= COPY_NCC):
        identity = "copy"
    elif agreement >= CLONE_AT:
        identity = "similar"
    else:
        identity = "different"
    return {"inliers": inliers, "agreement": round(agreement, 3), "strict": round(strict, 3),
            "ncc": None if ncc is None else round(ncc, 3), "identity": identity, "regions": regions[:12],
            "warped": warped, "missing": missing, "added": added}


# ---------------------------------------------------------------- metadata

AI_MARKERS = [b"trainedAlgorithmicMedia", b"compositeWithTrainedAlgorithmicMedia", b"OpenAI",
              b"ChatGPT", b"DALL", b"Firefly", b"Gemini", b"Imagen", b"Midjourney", b"Stable Diffusion"]


def metadata(path, rx_date):
    """Returns (level, text) pairs. 'strong' only for facts that are hard to explain innocently - and even
    those only ask for confirmation: a generator marker can come from an innocent edit (enhance, crop in an
    AI-assisted gallery), camera clocks are often wrong, and none of it says who authorised the slip.
    Missing metadata is never evidence: WhatsApp, screenshots and re-saves all strip it."""
    out = []
    raw = Path(path).read_bytes()
    im = Image.open(path)
    exif = im.getexif()
    sub = exif.get_ifd(0x8769) if exif else {}

    hits = [m.decode() for m in AI_MARKERS if m in raw]
    if hits:
        out.append(("strong", f"Generator/AI provenance markers in file: {', '.join(hits)}"))
    if b"c2pa" in raw or b"jumb" in raw:
        out.append(("info", "C2PA / Content Credentials present - inspect at contentcredentials.org/verify"))

    if not exif:
        out.append(("info", "No EXIF - normal for WhatsApp/screenshots; neutral"))
    else:
        cam = [t for t in (33434, 33437, 34855, 37386) if t in sub]  # exposure, f-number, ISO, focal length
        if not exif.get(271) and not exif.get(272) and not cam:
            sw = exif.get(305)
            out.append(("weak", "No camera data - file was written by software, not a camera"
                                + (f" (Software: {sw})" if sw else "")))
        taken = sub.get(36867) or exif.get(306)
        if taken:
            try:
                t = datetime.strptime(str(taken)[:19], "%Y:%m:%d %H:%M:%S").date()
                if rx_date and t < rx_date:
                    out.append(("strong", f"Image file is dated {t}, BEFORE the prescription date {rx_date}"))
            except ValueError:
                pass

    q = getattr(im, "quantization", None)
    if q and max(q[0]) <= 2:
        out.append(("weak", "Saved at JPEG quality ~100 - editor/gallery re-export"))
    return out


# ---------------------------------------------------------------- image quality gate

MIN_SIDE_PX = 400      # shorter side of the received image
MIN_KEYPOINTS = 1500   # usable images had >= 6230 in tests; heavy blur 168
MAX_GLARE = 0.20       # blown-out (saturated) share of the handwritten band; usable 0%, glare 73-79%
MIN_INK, MAX_INK = 0.008, 0.30   # usable 3-8%; blank 0%, near-black 49%
MIN_CONTRAST = 0.25    # how much darker the darkest strokes are than paper; usable >= 0.40, washed-out <= 0.24


def quality(img, sheet, feats):
    """Reasons the image cannot be compared reliably ([] = usable). An unusable image is never
    treated as a 'new slip' - it goes to manual review."""
    reasons = []
    if min(img.shape[:2]) < MIN_SIDE_PX:
        reasons.append(f"image too small ({img.shape[1]}x{img.shape[0]}px)")
    h = sheet.shape[0]
    band = slice(int(BODY_Y[0] * h), int(BODY_Y[1] * h))
    glare = float(np.mean(sheet[band].min(axis=2) >= 250))
    if glare > MAX_GLARE:
        reasons.append(f"glare washes out {glare:.0%} of the writing")
    g = gray(sheet)
    ratio = paper_ratio(g)[band]
    ink_frac = float(np.mean(ratio < INK_BELOW))
    if ink_frac < MIN_INK:
        reasons.append("almost no legible writing (blank, over-exposed or out of focus)")
    elif ink_frac > MAX_INK:
        reasons.append("too dark or noisy to separate writing from paper")
    if 1 - float(np.percentile(ratio, 1)) < MIN_CONTRAST:
        reasons.append("writing is too faint / washed out")
    if len(feats["desc"]) < MIN_KEYPOINTS:
        reasons.append("too blurred - too little detail to match")
    return reasons


# ---------------------------------------------------------------- data files
# The registry, drug-risk list and policy decide verdicts, so a broken file must never crash a check or
# quietly relax it. Each loader validates its file and reports problems; a check then becomes MORE
# cautious (drug list unusable -> every medicine treated as high-risk; registry unusable -> prescriber
# unverified, never "invalid"). Every report carries the hash of each data file it was decided with.

REGISTRY_MAX_AGE_DAYS = 365
# "not in the complete extract" is a hard fact only while the extract is recent: a doctor registered or
# re-registered since would be missing from an old one
COMPLETE_EXTRACT_MAX_AGE_DAYS = 90
DRUG_LIST_MAX_AGE_DAYS = 180
REGISTRY_COVERAGE = ("partial", "complete")


def load_json(p, default):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def save_json(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def iso_date(s):
    try:
        return date.fromisoformat(str(s)) if s else None
    except ValueError:
        return None


def read_data(p):
    """(data, sha12, problem). Never raises: a missing or broken file is reported, not fatal."""
    try:
        raw = Path(p).read_bytes()
    except FileNotFoundError:
        return None, None, f"{Path(p).name} is missing"
    except OSError as ex:
        return None, None, f"{Path(p).name} cannot be read ({ex.strerror})"
    sha = hashlib.sha256(raw).hexdigest()[:12]
    try:
        return json.loads(raw.decode("utf-8")), sha, None
    except (UnicodeDecodeError, json.JSONDecodeError) as ex:
        return None, sha, f"{Path(p).name} is not valid JSON ({ex})"


def validate_registry(data):
    """(entries, meta, problems). Entries with bad fields are kept but marked, so they count as unverified;
    an entry without a name cannot be matched and is dropped."""
    if not isinstance(data, dict):
        return {}, {}, ["registry is not a JSON object"]
    meta = {k: v for k, v in data.items() if k.startswith("_")}
    problems, entries = [], {}
    if meta.get("_coverage", "partial") not in REGISTRY_COVERAGE:
        problems.append(f"_coverage must be one of {REGISTRY_COVERAGE}; treated as partial")
        meta["_coverage"] = "partial"
    for regn, rec in data.items():
        if regn.startswith("_"):
            continue
        if not isinstance(rec, dict) or not isinstance(rec.get("name"), str) or not rec["name"].strip():
            problems.append(f"entry {regn}: no doctor name - ignored")
            continue
        rec = dict(rec)
        bad = []
        if rec.get("verified_on") and not iso_date(rec["verified_on"]):
            bad.append(f"verified_on '{rec['verified_on']}' is not YYYY-MM-DD")
        if rec.get("phone") and not rec.get("phone_source"):
            bad.append("phone has no phone_source - not used for callbacks")
        if rec.get("issuer_key") and not rec.get("issuer_key_source"):
            bad.append("issuer_key has no issuer_key_source - not trusted")
        if bad:
            rec["_problems"] = bad
            problems += [f"entry {regn}: {b}" for b in bad]
        entries[str(regn)] = rec
    return entries, meta, problems


DRUG_TIERS = ("ndps", "high", "routine")
HIGH_TIERS = ("ndps", "high")


def validate_drugs(data):
    """(table, problems, usable). Unusable = no trustworthy classification at all. Tiers: 'ndps' (NDPS Act /
    Schedule X: high-risk, and never dispensed on an override), 'high' (H1 / abuse-prone), 'routine'."""
    if not isinstance(data, dict) or not isinstance(data.get("molecules"), dict) or not data["molecules"]:
        return None, ["drug-risk list has no 'molecules' table"], False
    problems = []
    mols = {}
    for m, tier in data["molecules"].items():
        if tier in DRUG_TIERS:
            mols[str(m).lower()] = tier
        else:
            # an unknown tier must not quietly become "routine"
            mols[str(m).lower()] = "high"
            problems.append(f"molecule {m}: tier '{tier}' is not {'/'.join(DRUG_TIERS)} - treated as high")
    brands = {}
    for b, m in (data.get("brands") or {}).items():
        if str(m).lower() not in mols:
            problems.append(f"brand {b} -> '{m}', which is not in the molecule table - ignored")
            continue
        brands[str(b).lower()] = str(m).lower()
    table = {**{k: v for k, v in data.items() if k.startswith("_")}, "molecules": mols, "brands": brands}
    return table, problems, True


def data_status():
    """State of every data file: hash, whether it can be trusted, and why not. Shown by `data status`,
    attached to every report."""
    out = {}
    reg, sha, err = read_data(REGISTRY)
    entries, meta, probs = validate_registry(reg) if err is None else ({}, {}, [err])
    out["registry"] = {"sha": sha, "usable": err is None and isinstance(reg, dict), "problems": probs,
                       "entries": len(entries), "coverage": meta.get("_coverage", "partial"),
                       "source": meta.get("_source"), "updated_on": meta.get("_updated_on")}
    drugs, sha, err = read_data(DRUG_RISK)
    table, probs, usable = validate_drugs(drugs) if err is None else (None, [err], False)
    checked = iso_date((table or {}).get("_verified_on"))
    stale = not checked or (date.today() - checked).days > DRUG_LIST_MAX_AGE_DAYS
    out["drug_risk"] = {"sha": sha, "usable": usable, "problems": probs,
                        "verified": bool(usable and table.get("_verified") and table.get("_source") and not stale),
                        "source": (table or {}).get("_source"), "verified_on": (table or {}).get("_verified_on"),
                        "molecules": len((table or {}).get("molecules", {}))}
    pol, sha, err = read_data(POLICY)
    probs = [err] if err and POLICY.exists() else []
    if isinstance(pol, dict):
        probs += [f"policy {k} must be true/false - default used" for k in POLICY_DEFAULTS
                  if k in pol and not isinstance(pol[k], bool)]
    elif pol is not None:
        probs.append("policy is not a JSON object - defaults used")
    out["policy"] = {"sha": sha, "usable": not probs, "problems": probs}
    return out


EMPTY_TABLE = {"molecules": {}, "brands": {}}


def drug_table():
    """Validated drug-risk table, or None when it cannot be used (missing, corrupt, empty)."""
    data, _, err = read_data(DRUG_RISK)
    if err:
        return None
    table, _, usable = validate_drugs(data)
    return table if usable else None


def drug_risk(drugs):
    """('high' | 'routine' | None, high-risk hits, names not in the list). None = no drugs given.
    Names not in the list are treated as routine - and reported, so a misspelt or missing
    high-risk brand is visible to the pharmacist instead of silently downgraded. Without a usable
    drug list nothing can be called routine, so everything is 'high'."""
    if not drugs:
        return None, [], []
    table = drug_table()
    if table is None:
        return "high", [], [d.strip() for d in re.split(r"[,;+\n]", drugs) if d.strip()]
    hits, unknown = [], []
    for item in re.split(r"[,;+\n]", drugs):
        toks = re.findall(r"[a-z]{3,}", item.lower())
        mols = [table["brands"].get(t, t) for t in toks]
        if any(table["molecules"].get(m) in HIGH_TIERS for m in mols):
            hits.append(f"{item.strip()} ({next(m for m in mols if table['molecules'].get(m) in HIGH_TIERS)})")
        elif toks and not any(m in table["molecules"] for m in mols):
            unknown.append(item.strip())
    return ("high" if hits else "routine"), hits, unknown


def fail_closed(drugs):
    """Medicines that are never dispensed on an override: NDPS Act / Schedule X ('ndps' tier). Their holds
    close only when the clinic confirms, or a counter check settles the warning. Without a usable drug
    list nothing can be ruled out, so every medicine counts."""
    items = [d.strip() for d in re.split(r"[,;\n]", drugs or "") if d.strip()]
    table = drug_table()
    if table is None:
        return items
    return [i for i in items if any(table["molecules"].get(table["brands"].get(t, t)) == "ndps"
                                    for t in re.findall(r"[a-z]{3,}", i.lower()))]


def parse_drugs(s):
    """{'alprazolam': ['0.25'], 'dolo': ['650']} - first word (brand mapped to molecule) -> numbers."""
    brands = (drug_table() or EMPTY_TABLE)["brands"]
    out = {}
    for item in re.split(r"[,;\n]", s or ""):
        words = re.findall(r"[a-z]{3,}", item.lower())
        if words:
            out[brands.get(words[0], words[0])] = sorted(re.findall(r"\d+(?:\.\d+)?", item))
    return out


def drug_diff(requested, recorded):
    """Differences between what is requested now and what was recorded for the matched slip."""
    a, b = parse_drugs(requested), parse_drugs(recorded)
    if not a or not b:
        return []
    out = []
    for name, nums in a.items():
        if name not in b:
            out.append(f"'{name}' requested but not on the slip as recorded ({recorded})")
        elif nums and b[name] and nums != b[name]:
            out.append(f"'{name}' requested as {'/'.join(nums)}, recorded as {'/'.join(b[name])}")
    return out


def registry():
    """(entries, meta, problem). problem is set when the file itself cannot be used."""
    data, _, err = read_data(REGISTRY)
    if err:
        return {}, {}, err
    entries, meta, _ = validate_registry(data)
    return entries, meta, None if isinstance(data, dict) else "registry is not a JSON object"


def trusted(rec, field):
    """An entry's sourced field (verified registration, callback phone, clinic key) - or None."""
    if not rec:
        return None
    src = {"source": "verified_on", "phone": "phone_source", "issuer_key": "issuer_key_source"}[field]
    if field == "source":
        checked = iso_date(rec.get("verified_on"))
        fresh = checked and (date.today() - checked).days <= REGISTRY_MAX_AGE_DAYS
        return rec.get("source") if fresh else None
    return rec.get(field) if rec.get(src) else None


NAME_NOISE = {"dr", "doctor", "prof", "mbbs", "md", "dgo", "dch", "dnb", "frcs", "mrcp", "bams", "bhms", "bds", "mds",
              "consultant", "physician", "surgeon"}


def name_relation(written, listed):
    """'same' | 'unclear' | 'different' - how the doctor's name on the slip relates to the registry's.
    Initials, word order, spacing and small spelling differences are 'same'. A shared surname with a
    conflicting first name or initial is 'unclear' (variant spelling, or a different doctor). Only names
    with no word in common are 'different', the one name mismatch that is a hard fact."""
    a = [t for t in re.findall(r"[a-z]+", (written or "").lower()) if t not in NAME_NOISE]
    b = [t for t in re.findall(r"[a-z]+", (listed or "").lower()) if t not in NAME_NOISE]
    if not a or not b or "".join(a) in "".join(b) or "".join(b) in "".join(a):
        return "same"

    def like(x, y):
        if len(x) == 1 or len(y) == 1:
            return x[0] == y[0]
        return x == y or difflib.SequenceMatcher(None, x, y).ratio() >= 0.8
    short, long_ = sorted((a, b), key=len)
    if all(any(like(x, y) for y in long_) for x in short):
        return "same"
    word_shared = any(like(x, y) for x in short if len(x) > 1 for y in long_ if len(y) > 1)
    return "unclear" if word_shared else "different"


def regn_key(regn):
    """Canonical Regn No: case, spaces and separators (/ . - _) do not change the number. 'tn 73816' == 'TN/73816'."""
    return re.sub(r"[\s/.\-_]+", "", str(regn or "")).upper()


def regn_relation(a, b):
    """'same' | 'format' | 'different' - how two written forms of a Regn No relate.
    same      = identical once case, spaces and separators are ignored
    format    = same digits, and the council prefixes don't conflict: one side has none, or only zero padding
                differs ('73816' vs 'TN/073816'). Staff and slips write numbers differently: never a contradiction
    different = the digits differ, or both carry council prefixes that disagree ('KA 12345' vs 'TN 12345')"""
    ka, kb = regn_key(a), regn_key(b)
    if ka == kb:
        return "same"
    da, db_ = re.sub(r"\D", "", ka).lstrip("0"), re.sub(r"\D", "", kb).lstrip("0")
    la, lb = re.sub(r"[^A-Z]", "", ka), re.sub(r"[^A-Z]", "", kb)
    if da and da == db_ and (not la or not lb or la == lb):
        return "format"
    return "different"


def prescriber(regn, doctor):
    """('valid' | 'unverified' | 'invalid' | 'unknown', record, message).
    valid      = in the registry with an authoritative source checked within a year
    unverified = in the registry but with no source / stale check, OR not in a registry that only covers
                 some doctors (or in an old or undated complete extract), OR the registry file is unusable,
                 OR the name on the slip only partly matches - treated like unknown
    unknown    = no Regn No (OP chits, small clinics) - not suspicious by itself
    invalid    = hard facts only: not in a recent complete extract, or a verified entry whose doctor's name
                 has no word in common with the name on the slip. Anything softer is 'unverified', because
                 registry data goes stale (doctors move clinics, re-register, change names)."""
    if not regn:
        return "unknown", None, "Regn No not entered / not on slip"
    entries, meta, err = registry()
    if err:
        return "unverified", None, f"Prescriber registry unusable ({err}) - Regn No {regn} could not be checked"
    rec = entries.get(str(regn))
    if not rec:   # the same number written another way ('tn 73816' for 'TN/73816') is the same entry
        same = [k for k in entries if regn_key(k) == regn_key(regn)]
        rec = entries[same[0]] if len(same) == 1 else None
    if not rec:
        near = [k for k in entries if regn_relation(k, regn) == "format"]
        if near:   # same digits, prefix or zero padding differs: ask for a look-up, never 'invalid'
            listed = ", ".join(f"{k} ({entries[k]['name']})" for k in near[:3])
            return "unverified", None, (f"Regn No {regn} is not in the registry in this form, but {listed} has the "
                                        f"same number - check the council prefix on the medical council register")
        if meta.get("_coverage") == "complete":
            updated = iso_date(meta.get("_updated_on"))
            if updated and (date.today() - updated).days <= COMPLETE_EXTRACT_MAX_AGE_DAYS:
                return "invalid", None, (f"Regn No {regn} not found in registry (complete extract: "
                                         f"{meta.get('_source') or 'source not recorded'}, {updated})")
            return "unverified", None, (f"Regn No {regn} is not in the complete extract, but the extract is "
                                        f"{'dated ' + str(updated) if updated else 'undated'} - a doctor registered "
                                        f"since may be missing: look it up on the medical council register")
        return "unverified", None, (f"Regn No {regn} is not in the local registry, which lists only some doctors - "
                                    f"look it up on the medical council register")
    source = trusted(rec, "source")
    relation = name_relation(doctor, rec["name"]) if doctor else "same"
    if relation == "different":
        if source:
            return "invalid", rec, f"Regn No {regn} belongs to {rec['name']}, not {doctor}"
        return "unverified", rec, (f"Regn No {regn} is listed for {rec['name']}, not {doctor} - but that entry was "
                                   f"never checked against an authoritative source")
    if relation == "unclear":
        return "unverified", rec, (f"Regn No {regn} is listed for {rec['name']}; the slip says {doctor}. This may "
                                   f"be a spelling variant or a different doctor - look it up on the council register")
    if not source:
        return "unverified", rec, (f"Regn No {regn} -> {rec['name']}, but the registry entry has no authoritative "
                                   f"source checked in the last year - treated as unverified")
    return "valid", rec, f"Regn No {regn} -> {rec['name']} (source: {source}, checked {rec['verified_on']})"


POLICY_DEFAULTS = {"confirm_new_high_risk": True, "require_receipt_ndps": False}


def policy():
    """Policy switches; a missing, broken or mistyped setting falls back to the cautious default."""
    data, _, _ = read_data(POLICY)
    data = data if isinstance(data, dict) else {}
    return {k: data[k] if isinstance(data.get(k), bool) else v for k, v in POLICY_DEFAULTS.items()}


# ---------------------------------------------------------------- medicine entry review
# Typed (and, later, OCR-read) medicine lines are where 0.25 becomes 2.5 and Lonazep becomes Lonapen.
# These checks never change the verdict; they list what staff must confirm against the slip before
# the check runs. The counter app will not run a check until each item is confirmed.

def review_drugs(text):
    """[{'item', 'issue'}] - entries that must be confirmed against the slip before checking."""
    table = drug_table()
    if not text or table is None:
        return []
    known = sorted(set(table["brands"]) | set(table["molecules"]))
    out = []
    for item in [s.strip() for s in re.split(r"[,;\n]", text) if s.strip()]:
        words = re.findall(r"[a-z]{3,}", item.lower())
        if not words:
            out.append({"item": item, "issue": "no medicine name recognised"})
            continue
        name = words[0]
        mol = table["brands"].get(name, name)
        if mol not in table["molecules"]:
            near = difflib.get_close_matches(name, known, n=1, cutoff=0.75)
            if near:
                tier = table["molecules"].get(table["brands"].get(near[0], near[0]))
                out.append({"item": item, "issue": f"'{name}' is not in the drug list - did you mean "
                                                   f"'{near[0]}'{' (HIGH-RISK)' if tier in HIGH_TIERS else ''}?"})
        if re.search(r"(?<![\d.])\.\d", item):
            out.append({"item": item, "issue": "strength starts with '.' - confirm it is 0.x, not x"})
        if re.search(r"\d\.\d*0(?!\d)", item):
            out.append({"item": item, "issue": "trailing zero in the strength (e.g. 5.0 can be read as 50) - confirm"})
        if table["molecules"].get(mol) in HIGH_TIERS and not re.search(r"\d", item):
            out.append({"item": item, "issue": "high-risk medicine without a strength - enter it from the slip"})
    return out


# ---------------------------------------------------------------- storage (SQLite)
# One database file: transactions (no half-written ledger after a power cut), WAL so `watch` and a
# counter `check` can run at the same time on one PC, an append-only audit trail, online backup.
# Feature files stay on disk next to it; images only for dispensed high-risk slips.

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('dispensed', 'seen')),
  image TEXT, sha256 TEXT, date TEXT NOT NULL,
  regn_no TEXT, doctor TEXT, drugs TEXT, note TEXT, verdict TEXT,
  fp BLOB NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS entries_date ON entries(date);
CREATE INDEX IF NOT EXISTS entries_sha ON entries(sha256);
CREATE TABLE IF NOT EXISTS holds (
  id TEXT PRIMARY KEY, created TEXT NOT NULL, image TEXT, verdicts TEXT, findings TEXT,
  status TEXT NOT NULL CHECK (status IN ('open', 'waiting', 'closed')));
CREATE TABLE IF NOT EXISTS audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL,
  action TEXT NOT NULL, ref TEXT, detail TEXT);
CREATE TRIGGER IF NOT EXISTS audit_append_only_u BEFORE UPDATE ON audit
  BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_append_only_d BEFORE DELETE ON audit
  BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TABLE IF NOT EXISTS audit_chain (
  seq INTEGER PRIMARY KEY, detail_sha TEXT NOT NULL, hash TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS audit_chain_append_only_u BEFORE UPDATE ON audit_chain
  BEGIN SELECT RAISE(ABORT, 'audit chain is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_chain_append_only_d BEFORE DELETE ON audit_chain
  BEGIN SELECT RAISE(ABORT, 'audit chain is append-only'); END;
CREATE TABLE IF NOT EXISTS staff (
  name TEXT PRIMARY KEY, role TEXT NOT NULL CHECK (role IN ('pharmacist', 'staff')),
  pin_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS issuance_codes (
  regn_no TEXT NOT NULL, serial TEXT NOT NULL, first_seen TEXT NOT NULL, sha256 TEXT NOT NULL,
  PRIMARY KEY (regn_no, serial));
CREATE TABLE IF NOT EXISTS patients (
  name TEXT PRIMARY KEY COLLATE NOCASE, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
  visits INTEGER NOT NULL DEFAULT 1);
"""
_CONNS = {}            # (db path, thread id) -> connection: sqlite3 connections must not cross threads
_CONNS_LOCK = threading.Lock()
_LOCAL = threading.local()


def actor():
    """Who is acting: the signed-in staff member (counter app), else RXCHECK_USER, else the OS user.
    Only the counter app authenticates; the CLI records a claimed name."""
    return getattr(_LOCAL, "actor", None) or os.environ.get("RXCHECK_USER") or getpass.getuser()


def set_actor(name):
    _LOCAL.actor = name


def now():
    return datetime.now().isoformat(timespec="seconds")


def db():
    key = (str(DB), threading.get_ident())
    con = _CONNS.get(key)
    if con is None:
        DB.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(key[0], timeout=30, check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")    # survive power loss at the cost of a little speed
        con.execute("PRAGMA busy_timeout=30000")
        con.executescript(SCHEMA)
        with _CONNS_LOCK:
            _CONNS[key] = con
        chain_existing(con)
        migrate_json(con)
    return con


def close_thread_db():
    """Close this thread's connection (server request threads end; their connections must not pile up)."""
    with _CONNS_LOCK:
        con = _CONNS.pop((str(DB), threading.get_ident()), None)
    if con is not None:
        con.close()


def close_db():
    with _CONNS_LOCK:
        for con in _CONNS.values():
            con.close()
        _CONNS.clear()


# Hash chain: each audit record's hash covers the previous hash, so changing, removing or inserting a
# record breaks every hash after it. The triggers stop casual edits; the chain makes a determined edit
# (triggers dropped, rows rewritten) detectable - against any backup or written-down head taken earlier
# (`verify --against`, and every `backup` compares itself with the previous one). The chain covers a hash
# of each record's detail rather than the detail itself. Records hold decisions, hashes and staff names,
# never images or patient names.
CHAIN_GENESIS = "0" * 64
CHAIN_VERSION = 1      # PRAGMA user_version once the trail is chained: a missing chain is then a problem


def _detail_sha(detail):
    return hashlib.sha256((detail or "").encode("utf-8")).hexdigest()


def _chain_hash(prev, seq, at, who, action, ref, detail_sha):
    body = json.dumps([seq, at, who, action, ref, detail_sha], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(f"{prev}|{body}".encode("utf-8")).hexdigest()


def _link(con, seq, at, who, action, ref, detail):
    """Chain one audit record. Runs inside the writer's transaction, which holds the write lock, so the
    previous hash cannot change underneath it."""
    prev = con.execute("SELECT hash FROM audit_chain WHERE seq < ? ORDER BY seq DESC LIMIT 1", (seq,)).fetchone()
    dsha = _detail_sha(detail)
    con.execute("INSERT INTO audit_chain (seq, detail_sha, hash) VALUES (?, ?, ?)",
                (seq, dsha, _chain_hash(prev[0] if prev else CHAIN_GENESIS, seq, at, who, action, ref, dsha)))


def audit(con, action, ref, detail=None):
    at, who, det = now(), actor(), json.dumps(detail) if detail is not None else None
    seq = con.execute("INSERT INTO audit (at, actor, action, ref, detail) VALUES (?, ?, ?, ?, ?)",
                      (at, who, action, ref, det)).lastrowid
    _link(con, seq, at, who, action, ref, det)


def chain_existing(con):
    """First start after the upgrade to a chained trail: chain the records written before it, once.
    Afterwards (user_version set) nothing is ever chained retrospectively - an unchained record is a problem."""
    if con.execute("PRAGMA user_version").fetchone()[0] >= CHAIN_VERSION:
        return
    con.execute("BEGIN IMMEDIATE")
    try:
        if con.execute("PRAGMA user_version").fetchone()[0] < CHAIN_VERSION:   # another process may have won
            rows = con.execute("SELECT a.* FROM audit a LEFT JOIN audit_chain c USING (seq) "
                               "WHERE c.seq IS NULL ORDER BY a.seq").fetchall()
            for x in rows:
                _link(con, x["seq"], x["at"], x["actor"], x["action"], x["ref"], x["detail"])
            if rows:
                audit(con, "chain-start", None, {"chained_existing": len(rows)})
            con.execute(f"PRAGMA user_version = {CHAIN_VERSION}")
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def chain_head(con):
    """(seq, hash) of the last chained audit record, or (0, genesis)."""
    row = con.execute("SELECT seq, hash FROM audit_chain ORDER BY seq DESC LIMIT 1").fetchone()
    return (row[0], row[1]) if row else (0, CHAIN_GENESIS)


def check_chain(con):
    """Problems with the audit hash chain on an open connection. [] = intact."""
    tables = {x[0] for x in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "audit_chain" not in tables or con.execute("PRAGMA user_version").fetchone()[0] < CHAIN_VERSION:
        if con.execute("PRAGMA user_version").fetchone()[0] >= CHAIN_VERSION:
            return ["audit hash chain is missing"]
        return []    # written before hash chaining: chained on the next start
    problems = []
    trig = {x[0] for x in con.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    for t in ("audit_chain_append_only_u", "audit_chain_append_only_d"):
        if t not in trig:
            problems.append(f"audit protection trigger {t} is missing")
    prev, unchained = CHAIN_GENESIS, []
    for x in con.execute("SELECT a.seq, a.at, a.actor, a.action, a.ref, a.detail, c.detail_sha, c.hash "
                         "FROM audit a LEFT JOIN audit_chain c USING (seq) ORDER BY a.seq"):
        seq, at, who, action, ref, detail, dsha, h = tuple(x)
        if h is None:
            unchained.append(seq)
            continue
        if dsha != _detail_sha(detail):
            problems.append(f"audit record {seq}: its detail was changed after it was written")
        if h != _chain_hash(prev, seq, at, who, action, ref, dsha):
            problems.append(f"audit hash chain broken at record {seq}: a record at or before it was changed, "
                            f"removed or inserted")
            break
        prev = h
    if unchained:
        problems.append(f"{len(unchained)} audit record(s) are not in the hash chain (first: {unchained[0]})")
    orphans = con.execute("SELECT COUNT(*) FROM audit_chain WHERE seq NOT IN (SELECT seq FROM audit)").fetchone()[0]
    if orphans:
        problems.append(f"{orphans} chained audit record(s) were deleted")
    return problems


def chain_extends(earlier_db, con):
    """None if the live trail still contains, unchanged, every record of an earlier copy (a backup);
    otherwise what differs. This is what makes the chain tamper-evident against someone who could
    rewrite the whole live file: they cannot also rewrite copies kept elsewhere."""
    try:
        old = sqlite3.connect(f"file:{Path(earlier_db).as_posix()}?mode=ro", uri=True)
        try:
            tables = {x[0] for x in old.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "audit_chain" not in tables:
                return None   # copy made before hash chaining: nothing to compare
            seq, h = chain_head(old)
        finally:
            old.close()
    except sqlite3.DatabaseError as ex:
        return f"earlier copy {earlier_db} cannot be read ({ex})"
    if not seq:
        return None
    row = con.execute("SELECT hash FROM audit_chain WHERE seq = ?", (seq,)).fetchone()
    if not row or row[0] != h:
        return (f"audit trail differs from the earlier copy {Path(earlier_db).parent.name} at or before record "
                f"{seq}: history was changed after that copy was made")
    return None


def entries_since(since):
    return [dict(r) for r in db().execute("SELECT * FROM entries WHERE date >= ? ORDER BY date, id", (since,))]


def ledger_features(e):
    """Cached features of a ledger/index entry (computed once, on add or first use)."""
    f = FEATURES / f"{e['id']}.npz"
    if f.exists():
        z = np.load(f)
        return {k: z[k] for k in z.files}
    feats = features(paper_crop(read_bgr(LEDGER_DIR / e["image"])))
    FEATURES.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(f, **feats)
    return feats


def fp_matrix(entries):
    """Fingerprints of the given entries as one matrix - shortlisting thousands of slips is one multiply."""
    if not entries:
        return np.zeros((0, 768), np.float32)
    return np.stack([np.frombuffer(e["fp"], np.float32) for e in entries])


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _insert_entry(con, row, action):
    with con:   # one transaction: entry + audit record, or neither
        con.execute("INSERT INTO entries (id, kind, image, sha256, date, regn_no, doctor, drugs, note, verdict, fp,"
                    " created_at, created_by) VALUES (:id, :kind, :image, :sha256, :date, :regn_no, :doctor, :drugs,"
                    " :note, :verdict, :fp, :created_at, :created_by)", row)
        audit(con, action, row["id"], {k: row[k] for k in ("kind", "date", "regn_no", "drugs", "verdict")})


def add_entry(image, eid, dispensed, regn=None, doctor=None, drugs="", note=""):
    """Image ledger: slips dispensed for high-risk drugs (kept alongside the H1 register)."""
    con = db()
    if con.execute("SELECT 1 FROM entries WHERE id = ?", (eid,)).fetchone():
        raise ValueError(f"ledger already has {eid}")
    src = Path(image)
    rel = f"images/{eid}{src.suffix.lower()}"
    (LEDGER_DIR / "images").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, LEDGER_DIR / rel)
    feats = features(paper_crop(read_bgr(src)))
    FEATURES.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(FEATURES / f"{eid}.npz", **feats)
    row = {"id": eid, "kind": "dispensed", "image": rel, "sha256": sha256(src), "date": dispensed,
           "regn_no": regn, "doctor": doctor, "drugs": drugs, "note": note, "verdict": None,
           "fp": np.float32(feats["fp"]).tobytes(), "created_at": now(), "created_by": actor()}
    _insert_entry(con, row, "add")
    _shared_publish(eid, "dispensed", feats["fp"], dispensed)
    return row


def record_seen(image, feats, dens, sha, drugs=None, regn=None, doctor=None, verdict=None, when=None):
    """No-image index: every checked slip, so re-use of routine prescriptions is noticed too.
    Stores keypoints + coarse density + file hash and what was requested - not the image."""
    when = when or date.today().isoformat()
    eid = f"S-{when}-{sha[:10]}"
    con = db()
    old = con.execute("SELECT drugs, regn_no, doctor FROM entries WHERE id = ?", (eid,)).fetchone()
    if old:
        # same slip already indexed today (e.g. pre-checked by `watch` with no medicines known yet): fill in
        # what the counter now knows, so a later re-use with a changed request can be noticed
        fill = {k: v for k, v in (("drugs", drugs), ("regn_no", regn), ("doctor", doctor)) if v and not old[k]}
        if fill:
            with con:
                con.execute(f"UPDATE entries SET {', '.join(f'{k} = ?' for k in fill)}, verdict = ? WHERE id = ?",
                            (*fill.values(), verdict, eid))
                audit(con, "index-update", eid, fill)
        return eid
    FEATURES.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(FEATURES / f"{eid}.npz", **compact(feats, dens))
    row = {"id": eid, "kind": "seen", "image": None, "sha256": sha, "date": when, "regn_no": regn,
           "doctor": doctor, "drugs": drugs or "", "note": "", "verdict": verdict,
           "fp": np.float32(feats["fp"]).tobytes(), "created_at": now(), "created_by": actor()}
    _insert_entry(con, row, "index")
    _shared_publish(eid, "seen", feats["fp"], when)
    return eid


def _shared_publish(eid, kind, fp, when):
    """Offer a freshly recorded slip to the shared index (a no-op unless this pharmacy joined a network).
    Never lets a network problem break recording: an unpublished entry is picked up by `rxcheck shared sync`."""
    try:
        import sharedindex
        sharedindex.publish(eid, kind, fp, when)
    except Exception as ex:   # noqa: BLE001 - recording a slip must not fail because of the network layer
        print(f"shared index: could not publish {eid}: {ex}", file=sys.stderr)


def migrate_json(con):
    """One-time import of the earlier JSON ledger / holds files."""
    lj, hj = LEDGER_DIR / "ledger.json", LEDGER_DIR / "holds.json"
    if lj.exists():
        for e in load_json(lj, []):
            if con.execute("SELECT 1 FROM entries WHERE id = ?", (e["id"],)).fetchone():
                continue
            fp = ledger_features(e)["fp"]
            row = {"id": e["id"], "kind": e.get("kind", "dispensed"), "image": e.get("image"),
                   "sha256": e.get("sha256") or (sha256(LEDGER_DIR / e["image"]) if e.get("image") else None),
                   "date": e.get("dispensed") or e.get("seen"), "regn_no": e.get("regn_no"),
                   "doctor": e.get("doctor"), "drugs": e.get("drugs", ""), "note": e.get("note", ""),
                   "verdict": e.get("verdict"), "fp": np.float32(fp).tobytes(), "created_at": now(),
                   "created_by": "migration"}
            _insert_entry(con, row, "migrate")
        lj.rename(lj.with_suffix(".json.migrated"))
    if hj.exists():
        with con:
            for h in load_json(hj, []):
                con.execute("INSERT OR IGNORE INTO holds VALUES (?, ?, ?, ?, ?, ?)",
                            (h["id"], h["created"], h["image"], json.dumps(h["verdicts"]),
                             json.dumps(h["findings"]), h["status"]))
                audit(con, "migrate", h["id"], {"status": h["status"]})
        hj.rename(hj.with_suffix(".json.migrated"))


# ---------------------------------------------------------------- staff (counter app sign-in)

PIN_ITERATIONS = 200_000
ROLES = ("pharmacist", "staff")


def hash_pin(pin, salt=None):
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, PIN_ITERATIONS)
    return f"pbkdf2_sha256${PIN_ITERATIONS}${salt.hex()}${dk.hex()}"


def check_pin(pin, stored):
    try:
        _, it, salt, want = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), int(it))
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(dk.hex(), want)


def add_staff(name, role, pin):
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    if not re.fullmatch(r"\d{6,}", pin or ""):
        raise ValueError("PIN must be at least 6 digits")
    con = db()
    with con:
        con.execute("INSERT INTO staff (name, role, pin_hash, active, created_at) VALUES (?, ?, ?, 1, ?)",
                    (name, role, hash_pin(pin), now()))
        audit(con, "staff-add", name, {"role": role})


def authenticate(name, pin):
    """Staff row for a correct PIN of an active account, else None."""
    row = db().execute("SELECT * FROM staff WHERE name = ? AND active = 1", (name,)).fetchone()
    if row is None:
        check_pin(pin, hash_pin("000000"))   # same work either way: no timing hint that the name exists
        return None
    return dict(row) if check_pin(pin, row["pin_hash"]) else None


# ---------------------------------------------------------------- issuance codes
# The one check here that speaks to AUTHORSHIP. A participating clinic signs each prescription it issues
# (Ed25519) and prints the result as a QR code on the slip. The pharmacy holds the clinic's public key in
# the registry, collected in person (issuer_key_source). A forger can copy a QR code but cannot make one
# for a new prescription, and a copied code is caught by what it binds (Regn No, date, medicines, patient)
# and by its serial being seen again. Pixel analysis cannot provide this; it needs the clinic to take part.

ISSUE_PREFIX = "RX1:"   # binary payload + Ed25519 signature, Base45-encoded so the QR uses its dense
                        # alphanumeric mode (as EU vaccination certificates do): ~190 chars -> QR version 7
EPOCH = date(2000, 1, 1)
B45 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:"


def b45encode(b):
    out = []
    for i in range(0, len(b), 2):
        if i + 1 < len(b):
            n = b[i] * 256 + b[i + 1]
            out += [B45[n % 45], B45[n // 45 % 45], B45[n // 2025]]
        else:
            out += [B45[b[i] % 45], B45[b[i] // 45]]
    return "".join(out)


def b45decode(s):
    vals = [B45.index(c) for c in s]   # ValueError on a foreign character
    out = bytearray()
    for i in range(0, len(vals), 3):
        chunk = vals[i:i + 3]
        n = sum(v * 45 ** k for k, v in enumerate(chunk))
        if len(chunk) == 3:
            if n > 0xFFFF:
                raise ValueError("bad base45")
            out += bytes([n // 256, n % 256])
        elif len(chunk) == 2 and n <= 0xFF:
            out.append(n)
        else:
            raise ValueError("bad base45")
    return bytes(out)


def _b64e(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def crypto_available():
    try:
        import cryptography.hazmat.primitives.asymmetric.ed25519  # noqa: F401
        return True
    except ImportError:
        return False


def _field(s, limit, what):
    b = (s or "").strip().encode("utf-8")
    if len(b) > limit:
        raise ValueError(f"{what} is too long for the QR code ({len(b)} > {limit} bytes) - abbreviate")
    return bytes([len(b)]) + b


def issue_payload(regn, rx_date, serial, patient, drugs):
    """Signed bytes: version, Regn No, date (days since 2000), 4-byte serial, patient, medicines as written."""
    d = iso_date(rx_date)
    if not d:
        raise ValueError("date must be YYYY-MM-DD")
    if not str(regn or "").strip() or not (drugs or "").strip():
        raise ValueError("Regn No and medicines are required")
    return (b"\x01" + _field(str(regn), 20, "Regn No") + (d - EPOCH).days.to_bytes(2, "big") + serial
            + _field(patient, 40, "patient") + _field(drugs, 120, "medicines"))


def parse_payload(b):
    if not b or b[0] != 1:
        raise ValueError("unknown issuance code version")
    pos, out = 1, {}
    def take(n):
        nonlocal pos
        if pos + n > len(b):
            raise ValueError("truncated")
        pos += n
        return b[pos - n:pos]
    out["regn"] = take(take(1)[0]).decode()
    out["date"] = (EPOCH + timedelta(days=int.from_bytes(take(2), "big"))).isoformat()
    out["serial"] = take(4).hex().upper()
    out["patient"] = take(take(1)[0]).decode()
    out["drugs"] = take(take(1)[0]).decode()
    return out, pos


def clinic_keygen(out):
    """Write a new clinic private key (keep it at the clinic) and return the public key to give pharmacies."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    Path(out).write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption()))
    return _b64e(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def make_code(key_pem, regn, rx_date, drugs, patient="", serial=None):
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    key = load_pem_private_key(key_pem, None)
    payload = issue_payload(regn, rx_date, serial or secrets.token_bytes(4), patient, drugs)
    return ISSUE_PREFIX + b45encode(payload + key.sign(payload))


def code_matrix(code):
    """QR modules (True = dark) with the 4-module quiet zone, error-correction level M. Uses the `qrcode`
    package: OpenCV's own encoder writes unreadable codes above version 7 (its decoder is fine)."""
    import qrcode
    q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=1, border=4)
    q.add_data(code)
    q.make(fit=True)
    return np.array(q.get_matrix(), bool)


def code_qr(code, out_png, module_px=8):
    img = (~code_matrix(code)).astype(np.uint8) * 255
    img = cv2.resize(img, None, fx=module_px, fy=module_px, interpolation=cv2.INTER_NEAREST)
    cv2.imencode(".png", img)[1].tofile(str(out_png))


def _decode_region(det, g, pts):
    """Decode one located QR code that failed at its photographed size: crop, upscale, binarise.
    Rescues forwarded / recompressed images where modules are 2-4 px and soft."""
    pts = pts.reshape(-1, 2)
    pad = 0.15 * np.ptp(pts, 0)
    x0, y0 = np.maximum(pts.min(0) - pad, 0).astype(int)
    x1, y1 = (pts.max(0) + pad).astype(int)
    crop = g[y0:y1, x0:x1]
    if crop.size == 0:
        return ""
    for f in (2, 3, 4):
        up = cv2.resize(crop, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
        binar = cv2.threshold(cv2.GaussianBlur(up, (0, 0), f * 0.5), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
        for v in (up, binar):
            t = det.detectAndDecode(v)[0]
            if t:
                return t
    return ""


def find_codes(sheet):
    """Text of the QR codes on the (cropped, normalised) sheet, stopping at the first issuance code.
    OpenCV's two QR detectors fail on different images, so both run, on the grey sheet and then a binarised
    one, before a crop-and-upscale fallback. In tests a code printed ~3.3 cm wide was read in 96/96 cases
    (12 random codes x printed, WhatsApp, forwarded 5x, dark, photocopy, angled, dim light, phone screen)."""
    g = gray(sheet)
    std, aruco = cv2.QRCodeDetector(), cv2.QRCodeDetectorAruco()
    found = []

    def add(t):
        if t and t not in found:
            found.append(t)
        return bool(t) and t.startswith(ISSUE_PREFIX)

    try:
        # presence gate at half size (~90 ms): found 64/64 printed codes, fired on 0/6 slips without one
        half = cv2.resize(g, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
        if not std.detect(half)[0] and not aruco.detect(half)[0]:
            return []   # no QR code on the slip (the common case): stop here
        binar = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
        for img in (g, binar):
            for det in (std, aruco):
                if add(det.detectAndDecode(img)[0]):
                    return found
        ok, texts, pts, _ = std.detectAndDecodeMulti(g)   # a payment / website QR may sit beside it
        for i, t in enumerate(texts if ok else ()):
            if add(t or _decode_region(std, g, pts[i])):
                return found
        ok, p = std.detect(g)
        if ok and p is not None:
            add(_decode_region(std, g, p))
    except cv2.error:
        pass
    return found


def verify_code(code):
    """{'status': verified | invalid | unverifiable, 'message', 'fields'}.
    unverifiable = no trusted key for that prescriber / crypto library missing: treated as no code at all."""
    try:
        raw = b45decode(code[len(ISSUE_PREFIX):]) if code.startswith(ISSUE_PREFIX) else b""
        fields, n = parse_payload(raw)
        payload, sig = raw[:n], raw[n:]
        if len(sig) != 64:
            raise ValueError("signature missing")
    except (ValueError, UnicodeDecodeError):
        return {"status": "invalid", "message": "issuance code is malformed", "fields": None}
    regn, rx_date, patient, drugs = fields["regn"], fields["date"], fields["patient"], fields["drugs"]
    entries, _, _ = registry()
    rec = entries.get(regn)
    key = trusted(rec, "issuer_key")
    if not key:
        return {"status": "unverifiable", "fields": fields,
                "message": f"slip carries a signed code for Regn No {regn}, but no clinic key collected in person "
                           f"is on record - the code cannot be checked"}
    if not crypto_available():
        return {"status": "unverifiable", "fields": fields,
                "message": "issuance code present but the 'cryptography' package is not installed"}
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        Ed25519PublicKey.from_public_bytes(_b64d(key)).verify(sig, payload)
    except (InvalidSignature, ValueError):
        return {"status": "invalid", "fields": fields,
                "message": f"issuance code does NOT verify with the clinic key on record for {rec['name']} - it was "
                           f"not signed with that key (if the clinic has changed its key, collect the new one in person)"}
    return {"status": "verified", "fields": fields,
            "message": f"issued by {rec['name']} (Regn No {regn}) on {rx_date}"
                       + (f" for {patient}" if patient else "") + f": {drugs}"}


def code_seen(regn, serial):
    row = db().execute("SELECT * FROM issuance_codes WHERE regn_no = ? AND serial = ?", (regn, serial)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------- decision

def decide(flags, risk, pol=None):
    """Verdict from document flags + drug risk. RED needs a fact that holds up when challenged: an edited
    copy of a recorded slip, a Regn No that is not the doctor's, or a clinic signature that fails.
    Provenance metadata (AI markers, file dates) is supporting evidence only - it asks for confirmation,
    never rejects. Weak metadata does not change the verdict at all: at a busy counter, false alarms train
    staff to ignore the tool. Uncertain similarity, unusable images or detail mismatches go to pharmacist
    review (AMBER), never to an automated accusation."""
    pol = policy() if pol is None else pol
    issuance = flags.get("issuance", "none")
    if flags["edited"]:
        return "RED"
    if flags["prescriber"] == "invalid" or issuance == "invalid":
        return "RED" if risk == "high" else "AMBER"
    if flags.get("unusable") or flags.get("similar") or flags.get("mismatch") or flags.get("meta_conflict"):
        return "AMBER"
    if risk == "high":
        if flags["repeat"]:
            return "AMBER"
        if issuance == "verified" and flags["prescriber"] == "valid":
            return "GREEN"   # the clinic's signature is issuance confirmation, done before the customer arrived
        if flags["prescriber"] in ("unknown", "unverified"):
            return "AMBER"
        if flags.get("new_slip") and flags.get("shared_unreachable"):
            return "AMBER"   # not knowing what other pharmacies saw is never more lenient, whatever the policy
        if flags.get("new_slip") and pol.get("confirm_new_high_risk", True):
            return "AMBER"   # a valid Regn No proves the doctor exists, not that they wrote this slip
    return "GREEN"


ACTIONS = {
    "RED": ("Do not dispense until confirmed. Call the doctor/clinic on the registry number (not the number printed "
            "on the slip). A failed check is not, by itself, proof of fraud."),
    "AMBER": "Confirm before dispensing: call the clinic, or ask for the original slip / OP card.",
    "GREEN": "Dispense with normal checks.",
}
# GREEN is deliberately not "verified": a clean image, a valid Regn No and no duplicate do not prove
# the doctor issued this prescription.
# RED means "do not dispense until confirmed", never "this is fraud": genuine patients get caught by stale data,
# and an accusation the pharmacy cannot back up hurts both.
LABELS = {"RED": "do not dispense until confirmed", "AMBER": "needs confirmation", "GREEN": "no issues found"}
ORDER = ["GREEN", "AMBER", "RED"]


# ---------------------------------------------------------------- after a warning
# The bottleneck at a counter is not the ~1 s check; it is what happens after a warning. If every warning
# means "phone the clinic", an unanswered phone and a queue teach staff to wave warnings through. So each
# warning names the QUICKEST step that actually settles it:
#   retake  - a better photo of the paper slip, then check again          (seconds, no call)
#   counter - something the pharmacist can see at the counter: the paper original, the recorded slip,
#             the H1 register; or sell only what the slip says              (under a minute, no call)
#   call    - only the clinic can settle it                                 (minutes; may not answer)
# and medicines are decided one by one, so routine items with no issue are handed over while only the
# item that needs confirmation waits. Resolving a hold records which check was done (REVIEW_REASONS);
# dispensing without the step that was asked for is an override, allowed but counted (`stats`).

PATHS = ("retake", "counter", "call")
PATH_LABELS = {"retake": "Retake the photo - no call needed", "counter": "Check at the counter - no call needed",
               "call": "Call the clinic"}


def next_step(flags, risk, ctx=None, pol=None):
    """(path, [instructions]) for medicines of this risk under these document flags; (None, []) if GREEN."""
    pol = policy() if pol is None else pol
    if decide(flags, risk, pol) == "GREEN":
        return None, []
    ctx = ctx or {}
    rec = ctx.get("recorded") or {}
    when = f" (recorded {rec['date']}: {rec['drugs'] or 'no medicines recorded'})" if rec.get("date") else ""
    if flags.get("unusable"):
        return "retake", ["Photograph the paper slip itself - flat, in good light, not a phone screen - and check "
                          "again. If there is no paper slip, treat the prescription as unchecked."]
    high = risk == "high"
    call, counter = [], []
    if flags["edited"]:
        call.append("This is an edited copy of a slip already dispensed: do not dispense until the clinic confirms it. "
                    "Call the clinic on the registry number - never the number printed on the slip.")
    if flags["prescriber"] == "invalid":
        call.append("The Regn No is not this doctor's: call the clinic on the registry number, never the one on the slip.")
    if flags.get("issuance") == "invalid":
        call.append("The clinic signature in the QR code fails: call the clinic on the registry number.")
    if flags.get("meta_conflict"):
        call.append("The image file carries an AI-generator marker or predates the prescription: ask the clinic "
                    "whether it issued this slip.")
    if flags["similar"]:
        if high:
            call.append("Very similar to an earlier slip but not a copy - for a high-risk medicine, confirm with the "
                        "clinic that it wrote a new slip.")
        else:
            counter.append(f"Very similar to an earlier slip{when} but not a copy: look at the paper original. A fresh "
                           f"slip from the same doctor (new date, real ink) is fine; a print or photocopy is not.")
    if flags["mismatch"]:
        if flags.get("doc_mismatch"):
            counter.append("The Regn No or date entered differs from the record: re-read them on the slip. If the "
                           "slip itself differs, call the clinic.")
        if not flags.get("doc_mismatch") or flags.get("drug_mismatch"):
            signed = ctx.get("signed_drugs")
            what = f" (clinic-signed: {signed})" if signed else when
            counter.append(f"Sell only what the prescription says{what}. A different medicine or a higher strength "
                           f"needs the doctor.")
    sh = ctx.get("shared") or {}
    if high and flags.get("shared"):
        call.append(f"Another pharmacy {'dispensed' if sh.get('kind') == 'dispensed' else 'checked'} this slip on "
                    f"{sh.get('date', 'an earlier date')}; this pharmacy's own record cannot show whether repeats are "
                    f"allowed. Call the clinic on the registry number, or ask the customer for the paper original.")
    if high and flags["repeat"] and not flags.get("shared"):
        counter.append(f"This slip was already presented{(' on ' + rec['date']) if rec.get('date') else ''}. Dispense "
                       f"again only if the prescription allows repeats and the H1 register shows some are left; "
                       f"otherwise refuse, or call the clinic.")
    if high and flags["prescriber"] in ("unknown", "unverified") and not flags["edited"]:
        call.append("The prescriber is not verified and this is a high-risk medicine: look the doctor up on the "
                    "medical council register, then call the clinic on a number found independently.")
    if (high and flags.get("new_slip") and flags["prescriber"] == "valid" and flags.get("issuance") != "verified"
            and (pol.get("confirm_new_high_risk", True) or flags.get("shared_unreachable"))):
        unreachable = " here, and the shared index could not be reached to ask other pharmacies" \
            if flags.get("shared_unreachable") else ""
        call.append(f"First time this slip is seen{unreachable}: call the clinic on the registry number to confirm it "
                    "issued this prescription. (Clinics that print a signed QR code on their slips make this call "
                    "unnecessary.)")
    if not call and not counter:   # never leave a warning without a step (tests check this never happens)
        call.append(ACTIONS[decide(flags, risk, pol)])
    return ("call" if call else "counter"), call + counter


def item_verdicts(drugs, flags, pol, recorded=None, signed=None, shared=None):
    """Each medicine decided on its own: document-level problems apply to all, a medicine that differs from
    the recorded or signed prescription only to itself. [{'item', 'risk', 'verdict'}]"""
    out = []
    for item in [x.strip() for x in re.split(r"[,;\n]", drugs or "") if x.strip()]:
        risk = drug_risk(item)[0] or "routine"
        own = bool((recorded and drug_diff(item, recorded)) or (signed and drug_diff(item, signed)))
        if shared:
            import sharedindex
            own = own or sharedindex.item_changed(shared, item)
        f = dict(flags, mismatch=flags.get("doc_mismatch", False) or own, drug_mismatch=own)
        out.append({"item": item, "risk": risk, "verdict": decide(f, risk, pol), "drug_mismatch": own})
    return out


# ---------------------------------------------------------------- check

def check_issuance(code_texts, sha, regn, drugs, rx_date, today, flags, findings, notes):
    """Verify a clinic-signed code and cross-check it with what is entered. Returns (issuance, regn, rx_date):
    the Regn No and date are taken from a verified code when staff did not enter them."""
    codes = [c for c in code_texts if c.startswith(ISSUE_PREFIX)]
    if len(code_texts) > len(codes):
        notes.append("QR code on the slip is not a clinic issuance code (payment / website?) - ignored")
    if not codes:
        return {"status": "none", "message": "no clinic-signed issuance code - authorship not verified"}, regn, rx_date
    iss = verify_code(codes[0])
    f = iss["fields"] or {}
    if iss["status"] == "unverifiable":
        notes.append(iss["message"])
    elif iss["status"] == "invalid":
        flags["issuance"] = "invalid"
        findings.append("ISSUANCE CODE FAILED: " + iss["message"])
    else:
        flags["issuance"] = "verified"
        if not regn:
            regn = f["regn"]
            notes.append(f"Regn No {regn} taken from the signed issuance code")
        elif regn_relation(regn, f["regn"]) == "different":
            flags["mismatch"] = flags["doc_mismatch"] = True
            findings.append(f"Regn No {regn} entered, but the signed code was issued under {f['regn']}")
        elif regn_relation(regn, f["regn"]) == "format":
            notes.append(f"Regn No {regn} entered; the signed code says {f['regn']} - same number, written differently")
        if rx_date and str(rx_date) != f["date"]:
            flags["mismatch"] = flags["doc_mismatch"] = True
            findings.append(f"Prescription date {rx_date} entered, but the signed code says {f['date']}")
        rx_date = rx_date or iso_date(f["date"])
        for d in drug_diff(drugs, f["drugs"]):
            flags["mismatch"] = flags["drug_mismatch"] = True
            findings.append(f"Not in the clinic-signed prescription: {d}")
        prev = code_seen(f["regn"], f["serial"])
        if prev and not (prev["sha256"] == sha and prev["first_seen"][:10] == today):
            flags["repeat"] = True
            findings.append(f"Signed prescription {f['serial']} was already presented here on {prev['first_seen'][:10]}")
        if f.get("patient"):
            notes.append(f"Signed for patient: {f['patient']} - check it matches the customer")
    return iss, regn, rx_date


def run_check(image, drugs=None, regn=None, doctor=None, rx_date=None, window_days=365, draw=True, issue_code=None):
    t0 = time.perf_counter()
    sha = sha256(image)
    raw = read_bgr(image)
    sus = paper_crop(raw)
    fs = features(sus)
    dens = density(sus)
    findings, notes = [], []
    flags = {"edited": False, "repeat": False, "similar": False, "mismatch": False, "doc_mismatch": False, "drug_mismatch": False,
             "new_slip": False, "unusable": False, "prescriber": "unknown", "meta_conflict": False, "issuance": "none"}
    today = date.today().isoformat()
    since = (date.today() - timedelta(days=window_days)).isoformat()
    match, matched_e, best, best_e = None, None, None, None
    shared = {"status": "off"}

    # 0. clinic-signed issuance code: read from the QR on the image unless typed/scanned separately
    issuance, regn, rx_date = check_issuance([issue_code] if issue_code else find_codes(sus), sha, regn,
                                             drugs, rx_date, today, flags, findings, notes)

    bad = quality(raw, sus, fs)
    if bad:
        # an image the tool cannot read is NOT a "new slip": route it to a person
        flags["unusable"] = True
        findings.append("IMAGE UNUSABLE for automated comparison: " + "; ".join(bad) +
                        " - review manually: ask for the paper slip or a direct photo")
    else:
        entries = entries_since(since)
        # the same file already checked today is this same visit (e.g. WhatsApp pre-check, then counter)
        same_visit = [e for e in entries if e["sha256"] == sha and e["date"] == today and e["kind"] == "seen"]
        if same_visit:
            notes.append("Already pre-checked today (same file)")
        entries = [e for e in entries if e not in same_visit]

        # 1. re-use: exact file, then fingerprint shortlist -> precise comparison / index match
        exact = [e for e in entries if e["sha256"] == sha]
        sims = fp_matrix(entries) @ fs["fp"]
        shortlist = [entries[i] for i in np.argsort(-sims)[:SHORTLIST] if sims[i] >= FP_MIN]
        rank = lambda res: ({"copy": 2, "similar": 1, "different": 0}[res["identity"]], res["agreement"])
        seen_e, seen_c = None, 0.0
        for e in shortlist:
            if e["image"]:
                res = compare(paper_crop(read_bgr(LEDGER_DIR / e["image"])), sus, ledger_features(e), fs)
                if res and (best is None or rank(res) > rank(best)):
                    best, best_e = res, e
                if best and best["identity"] == "copy" and best["strict"] >= 0.97:
                    break   # unmistakable copy - no need to look further
            else:
                c = index_match(ledger_features(e), fs, dens)
                if c > seen_c:
                    seen_e, seen_c = e, c

        if best and best["identity"] == "copy":
            matched_e = best_e
            edits = [g for g in best["regions"] if g["zone"] == "handwritten"]
            if edits:
                flags["edited"] = True
                findings.append(f"COPY of {best_e['id']} (dispensed {best_e['date']}) with "
                                f"{len(edits)} handwritten edit(s) - strokes match {best['strict']:.0%} at 1px")
            else:
                flags["repeat"] = True
                findings.append(f"SAME SLIP as {best_e['id']} (dispensed {best_e['date']}) - repeat")
            if regn and best_e["regn_no"] and regn_relation(regn, best_e["regn_no"]) == "different":
                flags["edited"] = True
                findings.append(f"Regn No {regn} differs from {best_e['regn_no']} recorded on {best_e['id']}")
            elif regn and best_e["regn_no"] and regn_relation(regn, best_e["regn_no"]) == "format":
                notes.append(f"Regn No {regn} is {best_e['regn_no']} on {best_e['id']}, written differently")
            others = len(best["regions"]) - len(edits)
            if others:
                notes.append(f"{others} letterhead/edge difference(s) - low confidence")
        elif best and best["identity"] == "similar":
            matched_e = best_e
            flags["similar"] = True
            findings.append(f"VERY SIMILAR to {best_e['id']} (dispensed {best_e['date']}) but not a stroke-for-"
                            f"stroke copy - a fresh slip from the same doctor, or a redrawn copy: confirm")
        elif seen_e and seen_c >= INDEX_MIN_CORR:
            matched_e = seen_e
            flags["repeat"] = True
            findings.append(f"SEEN BEFORE on {seen_e['date']} (layout match {seen_c:.2f}; "
                            f"no image kept, so edits cannot be located)")
        elif exact:
            matched_e = exact[0]
            flags["repeat"] = True
            findings.append(f"Exact same image file was presented on {exact[0]['date']}")
        else:
            flags["new_slip"] = True
            notes.append(f"Not seen before in {len(entries)} recorded slip(s)")
            # not known here: has another pharmacy seen it? (no-op unless this pharmacy joined a network)
            import sharedindex
            shared = sharedindex.lookup(fs, dens, drugs, window_days)
            if shared["status"] == "match":
                flags["new_slip"], flags["repeat"], flags["shared"] = False, True, True
                who = "dispensed" if shared["kind"] == "dispensed" else "checked"
                findings.append(f"SEEN AT ANOTHER PHARMACY: {who} there on {shared['date']} (layout match "
                                f"{shared['corr']:.2f}; no image shared, so edits cannot be located)")
                for d in shared["changes"]:
                    flags["mismatch"] = flags["drug_mismatch"] = True
                    findings.append(f"Differs from the slip at the other pharmacy: {d}")
            elif shared["status"] == "unreachable":
                flags["shared_unreachable"] = True
                notes.append("Shared index could not be asked (" + shared["detail"] + ") - other pharmacies' records "
                             "are unknown; treated as a new slip, and re-checked when the connection returns")
            elif shared["status"] == "clear":
                notes.append(f"Not seen at other pharmacies ({shared['searched']} entries searched)")

        if best and best["identity"] in ("copy", "similar"):
            match = {"ledger_id": best_e["id"], "identity": best["identity"], "agreement": best["agreement"],
                     "strict": best["strict"], "ncc": best["ncc"], "regions": best["regions"]}
            if draw:
                REPORTS.mkdir(exist_ok=True)
                out = REPORTS / f"{Path(image).stem}_vs_{best_e['id']}.png"
                draw_report(paper_crop(read_bgr(LEDGER_DIR / best_e["image"])), best, out,
                            f"{Path(image).name}  vs  {best_e['id']}  ({best['identity']})")
                match["report_png"] = str(out)
        elif matched_e:
            match = {"ledger_id": matched_e["id"], "identity": "seen", "index_corr": round(seen_c, 3), "regions": []}

        # 2. consistency with the matched slip's record (pharmacist-entered, no OCR)
        if matched_e and not flags["edited"]:
            diffs = drug_diff(drugs, matched_e["drugs"])
            if diffs:
                flags["mismatch"] = flags["drug_mismatch"] = True
                findings += [f"Differs from record of {matched_e['id']}: {d}" for d in diffs]
            is_copy = best is not None and best["identity"] == "copy"   # copies already handled above
            if regn and matched_e["regn_no"] and regn_relation(regn, matched_e["regn_no"]) == "different" and not is_copy:
                flags["mismatch"] = flags["doc_mismatch"] = True
                findings.append(f"Regn No {regn} differs from {matched_e['regn_no']} recorded for {matched_e['id']}")

    # 3. prescriber
    status, rec, msg = prescriber(regn, doctor)
    flags["prescriber"] = status
    (findings if status == "invalid" else notes).append(msg)

    # 4. metadata: supporting evidence only (strong -> confirm, weak/info -> note)
    for lvl, text in metadata(image, rx_date):
        if lvl == "strong":
            flags["meta_conflict"] = True
            findings.append(text + " - supporting evidence only: confirm issuance with the clinic")
        else:
            notes.append(text)

    # 5. drug risk + policy -> verdict
    pol = policy()
    data = data_status()
    risk, risky, unknown = drug_risk(drugs)
    if not data["drug_risk"]["usable"]:
        (findings if risk else notes).append(
            f"Drug-risk list unusable ({'; '.join(data['drug_risk']['problems'])}) - every medicine is treated "
            f"as high-risk until it is fixed (rxcheck data status)")
    if not data["policy"]["usable"]:
        notes.append("Policy file problem (" + "; ".join(data["policy"]["problems"]) + ") - cautious defaults used")
    if risk:
        verdicts = {risk: decide(flags, risk, pol)}
        if risky:
            notes.append("High-risk: " + ", ".join(risky) + " - record in H1 register / `rxcheck add` after dispensing")
        if unknown and data["drug_risk"]["usable"]:
            notes.append("Not in drug-risk list, treated as routine: " + ", ".join(unknown) + " - check if unsure")
        if data["drug_risk"]["usable"] and not data["drug_risk"]["verified"]:
            notes.append("Drug-risk list is not verified against a current official source (or the check is over "
                         f"{DRUG_LIST_MAX_AGE_DAYS} days old) - classification may be wrong")
        for e in review_drugs(drugs):
            notes.append(f"Confirm entry '{e['item']}': {e['issue']}")
    else:
        verdicts = {"routine": decide(flags, "routine", pol), "high": decide(flags, "high", pol)}
    if flags["new_slip"] and pol.get("confirm_new_high_risk", True) and verdicts.get("high") == "AMBER" \
            and not findings and flags["issuance"] != "verified":
        notes.append("High-risk + first time seen: a valid Regn No does not prove the doctor wrote it - confirm issuance")
    if flags["issuance"] == "verified":
        notes.insert(0, "ISSUANCE VERIFIED: " + issuance["message"])

    # 6. medicine by medicine, and the quickest step that settles what is held
    recorded = {"id": matched_e["id"], "date": matched_e["date"], "drugs": matched_e["drugs"]} \
        if matched_e and not flags["edited"] else None
    signed = (issuance.get("fields") or {}).get("drugs") if flags["issuance"] == "verified" else None
    shared_hit = shared if shared["status"] == "match" else None
    items = item_verdicts(drugs, flags, pol, (recorded or {}).get("drugs"), signed, shared_hit) if risk else []
    worst = max(verdicts.values(), key=ORDER.index)
    if items and worst != "GREEN" and all(i["verdict"] == "GREEN" for i in items):
        for i in items:            # cannot happen by construction; if it ever does, hold everything
            i["verdict"] = worst
    held = [i for i in items if i["verdict"] != "GREEN"]
    if items:
        step_risk = "high" if any(i["risk"] == "high" for i in held) else "routine"
    else:   # pre-check, medicines not known yet: a routine warning is a document problem and applies to all
        step_risk = risk or ("routine" if verdicts["routine"] != "GREEN" else "high")
    step_flags = dict(flags, drug_mismatch=any(i["drug_mismatch"] for i in held) if items else flags["drug_mismatch"])
    path, steps = next_step(step_flags, step_risk,
                            {"recorded": recorded, "signed_drugs": signed, "shared": shared_hit}, pol)

    callback = trusted(rec, "phone")
    import sharedindex
    rep = {"image": str(image), "checked_at": now(), "seconds": round(time.perf_counter() - t0, 2),
           "risk": risk, "verdicts": verdicts, "findings": findings, "notes": notes, "match": match,
           "sha256": sha, "callback": callback, "unusable": flags["unusable"], "flags": flags,
           "issuance": issuance, "prescriber_message": msg if status != "invalid" else None,
           "shared": sharedindex.public_result(shared), "items": items, "hand_over": [i["item"] for i in items if i["verdict"] == "GREEN"],
           "held": [i["item"] for i in held], "path": path, "steps": steps, "recorded": recorded,
           "fail_closed": fail_closed(", ".join(i["item"] for i in held)) if items else [],
           "data": {k: v["sha"] for k, v in data.items()}, "actor": actor()}
    rep["reason"] = reason(rep)
    rep["_features"], rep["_dens"] = fs, dens   # for recording into the index; stripped before saving
    return rep


def reason(rep):
    """The one sentence that explains the (worst) verdict - what the counter shows first."""
    worst = max(rep["verdicts"].values(), key=ORDER.index)
    f = rep["flags"]
    if worst != "GREEN" and rep["findings"]:
        return rep["findings"][0]
    if worst == "AMBER":
        if f["prescriber"] in ("unknown", "unverified"):
            return (rep.get("prescriber_message") or "Prescriber could not be verified") + \
                " - for a high-risk medicine, confirm with the clinic"
        if f["new_slip"]:
            return ("First time this slip is seen - a valid Regn No does not prove the doctor wrote it; "
                    "confirm issuance for the high-risk medicine")
        return "Needs confirmation for the high-risk medicine"
    if f["issuance"] == "verified":
        return "No check failed; issuance verified by the clinic's signature"
    return "No check failed (issuance not verified - normal for routine medicines)"


def headline(rep):
    v = rep["verdicts"]
    if len(v) == 1:
        return next(iter(v.values()))
    if v["routine"] == v["high"]:
        return v["routine"]
    return f"routine meds: {v['routine']} | H1/X/abuse-prone: {v['high']}"


def public(rep):
    return {k: v for k, v in rep.items() if not k.startswith("_")}


def record_check(rep, drugs=None, regn=None, doctor=None):
    """Index the slip (unless unusable) and write the decision to the audit trail."""
    worst = max(rep["verdicts"].values(), key=ORDER.index)
    if not rep["unusable"]:
        eid = record_seen(rep["image"], rep["_features"], rep["_dens"], rep["sha256"], drugs, regn, doctor, worst)
        if (rep.get("shared") or {}).get("status") == "unreachable":
            import sharedindex
            sharedindex.mark_unchecked(eid)   # asked while the network was down: `shared sync` asks again
    con = db()
    with con:
        iss = rep.get("issuance") or {}
        if iss.get("status") == "verified":
            f = iss["fields"]
            con.execute("INSERT OR IGNORE INTO issuance_codes (regn_no, serial, first_seen, sha256) VALUES (?, ?, ?, ?)",
                        (f["regn"], f["serial"], now(), rep["sha256"]))
        audit(con, "check", rep["sha256"][:16], {"image": Path(rep["image"]).name, "verdicts": rep["verdicts"],
                                                  "reason": rep.get("reason"), "findings": rep["findings"],
                                                  "path": rep.get("path"), "held": rep.get("held"),
                                                  "hand_over": rep.get("hand_over"),
                                                  "issuance": iss.get("status"), "data": rep.get("data")})


# ---------------------------------------------------------------- holds (hold-and-escalate)

def open_hold(rep):
    con = db()
    hid = f"H-{datetime.now():%Y%m%d-%H%M%S}-{rep['sha256'][:6]}"
    with con:
        con.execute("INSERT OR IGNORE INTO holds VALUES (?, ?, ?, ?, ?, 'open')",
                    (hid, now(), rep["image"], json.dumps(rep["verdicts"]), json.dumps(rep["findings"])))
        audit(con, "hold", hid, {"verdicts": rep["verdicts"], "path": rep.get("path"), "held": rep.get("held"),
                                 "settled_by": settling_reasons(rep["flags"]) if rep.get("path") == "counter" else [],
                                 "fail_closed": rep.get("fail_closed") or []})
    return hid


def _hold_detail(hid):
    row = db().execute("SELECT detail FROM audit WHERE action = 'hold' AND ref = ? ORDER BY seq LIMIT 1",
                       (hid,)).fetchone()
    return json.loads(row["detail"] or "{}") if row else {}


def hold_info(hid):
    """(path, counter checks that settle it) as recorded when the hold was opened."""
    d = _hold_detail(hid)
    return d.get("path") or "call", d.get("settled_by") or []


def hold_fail_closed(hid):
    """Held NDPS / Schedule X medicines: no override for this hold."""
    return _hold_detail(hid).get("fail_closed") or []


RESOLUTIONS = {
    "confirmed": ("closed", "Prescriber confirmed issuing it. Dispense; `rxcheck add` it if high-risk."),
    "refused": ("closed", "Not dispensed. Return the slip politely; do not accuse - a failed check is not proof."),
    "dispensed-after-review": ("closed", "Recorded as dispensed after pharmacist review."),
    "unreachable": ("waiting", "Clinic not reachable: hand over the routine items, keep holding the rest (do not "
                               "auto-approve). Offer to call back, or ask the patient to return with the original "
                               "slip / OP card."),
    "retaken": ("closed", "Replaced by a check of a better photo."),
}
# what was actually checked when a held prescription is dispensed after review. The first four settle a
# counter-path warning; "other" (note required) is an OVERRIDE - dispensing without the step that was asked for.
REVIEW_REASONS = {
    "paper-original": "Saw the paper original: a fresh slip from the same doctor",
    "sold-as-prescribed": "Sold only what the recorded / signed prescription says",
    "repeat-allowed": "Prescription allows repeats and the H1 register shows some left",
    "entry-corrected": "Regn No / date re-read on the slip: the entry was wrong",
    "other": "Other - dispensed without the step asked for (explain in the note)",
}
COUNTER_REASONS = ("paper-original", "sold-as-prescribed", "repeat-allowed", "entry-corrected")


def settling_reasons(flags):
    """The counter checks that settle THESE warnings - the only ones 'checked - dispense' may cite."""
    out = []
    if flags.get("similar"):
        out.append("paper-original")
    if flags.get("mismatch") and (flags.get("drug_mismatch") or not flags.get("doc_mismatch")):
        out.append("sold-as-prescribed")
    if flags.get("repeat"):
        out.append("repeat-allowed")
    if flags.get("doc_mismatch"):
        out.append("entry-corrected")
    return out


def cmd_holds(a):
    rows = db().execute("SELECT * FROM holds WHERE status != 'closed' ORDER BY created").fetchall()
    if not rows:
        print("no open holds")
        return
    print(f"{'hold':26} {'age':>6}  {'status':8} {'verdict':8} reason / image")
    for h in rows:
        age = int((datetime.now() - datetime.fromisoformat(h["created"])).total_seconds() // 60)
        worst = max(json.loads(h["verdicts"]).values(), key=ORDER.index)
        findings = json.loads(h["findings"])
        why = findings[0] if findings else Path(h["image"]).name
        print(f"{h['id']:26} {age:>4}m  {h['status']:8} {worst:8} {why[:90]}")


def resolve_hold(hid, outcome, note="", reason=None):
    """(status, advice). A closed hold cannot be reopened or re-resolved. Dispensing after review must say
    what was checked; a counter reason only settles a counter-path warning, anything else is an override
    and needs a note."""
    con = db()
    status, advice = RESOLUTIONS[outcome]
    import doctorping   # the prescriber's signed answer, if this hold was put to the doctor
    said = doctorping.hold_decision(hid)
    if said and said["decision"] == "denied" and outcome in ("confirmed", "dispensed-after-review"):
        raise ValueError(f"the prescriber DENIED issuing this prescription (request {said['request_id']}, "
                         f"{said['via']}): record 'refused'. A denial is not overridden on anyone's word")
    override = False
    if outcome == "dispensed-after-review":
        if reason not in REVIEW_REASONS:
            raise ValueError(f"say what was checked: --reason {'|'.join(REVIEW_REASONS)}")
        path, settled_by = hold_info(hid)
        override = path != "counter" or reason not in settled_by
        if override and hold_fail_closed(hid):
            raise ValueError(f"no override for {', '.join(hold_fail_closed(hid))}: an NDPS / Schedule X medicine is "
                             f"dispensed only when the clinic confirms (or a counter check settles the warning). "
                             f"Hand over the routine items; the customer can return once the clinic confirms")
        if override and len((note or "").strip()) < 10:
            raise ValueError("dispensing without the step that was asked for is an override: explain it in the note")
    authenticated = bool(said and said["decision"] == "approved" and outcome == "confirmed")
    if outcome == "confirmed" and not authenticated and policy().get("require_receipt_ndps") and hold_fail_closed(hid):
        raise ValueError(f"policy: {', '.join(hold_fail_closed(hid))} (NDPS / Schedule X) is confirmed only by the "
                         f"prescriber's signed receipt - ask the doctor (rxcheck doctor ask ...)")
    with con:
        row = con.execute("SELECT status FROM holds WHERE id = ?", (hid,)).fetchone()
        if row is None:
            raise ValueError(f"no hold {hid}")
        if row["status"] == "closed":
            raise ValueError(f"hold {hid} is already closed")
        con.execute("UPDATE holds SET status = ? WHERE id = ?", (status, hid))
        audit(con, "resolve", hid, {"outcome": outcome, "note": note, "reason": reason, "override": override,
                                    "authenticated": authenticated,
                                    "via": said["via"] if authenticated else None,
                                    "request": said["request_id"] if authenticated else None})
    return status, advice


def cmd_resolve(a):
    try:
        status, advice = resolve_hold(a.hold, a.outcome, a.note, a.reason)
    except ValueError as ex:
        sys.exit(str(ex))
    print(f"{a.hold}: {a.outcome} -> {status}. {advice}")


def cmd_log(a):
    rows = db().execute("SELECT * FROM audit ORDER BY seq DESC LIMIT ?", (a.n,)).fetchall()
    for row in reversed(rows):
        print(f"{row['seq']:5}  {row['at']}  {row['actor']:12} {row['action']:8} {row['ref'] or '':26} "
              f"{(row['detail'] or '')[:80]}")


def last_activity():
    """The most recent check and the most recent override: who, when, and what - for the counter screens."""
    con = db()
    check = con.execute("SELECT * FROM audit WHERE action = 'check' ORDER BY seq DESC LIMIT 1").fetchone()
    override = con.execute("SELECT * FROM audit WHERE action = 'resolve' AND json_extract(detail, '$.override') = 1 "
                           "ORDER BY seq DESC LIMIT 1").fetchone()
    return {"check": dict(check) if check else None, "override": dict(override) if override else None}


# ---------------------------------------------------------------- patient names (counter lookup only)
# Names typed at the counter, kept so the next entry can be completed instead of retyped. Only the name and
# when it was last used - nothing about what was dispensed. Dropped after PATIENT_RETENTION_DAYS without a
# visit; the retention period must be confirmed with a pharmacy compliance professional.
PATIENT_RETENTION_DAYS = 365


def remember_patient(name):
    name = re.sub(r"\s+", " ", (name or "")).strip()[:80]
    if len(name) < 2:
        return
    today = date.today().isoformat()
    con = db()
    with con:
        con.execute("INSERT INTO patients (name, first_seen, last_seen, visits) VALUES (?, ?, ?, 1) "
                    "ON CONFLICT(name) DO UPDATE SET visits = visits + (last_seen < excluded.last_seen), "
                    "last_seen = excluded.last_seen", (name, today, today))


def find_patients(q, n=8):
    q = (q or "").strip()
    if len(q) < 2:
        return []
    like = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = db().execute("SELECT name, last_seen, visits FROM patients WHERE name LIKE ? ESCAPE '\\' "
                        "ORDER BY (name LIKE ? ESCAPE '\\') DESC, last_seen DESC LIMIT ?",
                        (f"%{like}%", f"{like}%", n)).fetchall()
    return [dict(x) for x in rows]


def purge_patients(days=PATIENT_RETENTION_DAYS):
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    con = db()
    with con:
        return con.execute("DELETE FROM patients WHERE last_seen < ?", (cutoff,)).rowcount


OVERRIDE_ALERT = 0.20   # share of closed holds dispensed without the asked-for step that triggers a warning


def workload(days=1):
    """What warnings cost the counter, and whether they are being waved through. From the audit trail."""
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    rows = [dict(r_) for r_ in db().execute("SELECT * FROM audit WHERE at >= ? ORDER BY seq", (since,))]
    det = lambda x: json.loads(x["detail"] or "{}")
    slips = {}
    for x in rows:   # one slip checked twice (WhatsApp pre-check, then counter) counts once: its last check
        if x["action"] == "check":
            slips[(x["at"][:10], x["ref"])] = det(x)
    paths = {p_: 0 for p_ in PATHS}
    green = 0
    for d in slips.values():
        if max(d.get("verdicts", {"x": "GREEN"}).values(), key=ORDER.index) == "GREEN":
            green += 1
        elif d.get("path") in paths:
            paths[d["path"]] += 1
    holds = {x["ref"]: x for x in rows if x["action"] == "hold"}
    outcomes, minutes, overrides, closed, by = {}, [], 0, 0, {}
    for x in rows:
        if x["action"] != "resolve":
            continue
        d = det(x)
        outcomes[d.get("outcome")] = outcomes.get(d.get("outcome"), 0) + 1
        if RESOLUTIONS.get(d.get("outcome"), ("",))[0] == "closed":
            closed += 1
            overrides += bool(d.get("override"))
            mine = by.setdefault(x["actor"], [0, 0])   # [overrides, holds closed] per person
            mine[0] += bool(d.get("override"))
            mine[1] += 1
            if x["ref"] in holds:
                minutes.append((datetime.fromisoformat(x["at"]) - datetime.fromisoformat(holds[x["ref"]]["at"]))
                               .total_seconds() / 60)
    waiting = db().execute("SELECT COUNT(*) FROM holds WHERE status = 'waiting'").fetchone()[0]
    open_ = db().execute("SELECT COUNT(*) FROM holds WHERE status = 'open'").fetchone()[0]
    share = overrides / closed if closed else 0.0
    return {"days": days, "slips": len(slips), "green": green, "paths": paths, "holds_opened": len(holds),
            "outcomes": outcomes, "closed": closed, "overrides": overrides, "override_share": round(share, 2),
            "median_minutes_to_close": round(float(np.median(minutes)), 1) if minutes else None,
            "open": open_, "waiting_for_callback": waiting,
            "override_alert": closed >= 5 and share > OVERRIDE_ALERT,
            # one person who overrides far more than the counter as a whole is the pattern collusion leaves
            "overrides_by": {k: {"overrides": o, "closed": c} for k, (o, c) in sorted(by.items())},
            "staff_alerts": sorted(k for k, (o, c) in by.items() if c >= 5 and o / c > OVERRIDE_ALERT)}


def cmd_stats(a):
    w = workload(a.days)
    print(f"last {w['days']} day(s): {w['slips']} slip(s) checked, {w['green']} with no issue")
    print(f"  warnings: {w['paths']['retake']} retake, {w['paths']['counter']} counter check, "
          f"{w['paths']['call']} call to the clinic")
    print(f"  holds: {w['holds_opened']} opened, {w['closed']} closed "
          f"(median {w['median_minutes_to_close'] if w['median_minutes_to_close'] is not None else '-'} min), "
          f"{w['open']} open, {w['waiting_for_callback']} waiting for a call-back")
    print(f"  outcomes: " + (", ".join(f"{k} {v}" for k, v in sorted(w["outcomes"].items())) or "-"))
    print(f"  dispensed WITHOUT the step asked for (overrides): {w['overrides']} of {w['closed']} closed holds")
    for name, v in w["overrides_by"].items():
        if v["overrides"]:
            print(f"    {name}: {v['overrides']} override(s) of {v['closed']} hold(s) closed")
    if w["override_alert"]:
        print(f"  ! more than {OVERRIDE_ALERT:.0%} of warnings are being overridden - review the cases with the team: "
              f"either the warnings are wrong for this counter, or they are being waved through")
    for name in w["staff_alerts"]:
        print(f"  ! {name} dispensed more than {OVERRIDE_ALERT:.0%} of the holds they closed without the step asked "
              f"for - review those cases (`rxcheck log`, or Audit log > Overrides only in the counter app)")


def cmd_backup(a):
    """Consistent online snapshot of the database plus images and feature files."""
    dest = Path(a.dest) / f"rxcheck-backup-{datetime.now():%Y%m%d-%H%M%S}"
    earlier = [d for d in sorted(Path(a.dest).glob("rxcheck-backup-*")) if (d / "rxcheck.db").exists()]
    n = 1
    while dest.exists():   # two backups in one second (scheduled + manual): never write into an existing one
        n += 1
        dest = dest.with_name(f"{dest.name.split('~')[0]}~{n}")
    dest.mkdir(parents=True)
    con = db()
    restored = con.execute("SELECT at, ref FROM audit WHERE action = 'restore' ORDER BY seq DESC LIMIT 1").fetchone()
    if restored:   # a restore rewinds the trail on purpose: backups it superseded are not compared
        at = restored["at"].replace("-", "").replace(":", "").replace("T", "-")
        earlier = [d for d in earlier if d.name[len("rxcheck-backup-"):] > at
                   or d.resolve() == Path(restored["ref"]).resolve()]
    # the live trail must still contain the previous backup's trail unchanged
    changed = chain_extends(earlier[-1] / "rxcheck.db", con) if earlier else None
    target = sqlite3.connect(dest / "rxcheck.db")
    with target:
        con.backup(target)
    target.close()
    for sub in ("images", "features"):
        if (LEDGER_DIR / sub).exists():
            shutil.copytree(LEDGER_DIR / sub, dest / sub)
    problems = verify_store(dest) + ([changed] if changed else [])
    with con:
        audit(con, "backup", str(dest), {"problems": problems, "chain_head": list(chain_head(con))})
    if getattr(a, "keep", None):
        old = sorted(Path(a.dest).glob("rxcheck-backup-*"))[:-a.keep]
        for d in old:
            shutil.rmtree(d, ignore_errors=True)
    print(f"backup written to {dest}" + ("" if not problems else f"  - WARNING: {len(problems)} problem(s): {problems[:3]}"))
    return dest, problems


def verify_store(folder=None):
    """Problems with a ledger folder (the live one, or a backup): database integrity, append-only audit
    trail intact, every entry has its audit record and its files, every image unchanged. [] = healthy."""
    folder = Path(folder or LEDGER_DIR)
    path = folder / "rxcheck.db"
    if not path.exists():
        return [f"no database at {path}"]
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.DatabaseError as ex:
        return [f"database unreadable: {ex}"]
    try:   # always closed: on Windows an open handle would block `restore` from moving the damaged file
        return _verify(con, folder)
    except sqlite3.DatabaseError as ex:
        return [f"database unreadable: {ex}"]
    finally:
        con.close()


def _verify(con, folder):
    """Checks behind verify_store, on an open read-only connection."""
    problems = []
    con.row_factory = sqlite3.Row
    res = con.execute("PRAGMA integrity_check").fetchone()[0]
    if res != "ok":
        return [f"database integrity check failed: {res}"]
    trig = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    for t in ("audit_append_only_u", "audit_append_only_d"):
        if t not in trig:
            problems.append(f"audit protection trigger {t} is missing")
    seqs = [r[0] for r in con.execute("SELECT seq FROM audit ORDER BY seq")]
    gaps = [b for a_, b in zip(seqs, seqs[1:]) if b != a_ + 1]
    if gaps or (seqs and seqs[0] != 1):
        problems.append(f"audit trail has missing records (gaps before seq {gaps[:5] or [seqs[0]]})")
    problems += check_chain(con)
    logged = {r[0] for r in con.execute("SELECT ref FROM audit WHERE action IN ('add', 'index', 'migrate')")}
    for e in con.execute("SELECT id, kind, image, sha256 FROM entries"):
        if e["id"] not in logged:
            problems.append(f"entry {e['id']} has no audit record")
        if not (folder / "features" / f"{e['id']}.npz").exists() and e["kind"] == "seen":
            problems.append(f"index entry {e['id']}: feature file missing (entry cannot be matched)")
        if e["image"]:
            img = folder / e["image"]
            if not img.exists():
                problems.append(f"ledger entry {e['id']}: image {e['image']} missing")
            elif e["sha256"] and sha256(img) != e["sha256"]:
                problems.append(f"ledger entry {e['id']}: image {e['image']} has changed since it was recorded")
    return problems


def cmd_verify(a):
    problems = verify_store(a.folder)
    folder = Path(a.folder or LEDGER_DIR)
    if (folder / "rxcheck.db").exists() and not any("unreadable" in p for p in problems):
        con = sqlite3.connect(f"file:{(folder / 'rxcheck.db').as_posix()}?mode=ro", uri=True)
        try:
            for earlier in getattr(a, "against", None) or []:
                diff = chain_extends(Path(earlier) / "rxcheck.db", con)
                if diff:
                    problems.append(diff)
            seq, h = chain_head(con) if "audit_chain" in {x[0] for x in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")} else (0, CHAIN_GENESIS)
        finally:
            con.close()
        print(f"audit chain head: record {seq}, {h[:16]}  (note it down or keep a backup off-site: "
              f"a later `verify` can then show whether anything before it was changed)")
    for p in problems:
        print(f"  ! {p}")
    print(f"{folder}: " + ("healthy" if not problems else f"{len(problems)} problem(s)"))
    sys.exit(1 if problems else 0)


def restore_store(src, force=False):
    """Replace the live database, images and features with a backup. The current files are moved aside
    (never deleted) so a wrong restore can be undone. The backup is verified first."""
    src = Path(src)
    problems = verify_store(src)
    if problems and not force:
        raise ValueError(f"backup {src} has problems, not restored (use --force to restore anyway): {problems[:3]}")
    close_db()
    aside = LEDGER_DIR / f"pre-restore-{datetime.now():%Y%m%d-%H%M%S}"
    aside.mkdir(parents=True)
    for name in ("rxcheck.db", "rxcheck.db-wal", "rxcheck.db-shm", "images", "features"):
        if (LEDGER_DIR / name).exists():
            shutil.move(str(LEDGER_DIR / name), str(aside / name))
    shutil.copyfile(src / "rxcheck.db", LEDGER_DIR / "rxcheck.db")
    for sub in ("images", "features"):
        if (src / sub).exists():
            shutil.copytree(src / sub, LEDGER_DIR / sub)
    con = db()
    with con:
        audit(con, "restore", str(src), {"moved_aside": str(aside), "backup_problems": problems})
    return aside, verify_store()


def cmd_restore(a):
    try:
        aside, problems = restore_store(a.backup, a.force)
    except ValueError as ex:
        sys.exit(str(ex))
    print(f"restored from {a.backup}; previous files kept in {aside}")
    print("verify: " + ("healthy" if not problems else f"{len(problems)} problem(s): {problems[:3]}"))


# ---------------------------------------------------------------- data maintenance

def write_data(path, data, action, detail):
    """Atomic replace of a data file, keeping the previous version in data/history and logging both hashes."""
    old_sha = read_data(path)[1]
    hist = path.parent / "history"
    hist.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copyfile(path, hist / f"{path.stem}-{datetime.now():%Y%m%d-%H%M%S}-{old_sha}.json")
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    con = db()
    with con:
        audit(con, action, path.name, {**detail, "old_sha": old_sha, "new_sha": read_data(path)[1]})


def import_registry(csv_path, source, coverage="partial", replace=False, verified_on=None):
    """Merge (or replace) registry entries from a CSV that a person checked against the medical council
    register. Columns: regn_no, name, [council, verified_on, phone, phone_source, issuer_key,
    issuer_key_source]. All-or-nothing: any bad row rejects the whole file."""
    if not source or not source.strip():
        raise ValueError("--source is required: say which register was checked, by whom")
    if coverage not in REGISTRY_COVERAGE:
        raise ValueError(f"coverage must be one of {REGISTRY_COVERAGE}")
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8-sig", newline="")))
    errors, new = [], {}
    for i, row in enumerate(rows, 2):
        row = {k.strip(): (v or "").strip() for k, v in row.items() if k}
        regn, name = row.get("regn_no", ""), row.get("name", "")
        checked = row.get("verified_on") or verified_on
        if not re.fullmatch(r"[A-Za-z0-9/.-]{2,20}", regn):
            errors.append(f"line {i}: regn_no '{regn}' is missing or malformed")
        if not name:
            errors.append(f"line {i}: name is missing")
        if not iso_date(checked):
            errors.append(f"line {i}: verified_on '{checked or ''}' is not YYYY-MM-DD (or pass --verified-on)")
        elif iso_date(checked) > date.today():
            errors.append(f"line {i}: verified_on {checked} is in the future")
        twin = next((k for k in new if regn_key(k) == regn_key(regn)), None)
        if twin:
            errors.append(f"line {i}: regn_no {regn} appears twice" + (f" (as {twin})" if twin != regn else ""))
        for f in ("phone", "issuer_key"):
            if row.get(f) and not row.get(f + "_source"):
                errors.append(f"line {i}: {f} given without {f}_source (where was it independently obtained?)")
        new[regn] = {"name": name, "council": row.get("council") or None, "source": row.get("source") or source,
                     "verified_on": checked, "phone": row.get("phone") or None,
                     "phone_source": row.get("phone_source") or None,
                     "issuer_key": row.get("issuer_key") or None,
                     "issuer_key_source": row.get("issuer_key_source") or None}
    if not rows:
        errors.append("no rows")
    if errors:
        raise ValueError("registry import rejected:\n  " + "\n  ".join(errors))
    old, _, err = read_data(REGISTRY)
    base = {} if replace or err or not isinstance(old, dict) else old
    data = {**{k: v for k, v in base.items()}, **new,
            "_note": (old or {}).get("_note") if isinstance(old, dict) else None,
            "_source": source, "_coverage": coverage, "_updated_on": date.today().isoformat()}
    data = {k: v for k, v in data.items() if v is not None}
    write_data(REGISTRY, data, "data-import", {"file": "registry", "rows": len(new), "replace": replace,
                                               "coverage": coverage, "source": source})
    return len(new)


def import_drugs(csv_path, source, verified_by):
    """Replace the drug-risk list from a CSV reviewed against the current official schedules.
    Columns: molecule, tier (ndps|high|routine), [brands separated by ';', schedule]. All-or-nothing."""
    if not (source or "").strip() or not (verified_by or "").strip():
        raise ValueError("--source and --verified-by are required: the list decides which sales need a call")
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8-sig", newline="")))
    errors, mols, brands, sched = [], {}, {}, {}
    for i, row in enumerate(rows, 2):
        row = {k.strip(): (v or "").strip() for k, v in row.items() if k}
        m, tier = row.get("molecule", "").lower(), row.get("tier", "").lower()
        if not re.fullmatch(r"[a-z][a-z -]{2,}", m):
            errors.append(f"line {i}: molecule '{m}' is missing or malformed")
        if tier not in DRUG_TIERS:
            errors.append(f"line {i}: tier '{tier}' must be ndps, high or routine")
        if m in mols:
            errors.append(f"line {i}: molecule {m} appears twice")
        mols[m] = tier
        if row.get("schedule"):
            sched[m] = row["schedule"]
        for b in filter(None, (x.strip().lower() for x in row.get("brands", "").split(";"))):
            if b in brands and brands[b] != m:
                errors.append(f"line {i}: brand {b} maps to both {brands[b]} and {m}")
            brands[b] = m
    if not rows:
        errors.append("no rows")
    if errors:
        raise ValueError("drug list import rejected:\n  " + "\n  ".join(errors))
    data = {"_verified": True, "_source": source, "_verified_by": verified_by,
            "_verified_on": date.today().isoformat(), "molecules": mols, "brands": brands, "schedules": sched}
    write_data(DRUG_RISK, data, "data-import", {"file": "drug_risk", "molecules": len(mols), "brands": len(brands),
                                                "source": source, "verified_by": verified_by})
    return len(mols), len(brands)


def cmd_data(a):
    try:
        if a.action == "import-registry":
            n = import_registry(a.csv, a.source, a.coverage, a.replace, a.verified_on)
            print(f"registry: {n} entr{'y' if n == 1 else 'ies'} imported")
        elif a.action == "import-drugs":
            m, b = import_drugs(a.csv, a.source, a.verified_by)
            print(f"drug-risk list replaced: {m} molecules, {b} brands")
    except (ValueError, OSError) as ex:
        sys.exit(str(ex))
    st = data_status()
    for name, s in st.items():
        state = "OK" if s["usable"] else "UNUSABLE"
        extra = ""
        if name == "registry":
            extra = f"{s['entries']} entries, coverage {s['coverage']}, source {s['source'] or 'not recorded'}"
        elif name == "drug_risk":
            extra = (f"{s['molecules']} molecules, " + (f"verified {s['verified_on']} ({s['source']})" if s["verified"]
                                                       else "NOT VERIFIED against a current official source"))
        print(f"  {name:10} {state:9} sha {s['sha'] or '-':12}  {extra}")
        for p in s["problems"]:
            print(f"             ! {p}")


# ---------------------------------------------------------------- staff, clinic, serve

def cmd_staff(a):
    con = db()
    if a.action == "list":
        for s in con.execute("SELECT name, role, active, created_at FROM staff ORDER BY name"):
            print(f"  {s['name']:20} {s['role']:11} {'active' if s['active'] else 'disabled':9} since {s['created_at'][:10]}")
        return
    if not a.name:
        sys.exit("staff name required")
    if a.action == "disable":
        with con:
            n = con.execute("UPDATE staff SET active = 0 WHERE name = ?", (a.name,)).rowcount
            if n:
                audit(con, "staff-disable", a.name)
        print(f"{a.name}: {'disabled' if n else 'no such staff member'}")
        return
    pin = sys.stdin.readline().strip() if a.pin_stdin else getpass.getpass("PIN (6+ digits): ")
    if not a.pin_stdin and getpass.getpass("PIN again: ") != pin:
        sys.exit("PINs do not match")
    try:
        if a.action == "add":
            add_staff(a.name, a.role, pin)
        else:   # pin reset
            if not re.fullmatch(r"\d{6,}", pin):
                raise ValueError("PIN must be at least 6 digits")
            with con:
                if not con.execute("UPDATE staff SET pin_hash = ? WHERE name = ?", (hash_pin(pin), a.name)).rowcount:
                    raise ValueError(f"no staff member {a.name}")
                audit(con, "staff-pin", a.name)
    except (ValueError, sqlite3.IntegrityError) as ex:
        sys.exit(str(ex))
    print(f"{a.name}: {a.action} done")


def cmd_clinic(a):
    if not crypto_available():
        sys.exit("needs the 'cryptography' package: pip install cryptography")
    if a.action == "keygen":
        if Path(a.key).exists():
            sys.exit(f"{a.key} exists - not overwritten")
        pub = clinic_keygen(a.key)
        print(f"private key written to {a.key} - keep it at the clinic, never send it")
        print(f"public key for pharmacies (hand over in person; they record it as issuer_key):\n  {pub}")
        return
    try:
        code = make_code(Path(a.key).read_bytes(), a.regn, a.date, a.drugs, a.patient)
    except ValueError as ex:
        sys.exit(str(ex))
    code_qr(code, a.out)
    print(f"QR written to {a.out} ({len(code)} chars) - print it on the slip\n  {code}")


def seed_home():
    """First start on an empty disk (RXCHECK_HOME): copy the default data files there. Never overwrites."""
    if HOME == ROOT:
        return []
    copied = []
    for src in sorted((ROOT / "data").glob("*.json")):
        dst = HOME / "data" / src.name
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            copied.append(src.name)
    return copied


def cmd_serve(a):
    import counter_app
    for name in seed_home():
        print(f"seeded {HOME / 'data' / name} from the defaults")
    counter_app.serve(a.host, a.port, inbox=a.inbox, backup_dir=a.backup_dir, backup_hours=a.backup_hours,
                      cert=a.cert, key=a.key, behind_proxy=a.behind_proxy)


# ---------------------------------------------------------------- commands

def cmd_check(a):
    rx_date = date.fromisoformat(a.rx_date) if a.rx_date else None
    rep = run_check(a.image, a.drugs, a.regn, a.doctor, rx_date, a.window, issue_code=a.issue_code)
    worst = max(rep["verdicts"].values(), key=ORDER.index)
    label = f" - {LABELS[worst]}" if len(rep["verdicts"]) == 1 else ""
    print(f"\n  {headline(rep)}{label}   ({rep['seconds']}s)   {a.image}")
    print(f"  why: {rep['reason']}")
    if rep["hand_over"] and rep["held"]:
        print(f"  hand over now: {', '.join(rep['hand_over'])}")
        print(f"  hold:          {', '.join(rep['held'])}")
    if rep["path"]:
        print(f"  -> {PATH_LABELS[rep['path']]}" +
              (f"  (registry phone: {rep['callback']})" if rep["callback"] and rep["path"] == "call" else ""))
        for s_ in rep["steps"]:
            print(f"     - {s_}")
    else:
        print(f"  -> {ACTIONS[worst]}")
    for f in rep["findings"]:
        print(f"     ! {f}")
    for n in rep["notes"]:
        print(f"     . {n}")
    if rep["match"]:
        for i, r in enumerate(rep["match"]["regions"], 1):
            x, y, bw, bh = r["box"]
            print(f"       changed #{i}: {r['zone']:<11} at x={x} y={y} ({bw}x{bh}px)")
        if rep["match"].get("report_png"):
            print(f"     report: {rep['match']['report_png']}")
    if not a.no_record:
        record_check(rep, a.drugs, a.regn, a.doctor)
        if worst != "GREEN":
            print(f"     hold: {open_hold(rep)}  (rxcheck resolve <id> --outcome ...)")
    print()
    REPORTS.mkdir(exist_ok=True)
    save_json(REPORTS / f"{Path(a.image).stem}_report.json", public(rep))


def cmd_watch(a):
    """Pre-check images as they arrive (e.g. the WhatsApp Desktop download folder of the pharmacy's
    WhatsApp Business number) so the verdict is ready before the customer reaches the counter."""
    inbox = Path(a.folder)
    inbox.mkdir(parents=True, exist_ok=True)
    seen = {p.name for p in inbox.iterdir()} if not a.include_existing else set()
    print(f"watching {inbox.resolve()}  (Ctrl+C to stop)\n")
    print(f"{'time':8}  {'verdict':48}  {'secs':>4}  image / reason")
    while True:
        for p in sorted(inbox.iterdir(), key=lambda p: p.stat().st_mtime):
            if p.name in seen or p.suffix.lower() not in IMG_EXT:
                continue
            size = p.stat().st_size
            time.sleep(0.3)
            if p.stat().st_size != size:      # still being written
                continue
            seen.add(p.name)
            try:
                rep = run_check(p)
            except Exception as ex:  # a bad file must not stop the counter
                print(f"{datetime.now():%H:%M:%S}  {'ERROR':48}  {'':>4}  {p.name}: {ex}")
                continue
            why = (rep["findings"] or [f"new slip; {n}" for n in rep["notes"] if n.startswith("Regn")] or [""])[0]
            print(f"{datetime.now():%H:%M:%S}  {headline(rep):48}  {rep['seconds']:>4}  {p.name}  {why}")
            if not a.no_record:
                record_check(rep)
                # hold only document-level problems; drug-dependent AMBERs are decided at the counter
                if "RED" in rep["verdicts"].values() or rep["verdicts"].get("routine", "GREEN") != "GREEN":
                    print(f"{'':8}  hold {open_hold(rep)}")
            save_json(REPORTS / f"{p.stem}_report.json", public(rep))
        if a.once:
            return
        time.sleep(1)


def cmd_shared(a):
    """Shared (cross-pharmacy) index: see sharedindex.py and the README section 'Shared index'."""
    import sharedindex as sx
    if a.action == "hub-init":
        if not a.target:
            sys.exit("hub-init needs a folder for the hub")
        sx.Hub(a.target).stats()    # creates hub.db
        key = Path(a.key_out or "network.key")
        if key.exists():
            sys.exit(f"{key} exists - refusing to overwrite a network key")
        sx.new_network_key(key)
        print(f"hub created in {a.target}; network key written to {key}\n"
              f"Give the key file to each member pharmacy in person. Do NOT give it to the hub operator: "
              f"without it they cannot read or test signatures.")
    elif a.action == "join":
        if not (a.hub and a.key):
            sys.exit("join needs --hub DIR and --key FILE")
        print(f"joined as node {sx.join(a.hub, a.key)} (pseudonymous id, not the pharmacy's name)")
    elif a.action == "sync":
        print(json.dumps(sx.sync(), indent=2))
    elif a.action == "status":
        print(json.dumps(sx.status(), indent=2))
    elif a.action == "answer":      # internal: another pharmacy's request on stdin, the answer on stdout
        print(json.dumps(sx.answer_request(json.loads(sys.stdin.read()))))


def cmd_doctor(a):
    """Authenticated doctor confirmation: see doctorping.py and README 'Doctor confirmation'."""
    import doctorping as dp
    try:
        svc = dp.Service(a.service) if a.service else None
        if a.action == "service-init":
            print(f"service initialised in {a.service}; public key {svc.init()}")
        elif a.action == "enrol":     # doctor's side; the contact comes from the register, not from an option
            reg, _, _ = registry()
            entry = (reg or {}).get(a.regn)
            if a.registry:
                entry = (load_json(Path(a.registry), {}) or {}).get(a.regn)
            res = svc.enrol_start(a.regn, entry or {})
            print(f"enrolment code sent to {res['contact_hint']} (the number on the council register)")
        elif a.action == "enrol-confirm":
            if not (a.regn and a.otp and a.device_key):
                sys.exit("enrol-confirm needs --regn, --otp and --device-key FILE (written here: the doctor's phone key)")
            kf = Path(a.device_key)
            if not kf.exists():
                priv, pub = dp.new_keypair()
                kf.write_text(priv, encoding="utf-8")
            else:
                from cryptography.hazmat.primitives import serialization
                from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
                pub = dp._b64(Ed25519PrivateKey.from_private_bytes(base64.b64decode(kf.read_text(encoding="utf-8")))
                              .public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
            print(svc.enrol_confirm(a.regn, a.otp, pub))
        elif a.action == "inbox":
            for m in svc.inbox(a.contact or ""):
                print(f"{m['type']}: {m['text']}")
        elif a.action == "respond":
            row = dp.Service(a.service)._con().execute("SELECT summary_sha FROM requests WHERE id = ?", (a.ref,)).fetchone()
            if not row:
                sys.exit("unknown request")
            dec = "denied" if a.deny else "approved"
            priv = Path(a.device_key).read_text(encoding="utf-8")
            print(svc.respond(a.ref, dec, dp.device_sign(priv, a.ref, dec, row["summary_sha"])))
        elif a.action == "trust":
            print(f"trusting service {a.service}; this pharmacy is node {dp.trust(a.service)}")
        elif a.action == "ask":
            if not (a.hold and a.regn and a.drugs):
                sys.exit("ask needs --hold, --regn and --drugs (and --patient, --rx-date)")
            print(json.dumps(dp.ask(a.hold, a.regn, a.drugs, a.patient or "", a.rx_date), indent=2))
        elif a.action == "otp":
            print(json.dumps(dp.submit_otp(a.ref, a.code), indent=2))
        elif a.action == "status":
            print(json.dumps(dp.poll(a.ref), indent=2))
    except (dp.Refused, dp.Unreachable) as ex:
        sys.exit(str(ex))


def cmd_add(a):
    try:
        e = add_entry(a.image, a.id, a.dispensed, a.regn, a.doctor, a.drugs, a.note)
    except ValueError as ex:
        sys.exit(str(ex))
    print(f"added {e['id']} -> {LEDGER_DIR / e['image']}")


# ---------------------------------------------------------------- report image

def draw_report(ref, res, out_png, title):
    h, w = ref.shape[:2]
    a, b = ref.copy(), res["warped"].copy()
    overlay = cv2.addWeighted(ref, 0.35, np.full_like(ref, 255), 0.65, 0)
    overlay[res["missing"] > 0] = (0, 0, 220)   # red: on recorded slip, gone now
    overlay[res["added"] > 0] = (0, 160, 0)     # green: new on suspect
    for i, r in enumerate(res["regions"], 1):
        x, y, bw, bh = r["box"]
        for im in (a, b, overlay):
            cv2.rectangle(im, (x, y), (x + bw, y + bh), (0, 0, 255), 4)
            cv2.putText(im, f"#{i}", (x, max(30, y - 8)), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 0, 255), 3)
    labels = ["RECORDED (ledger)", "SUSPECT (aligned)", "DIFF  red=removed  green=added"]
    panels = []
    for im, lab in zip((a, b, overlay), labels):
        bar = np.full((70, w, 3), 255, np.uint8)
        cv2.putText(bar, lab, (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 0, 0), 3)
        panels.append(np.vstack([bar, im]))
    sheet = np.hstack([np.hstack([p, np.full((p.shape[0], 20, 3), 200, np.uint8)]) for p in panels])
    head = np.full((90, sheet.shape[1], 3), 255, np.uint8)
    cv2.putText(head, title, (20, 62), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (0, 0, 180), 4)
    sheet = np.vstack([head, sheet])
    cv2.imencode(".png", cv2.resize(sheet, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA))[1].tofile(str(out_png))

    # close-up pairs of each changed region: recorded on top, suspect below
    crops = []
    for i, r in enumerate(res["regions"], 1):
        x, y, bw, bh = r["box"]
        pad = 50
        x0, y0, x1, y1 = max(0, x - pad), max(0, y - pad), min(w, x + bw + pad), min(h, y + bh + pad)
        pair = np.vstack([ref[y0:y1, x0:x1], np.full((6, x1 - x0, 3), (0, 0, 255), np.uint8),
                          res["warped"][y0:y1, x0:x1]])
        tag = np.full((44, pair.shape[1], 3), 255, np.uint8)
        cv2.putText(tag, f"#{i}", (6, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 180), 2)
        crops.append(np.vstack([tag, pair]))
    if crops:
        H = max(c.shape[0] for c in crops)
        row = np.hstack([np.vstack([c, np.full((H - c.shape[0], c.shape[1], 3), 255, np.uint8)]) for c in
                         [np.hstack([c, np.full((c.shape[0], 16, 3), 255, np.uint8)]) for c in crops]])
        cv2.imencode(".png", row)[1].tofile(str(out_png.with_name(out_png.stem + "_closeups.png")))


# ---------------------------------------------------------------- cli

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)
    ad = sp.add_parser("add", help="record a dispensed high-risk prescription with its image")
    ad.add_argument("image")
    ad.add_argument("--id", required=True)
    ad.add_argument("--dispensed", required=True, help="YYYY-MM-DD")
    ad.add_argument("--regn", help="prescriber registration number as read on the slip")
    ad.add_argument("--doctor")
    ad.add_argument("--drugs", default="", help='as dispensed, e.g. "Alprax 0.25, Dolo 650"')
    ad.add_argument("--note", default="")
    ck = sp.add_parser("check", help="triage one prescription image")
    ck.add_argument("image")
    ck.add_argument("--drugs", help='what the customer is buying, e.g. "Alprax 0.25, Dolo 650"')
    ck.add_argument("--regn", help="registration number as read on the slip")
    ck.add_argument("--doctor")
    ck.add_argument("--rx-date", help="date written on the prescription, YYYY-MM-DD")
    ck.add_argument("--window", type=int, default=365, help="days of history to search")
    ck.add_argument("--issue-code", help="clinic-signed issuance code, if scanned separately (else read from the image)")
    ck.add_argument("--no-record", action="store_true", help="do not add to the index or open a hold")
    wt = sp.add_parser("watch", help="pre-check every new image in a folder")
    wt.add_argument("folder")
    wt.add_argument("--include-existing", action="store_true", help="also check images already in the folder")
    wt.add_argument("--once", action="store_true", help="process the folder once and exit")
    wt.add_argument("--no-record", action="store_true", help="do not add to the index or open holds")
    sp.add_parser("holds", help="list unresolved holds, oldest first")
    rs = sp.add_parser("resolve", help="record the outcome of a hold")
    rs.add_argument("hold")
    rs.add_argument("--outcome", required=True, choices=list(RESOLUTIONS))
    rs.add_argument("--note", default="")
    rs.add_argument("--reason", choices=list(REVIEW_REASONS), help="with dispensed-after-review: what was checked")
    sa = sp.add_parser("stats", help="warnings by path, holds, time to resolve, overrides")
    sa.add_argument("--days", type=int, default=1)
    lg = sp.add_parser("log", help="show the audit trail (append-only)")
    lg.add_argument("-n", type=int, default=20)
    bk = sp.add_parser("backup", help="consistent snapshot of the database, images and features")
    bk.add_argument("dest")
    bk.add_argument("--keep", type=int, help="keep only the newest N backups in DEST")
    vf = sp.add_parser("verify", help="check the ledger (or a backup folder) for damage or tampering")
    vf.add_argument("folder", nargs="?")
    vf.add_argument("--against", action="append", metavar="BACKUP",
                    help="also check that the audit trail still contains this earlier backup's trail unchanged "
                         "(repeatable; use a copy kept off-site)")
    rt =sp.add_parser("restore", help="replace the live ledger with a verified backup (current files kept aside)")
    rt.add_argument("backup")
    rt.add_argument("--force", action="store_true", help="restore even if the backup fails verification")
    dt = sp.add_parser("data", help="status of registry / drug list / policy, and audited imports")
    dt.add_argument("action", choices=["status", "import-registry", "import-drugs"])
    dt.add_argument("csv", nargs="?")
    dt.add_argument("--source", help="where the rows were checked (register / notification), by whom")
    dt.add_argument("--coverage", default="partial", choices=REGISTRY_COVERAGE,
                    help="'complete' only for a full official extract: then an unlisted Regn No is invalid")
    dt.add_argument("--replace", action="store_true", help="registry: replace all entries instead of merging")
    dt.add_argument("--verified-on", help="registry: date the rows were checked, if not in the CSV")
    dt.add_argument("--verified-by", help="drug list: name of the pharmacist / adviser who reviewed it")
    st = sp.add_parser("staff", help="counter app accounts")
    st.add_argument("action", choices=["add", "list", "disable", "pin"])
    st.add_argument("name", nargs="?")
    st.add_argument("--role", default="staff", choices=ROLES)
    st.add_argument("--pin-stdin", action="store_true", help="read the PIN from stdin (scripts)")
    cl = sp.add_parser("clinic", help="clinic side: key pair and signed issuance QR codes")
    cl.add_argument("action", choices=["keygen", "issue"])
    cl.add_argument("key", help="clinic private key file (.pem)")
    cl.add_argument("--regn")
    cl.add_argument("--date", help="prescription date YYYY-MM-DD")
    cl.add_argument("--drugs", help='as prescribed, e.g. "Alprax 0.25, Dolo 650"')
    cl.add_argument("--patient", default="", help="name/initials and age, shown to the pharmacist")
    cl.add_argument("--out", default="issuance_qr.png")
    sh = sp.add_parser("shared", help="shared index across pharmacies (hash-only; see README)")
    sh.add_argument("action", choices=["hub-init", "join", "status", "sync", "answer"])
    sh.add_argument("target", nargs="?", help="hub-init: folder for the hub")
    sh.add_argument("--key-out", help="hub-init: where to write the network key (default network.key)")
    sh.add_argument("--hub", help="join: hub folder")
    sh.add_argument("--key", help="join: the network key file handed over in person")
    dc = sp.add_parser("doctor", help="authenticated doctor confirmation (mock service; see README)")
    dc.add_argument("action", choices=["service-init", "enrol", "enrol-confirm", "inbox", "respond", "trust", "ask",
                                       "otp", "status"])
    dc.add_argument("ref", nargs="?", help="request id (otp, status, respond)")
    dc.add_argument("code", nargs="?", help="otp: the code the doctor read out")
    dc.add_argument("--service", help="service folder (service-side commands, trust)")
    dc.add_argument("--regn")
    dc.add_argument("--registry", help="enrol: council register file whose sourced phone is the doctor's contact")
    dc.add_argument("--otp", help="enrol-confirm: the code sent to the doctor")
    dc.add_argument("--device-key", help="the doctor's phone key file")
    dc.add_argument("--contact", help="inbox: which phone's messages to show")
    dc.add_argument("--hold")
    dc.add_argument("--drugs")
    dc.add_argument("--patient", help="initials and age are sent, never the name")
    dc.add_argument("--rx-date")
    dc.add_argument("--deny", action="store_true", help="respond: deny instead of approve")
    sv = sp.add_parser("serve", help="counter app: one decision, its reason, the evidence and the next action")
    sv.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to serve other billing PCs on the LAN")
    sv.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 8765), help="default: $PORT or 8765")
    sv.add_argument("--inbox", default=str(HOME / "inbox"), help="pre-checked images to offer at the counter")
    sv.add_argument("--backup-dir", default=os.environ.get("RXCHECK_BACKUP_DIR"),
                    help="scheduled backups go here (another disk / USB; default $RXCHECK_BACKUP_DIR)")
    sv.add_argument("--behind-proxy", action="store_true",
                    help="HTTPS is terminated by a reverse proxy / cloud host: secure cookies, client IP from it")
    sv.add_argument("--backup-hours", type=float, default=4.0)
    sv.add_argument("--cert", help="TLS certificate (PEM) - use when other PCs connect over the LAN")
    sv.add_argument("--key", help="TLS private key (PEM)")
    a = p.parse_args()
    if a.cmd == "data" and a.action != "status" and not a.csv:
        p.error("data import needs a CSV file")
    if a.cmd == "clinic" and a.action == "issue" and not (a.regn and a.date and a.drugs):
        p.error("clinic issue needs --regn, --date and --drugs")
    {"add": cmd_add, "check": cmd_check, "watch": cmd_watch, "holds": cmd_holds, "resolve": cmd_resolve,
     "log": cmd_log, "backup": cmd_backup, "verify": cmd_verify, "restore": cmd_restore, "data": cmd_data,
     "staff": cmd_staff, "clinic": cmd_clinic, "serve": cmd_serve, "stats": cmd_stats,
     "shared": cmd_shared, "doctor": cmd_doctor}[a.cmd](a)


if __name__ == "__main__":
    sys.modules.setdefault("rxcheck", sys.modules["__main__"])   # `import rxcheck` elsewhere must be THIS module
    main()
