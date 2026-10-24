# rxcheck: prescription verification triage for Indian pharmacy counters

A research prototype that helps a pharmacist decide in a second or two whether a prescri
ption image can be dispensed, needs confirmation, or has failed verification.

> **This is a triage tool, not a certificate of authenticity.** A clean result does not prove a doctor issued the prescription, unless the slip carries a clinic-signed issuance code that verified. A failed check is not, by itself, proof of fraud.

Related documents: [docs/ANALYSIS.md](docs/ANALYSIS.md) (technical overview) · [docs/field-validation.md](docs/field-validation.md) (what evidence is still needed, and how to collect it) · [docs/blog.md](docs/blog.md) (public write-up, draft)

## Project structure

| Path | Purpose |
|---|---|
| `app/` | Python verification engine, CLI, counter web app, and optional slip reading (OCR) |
| `data/` | Empty prescriber registry plus illustrative policy and drug-risk defaults |
| `docs/` | Technical overview (ANALYSIS.md), field-validation protocol and public write-up |
| `eval/` | Dataset builder and evaluator; generated datasets and results stay local |
| `tests/` | Automated checks and synthetic test-image generation |
| `web/` | Vue/Vite source for the counter UI |
| `static/` | Committed browser bundle served by the Python app |
| `site/` | Static landing page for the project |

## Problem

Customers increasingly show prescriptions as phone images, often forwarded on WhatsApp. These images can be AI-generated, edited copies of genuine slips, or genuine slips re-used. Pixel-level "AI detectors" can't settle this:

- a fraudulent prescription need not be AI-made,
- an AI-made one can look perfect,
- and genuine images forwarded on WhatsApp lose their metadata and look "processed".

What a pharmacist can act on are facts a forger doesn't control:

- **What has been seen or dispensed before.** Is this the same slip, possibly with edits?
- **Who the prescriber is.** Does the registration number exist and belong to that doctor?
- **Who issued this slip.** Did a participating clinic sign it?
- **What is being sold.** Does this drug, and does this request match what was on the slip?

## Intended users and scope

| | |
|---|---|
| **Users** | Retail pharmacists and counter staff, initially single pharmacies in Tier-2 cities, with one or more billing counters |
| **In scope** | Re-use and edit detection; authenticated doctor confirmation (simulated service); hash-only shared index across pharmacies (simulated network); no-image index of every checked slip; prescriber registry check; clinic-signed issuance codes; drug-risk policy; consistency of requested drugs with the matched record or signed code; confirmation of ambiguous medicine entries; reading the slip photo to fill in the form (optional, offline OCR); pre-checking images as they arrive; counter app with staff sign-in; hold-and-escalate queue; audited data imports; verify, backup and restore; offline operation |
| **Out of scope (now)** | Proving issuance for slips without a signed code (needs a call to the clinic); reading handwriting reliably; a production shared-index service (HTTPS, member authentication, key rotation); verifying e-prescription platforms (a PDF is accepted and read, but its text proves no more than a photo); edit detection beyond page 1 of a PDF; clinical appropriateness |

## How it works

```
image ─► issuance QR? ── clinic-signed code ──► verify with the clinic's key ─► cross-check Regn No, date, medicines
  │                                                                               and whether this code was seen before
  ▼
quality gate ── unusable (glare, blur, tiny, dark) ──► manual review (AMBER), never "new slip"
  │ usable
  ▼
paper detection ─► fingerprint shortlist ─┬─► image ledger entry:  align (ORB+RANSAC → ECC sub-pixel)
                   (~5 ms over 20k slips)  │     → identity: copy / similar / different
                                           │     → edits boxed (copy only)
                                           └─► index entry (no image): layout correlation → "seen before"
 Regn No ─► registry + cross-check with matched record ──┐
 drugs   ─► risk tier + compare with matched record ─────┼─► decision matrix + policy ─► verdict + one reason ─► hold queue
 file    ─► metadata: supporting evidence only ──────────┘
```

### What the ledger covers

The tamper check can only compare against slips it knows. This is the coverage decision:

| Store | What is kept | For which slips | What it can say |
|---|---|---|---|
| **Image ledger** (`rxcheck add`, or "Record as dispensed" in the counter app) | Prescription image + features | Slips dispensed for **high-risk drugs**, alongside the legally required H1 register | "Copy with edits here" (RED), "same slip" (repeat), "similar, possibly rewritten" |
| **No-image index** (automatic on every check) | 1,500 keypoints + a 175×238 px ink-density map (unreadable) + file hash + what was requested. About 110 KB | **Every checked slip** | "Seen before on <date>", plus whether today's request differs from the recorded one. **Never RED**, because without an image it can't locate edits |

Index matching tries two alignments and keeps the better one: a full perspective model (needed for photos at an angle or of a phone screen), and a rotation-scale-shift model. With only a dozen matching points the perspective model overfits, and Windows and Linux builds of OpenCV overfit differently. A heavily forwarded copy scored 0.88 on one and 0.75 on the other, against a 0.75 threshold. The simpler model scored 0.97 on both. Different slips score 0.0 under either.

So routine prescriptions *are* covered for re-use and changed requests, but not for edit localisation. RED for an edited copy needs a slip in the image ledger. When `watch` pre-checks a slip before its medicines are known, the counter check later that day fills them in, so the index records what was actually sold. Retention periods, consent and what may be recorded must be confirmed with a pharmacy compliance professional.

### Doctor confirmation: the prescriber's own signed answer

"Clinic confirmed" used to be recorded on the pharmacist's word. `app/doctorping.py` replaces the word with evidence, for the holds where it matters (a high-risk medicine waiting for a call):

| Step | What happens | Why it holds up |
|---|---|---|
| **Enrolment** | The doctor's contact is the **sourced phone on the council register** (the same `phone` + `phone_source` rule as the callback number). A one-time code goes to that number; the doctor enters it and registers a device key | The enrolment call has no contact parameter. Someone who enrols a doctor they are not still sends the code to the official number. 5 wrong codes lock it; an enrolled doctor cannot be re-enrolled |
| **Ping** | `rxcheck doctor ask` for a hold. The doctor's phone gets: **patient initials and age, the medicines, the prescription date, a pseudonymous pharmacy id**, and a one-time code | Neither the slip nor the pharmacist can supply or redirect the number (`ask()` has no contact parameter). The service refuses anything but initials. At most 3 pings per doctor per hour, 30 per pharmacy per day |
| **Answer: app tap** | The doctor's device signs `request, decision, hash of what was asked` with the enrolled key | A signature from any other key, or an approval replayed as a denial, is refused |
| **Answer: read-out code** | The doctor reads the code to the pharmacist, who enters it | Bound to that request **and** that pharmacy, single use, 3 tries, 15 minutes. It can approve, never deny |
| **Receipt** | The service signs `{request, doctor, hash of the exact prescription, decision, channel, pharmacy, time}`. The pharmacy verifies it with the service's public key and stores it | Edited, for another prescription, for another pharmacy, signed by another key, or older than 24 h: worth nothing |

**What it changes in a hold.** `confirmed` after a verified approval is recorded as **authenticated** (channel and request id in the audit trail). `confirmed` without a receipt is still possible and is recorded as **not authenticated**, so the pharmacist's word stays visible as the weaker evidence. A verified **denial** blocks `confirmed` and `dispensed-after-review` for that hold: only `refused` can close it. A denial is final: it is read from the audit trail, so unlike an approval it does not expire after 24 h and does not disappear if the service config is removed. Policy switch `require_receipt_ndps` in `data/policy.json` (default **off**): when on, an NDPS / Schedule X item is confirmed only on a receipt. A doctor who is not enrolled, a service that cannot be reached or a ping with no answer all fall back to the call-back path; nothing is ever auto-approved.

