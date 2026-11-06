# Insurance and TISS

Binding contract. Insurance owns payer identities, versioned payer contracts,
versioned price tables, TUSS imports, encrypted patient memberships, source-backed
guides, batches, submission attempts, returns, glosas, appeals, clinical assertion
approvals, payer checks and journal reconciliation links.

## Facts and exact money

All services are keyword-only and explicitly clinic-scoped. Current DB actor and
tenant GUCs provide authority; callers cannot supply an acting clinician or tenant.
Unknown, unauthorized and wrong-clinic selectors receive the same denial.
Finance uses `tiss.manage`. Read authority remains `tiss.read`.

A guide joins an actual completed encounter, its insurer invoice, the enrolled
patient's valid membership and a versioned price mapping for the scheduled service
or an explicitly priced unscheduled encounter. The invoice's recognized net amount
must equal the integer-centavo guide total. Clinical facts, encounters and
appointment care/status/time bindings are never rewritten for billing.
Only explicit appointment insurance-check pointers/reference advance through
their retained insert receipt. Prices reference imported TUSS terms. Billing identities live in
the encrypted contract version and are not signing or professional-verification
evidence.

The CSV terminology import accepts the closed UTF-8 export columns
`table,code,description`. Every imported term retains its component version,
source date and SHA-256 of the exact export bytes. This is not a claim that a
synthetic fixture is the complete ANS component. Import the authorized component
export before using actual terminology.

## Pinned XML and state

`TISS_COMPONENT_VERSIONS` explicitly pins organizational `202601`, content
`202511`, TUSS `202601`, security `202511` and communication `04.03.00`. These are
historical release pins, not a claim to the latest ANS publication. The message's
official lexical version is `4.03.00`. A new component combination requires its
own reviewed schemas and release provenance.

Consulta, SP/SADT and honorarios messages validate against the vendored official
ANS XSD files. `schemas/manifest.json` records the source archive, retrieval date,
upstream and normalized-file checksums. Latin-1 sources are normalized to UTF-8;
the unused external DTD in the W3C signature schema is removed and its license
is retained. Validation cannot fetch network resources or resolve caller-supplied
entities, local files or schema locations. Errors contain only bounded,
schema-owned field names and translated messages, never raw validator text.

Guide states are draft -> validated -> sealed. Batches retain immutable ordered
guide versions, component versions, exact encrypted XML and SHA-256 from creation.
They advance draft -> validated -> sealed -> submitting -> submitted/unknown ->
acknowledged -> returned. Unknown delivery cannot trigger an automatic retry.
One guide version belongs to one batch; resubmission creates a successor guide
and preserves the original. State transitions, exact replay receipts and their
registered metadata-only audit appends share one transaction.

The legacy ANS MD5 content checksum provides protocol compatibility only; it is
neither authentication nor the SHA-256 sealing boundary. Return files are XSD
validated and content-checked before matching exact payer, batch, guide, card,
procedure and amount facts. Duplicate files are idempotent. Acknowledgment is not
adjudication, and adjudication is not payment.

## Appeals and cash

Appeals are immutable drafts. Finance cannot approve new clinical assertions.
Dedicated catalog v2 permission `tiss.clinical_approve` is granted only to current,
registered physicians in the exact clinic. It does not reuse `clinical.write`
and does not change patient-scoped `tiss.clinical_read`. Clinical approval binds
the exact encrypted appeal body and assertions. Finance separately approves
submission; the database rechecks the original clinician's current membership,
registration and removal grants at approval and submission.

Payer checks create an accountable task through the existing workflows service;
the operator must have that service's configured workflow permission bundle.
The task is claimed by its creating finance operator, not by a fabricated user.

`MembershipTerms(registration_id, valid_from, newborn)` selects the retained
intake registration. Its card/expiry are inherited, not independently re-entered.
`backfill_membership_payers(*, clinic_id)` populates only null intake payer FKs
on unique exact ANS matches and returns unmatched/ambiguous identifiers. It never
guesses from names, changes ciphertext or modifies existing non-null links.

`PayerCheck(appointment_id, kind, due_at, fixture_outcome)` binds a check to the
actual insured appointment and its immutable membership/contract selection.
The appointment keeps current eligibility/authorization FKs; only insertion
of a new retained result can advance them. Affirmative synthetic authorization
has an explicitly synthetic reference; denial and unknown have none. Old
command replay returns its historical check without replacing a later result.
Booking confirms only the supported affirmative checks. These financial
updates preserve scheduling lock order and do not grant finance care/move
authority. Clinical progress is not automatically reversed by a payer result.

The cross-app migrations follow `billing.0011_operational_journal` (which follows
`scheduling.0010_patient_service_booking`). They are non-atomic and irreversible. Existing
unknown appointment UUIDs or unverified authorization strings stop upgrade,
rather than being cleared or guessed. The existing restore inventory already
contains all FK source/target tables; no new table is added by these fixes.
Relationship/count evidence is derived from model/migration code and requires
live-catalog, forward-upgrade and restore confirmation at runtime.

Reconciliation accepts only independently posted insurer settlements. It calls
the existing billing allocation service and records its exact allocation identity.
Neither a return nor an insurer acknowledgment can create or verify cash.
Accepted amounts bound the allocation; denied balances remain outstanding pending
appeal or a separately authorized finance decision. No automatic write-off,
revenue adjustment or change to clinical facts is made.

## External gates and recovery

`INSURANCE_SYNTHETIC_ENABLED` defaults off. The only runnable operator path is the
explicit local `synthetic-tiss-v1` rehearsal. Portal export returns the sealed
bytes and does not imply upload, receipt, adjudication or payment. There is no
real network adapter or provider call.

All real operator/TISS connectivity is **BLOCKED-ON-EG**, including EG-1, EG-5 and
EG-8. A future live adapter must require an activated provider lifecycle record
and the live runtime gate; neither this flag nor a payer contract authorizes it.

All 16 tables subclass `TenantScopedModel`, FORCE RLS and revoke default
privileges. They deny hard deletion and truncation; only the three state columns
have UPDATE grants. Initial encryption is additive, non-atomic and irreversible:
**rollback = restore**, including tenant keys. Posture, resolver, module-boundary,
audit, coverage and recovery registries are registered together.

`tests/insurance/test_tiss.py` and `test_contracts.py` run without a DB.
`test_revenue_cycle.py`, `test_posture.py` and
`tests/identity/test_tiss_approval_permission.py` require the real PostgreSQL
roles and must be run by the root validation lane. Source review is not database,
migration-upgrade, provider-sandbox or live evidence.
