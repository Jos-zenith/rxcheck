# Counter usability test: protocol (draft)

Status: ready to run; not run yet. It needs no real prescriptions: every case is synthetic. It answers one
question the lab tests cannot: **do pharmacists, under queue pressure, make the right call quickly, and do held
cases get followed up?** Field accuracy on real slips is a separate study ([field-validation-protocol.md](field-validation-protocol.md)).

## Who and where

- 6-8 pharmacists and counter staff from at least 3 pharmacies (tier-2, Tamil Nadu first), each paid for their
  time and told in writing that this is a test of the software, not of them. No patient data is used or collected.
- Their own counter, or a desk set up like one, with the counter app on a PC (`python app/rxcheck.py serve`,
  slip reading installed) and a second person playing the customer.
- One session per person, about 45 minutes: 5 minutes of introduction with the demo samples, then the timed
  cases, then 10 minutes of questions.

## The cases

24 synthetic cases per person, in a shuffled order, from material already in the repository:

| # | Kind | Built from | Right outcome |
|---|---|---|---|
| 4 | Printed slip, routine medicines, clean | `eval/build_slip_reader_set.py` printed slips | Dispense; nothing held |
| 3 | Same slip re-sent (WhatsApp, forwarded) | `tests/synthetic_slips.py` whatsapp / forwarded of a recorded slip | Routine: dispense; high-risk: hold for the register check |
| 3 | Edited copy of a recorded slip (drug line added, strength raised) | `tests/synthetic_slips.py` added_line, a raised strength entered | Hold; do not dispense until the clinic confirms |
| 3 | New slip with a high-risk medicine | `other_slip`, an Alprax / Lyrica line | Hold; call the clinic on the registry number |
| 2 | Look-alike or unlisted controlled name ("Panazep", "Alprex") | typed or printed | Held as high-risk; confirm what it is |
| 2 | Unreadable photo (glare, thumbnail) | `blown_glare`, `tiny` | Ask for a retake; nothing decided on the image |
| 2 | Doctor's genuine rewrite of a recorded slip | `rewrite` | Confirm with the clinic; not treated as fraud |
| 2 | Smudged line the reader cannot read | `tests/synthetic_slips.py` printed_slip | Type it from the slip; tick the readings |
| 2 | PDF, 2 pages | `eval/build_slip_reader_set.py` PDFs | As for the medicines on it |
| 1 | Clinic does not answer the call | a held case, the "clinic" never picks up | Record "No answer"; the hold waits and is followed up |

The registry is filled with the synthetic doctors used in the cases, so "not in the registry" appears only where
a case is built for it. A short queue (two customers waiting) is kept going by the second person.

## What is measured

All from the app's own records (the audit trail and holds), plus a stopwatch for what the app cannot see:

| Measure | From | Target to discuss (not a claim) |
|---|---|---|
| Time per case: photo chosen to outcome recorded | audit: `check` and `resolve` times; stopwatch for GREEN cases | under 60 s for a GREEN case, under 3 min to open a hold |
| **Missed alterations**: an edited or look-alike case dispensed without a hold being resolved | holds + case outcomes vs the right outcome | 0 |
| **Unnecessary holds**: a hold on a case whose right outcome is "dispense" | holds vs the right outcome | as few as the rules allow; each one explained |
| Correct escalation: held cases closed with the right outcome (call, retake, counter check) | `resolve` outcome and reason | all |
| Overrides, and whether the note says why | `resolve` with override | each with a reason staff can defend |
| Follow-up: the "No answer" case picked up again at its follow-up time | holds page, `resolve` / `hold-assign` history | yes, within the session |
| Slip reading: values kept vs corrected, ticks per case | photo reading report (`/ocr-report`) | — |
| Did the page make them believe something unverified? | post-session question: "what did the screen tell you about who wrote this slip?" | nobody says "it was verified" for an unsigned slip |

Each wrong outcome is reviewed with the participant afterwards: was it the screen, the wording, the time
pressure, or the rule itself.

## After the sessions

- A short report per measure, with every miss and every unnecessary hold listed and its cause.
- Changes to wording or layout are re-tested with at least 2 new participants before the shadow pilot.
- Thresholds in `app/slip_reader.py` (FILL_MIN, CONFIRM_ABOVE) are **not** set from this study: synthetic slips do not
  say how real handwriting reads. They are set from the shadow pilot's photo reading report.
