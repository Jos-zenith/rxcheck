"""Write the public demo's synthetic sample slips to demo/ (images + samples.json).

Run:  python tests/build_demo_samples.py
The counter app records demo/reference.jpg as an already-dispensed slip when it starts in demo mode
(RXCHECK_DEMO=1), so the samples show a RED edited copy, a GREEN re-sent copy and an AMBER new slip.

The 'printed' sample shows the slip reader. The public deployment has no OCR engine (half a CPU: a reading would
take a minute), so the demo replays a RECORDED reading: what the real engine read from demo/printed.jpg, as text
pieces with their boxes and confidences. Everything after that - the fields filled in, what needs a tick, the
cut-outs, the states, the check - runs live on that recording. It is re-recorded here whenever the engine is
installed; tests/test_slip_reader.py checks the recording still matches a fresh reading.
"""
import json
import os
import sys
from datetime import date

import cv2

os.environ["RXCHECK_OCR"] = "on"   # synthetic_slips switches it off for the test runs; here the reading is the point
from synthetic_slips import ROOT, demo_slips  # noqa: E402

sys.path.insert(0, str(ROOT / "app"))
OUT = ROOT / "demo"
SAMPLES = {
    "reference": {"id": "RX-DEMO-0812", "dispensed": "2026-08-12", "regn": "DEMO-1001", "doctor": "Dr. Demo Sample",
                  "drugs": "Dolo 650, Pan 40, Alprax 0.25"},
    "samples": [
        {"key": "printed", "image": "printed.jpg", "label": "Printed slip, read by OCR",
         "expect": "AMBER", "what": "The reading fills the form; one smudged line is left for you to check",
         "fields": {"patient": "Sample Patient"},
         # what the recorded reading fills in on the demo counter (checked by tests/test_deployment.py)
         "after_reading": {"drugs": "Dolo 650, Pan 40, Alprax 0.25", "regn": "DEMO-1001", "doctor": "Dr. Demo Sample",
                           "rx_date": "2026-10-20"}},
        {"key": "edited", "image": "edited.jpg", "label": "Edited copy",
         "expect": "RED", "what": "The recorded slip with a drug line added by hand",
         "fields": {"drugs": "Alprax 0.5", "regn": "DEMO-1001", "doctor": "Dr. Demo Sample", "patient": "Sample Patient"}},
        {"key": "resent", "image": "resent.jpg", "label": "Re-sent on WhatsApp",
         "expect": "GREEN", "what": "The recorded slip again, routine medicines only: recognised as a repeat, and allowed",
         "fields": {"drugs": "Dolo 650, Pan 40", "regn": "DEMO-1001", "doctor": "Dr. Demo Sample", "patient": "Sample Patient"}},
        {"key": "new", "image": "new.jpg", "label": "New slip, high-risk drug",
         "expect": "AMBER", "what": "A slip never seen before, with a high-risk medicine on it",
         "fields": {"drugs": "Alprax 0.25, Dolo 650", "regn": "DEMO-2002", "doctor": "Dr. Second Sample", "patient": "Sample Patient"}},
    ],
}


def record(path, old):
    """The engine's reading of the shipped image, or the earlier recording when the engine is not installed."""
    import slip_reader as ocr
    if not ocr.available():
        print("OCR engine not installed: keeping the recorded reading in demo/samples.json")
        return old
    import importlib.metadata as md
    t0 = __import__("time").time()
    segs = ocr.read_segments(ocr.load(path))
    return {"engine": f"rapidocr_onnxruntime {md.version('rapidocr_onnxruntime')}", "recorded_on": date.today().isoformat(),
            "ms": int((__import__("time").time() - t0) * 1000),
            "segments": [{"box": list(b), "text": t, "conf": round(c, 4)} for b, t, c in segs]}


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for name, img in demo_slips().items():
        cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tofile(str(OUT / f"{name}.jpg"))
    SAMPLES["reference"]["image"] = "reference.jpg"
    try:
        old = json.loads((OUT / "samples.json").read_text(encoding="utf-8"))
        old_reading = next((s.get("reading") for s in old["samples"] if s["key"] == "printed"), None)
    except (OSError, ValueError, KeyError):
        old_reading = None
    printed = next(s for s in SAMPLES["samples"] if s["key"] == "printed")
    printed["reading"] = record(OUT / "printed.jpg", old_reading)
    if not printed["reading"]:
        sys.exit("no recorded reading for the printed sample: install the OCR engine once (requirements-ocr.txt)")
    (OUT / "samples.json").write_text(json.dumps({"_note": "Synthetic sample slips for the public demo. Not real "
                                                  "prescriptions. Regenerate with tests/build_demo_samples.py.",
                                                  **SAMPLES}, indent=2) + "\n", encoding="utf-8")
    print("wrote", sorted(p.name for p in OUT.iterdir()))