```bash
# doctor side (mock phone): enrol from the council register, then answer from the phone key
python app/rxcheck.py doctor service-init --service svc/
python app/rxcheck.py doctor enrol --service svc/ --regn REGN-X --registry council.json     # code goes to the register's number
python app/rxcheck.py doctor enrol-confirm --service svc/ --regn REGN-X --otp 123456 --device-key phone.key
python app/rxcheck.py doctor inbox --service svc/ --contact "+91 98400 11122"                # what the doctor's phone shows
python app/rxcheck.py doctor respond REQUEST_ID --service svc/ --device-key phone.key [--deny]
# pharmacy side
python app/rxcheck.py doctor trust --service svc/
python app/rxcheck.py doctor ask --hold H-... --regn REGN-X --drugs "Alprax 0.25" --patient "Ravi Kumar, 34" --rx-date 2026-10-22
python app/rxcheck.py doctor otp REQUEST_ID 123456        # the code the doctor read out
python app/rxcheck.py doctor status REQUEST_ID
python app/rxcheck.py resolve H-... --outcome confirmed   # authenticated if a verified approval exists
```

**At the counter.** A hold on the call path gets an **Ask the doctor** card (once the pharmacy has run `doctor trust`). A pharmacist sees a form with the Regn No prefilled, the patient as initials only (age added by hand), the held medicines and a plain statement of what the doctor's phone receives; after asking, the card shows where it went (masked number), a **Check for the doctor's answer** button (anyone signed in) and a field for the code the doctor reads out (pharmacist only). A verified approval shows *Doctor confirmed* and the confirm button says it is backed by the doctor's receipt; a verified denial shows *Doctor DENIED*, removes the confirm button and leaves only *Refuse*. Every outcome without an answer (not enrolled, rate-limited, service down, expired, locked) says so and points back to the call-back path. The code is never shown or echoed. The hold's history and the audit log list who asked, the doctor's answer and whether the confirmation was authenticated or on the pharmacist's word.

This is a **simulation**: the service is a folder (keys, `service.db`, a mock phone outbox), and the pharmacy side only calls request / verify code / status. **Not built:** a real SMS or app channel, a doctor-initiated change of contact, key rotation, and the real enrolment source (the council register or ABDM HPR holding a verified mobile number for each doctor, through an official integration). A doctor can only be pinged if they enrolled, so adoption is the limit: until a doctor enrols, the call-back path stands.

### Shared index: a clone dispensed at another pharmacy

The local ledger only knows this pharmacy's slips, so a clone of a slip dispensed across town looks new. `app/sharedindex.py` adds a network of member pharmacies and a hub, **without sharing prescriptions**:

