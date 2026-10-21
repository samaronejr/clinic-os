# Orders and results

**Binding contract.** An order identifies an enrolled patient, an ordering
clinician and version 1 of the local examination catalog. `OrderEvent` retains
draft, placed, collected and in-process transitions. No collection state implies
clinical review.

`ResultVersion` and `ResultObservation` are immutable. Source identity and exact
observation strings use tenant envelopes; original PDF/image bytes use the
attachment storage boundary with tenant encryption, byte count and SHA-256.
Pending upload receipts survive transaction rollback and are reconciled only by
the owner. Tables and source rows cannot be hard-deleted.

## Services and authority

`place_order`, `progress_order`, `ingest_result`, `match_result`,
`reject_result`, `acknowledge_result` and `release_result` are keyword-only,
explicitly clinic-scoped services. Staff authority comes from the current DB
actor, patient scope and professional registration. Unknown and wrong-clinic
selectors have the same payload-free denial.

Ingestion is not review. Filing requires an existing order and independently
provided date of birth plus an exact normalized name or current identifier.
Every supplied factor must agree; name matching never uses edit distance.
Missing, conflicting or unknown-order evidence creates a quarantine receipt and
an accountable receptionist task, not a patient chart entry. An operator may
select a verified order but cannot override the immutable source identity.
Unresolved operator attempts return `match_ambiguous` or `quarantined`.

Every matched version has an assigned review task for its ordering clinician.
An explicit critical source flag gives that task urgent priority immediately,
without depending on patient release. Trigger-created tasks have no fabricated
staff creator: a private exact-row predicate permits only the source-backed
bridge and ordinary NULL-creator inserts remain denied.

Acknowledgment records a clinician and explicit disposition for exactly one
current version. Release separately names patient or an active, correctly scoped
delegate grant and requires that version's acknowledgment. Correction names an
exact predecessor, retains earlier bytes and receipts, and carries no old
acknowledgment or release forward. Correction and clinical decisions share a
PostgreSQL advisory fence. Event replay is idempotent; altered replay and stale
predecessors write nothing.

`result_rows`, `trends` and `patient_sources` recheck current authorization.
Trends preserve decimal separators, units, flags and reference intervals from
the source rather than inferring conversions or normal ranges. Patient source
descriptors additionally require a current session, matching release audience
and current acknowledgment; storage locators never enter a public URL.

## Synthetic callback

`POST /orders/lab/callback/` is outside the staff tenant middleware. It
authenticates HMAC-SHA256 over the raw bytes before any account, correlation or
tenant lookup. Owner-provisioned `LabAccount` and active `ServicePrincipal`
records resolve the organization, clinic and permitted order correlation.
The closed payload rejects tenant claims, patient selectors and unknown keys.
A lab principal can ingest but cannot read clinical sources, acknowledge or
release. Its audit bridge verifies source-backed scope and appends through the
owner audit boundary without inventing a human actor.

Only `synthetic-lab-v1` is runnable, with
`ORDERS_SYNTHETIC_LAB_ENABLED` defaulting off and synthetic data mode required.
**BLOCKED-ON-EG:** there is no runnable real-lab adapter. A future real adapter
must use the provider lifecycle registry and the live runtime gate; neither this
setting nor a lab account can authorize it. There are no external sends and no
paid provider calls.

## Registration and recovery

`OrdersConfig`, `orders.rls`, the module/signature pins, coverage target, audit
catalog and resolver catalog are registered together. All 13 tenant relations
use FORCE RLS and least-privilege grants. Matching, quarantine and task linkage
are resolver-written; the runtime has no direct INSERT grant on those tables.
The lab principal has no direct table grant.

The initial protected schema is additive, non-atomic and irreversible:
**rollback = restore**. The recovery relation manifest includes all 13 tables;
its count must be derived from the live catalog, not incremented by guesswork.
Restore also requires the tenant keys and original source objects.

## Verification

`tests/orders/` exercises exact matching partitions, unknown-order resolution,
forged/replayed/revoked/out-of-order and concurrent callbacks, critical tasks,
version-bound acknowledgment/release, raw mutation refusal and every new
relation's cross-organization denial. Registered suite `results` drives the
native upload, acknowledgment and release surfaces with real middleware,
CSRF, PostgreSQL roles, keyboard/reflow checks and vendored axe.
