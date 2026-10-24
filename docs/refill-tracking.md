# Refill tracking: design sketch (draft, not built)

Status: proposal for review. Nothing here is implemented yet. The schedule rules (how many repeats each schedule
allows, how long a prescription stays valid) are placeholders until a compliance adviser confirms them.

## What it is for, and what it is not

Routine-drug repeats are already GREEN, so this only matters for **high-risk** (H1, abuse-prone) and **NDPS /
Schedule X** medicines. Today a repeat of such a slip is AMBER with a counter step: "dispense again only if the
prescription allows repeats and the H1 register shows some are left". The pharmacist checks the register by hand.

Refill tracking does two things:

1. **Blocks** what the record shows was never allowed: the balance is used up, or more is asked for than remains.
2. **Pre-fills** the register check, so the pharmacist confirms numbers on screen rather than hunting through the book.

**Release 1 only blocks and pre-fills. It never turns an AMBER into a GREEN.** Auto-clearing comes in release 2,
for the `high` tier only, after replaying real register data shows the tracker agrees with what was sold. NDPS /
Schedule X items never auto-clear.

## Data model

Three additions to `ledger/rxcheck.db`. The first two are append-only, like the audit trail: triggers reject
UPDATE and DELETE, and a correction is a new row that supersedes the old one. The balance is never stored; it is
always computed from the rows, so it cannot drift from them.

```sql
-- What one prescription line allows. One row per high-risk / NDPS drug on a dispensed slip.
CREATE TABLE authorizations (
  id            TEXT PRIMARY KEY,            -- AUTH-<entry>-<n>
  entry_id      TEXT NOT NULL REFERENCES entries(id),   -- the dispensed slip (image + fingerprint)
  item          TEXT NOT NULL,               -- as written on the slip: 'Alprax 0.25'
  molecule      TEXT NOT NULL,               -- from the drug list: 'alprazolam'
  strength      TEXT,                        -- '0.25'
  tier          TEXT NOT NULL CHECK (tier IN ('ndps', 'high')),   -- the tier when it was entered
  qty_per_fill  INTEGER,                     -- units per fill (tabs, caps, ml); NULL = not entered
  repeats       INTEGER NOT NULL DEFAULT 0,  -- extra fills after the first; 'Rpt x2' = 2
  min_days      INTEGER,                     -- shortest gap between fills; NULL = no rule
  valid_until   TEXT,                        -- Rx date + validity for the schedule (policy); NULL = unknown
  source        TEXT NOT NULL CHECK (source IN ('signed', 'typed')),  -- clinic-signed code, or typed by staff
  status        TEXT NOT NULL CHECK (status IN ('provisional', 'confirmed', 'disputed')),
  supersedes    TEXT REFERENCES authorizations(id),    -- a correction points at what it replaces
  entered_by    TEXT NOT NULL,
  entered_at    TEXT NOT NULL
);

-- Every sale of an authorised line. One row per drug per sale, written in the same transaction as the
-- hold resolution / ledger record, and audited with it.
CREATE TABLE fills (
  id            TEXT PRIMARY KEY,
  auth_id       TEXT NOT NULL REFERENCES authorizations(id),
  at            TEXT NOT NULL,
  qty           INTEGER NOT NULL CHECK (qty > 0),
  case_id       TEXT,                        -- the counter case it came from
  hold_id       TEXT,                        -- the hold it settled, if any
  by_actor      TEXT NOT NULL,               -- who sold it (signed-in pharmacist)
  register_ref  TEXT                         -- optional: H1 register page/line, to cross-check later
);

-- Derived on every check, never stored:
--   total     = qty_per_fill * (1 + repeats)
--   given     = SUM(fills.qty) over the current (non-superseded) authorization chain
--   remaining = total - given
--   in_fill   = given % qty_per_fill != 0    -- a partial fill is still open: the next sale continues it
```