| Step | What happens | What leaves the pharmacy |
|---|---|---|
| Publish | Every recorded slip gets a 256-bit signature: a keyed random +-1 projection (SimHash) of the 24x32 ink-layout grid, plus an opaque token, the date and `dispensed`/`seen` | Signature, token, date, kind. **No** image, Regn No, doctor, patient, medicine or local id |
| Stage 1 (hub) | The hub returns up to 5 entries of *other* pharmacies within 72 bits | The signature of the slip being checked |
| Stage 2 (the candidate's owner) | The asking pharmacy sends its **compact index entry** (the unreadable keypoints and 1/8-scale density map the local index keeps, about 94 KB) to that one pharmacy. The owner compares locally with the same `index_match` the local index uses (threshold 0.75) and answers match / no match; on a match also the date, `dispensed`/`seen` and **keyed tags** of the medicines recorded | To the owner only, and only for a stage-1 candidate. A non-match answer is just `{match: false}` |
| Decide | A match is "seen at another pharmacy": AMBER for high-risk medicines, never RED (without the image, edits cannot be located). Medicines added or strengths changed against the other pharmacy's record are noticed through the tags and held item by item | - |

The projection is derived from a **network key the hub operator does not hold**, so the hub cannot test a candidate image against what it stores (a different key gives an unrelated signature, about 123 of 256 bits). Requests between pharmacies carry a MAC under the network key, a timestamp (10 minutes) and a size limit; every answer is recorded in the answering pharmacy's audit trail (who asked, match or not, never content).

**Stage 1 alone is never a verdict.** In the tests a different slip on the same letterhead falls 46 bits from the original, inside the radius, and stage 2 rejects it. Same-slip copies measured 0-43 bits over seven kinds of copy; unrelated slips 89-175.

**Never more lenient when it cannot ask.** If the hub (or a candidate's owner) is unreachable, the slip is treated as new *and unchecked*: high-risk medicines wait for a call even when `confirm_new_high_risk` is off, routine sales continue. The slip is recorded as unchecked; `rxcheck shared sync` publishes what could not be published and **asks again**; a match found then opens a hold and an audit record (`shared-late-match`) instead of being dropped.

**Measured limits (`tests/test_shared.py`).** Stage 2 inherits the local no-image index, so a phone-screen photo, a heavily blurred photo and a 5x-forwarded copy are **not** recognised across pharmacies (the slip is then treated as new, so high-risk medicines are still held by the new-slip policy). A density-only alignment fallback was tried and rejected: it cannot tell a phone-screen photo (0.71-0.74) from a different slip on the same letterhead (0.82-0.84). Matched: WhatsApp forward, cropped photo, dim light, edited clone (added drug line), edited clone then WhatsApp. A washed-out photocopy is sent back for a retake by the quality gate before any network call.

```bash
python app/rxcheck.py shared hub-init hub/ --key-out network.key   # key goes to member pharmacies in person, not the hub operator
python app/rxcheck.py shared join --hub hub/ --key network.key     # each pharmacy (random pseudonymous node id)
python app/rxcheck.py shared status
python app/rxcheck.py shared sync                                  # after an outage
```

This is a **simulation**: the hub is a folder (`hub.db`) and a peer is another `RXCHECK_HOME` reached through a subprocess speaking JSON, so two pharmacies really are separate processes. The messages are the protocol; replacing both with HTTPS is a transport change. **Not built:** authenticated membership beyond the shared-key MAC, key rotation (a pharmacy that leaves still knows the key), rate limiting, consent text for the DPDP Act 2023. A stage-1 candidate's owner learns the coarse layout of the slip being checked; the hub learns only random-looking bits, dates and how often each node asks.

### Identity: copy, similar or different

- **Copy:** at least 92% of strokes coincide within 1 px **and** tile correlation is at least 0.88, after sub-pixel alignment. This is measured on the *unchanged* part of the page, so an added line doesn't hide the copy; that exclusion only applies when edits cover ≤ 25% of the handwriting. Only a copy can produce "edits", and so RED.
- **Similar:** strokes are near but don't coincide, as when the same doctor rewrites the slip or a copy is heavily redrawn. The result is **AMBER (confirm), never RED.**
- **Different:** a new slip.

### Issuance codes: the independent path for new prescriptions

Image matching only recognises slips the pharmacy has seen before. A **newly fabricated** slip using a real doctor's identity has nothing to match, and no pixel analysis can show who wrote it. For high-risk medicines the conservative rule stays: a new slip needs a call to the clinic. Issuance codes are the path that can replace the call, and the only check that speaks to *authorship*:

1. A participating clinic creates a key pair once: `rxcheck clinic keygen clinic.pem`. The private key stays at the clinic.
2. The pharmacy collects the clinic's **public** key in person and records it in the registry, together with where it came from (`issuer_key`, `issuer_key_source`). A key without a source is never trusted. If the clinic says it signs every prescription, also record `signs_all: true`, so a slip in that doctor's name *without* a code is not waved through.
3. For each prescription the clinic prints a QR code (`rxcheck clinic issue clinic.pem --regn ... --date ... --drugs ... --patient ...`). The code is an Ed25519 signature over the Regn No, date, a random serial, patient initials and age, and the medicines as written. It is 172 characters, a version-7 QR code, printed about 3 cm wide.
4. Every check looks for the code automatically. If it verifies, the Regn No and date come from the code, and the counter app pre-fills the medicines.

| What the forger does | What happens |
|---|---|
| Makes a new slip and invents a code | The signature fails → **RED** for high-risk medicine, AMBER for routine |
| Copies a genuine code onto another slip | Same serial seen again → repeat (**AMBER** for high-risk) |
| Adds a medicine or raises a strength | Not in the signed list → **AMBER** |
| Changes the Regn No or date | Differs from the signed values → **AMBER** |
| Removes the code | If the registry records that the clinic signs **every** slip (`signs_all: true`, only with a key collected in person): no code → **AMBER**, call the clinic. Otherwise the slip is judged like any unsigned slip |

What it does **not** cover: a copied code presented at *another* pharmacy (that needs a shared serial register); a stolen clinic key; and clinics that don't take part. Payment or website QR codes on a slip are ignored.

In tests the printed code was read in 96 of 96 cases: 12 random codes × printed, WhatsApp, forwarded 5×, dark photo, photocopy, angled photo, dim light and phone screen. One finding: OpenCV's own QR *encoder* writes unreadable codes above version 7, though its decoder is fine. So the clinic side uses the `qrcode` package, and the payload is Base45-packed (as on EU vaccination certificates) to stay small.

### Provenance metadata: supporting evidence only

An AI-generator marker in the file, or a file dated before the prescription date, asks for **confirmation (AMBER)**. It never produces RED. Markers are stripped by an ordinary re-save. They also appear after innocent edits in AI-assisted gallery apps, camera clocks are often wrong, and none of it says who authorised the slip. A generic "AI-generated image" detector is deliberately **not** used: such detectors are brittle under screenshots and recompression, and they can't establish issuance either.

### Verification states

| State | Meaning | Counter action |
|---|---|---|
| 🔴 **RED: do not dispense until confirmed** | A hard fact contradicts the slip: an edited copy of a recorded slip; a Regn No missing from a recent complete register, or a verified entry whose doctor has a different name (high-risk drug); a clinic signature that fails (high-risk drug) | Hold. Call the clinic on the registry number, not the number on the slip. RED does **not** mean "this is fraud": stale data and genuine mistakes also cause it, so nobody is accused |
| 🟠 **AMBER: needs confirmation** | Authenticity can't be established for the risk involved | Confirm with the clinic, or ask for the original slip or OP card |
| 🟢 **GREEN: no issues found** | No check failed. **Not "verified"** unless a clinic-signed code verified | Dispense with normal checks |

| Finding | Routine medicine | H1 / X / abuse-prone |
|---|---|---|
| Copy of a recorded slip **with edits** | RED | RED |
| Clinic-signed issuance code fails verification | AMBER | RED |
| Regn No invalid (not in a recent complete registry, or a verified entry names a different doctor) | AMBER | RED |
| AI/provenance marker, or file dated before the Rx (supporting evidence only) | AMBER | AMBER |
| **No clinic code**, but the prescriber's clinic signs every slip (`signs_all`) | AMBER | AMBER |
| Image unusable for automated comparison | AMBER | AMBER |
| **Similar** to a recorded slip (possible rewrite) | AMBER | AMBER |
| Requested drug/strength differs from the matched record | AMBER | AMBER |
| Same slip / seen before (repeat) | GREEN (note) | AMBER |
| Seen at **another pharmacy** (shared index) | GREEN (note) | AMBER |
| Shared index **unreachable**, slip new here | GREEN | AMBER, whatever the policy |
| Prescriber unknown or unverified (no Regn No, not in a partial registry, unchecked entry, registry unusable) | GREEN | AMBER |
| New slip, valid prescriber | GREEN | **AMBER** if `confirm_new_high_risk` (default), else GREEN |
| New slip, valid prescriber, **clinic-signed code verified** | GREEN | GREEN |

"Matched record" includes the medicines in a verified signed code. The `confirm_new_high_risk` row is a policy switch in `data/policy.json`: a valid registration number proves the doctor exists, not that they wrote this slip.

**What counts as "invalid" is kept narrow on purpose.** Registry data goes stale: doctors move clinics, re-register in another state, or write their name differently. So only hard facts make a Regn No invalid:

- **The number isn't in a complete register extract dated within the last 90 days.** An older or undated extract may be missing a doctor registered since, so the prescriber is "unverified".
- **A verified entry belongs to a doctor whose name shares no word with the name on the slip.** Initials, word order and small spelling differences count as the same name (for example, "Dr. A. Example" and "Example A."). A different name is "unverified" until the council register settles it.

Anything softer is "unverified" (AMBER for high-risk medicine, so it still waits for confirmation). A clinic-signed code that fails is RED for high-risk medicine, because the check is cryptographic. The message names the innocent explanation too: the clinic may have changed its key.

**This table is the policy.** `tests/test_docs.py` checks every row against `decide()` under both policy settings, and fails if another document carries its own decision table.

### After a warning: retake, counter check or call

The check takes a second or two. At 30 images a day that is about a minute of computing. **The bottleneck is what happens after a warning.** If every warning means "phone the clinic", the doctor doesn't answer, the patient is in a hurry and three customers are waiting, staff learn to wave warnings through. A technically accurate detector then fails in practice. So the tool does three things:

**1. Each warning names the quickest step that actually settles it.** Most warnings don't need the clinic:

| Warning | Routine medicine | High-risk medicine |
|---|---|---|
| Image unusable for automated comparison | retake | retake |
| Very similar to a recorded slip (possible rewrite) | counter | call |
| Medicine or strength differs from the recorded or signed prescription | counter | counter |
| Regn No or date entered differs from the record | counter | counter |
| Same slip presented again (repeat) | - | counter |
| Seen at another pharmacy (shared index) | - | call |
| Shared index unreachable, slip new here | - | call |
| Prescriber unknown or unverified | - | call |
| New slip, valid prescriber (`confirm_new_high_risk`) | - | call |
| AI/provenance marker, or file dated before the Rx | call | call |
| No clinic code, but the clinic signs every slip | call | call |
| Edited copy, invalid Regn No, or failed clinic signature (RED) | call | call |

- **retake** (seconds): photograph the paper slip directly and check again. For WhatsApp arrivals the counter screen asks for this while the customer is still queuing. No paper slip means the prescription stays unchecked. A clean image of a synthetic edited sample is compared like any other; an unusable image goes to manual review.
- **counter** (under a minute): something the pharmacist can see without the clinic. That means the paper original (a doctor's rewrite has a new date and real ink), the ledger and H1 register (was this slip already dispensed, does it allow repeats?), or simply **selling only what the prescription says**. That settles a raised strength or an added medicine without anyone having to accuse anyone.
- **call**: only the clinic can settle it. "No answer" parks the hold for a call-back (`waiting`); it is never auto-approved.
- "-" means no warning. When several apply, a call outranks a counter check, and a retake comes first because nothing else can be judged from an unreadable image.

**2. The sale is split, medicine by medicine.** A problem with the *document* (an edited copy, a wrong Regn No, an unreadable image) holds everything. A problem with *one medicine* (a high-risk drug on a new slip, a strength that differs from the record) holds only that medicine. The customer takes the paracetamol home while the alprazolam waits for the call-back.

**3. Overrides are deliberate and visible.** "Checked - dispense" must say what was checked: paper original seen, sold as prescribed, repeat allowed, or entry corrected. Only the checks that settle *that* warning are offered (for a repeat: "repeat allowed"); citing any other counts as an override. Dispensing without the step that was asked for is still possible (except for NDPS / Schedule X medicines), because a pharmacist sometimes knows better. But it is folded away, labelled as an override, needs a written reason, and is recorded under the pharmacist's name. The counter home screen shows today's checks, warnings settled at the counter, calls and overrides. If more than 20% of held prescriptions over 14 days were dispensed without the requested step, it says so: either the warnings don't fit this counter, or they are being waved through. Both need fixing, and neither should stay invisible. The same alert is raised **per person**: a pharmacist who overrides far more than the counter as a whole is the pattern a colluding chemist leaves. Alerts naming a person are shown to pharmacists only. `rxcheck stats --days 14` shows the same figures, broken down by person. **NDPS / Schedule X medicines can't be overridden at all** (see below).

Overrides are the abuse point, and a pharmacy can't be relied on to police itself. Rate alerts **across pharmacies** need someone outside the pharmacy to receive them: a chain's head office, a licensing authority or a shared service. Only counts (checks, holds, overrides per person) would be sent, never prescriptions. That reporting channel is not built.

This table is checked against `next_step()` by `tests/test_docs.py`, and `tests/test_robustness.py` checks that all 6,089 warning combinations of flags, risk, prescriber, issuance and policy name an explicit step.

### Hold and escalate

Every RED or AMBER opens a hold. `rxcheck holds` (or **Holds** in the counter app) lists them oldest first. Recording an outcome (confirmed, refused, unreachable, dispensed after review, or retaken) closes or parks the hold. **"Unreachable" keeps the hold waiting**: nothing is auto-approved when the clinic can't be reached, and a closed hold can't be re-opened or re-resolved. In the counter app only a pharmacist can record "confirmed", "checked - dispense" or an override, and a high-risk slip can only be added to the ledger once its hold was resolved that way.

### When the prescriber can't be reached: by drug class

Confirmation (a call, or a clinic-signed code) can fail because the doctor doesn't answer. What happens next depends on what is being sold. The drug-risk list sets the class (`data/drug_risk.json`, tiers `ndps`, `high`, `routine`):

| Drug class | Example (sample list) | If the clinic can't confirm | Override |
|---|---|---|---|
| **NDPS Act / Schedule X** (tier `ndps`) | alprazolam, tramadol, codeine | **Fails closed.** The hold waits for the call-back. If none comes, the item is not sold and the customer can return | **None.** Only "clinic confirmed", or a counter check that settles the warning (e.g. a repeat the register allows), closes it |
| **Schedule H1 / abuse-prone** (tier `high`) | pregabalin, zolpidem | **Call-back.** The hold waits (`waiting`), and other items are handed over | Pharmacist only, with a reason. Kept permanently and counted per person |
| **Schedule H and other routine medicines** (tier `routine`) | antihypertensives, paracetamol | Pharmacist judgement | With a reason, logged and counted |

If the drug-risk list itself is unusable, no medicine can be ruled out of the NDPS class, so every held item fails closed until the list is fixed (`rxcheck data status`). The tier assignments in the shipped sample list are illustrative, and must be checked against the current NDPS Act schedules and CDSCO lists before use.

### Offline operation

All checks run on the pharmacy's own PC. Only the confirmation call needs a phone. That also means the ledger and index see **only this pharmacy's** slips: a slip already dispensed at another pharmacy looks new here. If a shared, cross-pharmacy index is added later, a counter that can't reach it would be blind in the same way, and a fraudster could seek out such counters. The policy is the same either way, so being offline is never a bypass:

- **Not knowing what other pharmacies saw never makes a check more lenient.** A slip that is "new" only because the shared index can't be reached is treated as new: high-risk medicines wait for confirmation (`confirm_new_high_risk`), NDPS items fail closed, and routine sales continue.
- When the shared index comes back, slips checked while offline are checked against it. A match found then is reported, not silently dropped.

Today there is no shared index, so the first rule is how the tool already works. The second is a requirement for the shared index when it is built.

## The counter app

`python app/rxcheck.py serve` starts a browser app built around one screen per customer. The server is standard library only, and every page is complete HTML that works without JavaScript. A small Vue bundle served from the same PC adds speed and accessibility on top (see [Counter ergonomics](#counter-ergonomics-and-accessibility)):

1. **Photo in.** Upload a photo or a PDF, or open one that arrived on the pharmacy's WhatsApp (the `--inbox` folder, pre-checked by `watch`). The app runs an image pre-check and decodes any clinic-signed code. A PDF (up to 6 pages) is turned into page images: the image checks (seen before, edited copy) run on **page 1 only**, and the form and decision say so; every page is read for the medicines.
2. **What is being sold.** If OCR is installed, the photo is read in the background while this form is open, and what it finds fills the fields that are still empty (see *Reading the slip*). Staff enter the medicines and strengths, the Regn No, doctor and (optionally) the patient's name, with the Regn No, date and patient prefilled from a verified code. Before the check runs, each entry that is easy to misread must be **corrected or ticked as confirmed against the slip**: a name close to a known brand ("Alprx", did you mean Alprax, HIGH-RISK?), a strength starting with "." (.25 vs 25), a trailing zero (5.0 vs 50), or a high-risk medicine with no strength.
3. **The decision.** One word (**HOLD** / **CONFIRM FIRST** / **NO CONFLICT FOUND**; GREEN means nothing contradicts the slip, not that it is genuine) and the one sentence that caused it. Then what can be handed over now and what is held, and the one step that settles the rest (retake / check at the counter / call the clinic, with the registry number). The outcome buttons for that step come next, then the evidence: close-ups of changed areas next to the recorded slip, or the slip itself. Every individual finding and note is folded under "All checks".

### Reading the slip (OCR): a typing aid, not a check

Typing is the main friction for slips without a signed code, so the counter can read the photo (`app/ocr.py`, RapidOCR on onnxruntime). It runs **on the counter PC, offline**: no image or text leaves the pharmacy. It takes 5-15 s on a typical PC, so it runs in the background: the form opens at once and fills in when the reading is done. Nothing staff have typed is replaced, and a clinic-signed code always wins.

What it does with each reading:

| Reading | Filled in? | Tick needed when sent unchanged? |
|---|---|---|
| Medicine whose whole name is on file (drug list, or entered at this counter before), read with confidence 0.80 or more | Yes | If high-risk, without a strength, read below 0.95, or **at a strength not dispensed here before for that medicine** (a confident reading can still drop a digit: 500 → 50). Strengths from a PDF's text layer are exact and are exempt |
| Medicine name not on file, read with 0.95 or more | Yes | **Always** ("not a name this pharmacy has on file") |
| Medicine name not on file below 0.95, or anything below 0.80 | **No.** Shown with the cut-out of the slip it came from, for staff to type | - |
| Regn No | Yes | **Always**: a misread digit can name another doctor, which is RED |
| Doctor's name, date (day first, as written in India) | Yes, unless the slip shows two different ones | If read below 0.95 |

Three rules come from how OCR fails:

- **A reading is never swapped for the nearest known name.** "Metfornin" is shown as read, not offered as Metformin or Metoprolol: look-alike names one tap apart are a known cause of dispensing errors.
- **Confidence alone is weak evidence.** On the synthetic demo handwriting the engine reported 0.9 for nonsense, so a name must be on file, or read very clearly *and* ticked.
- **Whole names only.** "Pan D" is not the known "Pan".
- **Whole numbers only.** OCR drops spaces ("Telma40mgafterfood", "TabZolfresh"). The strength is always the whole number, and directions after it (1-0-1, x 5 days, after food, TDS) are removed before the name and strength are read, so "Becosules 1-1-1" is not "Becosules 1". The labelled set found both bugs: the first version read "Telma 40" as "Telma 4".

Each item that needs a tick appears in the existing "check these against the slip" box, next to the cut-out of the slip it was read from. The audit trail records which photo readings were ticked, and for every check an `ocr-use` line with counts only: values offered, kept as read, corrected, and left for staff to type. The 0.80 / 0.95 thresholds are **placeholders**: they are not measured on real slips, and the shadow pilot should set them from those counts.

Printed slips read well (test: name, strength, Regn No, doctor and date all correct). **Handwriting mostly does not**: expect handwritten medicine lines to be shown as unclear rather than filled in. Install (optional; the counter works without it):

```bash
pip install -r requirements-ocr.txt
pip install --no-deps rapidocr_onnxruntime==1.4.4   # without its deps: it asks for a second, non-headless OpenCV
python app/ocr.py slip.jpg                          # what it reads from one photo, and what it would fill in
```

`RXCHECK_OCR=off` switches it off. It is not installed in the container image, so the public demo does not read photos (its sample slips are filled in for the visitor).

**PDFs.** A page with a text layer (printed from clinic software) is read from that text, so it needs no OCR engine and its readings are reported in their own band, "PDF text". A scanned page is read by OCR like a photo. Locked, unreadable, empty or over-long PDFs (more than 6 pages) are refused with a message staff can act on.

**What the form shows.** One state at the top of the form, always, so staff know what the fields hold:

| State | When | What staff do |
|---|---|---|
| **Reading** | The slip is being read (5-15 s a page) | Start typing; nothing typed is replaced |
| **Ready** | Read; nothing filled in needs a tick | Check the values against the slip, then *Get decision* |
| **Needs confirmation (n)** | Read; n filled-in values will need a tick if sent unchanged | Check them; the ticks are asked on *Get decision* |
| **Check blocked** | Sent, but some entries still need a tick (from the reading or the entry checks) | Tick each against its cut-out, or correct it |
| **OCR disabled** | Switched off, not installed, or a demo sample - and the panel says which | Type the medicines |
| **Could not read** | The engine failed on this slip | Type the medicines |

**Pilot figures.** *Audit log → Photo reading report* (pharmacists) shows, for the last 7-90 days, how many checks had the slip read, how many were sent while still reading or could not be read, the median reading time, and a table per field and confidence band: values filled in, **kept as read (acceptance)**, **changed by staff (correction)**, ticks needed, and unclear readings that staff then typed exactly as read. That table is what the 0.80 / 0.95 thresholds should be set from; rows with fewer than 30 readings are marked as too few. *Download one row per reading as CSV* exports the same, without any slip text; the export is recorded in the audit trail.

**Measured on the labelled set.** `python eval/ocr/build_ocr_set.py` generates 50 synthetic slips into `samples/ocr/` (git-ignored): 30 printed, 5 in poor light (dim, side shadow, glare, angled, forwarded), 10 *handwriting-style fonts* and 5 multi-page PDFs (3 with a text layer, 2 scanned). `python eval/ocr/score.py` reads each once and scores it for a new counter (only the drug list on file) and an established one (every medicine entered before). Its key figure is **silent errors**: values filled in wrong that need no tick.

Results on this PC (2026-10-24, synthetic slips, 180 medicines):

| Kind | Slips | Medicines filled in right: new counter / established | Wrong, needing a tick | **Silent errors** | Regn No, doctor, date right | Ticks per slip (established) | Seconds per slip |
|---|---|---|---|---|---|---|---|
| Printed | 30 | 84% / 98% | 2 | **0** | 93% | 2.6 | 6.7 |
| Poor lighting | 5 | 57% / 86% | 2 | **0** | 87% | 3.4 | 7.8 |
| Handwriting-style font | 10 | 54% / 81% | 2 | **0** | 93% | 2.5 | 9.1 |
| PDF with text layer | 3 | 100% / 100% | 0 | **0** | 100% | 3.0 | 0.3 |
| Scanned PDF | 2 | 69% / 88% | 2 | **0** | 50% | 5.0 | 19.3 |
| All | 50 | 78% / 94% | 8 | **0** | 91% | 2.8 | 7.4 |

Every medicine ended up filled in right, shown as unclear, filled in wrong *with a tick asked*, or missed (11 of 180, left for staff to type). The first scoring run found **10 silent errors** on an established counter (strengths cut short: "Telma 40" read as "Telma 4", "Dolo 650" as "Dolo 65"; doses read as strengths); the parser fixes and the strength-history rule above brought that to zero on this set. A real counter will see misreads this set does not contain, which is why the tick rules do not rely on the parser being right.

The handwriting-style fonts are far tidier than a doctor's pen, so their row is **not** a handwriting score. Real slips from the consented field study go in `eval/field/ocr/` (git-ignored) with a `labels.json` shaped like `eval/ocr/labels-template.json`; the scorer then reports them as `real-...` rows.

**Browser test.** `tests/test_browser.py` drives the installed Edge or Chrome (Playwright, no browser download): sign in, choose a photo, see *Reading*, type a field, see the others fill in without it, the look-alike shown with its loaded cut-out, *Check blocked* with three ticks, the browser refusing to send without them, the decision after them; then a PDF, and *OCR disabled*. It is skipped when Playwright (`pip install playwright==1.64.1`) or a browser is missing.

Each person signs in with their own PIN (`rxcheck staff add NAME --role pharmacist|staff`), so the audit trail records who checked and who approved. Sessions end after 30 idle minutes or one 10-hour shift. Five wrong PINs lock that name for 10 minutes. Disabling a staff member signs them out at once. Every form carries an anti-forgery token, and the pages forbid framing and external content.

### Counter ergonomics and accessibility

- **Keyboard shortcuts.** `Ctrl+U` (`⌘U` on a Mac) focuses the photo input and opens the file picker; on a finished case it goes straight to the next customer. `Ctrl+Enter` sends the page's main form (*Check*, *Get decision*, *Check the new photo*) with the browser's own validation, so a required field or an unticked "confirm this entry" box still stops it. `?` lists the shortcuts. Recording a hold outcome (confirmed, refused, override) **never** has a shortcut. A form can only be sent once until the page changes, so a double press does not check a slip twice.
- **Text size and contrast.** The header has text size (100–150%, the whole page scales) and a high-contrast switch. The choice is kept per counter PC in a cookie and rendered by the server, so the first paint is already right. Until someone chooses, the app follows the operating system's "increase contrast" setting. All text meets **WCAG AAA (7:1)** in all four themes (standard and high contrast, light and dark), and focus rings and field borders meet 3:1. `tests/test_counter_ux.py` measures every pair from the stylesheet itself.
- **Lookups.** The medicine, Regn No, doctor and patient fields suggest from this pharmacy's own data: the drug list (high-risk marked in words), medicines entered here before, the prescriber registry (verified or not), prescribers on earlier slips, and patient names typed here before. They follow the ARIA combobox pattern (arrows, Enter, Esc, screen-reader announcements). Two safeguards are built in. A suggestion fills **only its own field**: choosing a doctor never fills in the Regn No, nor the other way round, because the prescriber check compares the two *as written on the slip*. And medicine suggestions are **names only**: the strength is always typed from the slip.
- **Who did what.** The counter and holds screens show the last check and the last override (who, when, and the override's note). Each decision shows who checked it, and each hold shows its history: who opened it and who recorded each outcome.
- **Audit log** (pharmacists, **Audit log** in the menu). Filter by person, kind of event (checks, holds, *overrides only*, sign-ins, ledger, staff/data/backups), date range and free text; see a readable line plus the raw detail for every entry; and export exactly what is filtered as CSV. Each export is itself recorded in the trail. CSV cells that a spreadsheet would run as a formula (`=`, `+`, `-`, `@`) are neutralised. Without JavaScript the page still lists the latest entries and offers the full CSV.

Patient names are kept only for the lookup: the name, when it was first and last used, and the number of visit days, with nothing about what was dispensed. A name is dropped after 365 days without a visit (`PATIENT_RETENTION_DAYS`). As with the ledger, the retention period and consent must be confirmed with a pharmacy compliance professional.

The bundle (`static/app.js`, `static/app.css`) is built from `web/` and committed, so counter PCs and the container never need Node or a CDN. The page's Content-Security-Policy allows scripts and fetches from the same server only, and nothing inline or `eval`ed. To change it: `cd web && npm install && npm run build`.

Uploaded images of finished cases are deleted after 24 hours. Images behind an open hold are kept until it's resolved, and images of dispensed high-risk slips move to the ledger.

### Several billing counters

Run the app on one PC, the server, with `--host 0.0.0.0 --cert server.pem --key server-key.pem`. Every other counter opens it in a browser and stores nothing locally. There is one database and one audit trail, and no two PCs can disagree. Without TLS the app warns at start-up that PINs would cross the network unencrypted.

Tested with four signed-in counters checking at the same time: every check was decided and attributed to the person at that counter, and the ledger verified healthy afterwards. Database connections are per request thread. SQLite's WAL mode with a 30-second busy timeout serialises writes.

### Public demo deployment (Render or any container host)

The counter app is designed to run inside a pharmacy's own network. A **public** deployment is a demonstration only. Every page carries a banner saying not to upload real prescriptions or patient data, and demo mode is on by default in the image.

**No sign-in for visitors.** In demo mode (`RXCHECK_DEMO=1`) anyone who opens the link lands on the counter, signed in as **Demo visitor** (a pharmacist account whose PIN is random and never shown, so nobody can sign in as it). The counter page offers three one-click **sample slips** from `demo/`: an edited copy (RED), the same slip re-sent on WhatsApp (GREEN) and a new slip with a high-risk medicine (AMBER). On start the app records `demo/reference.jpg` as an already-dispensed slip, so those verdicts come out as labelled. The samples are synthetic (`tests/make_demo_samples.py`); `tests/test_deploy.py` regenerates them and fails if any image in `demo/` differs, so a real photo cannot ship as a sample. Outside demo mode nothing changes: every user signs in with a PIN.

| File | What it does |
|---|---|
| `Dockerfile` | `python:3.12-slim` with the pinned `requirements.txt`. Runs `serve --host 0.0.0.0 --behind-proxy` on `$PORT`. All state (ledger, holds, audit, uploads, data files, backups) lives in `RXCHECK_HOME=/var/rxcheck`, which must be a persistent disk |
| `docker-entrypoint.sh` | Hands the mounted disk to an unprivileged user (uid 10001), then drops root |
| `.dockerignore` | **Allowlist**: only `app/rxcheck.py`, `app/counter_app.py`, the built `static/app.js` and `static/app.css`, the start script, `requirements.txt`, `data/*.json` and the synthetic demo slips in `demo/` can enter the image |
| `render.yaml` | Render Blueprint: Docker web service in Singapore, 1 GB disk at `/var/rxcheck`, health check `/healthz`, demo mode on |

**Deploy on Render:** push the repository, then in the Render dashboard choose *New → Blueprint* and select the repository. Render asks for `RXCHECK_ADMIN_NAME` and `RXCHECK_ADMIN_PIN` (6+ digits). Both are **optional** for the public demo, which needs no account; set them only if you want your own pharmacist sign-in as well. They create that pharmacist on first start, once, and are stored by Render, not in the repository. A persistent disk needs a paid instance type; without a disk every restart wipes the demo. Other hosts (Fly.io, Railway, a VPS behind Caddy or nginx) work with the same image: mount a disk at `/var/rxcheck`, optionally set those two variables, and terminate HTTPS in front of it.

Behind the host's HTTPS proxy (`--behind-proxy`), session cookies are `Secure`, and the client address comes from the last `X-Forwarded-For` entry, which the proxy wrote; earlier entries can be forged by the client. Wrong-PIN lockout is per staff name, not per address, so guessing from rotating addresses is still stopped after 5 tries. `/healthz` answers without sign-in and reveals nothing.

The deploy checks verify the image allowlist, the start script, the empty-disk setup, sign-in and a full check over HTTP. A deployment still needs a Docker build and runtime validation on its host.

Limits of a public demo: scheduled backups go to the same disk, so copy them off-site if the data matters. Visitors share one demo ledger and audit trail, so they see each other's checks; uploads are deleted after 24 hours. There is no rate limit on uploads. And this is a research prototype: it is not for dispensing decisions.

## Storage, audit, backup and recovery

- **One SQLite file** (`ledger/rxcheck.db`): every write is a transaction, so an entry and its audit record are written together or not at all. `synchronous=FULL` protects against power loss.
- **Append-only, hash-chained audit trail**: index entries, checks (with the verdict, reason and data-file versions), holds, resolutions, sign-ins, failed sign-ins, staff changes, data imports, backups, restores and audit exports. Database triggers reject edits and deletions. View it with `rxcheck log`, or filter and export it under **Audit log** in the counter app. On the CLI, `RXCHECK_USER` records a *claimed* name; only the counter app authenticates.
- **Tamper evidence.** Each audit record carries a SHA-256 hash of itself and the previous record's hash. Changing, removing or inserting a record breaks every hash after it, and `verify` reports where. Someone with full access to the file could drop the triggers and recompute the whole chain. Copies kept elsewhere catch that: every backup checks that the live trail still contains the previous backup's trail unchanged, and `rxcheck verify --against <backup>` checks against any copy, such as one kept off-site. `verify` prints the current chain head (record number and hash) so it can also be written down. An existing ledger is chained once, on the first start after the upgrade (logged as `chain-start`), and never again: an emptied chain is reported, not rebuilt.
- **What the trail holds.** Decisions, reasons, image hashes and file names, staff names, and the medicines on index entries: never images and never patient names. The free-text notes on holds are the exception risk, so the counter app asks staff not to type patient names or health details there. The chain covers a hash of each record's detail, not the detail itself. A lawful-erasure procedure that blanks a detail and records the erasure, without breaking the chain, is possible but **not built**. Retention and erasure obligations under the DPDP Act 2023 must be confirmed with a compliance professional.
- **`rxcheck backup <folder> [--keep N]`** takes a consistent online snapshot of the database, images and features, then verifies the copy. With `serve --backup-dir` this runs every 4 hours, and the counter screen shows the last verified backup or a warning.
- **`rxcheck verify [folder] [--against backup]`** checks the live ledger or a backup. It covers database integrity, audit protection triggers present, no gaps in the audit sequence, an intact hash chain (and, with `--against`, one that still contains that backup's trail), every entry has its audit record and feature file, and every ledger image is unchanged since it was recorded.
- **`rxcheck restore <backup>`** verifies the backup first and refuses a damaged one (unless `--force`). It moves the current files aside (never deletes them), restores, re-verifies, and logs the restore.
- Tested in `tests/test_storage.py`: two concurrent writers; a writer **killed mid-write** (every entry still had its audit record); audit tamper attempts; rollback of a failed write; detection of a changed or missing image and of removed audit protection; a **damaged database file restored from backup**; refusal of a damaged backup; JSON migration.

## Data files: registry, drug-risk list, policy

These files decide verdicts, so a problem with them must never crash a check or quietly relax it:

| Problem | What a check does |
|---|---|
| Drug-risk list missing, corrupt or empty | **Every medicine is treated as high-risk**, and the reason says so |
| Unknown tier in the drug list (valid: `ndps`, `high`, `routine`) | That molecule is treated as high-risk; reported |
| Medicine not in the drug list | Routine, and reported, **unless the name looks high-risk**: spelt like a listed high-risk brand or molecule, or carrying a benzodiazepine / opioid / gabapentinoid / z-drug stem (`-azep`, `-zolam`, `-tadol`, `-gabalin` ...). Then it is held like a high-risk item until confirmed (an override is allowed: it is not NDPS-tier) |
| Registry missing or corrupt | Prescriber "unverified" (AMBER for high-risk), **never "invalid"** |
| Regn No not in a registry that lists only some doctors | "Unverified: look it up on the council register", not "invalid" |
| Malformed registry entry, or a phone or clinic key without a recorded source | Entry treated as unverified; the phone or key is not used |
| Policy file broken or mistyped | Cautious defaults |

A registry entry counts as verified only with an authoritative `source` and a `verified_on` date within a year. A Regn No that isn't listed counts as **invalid** only when the registry is declared `"_coverage": "complete"` (a full official extract) **and** its `_updated_on` date is within 90 days. **Regn Nos written differently** are compared as one number: case, spaces and separators are ignored (`tn 73816` = `TN/73816`). The same digits with a missing council prefix or zero padding (`73816` vs `TN/073816`) is never a contradiction: against the registry it is "unverified, check the prefix on the council register", never "invalid", and against a recorded or signed slip it is a note, not an edit. Different digits, or two different council prefixes, still count. Every report and audit record carries the hash of each data file it was decided with. `rxcheck data status` shows the state of each file.

**Where registry data must come from.** This is the main feasibility question, because prescriber checks are only as good as their source. The authoritative sources are:

- the **ABDM Healthcare Professionals Registry (HPR)**,
- the **National Medical Commission's** national register of doctors,
- and the **state medical council** registers.

Each holds who is registered, under which number, and whether the registration is current. Automated or complete access means an **official integration**, such as onboarding as an ABDM integrator or a data-sharing arrangement with a council, with the terms, rate limits and data-protection conditions that come with it. Scraping a council website is not a credible dependency: it can break or be blocked at any time, and its legal standing is unclear. Until an integration exists, the registry is built from lookups a named person made by hand (`import-registry --source ... --verified-on ...`) and stays `"partial"`, so an unlisted number is "look it up", never "invalid".

**Updates** go through audited, all-or-nothing imports that keep the previous version in `data/history/`:

```bash
# rows a person checked against the medical council register (any bad row rejects the whole file)
python app/rxcheck.py data import-registry checked.csv --source "official register, checked by <name>" --verified-on 2025-01-15
# the drug-risk list, reviewed against the current official schedules (columns: molecule, tier ndps|high|routine, brands, schedule)
python app/rxcheck.py data import-drugs drugs.csv --source "<notification / list and date>" --verified-by "<pharmacist>"
```

The shipped registry contains no prescribers, and the drug-risk list is an illustrative sample (`_verified: false`). Neither is production data. Building the real ones means identifying authoritative sources (ABDM HPR, NMC and state medical council registers, through official integration; current CDSCO Schedule H1/X, NDPS and Tamil Nadu notifications), deciding who checks them and how often, and getting clinical and legal review. The tooling above makes that process safe; it can't supply the data.

## Usage

Requires Python 3 with `opencv-python`, `numpy` and `Pillow`. Issuance codes also need `cryptography` (to verify) and `qrcode` (clinic side, to print).

```bash
# accounts for the counter app (first one must be a pharmacist), then start it
python app/rxcheck.py staff add PharmacistA --role pharmacist
python app/rxcheck.py serve --inbox inbox/ --backup-dir E:/rxcheck-backups          # this PC only
python app/rxcheck.py serve --host 0.0.0.0 --cert server.pem --key server-key.pem --inbox inbox/ --backup-dir E:/rxcheck-backups

# pre-check images as they arrive (e.g. the pharmacy's WhatsApp Desktop download folder)
python app/rxcheck.py watch inbox/

# command-line check and ledger entry (same engine as the app)
python app/rxcheck.py check IMAGE --drugs "Alprax 0.25, Dolo 650" --regn REGN-EXAMPLE --doctor "Dr. Example" --rx-date 2025-01-15
python app/rxcheck.py add IMAGE --id RX-EXAMPLE-001 --dispensed 2026-10-01 --regn REGN-EXAMPLE --doctor "Dr. Example" --drugs "Alprax 0.25"

# clinic side
python app/rxcheck.py clinic keygen clinic.pem
python app/rxcheck.py clinic issue clinic.pem --regn REGN-EXAMPLE --date 2025-01-15 --drugs "Alprax 0.25, Dolo 650" --patient "Example Patient" --out qr.png

# holds, workload, audit, data, recovery
python app/rxcheck.py holds
python app/rxcheck.py resolve H-EXAMPLE-001 --outcome unreachable --note "line busy"
python app/rxcheck.py resolve H-EXAMPLE-001 --outcome dispensed-after-review --reason repeat-allowed
python app/rxcheck.py stats --days 14          # warnings by step, time to resolve, overrides
python app/rxcheck.py log -n 20
python app/rxcheck.py data status
python app/rxcheck.py backup D:/rxcheck-backups --keep 42
python app/rxcheck.py verify [--against D:/offsite/rxcheck-backup-example]   # prints the audit chain head
python app/rxcheck.py restore D:/rxcheck-backups/rxcheck-backup-example

# tests and evaluation
cd tests && for t in test_robustness test_issuance test_data test_storage test_server test_counter_ux test_eval test_shared test_doctorping test_doctor_screens test_docs test_deploy; do python $t.py || break; done
python eval/build_set.py && python eval/evaluate.py [--no-confirm-new-high-risk] [--complete-registry]
```

## Threat scenarios: status

| Scenario | What the prototype does | Evidence |
|---|---|---|
| **A. Doctor genuinely rewrites a slip** | Three-way identity; "similar" leads to AMBER, never RED | Simulated rewrites (strokes 0.5–1.2 mm off) all "similar", none "edited copy". **Not yet tested on real rewritten slips** |
| **B. WhatsApp, screenshots, phone screens** | Lighting-invariant ink, contrast-equalised features, sub-pixel alignment; missing metadata is neutral | Forwarded 5×, dark photo + 4 forwards, gallery screenshot, phone-screen photo with moiré and glare: all "same slip", no false edits (simulated) |
| **C. OP chits, local clinics, Tamil** | Handwriting band detected per document (plain paper = whole page); unknown prescriber leads to AMBER only for high-risk | Re-dated OP chit caught; added Tamil line caught (simulated). **No real OP chits or Tamil slips yet** |
| **D. Drug / dose changed** | Edits boxed if the original is in the image ledger; requested drugs compared with the matched record or signed code (brand→molecule aware); ambiguous entries must be confirmed | Added drug lines (RED); raised strength or added drug against a recorded slip or signed code (AMBER) |
| **E. Never-seen forgery, valid doctor, high-risk** | Policy: new high-risk slips need issuance confirmation, by call or by a verified clinic-signed code | Caught as AMBER with the policy on; missed with it off. A forged code is RED (tested) |
| **F. Offline, busy queue, several counters** | Pre-check while queuing; each warning names its quickest step (retake / counter / call); the sale is split so routine items go home; overrides counted; shared server for several PCs; "unreachable" parks a hold | Four simultaneous counters tested; **no field test** |
| **Unreadable image** (glare, heavy blur, thumbnail, dark) | Quality gate leads to manual review (AMBER), never "new slip" | 6 unusable and 8 degraded-but-usable cases classified correctly (simulated) |
| **Fabricated slip, real doctor, routine medicine** | GREEN if the prescriber is verified and the slip is new, unless the clinic signs its slips, in which case a forged or copied code is caught. **Accepted residual risk** for clinics that don't sign | Documented; mitigated only where clinics take part |

## Evaluation

`eval/build_set.py` creates a reproducible dataset using synthetic test fixtures. `eval/evaluate.py` reports behavior on that generated dataset, but its results are not evidence of field accuracy. In a synthetic-only checkout, visual regression and server tests explicitly skip assertions that require local sample images. This repository contains no real or derived prescription images or evaluation records. A field validation requires independently consented data collected under the protocol in [docs/field-validation.md](docs/field-validation.md); that data must remain private and is not included here.

## Readiness: would it work at a counter today?

**No, not as an operational product.** It's a research prototype with a tested engine and a working counter workflow. Status against what a real counter needs:

| Requirement | Current state | Assessment |
|---|---|---|
| Detect edited copies of recorded slips | Stroke alignment + edit localisation | Exercised with synthetic tests; real-world performance is not established |
| Catch new fabrications | Policy (call for new high-risk slips) + clinic-signed issuance codes | Codes work in tests; **need participating clinics**. Without them, routine fabrications remain a residual risk |
| Genuine WhatsApp / screen / photocopy images not flagged | Synthetic tests pass; quality gate for unreadable images | Needs real images from many phones and apps |
| Valid doctor registration | Registry with coverage, sources, audited import | **Data is a stub.** Tooling is ready; authoritative sources and an owner are not |
| Risk classification | Validated drug list, fail-safe to high-risk, audited import | **Sample list.** Needs clinical and legal review |
| Medicine names and strengths | Typed; ambiguous entries must be confirmed; prefilled from signed codes, or from the photo (optional OCR, ticked where unsure) | OCR reads printed slips in tests; **handwriting mostly not read**, and thresholds are unmeasured until the pilot |
| Representative accuracy | Origin-labelled evaluation, held-out lock, leakage check, protocol | **Not done.** No real held-out data yet (see the protocol) |
| One-glance counter experience | Counter app: one decision, reason, what to hand over, the one step that settles the rest, evidence | Built and tested; **no staff usability test** |
| Warnings that staff can act on in a queue | Quickest valid step per warning; split sale; call-backs parked; overrides need a reason and are counted | Simulated only. **Real call load and override rate unknown until the shadow pilot** |
| Several staff / terminals | Shared server, PIN sign-in, roles, concurrency tests, verified backups, restore | Tested in the lab; **no failure drill at a pharmacy yet** |
| Tamil handwriting, OP chits | Script-independent stroke comparison; per-document zones | Simulated only; **no real samples** |
| Patient privacy | Local only; images only for high-risk sales; uploads purged after 24 h; access by PIN | Retention periods and consent **not yet approved by an adviser** |

## Privacy and safeguards

- Everything runs locally; no image leaves the pharmacy's network.
- Images are kept only for high-risk sales (image ledger). Every other checked slip is stored only as an index entry: keypoints, an unreadable 1/8-scale density map, a file hash and the requested drugs. Counter-app uploads are deleted after 24 hours unless a hold is open.
- A shared, cross-pharmacy index should exchange index entries only, never images, and must be designed for the DPDP Act 2023.
- Call-back numbers come from the registry, and only when their source is recorded, never from the slip.
- Clinic private keys (`*.pem`), field-study images (`eval/field/`) and uploads are git-ignored.
- Real or derived prescription images, live ledgers, database journals, backups, and generated reports are excluded from Git. Tests use generated synthetic images only.

## Limitations

- The public tests use synthetic fixtures only; real-world performance is unknown until the [field validation](docs/field-validation.md) is done.
- Without a clinic-signed code, it can't establish that a doctor authorised a prescription. Only issuance confirmation can.
- "Clinic confirmed" is recorded on the pharmacist's word, under their name, unless the doctor answered a ping: then it is recorded as authenticated (see Doctor confirmation). That needs the doctor to have enrolled, through a service that is simulated here. Where there is no receipt, the per-person override and confirmation records are the safeguard.
- Prescriber data needs an official registry integration (ABDM HPR, NMC or state councils) that does not exist yet. Override alerts across pharmacies need a party outside the pharmacy to receive them.
- Issuance codes need clinics to take part. A copied code is caught only at the pharmacy that saw it first, since there is no shared serial register. A stolen clinic key defeats them.
- OCR reads printed slips but mostly not handwriting, and only fills in the form: drug consistency still relies on what staff send, which is confirmed where ambiguous or read from the photo. Edits to an index-only slip can be noticed (seen before + changed request) but not located.
- Cross-pharmacy coverage exists only between pharmacies that joined the (simulated) shared index, and only for copies the local no-image index recognises: phone-screen photos, heavy blur and 5x-forwarded copies are missed across pharmacies (see Shared index).
- A one-digit change in **printed** text (e.g. the Regn No) is below the pixel-diff noise of an AI redraw. It's caught by the registry and record cross-check.
- Real handwriting cut-and-pasted to a new position is not recognised as a copy (it falls back to policy).
- The registry and drug-risk list are not production data (see above). The decision matrix and policy need clinical and legal review.
- The counter app has not been used by pharmacy staff. Its TLS setup, backups and restore have not been rehearsed on pharmacy hardware.
- The container image has been checked by simulation (allowlist, start script, Linux test run) but not built with Docker on this machine. The first real build happens on the host.

## Repository layout

| Path | Contents |
|---|---|
| `app/rxcheck.py` | Engine and CLI: `check`, `add`, `watch`, `serve`, `staff`, `holds`, `resolve`, `stats`, `data`, `clinic`, `log`, `backup`, `verify`, `restore` |
| `app/doctorping.py` | Doctor confirmation: simulated service, enrolment, ping, one-time code, signed receipts |
| `app/sharedindex.py` | Shared index: keyed signatures, simulated hub, peer verification, `sync` |
| `app/counter_app.py` | Counter app (browser UI, sign-in, several counters, scheduled backups, lookups, audit log) |
| `web/` → `static/` | Vue 3 sources (Vite) for the counter app's shortcuts, display controls, lookups and audit log; `static/` is the committed build |
| `tests/scenarios.py` | Synthetic test images and transformations for counter conditions |
| `tests/test_robustness.py` | Scenario tests A–F, quality gate, index, decision matrix, a step for every warning, split sale, speed |
| `tests/test_issuance.py` | Signed codes: tampering, foreign keys, untrusted keys, QR survival, verdicts |
| `tests/test_data.py` | Broken/missing data files, registry coverage and extract age, name matching (what counts as a contradiction), drug tiers incl. NDPS fail-closed, audited imports, entry review |
| `tests/test_storage.py` | Concurrent writers, killed writer, append-only and hash-chained audit (edits, a fully recomputed chain caught against a backup, first-start chaining), rollback, verify, backup/restore, migration |
| `tests/test_server.py` | Counter app: sign-in, lockout, CSRF, roles, decision page, split sale, counter/override/retake flows, four counters at once |
| `tests/test_counter_ux.py` | WCAG AAA contrast in all themes, bundle and CSP, display preferences, shortcut markup, lookups and their safeguards, last activity, audit log filters, paging, CSV export, patient retention |
| `tests/test_eval.py` | Held-out leakage check, threshold lock, field-validation minimums |
| `tests/test_doctor_screens.py` | Counter screens for doctor confirmation: roles, CSRF, ask / refresh / enter code, approval, denial, read-out code, rate limit, expiry, lockout, not enrolled, service down, history and audit log |
| `tests/test_doctorping.py` | Doctor confirmation: enrolment from the register, the minimal ping, app-tap and read-out answers, forged and replayed answers, receipts (tampered, other prescription, other pharmacy, stale), denial blocks confirmation (also after 24 h and without the service config), policy, service down, end to end through the CLI |
| `tests/test_shared.py` | Shared index: keyed signature distances, what the hub stores, clones caught at another pharmacy, stage-2 rejection, minimal disclosure, request authentication, hub-down policy, `sync` late match, scale |
| `tests/test_docs.py` | README decision table must equal `decide()`, and the step table `next_step()`; no conflicting tables elsewhere |
| `tests/test_deploy.py` | What may enter the image, first start on an empty disk, admin bootstrap, proxy mode, demo banner, lockout |
| `Dockerfile`, `docker-entrypoint.sh`, `.dockerignore`, `render.yaml`, `requirements.txt` | Container deployment (see "Public demo deployment") |
| `eval/` | Set builder and evaluator; generated manifests, images, and results are local-only |
| `data/` | Empty prescriber registry, illustrative drug-risk list, policy switches |
| `ledger/`, `inbox/`, `reports/`, `samples/` | Local runtime or private dataset directories; excluded from Git |
