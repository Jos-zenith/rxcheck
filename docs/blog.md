# Why prescription-image checks should be triage, not verdicts

A prescription image can be new, reused, altered, blurry, or forwarded through a messaging app. Pixels
alone cannot prove who issued it. A detector that labels a document "authentic" risks overstating what
the software can know; a detector that calls every unusual image fraudulent can create harmful false
alarms.

Rxcheck is a research prototype built around a narrower question: **does this need a human check before
dispensing?** It combines comparisons with records already held by a pharmacy, local prescriber data,
medicine consistency checks, and optional clinic-signed issuance codes. Unreadable images require
manual review, and ambiguous matches prompt confirmation rather than accusation.

These signals have different limits. An image ledger can help locate edits only when the original is
already recorded. A no-image index can report a possible repeat but cannot show where a change was
made. A local registry can be incomplete or stale. A clinic-signed code can confirm issuance only
when a clinic participates and its public key was obtained through a trusted process.

The project tests software behavior with synthetic fixtures. Those tests are not evidence of accuracy
at a real counter. Before operational use, the system needs an independently collected, consented
dataset, clinical and legal review, representative usability testing, and a shadow pilot. The
[field-validation protocol](./field-validation.md) describes that work.

Rxcheck is not a certificate of authenticity, a clinical decision system, or a substitute for a
prescriber's confirmation. Its outputs should support a pharmacist's decision, never replace it.