`entries.drugs` stays as it is, so existing data keeps working. A slip recorded before this change, or one whose
quantities were never entered, simply has no authorization rows. That is the `unknown` state below, and it behaves
exactly like today.

## Balance states

Computed per held line when a slip comes back (a repeat):

| State | When | Meaning at the counter |
|---|---|---|
| `unknown` | No authorization row, or `qty_per_fill` not entered | Today's behaviour: check the H1 register by hand |
| `open` | `remaining >= requested`, not expired, gap respected | Enough left. Release 1: still a counter check, with the numbers pre-filled |
| `over` | `0 < remaining < requested` | Asks for more than is left |
| `exhausted` | `remaining <= 0` | Nothing left |
| `too_soon` | A new fill (not continuing a partial) before `min_days` since the last fill started | Early refill |
| `expired` | Today after `valid_until` | Prescription no longer valid for this schedule |
| `disputed` | The first entry was challenged (see below) and not yet settled | Numbers can't be trusted |

When the slip shows several drugs, each line gets its own state, and the sale is split per drug as today.

## Decision table changes

New rows for the README "Finding" table (and `tests/test_docs.py`, which must map each to its flags). "Repeat"
means the slip matched a recorded one, so the existing `repeat` flag is set and the new `balance` flag says more.

| Finding | Routine medicine | H1 / X / abuse-prone |
|---|---|---|
| Same slip again, **no quantity on record** (`unknown`) | GREEN (note) | AMBER (unchanged) |
| Same slip again, **enough left** (`open`) | GREEN (note) | AMBER in release 1. Release 2: GREEN only if `auto_clear_repeats` is on and the match is confident; NDPS never |
| Same slip again, **asks for more than is left** (`over`) | GREEN (note) | AMBER, **no override** |
| Same slip again, **nothing left** (`exhausted`) | GREEN (note) | AMBER, **no override** |
| Same slip again, **too soon** (`too_soon`) | GREEN (note) | AMBER |
| Same slip again, **past validity** (`expired`) | GREEN (note) | AMBER |
| Same slip again, **first entry disputed** (`disputed`) | GREEN (note) | AMBER |

Why AMBER and not RED for `exhausted` / `over`: RED is kept for a contradiction *in the document* (an edited copy, a
wrong Regn No, a failed signature). A used-up balance is a fact from the pharmacy's own record, and the usual cause
is a genuine patient who needs a new prescription, not a forger. But it is the exact sale the H1 rules forbid, so
the override is disabled, the same way NDPS items fail closed today. The hold closes only when the clinic issues or
confirms more, or the customer leaves with the rest of the sale.

Routine medicines are not tracked: no quantity field, no balance, nothing changes.

New rows for the "after a warning" step table (`next_step()`):

| Warning | Routine medicine | High-risk medicine |
|---|---|---|
| Same slip again, enough left | - | counter: *"Recorded 30 tabs, Rpt 2 (entered by Priya, 12 Aug). Given 10 on 12 Aug. 50 left. Asked for 10. Confirm against the H1 register."* |
| Same slip again, more asked than left / nothing left | - | call: *"The record shows 0 left. Sell the rest of the order; the high-risk medicine needs a new prescription. Call the clinic on the registry number if the customer disputes it."* |
| Same slip again, too soon | - | counter: *"Last fill started 9 days ago; this prescription allows one every 30 days. Ask why; refer to the doctor for an early refill."* |
| Same slip again, past validity | - | call |
| Same slip again, first entry disputed | - | call |

## Entering the numbers without adding a form

The numbers have to come from somewhere, and the optional OCR reads names and strengths only, not quantities. So:

- **Signed code first.** Version 2 of the clinic-signed code adds `qty`, `repeats` and `min_days` per line. A slip
  with such a code needs no typing at all (`source = 'signed'`, `status = 'confirmed'`).
