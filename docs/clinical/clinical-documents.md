# Clinical documents - synthetic contract v1

Todo 33 extends the existing prescription artifact pipeline. It does not
activate prescription classes, provider capabilities, licensed terminology,
or live issuance.

## Kinds and authority

`DocumentKind` contains `prescription`, `certificate`, `attestation`,
`referral`, `exam_request`, and `instruction`. There is at most one retained
draft per encounter and kind. Existing drafts migrate to `prescription`.
`create_document_draft(*, clinic_id, encounter_id, kind)` creates or resumes
one kind. The existing pinned `create_draft` prescription signature is
unchanged and selects only the prescription kind.

The encounter's assigned physician remains the issuer. New kinds additionally
require current professional registration and patient-scoped
`prescription.prepare`; signing requires `prescription.sign`. Draft reads and
writes enforce the same authority through FORCE RLS. Unknown and foreign
selectors return the same denial. Referral documents stand alone: there is
no referral-workflow or inbox linkage; todo 52 owns that later linkage.

Non-medication content is clinician-authored text, optional recipient and,
for exam requests only, an explicit selection from the landed local exam
catalog. The application neither generates instructions nor infers a
diagnosis, work restriction, duration, referral destination or examination.
Text is retained verbatim. Native forms retain bound text on rejected saves
and display success only after a confirmed server write.

## Saved versions and rendered bytes

`DocumentDraftContent` retains one encrypted, immutable JSON snapshot per
saved non-medication version. Its draft/organization/version binding is
checked by PostgreSQL; runtime mutation and deletion are denied. Saves
compare the expected version, advance the draft and append the snapshot
inside one transaction. Kind, encounter, patient and issuer cannot change.

`clinical-documents-pt-BR-v1` defines separate pt-BR titles and content
sections for every kind. `synthetic-pdf-v1` remains the fixed deterministic
renderer. Its textual clinic branding freezes the latest
`ClinicConfiguration` display name, contact details, version and brand token;
the fixed monochrome profile does not embed the optional raster logo.
The input digest binds the kind, template version, branding, exact authored
text, catalog terms, issuer, patient, clinic, timestamp with its UTC offset, review version
and public verification URL. Rendering the same frozen input produces the
same PDF bytes.

Every PDF states that it is synthetic and has no clinical validity. Signing
uses only stored, digest-checked bytes, the existing step-up and independent
provider verification. Edits invalidate the older version as an approval
candidate. Initiation, preparation, dispatch and callback completion recheck
the current saved version; PostgreSQL fences signature transitions against
concurrent edits independently. Old bytes and review history are retained.

Freshness applies to approval and signing, not to deletion or invalidation
of completed history. A document already successfully signed before a later
edit or discard remains a historical signed artifact. Its existing explicit
release, delivery, revocation and patient-access contracts remain in force.
Rendering, signing and patient release are separate actions.

## Exam-order timing and atomicity

Rendering an exam request does **not** place an order. It freezes catalog
version 1, the exact exam codes and display labels, and `order_command_id`.
That UUID is `uuid5(draft_id, "exam-request:<document_version>")`; it is
derived from the approved version identity, never from later draft input.

Only a successfully, independently verified signature callback calls the
landed `apps.orders.services.place_order`. It uses the document's frozen
exam codes and deterministic command ID, under the original clinician's
current tenant, professional and patient-care authority. The resulting
`Order.command_id` is the document linkage; the order retains its own
catalog-versioned items and audit events.

Order placement, the signing operation's terminal state, callback receipt
and completion audit share the callback transaction. A failure rolls them
all back; provider retry uses the same approved command identity. Duplicate
callbacks and duplicate landed commands cannot create additional orders.
Drafts, previews, stale approvals, failed signing and abandoned attempts
cannot place orders. Issuance neither acknowledges nor releases results.

This timing is a todo 33 implementation choice for independent reviewer
assessment: the requirement says exam requests create orders, while the
landed order contract preserves immutable placed orders. Delaying placement
until verified approval avoids orders from unreviewed or edited versions.
Root acknowledged the choice and required the real-role negative,
idempotency and transaction-rollback proofs recorded in task 33 evidence.

## CID-10

Clinical-content source record version: **clinical-documents-source-v1**.
The retained source record is
[`document-terminology-source-v1.json`](document-terminology-source-v1.json).
It records the WHO ICD-10 2019 English reference as a candidate, not an
installed pt-BR corpus or a redistribution approval.

| Field | Recorded state |
| --- | --- |
| Terminology | CID-10 |
| Installed dataset version | None |
| Installed source reference | None |
| EG-13 approval | Absent |
| Data shipped or invented by this task | None |

Optional diagnosis coding is explicitly unavailable until a versioned
source, redistribution rights and EG-13 approval have been recorded.
Caller-supplied codes, source versions and claimed approval are rejected;
they cannot establish content governance. Saved empty diagnosis selections
retain the source-record reference in their encrypted snapshot. No
unlicensed list, guessed code or synthetic code masquerading as CID-10 is
installed. A later activation must supply the governed versioned source
and its approval evidence before making the field usable.

Primary-source review on 2026-10-22: WHO's 2019 browser identifies that
release; WHO's licensing FAQ distinguishes licensed use and translation
rights from browser access. Ministry of Health Technical Note
91/2024-CGIAE/DAENT/SVSA/MS anticipates CID-11 use in health-surveillance
systems from January 2027. This does not establish a verified universal
clinical-document mandate or approve this product's corpus. References,
scope and the still-closed EG-13 decision are retained in the JSON record.

## Verification, privacy and qualification

Public verification remains status-only. No clinical body, kind-specific
text, diagnosis, exam selection, patient identity or document download is
added to its response. Opaque handles are not download or delivery
authority. Existing rate limits, uniform unknown-handle behavior,
revocation, synthetic labeling and provider gates remain unchanged.

Required real-role acceptance includes `tests/prescription/test_document_kinds.py`,
`tests/renewal/test_document_artifacts.py`,
`tests/renewal/test_document_verification.py`, and affected prescription,
signature and workflow suites, with `CLINIC_PDF_TOOLS=required`.
The existing `document-verification` browser suite includes the new
clinical-document scenario, sharing one session fixture and keeping its
existing CI shard identity. Run the relevant browser engines, static,
migration, posture and exact backup/recovery census gates, then unchanged
full native coverage before Ready.

## Landing integration

Task 32 owns workflows inbox/Today; this task changes no such read model.
Shared edits are limited to the phase-1 audit event matrix, schema/resolver
and recovery registries, prescription/EHR entry surfaces, the existing
verification-suite scenario additions, and locale artifacts.

Merge task 33's appended pt-BR entries with concurrent locale work and
regenerate `django.mo` from the merged `django.po`; do not binary-merge
compiled catalogs. Preserve the new relation in the exact live-catalog
manifest and backup census. No new browser suite name, CI shard, budget,
skip or coverage floor is introduced. Evidence lives in
`.omo/evidence/clinic-ops-premium-intelligence/task-33/`.
