"""Write the public demo's synthetic sample slips to demo/ (images + samples.json).

Run:  python tests/make_demo_samples.py
The counter app records demo/reference.jpg as an already-dispensed slip when it starts in demo mode
(RXCHECK_DEMO=1), so the samples show a RED edited copy, a GREEN re-sent copy and an AMBER new slip.
"""
import json

import cv2

from scenarios import ROOT, demo_slips

OUT = ROOT / "demo"
SAMPLES = {
    "reference": {"id": "RX-DEMO-0812", "dispensed": "2026-08-12", "regn": "DEMO-1001", "doctor": "Dr. Demo Sample",
                  "drugs": "Dolo 650, Pan 40, Alprax 0.25"},
    "samples": [
        {"key": "edited", "image": "edited.jpg", "label": "Edited copy",
         "expect": "RED", "what": "The recorded slip with a drug line added by hand",
         "fields": {"drugs": "Alprax 0.5", "regn": "DEMO-1001", "doctor": "Dr. Demo Sample", "patient": "Sample Patient"}},
        {"key": "resent", "image": "resent.jpg", "label": "Re-sent on WhatsApp",
         "expect": "GREEN", "what": "The same recorded slip, photographed again and forwarded",
         "fields": {"drugs": "Dolo 650, Pan 40", "regn": "DEMO-1001", "doctor": "Dr. Demo Sample", "patient": "Sample Patient"}},
        {"key": "new", "image": "new.jpg", "label": "New slip, high-risk drug",
         "expect": "AMBER", "what": "A slip never seen before, with a high-risk medicine on it",
         "fields": {"drugs": "Alprax 0.25, Dolo 650", "regn": "DEMO-2002", "doctor": "Dr. Second Sample", "patient": "Sample Patient"}},
    ],
}

if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for name, img in demo_slips().items():
        cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tofile(str(OUT / f"{name}.jpg"))
    SAMPLES["reference"]["image"] = "reference.jpg"
    (OUT / "samples.json").write_text(json.dumps({"_note": "Synthetic sample slips for the public demo. Not real "
                                                  "prescriptions. Regenerate with tests/make_demo_samples.py.",
                                                  **SAMPLES}, indent=2) + "\n", encoding="utf-8")
    print("wrote", sorted(p.name for p in OUT.iterdir()))