- **Otherwise, typed once, at first dispense,** in the step that already exists: "Record as dispensed", which the
  pharmacist does at the same moment as the H1 register entry. Each high-risk line gets **one** field,
  *"Prescribed"*, accepting short forms: `30`, `30 rpt 2`, `1-0-1 x 5d` (read as 10), `10 / 30d`. The parsed result
  is shown back in words ("30 tabs, 2 repeats = 90 in all") before saving. Anything the parser can't read with
  certainty, such as `SOS`, `as directed`, or a range, is refused, and the pharmacist types the plain number of units.
- **"Giving now" defaults to one full fill** and only needs editing for a partial sale. On a repeat it is pre-filled
  with what is left of the current fill.
- Routine lines get no field.

## The first entry is the weak point

A lenient or colluding first entry sets a balance every later sale relies on. Safeguards:

1. **Pharmacist only** for high-risk and NDPS lines. The record step already is.
2. **Who entered it is shown every time the slip comes back:** "30 tabs, Rpt 2, entered by Priya on 12 Aug".
3. **Compared on return.** The repeat screen asks the pharmacist to tick "matches the paper slip" next to the
   recorded numbers. If they correct it instead, the original stays, a new row supersedes it with
   `status = 'disputed'`, and the hold goes to a call. Corrections are counted per person, like overrides today, so a
   pattern of lenient first entries shows up in the 14-day alert.
4. **Sanity limits** per molecule in `data/drug_risk.json` (for example, more than 60 tablets of alprazolam per
   fill asks for a second look), when the adviser supplies them.
5. **A typed entry stays `provisional`** until it is confirmed by a signed code, a clinic call, or the tick on return.

## Release 2: when a repeat may auto-clear

All of these must hold. Anything less falls back to today's AMBER counter check.

- Policy switch `auto_clear_repeats` is on (default **off**). It is turned on only after the replay below shows no
  disagreement over a few months of real register data.
- The tier is `high`. **Never `ndps`.**
- The balance state is `open`, and the authorization is `confirmed` (signed, called, or ticked on return), not
  `provisional` or `disputed`.
- A **confident same-slip match** against the recorded image: identity `copy`, strict stroke agreement of 0.97 or
  higher, no edits, no Regn No / date / drug mismatch. An index-only "seen before" match never qualifies, because no
  image was kept to compare.
- The shared index, if configured, was reachable and shows no use at another pharmacy.

## Validating with the shop's own H1 register

This can start now, with no prescription images.

`rxcheck refill replay register.csv` reads exported register rows (`date, rx_ref, patient, regn_no, drug, qty`, and
`prescribed` / `repeats` where the register has them). Patient names are replaced with a salted hash on import; the
salt stays on the shop PC. For each row it reports what the tracker would have said (`allow`, `over`,
`exhausted`, `too_soon`, `unknown`) next to what was actually sold, and lists the disagreements:

- **Tracker blocks, sale happened:** either the tracker's rule is wrong (fix the rule), or the shop over-supplied
  (a finding in itself).
- **Tracker allows, register shows a refusal:** the rule is too lenient.

Report: rows replayed, disagreements by type, and the share of repeats that would have needed no register lookup.
Release 2 is gated on this report. Get the pharmacist's consent before exporting, and keep the export on the shop PC.

## Questions for the compliance adviser

The tracker encodes these as rules, so they need a confirmed answer, with a source, before release 1:

1. Which schedules allow repeats at all, and up to how many? (H1, X, NDPS-listed, and the rest of Schedule H)
2. How long is a prescription valid for each schedule?
3. Is a partial fill allowed, and does the rest of it count against the same prescription?
4. For Schedule X / NDPS: does the pharmacy have to retain the prescription after the final fill?
5. Which fields must the H1 register and the Schedule X record carry, so a fill row can be matched to them?

## Out of scope for both releases

Reading quantities from the slip image (OCR), tracking routine drugs, sharing balances between pharmacies (the
shared index would have to carry balances, a privacy question of its own), and per-patient limits across different
prescriptions.
