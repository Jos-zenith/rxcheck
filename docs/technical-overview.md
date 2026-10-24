# Rxcheck technical overview

**Why triage, not verdicts.** A prescription image can be new, reused, altered, blurry, or forwarded through a
messaging app. Pixels alone cannot prove who issued it. A detector that labels a document "authentic" overstates
what software can know; one that calls every unusual image fraudulent creates harmful false alarms. So rxcheck
answers a narrower question: **does this need a human check before dispensing?** - and it says what it does not
know.

Rxcheck is a research prototype for prescription-image triage at a pharmacy counter. It is not a
certificate of authenticity, a clinical decision system, or a substitute for contacting the prescriber.
The public repository contains no real or derived prescription images, patient records, or pharmacy
ledger data.

## Verification model

The application combines independent signals rather than attempting to classify whether an image was
AI-generated:

1. **Known-slip comparison.** An image is compared with records the pharmacy already holds. The image
   ledger supports edit localisation for recorded high-risk sales. The no-image index covers checked
   slips but can only report a possible repeat, never a changed region.
2. **Prescriber registry.** A local registry can identify verified or unverified registration details.
   The shipped registry is empty; production data needs an authoritative source and review process.
3. **Medicine consistency.** The requested medicine and strength can be compared with a matched record
   or clinic-signed issuance code. Ambiguous entries require staff confirmation.
4. **Clinic issuance codes.** A participating clinic can sign a prescription-specific payload. This is
   the only feature that can establish issuance by that clinic; it depends on trusted key enrollment.
5. **Image quality and metadata.** Unreadable images go to manual review. File metadata is supporting
   evidence only and never establishes authorship.

The decision table in [README.md](../README.md#verification-states) is the policy source of truth.
`tests/test_readme_tables.py` checks it against the decision functions in `app/rxcheck.py`.

## Evidence and limits

The tests use generated synthetic images and exercise transforms such as recompression, blur, glare,
and simulated edits. Those tests verify expected software behavior on those fixtures only. They do not
establish accuracy on real prescriptions, across prescribers, phones, languages, or pharmacies.

The field-validation protocol in [docs/field-validation-protocol.md](field-validation-protocol.md) defines the
independent dataset, consent, ground-truth, and shadow-pilot work needed before considering operational
use. Real or derived data is intentionally kept out of this repository.

Key residual risks include new unsigned fabrications, copied clinic codes presented at another
pharmacy, compromised clinic keys, incomplete registries, and poor-quality images. The application
must be treated as a conservative prompt for human confirmation, not proof that a document is genuine
or fraudulent.
