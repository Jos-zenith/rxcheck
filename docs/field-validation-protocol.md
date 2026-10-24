# Field validation protocol

The public tests use synthetic fixtures only. They show how the pipeline behaves on those fixtures;
they do not establish accuracy on real prescriptions. This document says what evidence would be needed
before relying on the results, and how to collect it without fooling ourselves.

`eval/score_image_checks.py` enforces the mechanical parts. It refuses a held-out set that shares a prescriber or pharmacy with the tuning set. It locks the thresholds the first time the held-out set is scored. Until the minimums below are met, it labels every report **"NOT A FIELD VALIDATION"**.

## 1. What to collect

| Set | Minimum | Why this many |
|---|---|---|
| Genuine prescriptions, as presented at real counters | **300**, from **≥ 20 prescribers** and **≥ 3 pharmacies** | With 0 false REDs in 300, the 95% upper bound on the false-RED rate is about 1% (rule of three). With 30 cases it is 10%, which is useless for a counter decision. |
| ... of which: same doctor's repeat slips rewritten by hand (chronic patients) | ≥ 30 pairs | Scenario A, the biggest open risk. A rewrite must never become "edited copy". |
| ... Tamil / regional-language slips; government OP chits; phone-screen photos; photocopies | ≥ 30 each | Each is a separate failure mode. Simulations exist; real samples don't. |
| Manipulated prescriptions (fraud set) | **60**, made by **people not involved in tuning** | Our own forgeries test what we already thought of. |
| ... re-dated or dose-edited copies of slips in the ledger (AI tools and manual editing) | ≥ 30 | The case the tool is built for: re-use with edits. |
| ... fresh fabrications using a real doctor's identity | ≥ 15 | Scenario E. Only issuance confirmation or a signed code can catch these, so expect the image checks to miss them. |
| ... real handwriting moved or pasted onto another slip | ≥ 15 | Known gap (cut-and-paste). |

Record for every case: phone model, how it arrived (WhatsApp / direct photo / screenshot / photocopy), language, prescriber, pharmacy, and the drugs requested.

## 2. Ground truth

- **Genuine** means the issuing clinic confirmed it, or the slip carries a clinic-signed issuance code that verified. "The customer seemed honest" is not ground truth.
- **Fraud** means it was made by the red team (known by construction), or a case the clinic denied issuing. Record which.
- Cases where nobody could confirm either way go into a separate **unresolved** column. They are not counted as genuine.

## 3. Consent, ethics and privacy

- Written consent from each patient whose prescription is used, and from each doctor whose identity or handwriting appears. **Fabrications that use a real doctor's identity need that doctor's explicit consent.** Never present a test fabrication at a real counter as if it were real.
- If an institution is involved, get ethics-committee approval first.
- Keep images in `eval/field/` (git-ignored) on an encrypted disk. Never commit them. Share only de-identified, consented images, and only for this study.
- Define retention and deletion dates before collecting, in line with the DPDP Act 2023. A pharmacy compliance adviser should approve the plan.

## 4. Splitting and freezing

1. Assign whole prescribers **and** whole pharmacies to `tune` or `holdout`. Put at least a third of the prescribers in `holdout`. Never split one prescriber's slips across both sides. The evaluator rejects a manifest that does.
2. Tune thresholds on `tune` only.
3. **Before** scoring `holdout`, write down the acceptance criteria (section 5), commit them, and commit the code.
4. Run `python eval/score_image_checks.py --split holdout --manifest eval/field/manifest.csv --setup eval/field/setup.json` **once**. The first run writes the lock. Commit it.
5. If a threshold changes afterwards, every later held-out result is reported as **CONTAMINATED**. To regain an independent estimate, collect a new held-out set.

## 5. Acceptance criteria (to be set before scoring the held-out set)

These are proposals. The pharmacy and its adviser decide the final values and record them in a committed file before scoring.

| Measure | Proposed requirement |
|---|---|
| Genuine marked RED | Upper 95% bound ≤ 2% |
| Fraud stopped before dispensing (RED or AMBER), for edited copies of ledger slips | Lower 95% bound ≥ 85% |
| Genuine needing confirmation (AMBER), routine medicines | ≤ 10% (otherwise staff will stop calling) |
| Genuine needing **a call to the clinic** (any medicine) | ≤ 5% of prescriptions, i.e. a number of calls per day the counter can actually make |
| Rewrites by the same doctor called "edited copy" | 0 |
| Median time per check on the pharmacy's own PC | ≤ 2 s |

Report every number with its Wilson interval, overall and **by origin, language, arrival channel and prescriber**. One prescriber or one phone model can hide a failure behind a good average.

## 6. Prospective shadow pilot

Held-out images still miss what happens at a real counter: the queue, the staff, the phone calls. After the held-out evaluation passes:

- Run the counter app in **shadow mode** for 4–8 weeks at 2–3 pharmacies. Staff check every prescription image, but dispense on their usual judgement, without seeing the verdict.
- Each week, compare verdicts with outcomes: clinic call-backs, refusals, the H1 register.
- Measure what warnings **cost the counter**, not only how accurate they are. A correct warning that nobody can act on in a queue gets ignored. `rxcheck stats --days 7` reports most of this from the audit trail:
  - warnings per day by step (retake / counter check / call), and how many prescriptions were split (routine items handed over, one item held);
  - calls per day, how many went unanswered (`unreachable`), and how long a held prescription waited (median minutes to close);
  - **the override rate**: held prescriptions dispensed without the step that was asked for. A rising rate is the early sign of warnings being waved through. Review every override with the team each week: was the warning wrong for this counter, or was the queue winning?
  - customers who left without their medicine because of a hold (record by hand), and time added per customer at peak hours.
- Also measure cases where staff disagreed with the tool, and why.
- Stop and redesign, rather than go live, if calls per day exceed what the counter can make at peak hours, or if overrides exceed 20% of holds. These are proposed limits, to be fixed with the acceptance criteria above.
- Only then let the verdict influence dispensing, starting with high-risk medicines.

## 7. Manifest format

Same columns as `eval/manifest.csv`:

`id, truth (genuine|fraud), scenario, image, drugs, regn, doctor, copy_of, origin (real|derived|synthetic), prescriber, pharmacy, split (tune|holdout)`

`setup.json` lists what the pharmacy already had on record before the study began: ledger entries with images, and index entries.
