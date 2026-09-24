# Prescription drafts

Task 32 provides an assigned-physician-only, synthetic draft workspace reached
from the encounter. Encounter, patient, clinic, organization and issuer bindings
are immutable and checked by services and FORCE RLS. Current role/assignment is
rechecked on every operation. A same-clinic colleague, receptionist, administrator
or patient cannot read drafts.

Each explicit save compares `expected_version`, increments `version` atomically,
and appends immutable item snapshots. Failed or stale saves preserve both stored
content and bound browser input. Clinician-entered text is never trimmed,
calculated, classified or used to generate treatment advice. Items capture
medication description (240), strength/form (160), dose (160), route (80),
frequency (160), duration (160), quantity (80) and instructions (2000 characters),
with at most 20 items. Metadata-only audit events track creation, viewing, saving
and discard. One resumable draft exists per encounter. Discard retains history,
hides content, and is terminal; live drafts block encounter closure.

## Category boundary

The task-6 [2026-09-12-v1 register](../../docs/integrations/capabilities.md) has no
confirmed real issuance contract. Consequently **no real document category is
supported**, including ordinary non-controlled prescriptions. Controlled,
notification, antimicrobial and unknown categories fail closed server-side.
The separate `synthetic_non_controlled` / `synthetic-draft-v1` contract is solely
for the plan's synthetic rehearsal. It is not a legal approval or a claim that
entered medication text is non-controlled or safe. This classification is never
inferred from drug text.

`issue_prescription()` and the signing adapter remain deferred. Task 31's fresh
professional verification and step-up apply at signing, not ordinary draft
entry. Drafts confer no signing authority, patient release, signature,
RNDS/SNCR exchange or live readiness. Later issuance must separately require the
approved category contract and fresh physician/signer verification.

## Rendered document artifacts

Task 33 adds `PrescriptionDocument`: one immutable rendered artifact per draft
version. `render_document` locks the draft, snapshots every visible field into a
canonical `frozen_input` (RFC 8785), binds `document_version`, `render_params`,
`input_digest` and `pdf_digest` in the same row, and stores the PDF bytes.
Rows are insert-only: a database trigger rejects every update and delete, and
FORCE RLS limits reads to physicians with a care relationship and inserts to
the assigned issuer. `verify_document_integrity` recomputes both digests from
stored content; `download_document` re-checks the byte digest, authorizes by
role plus `ehr_care`, and serves a bounded no-store response.

The QR carries only a random public verification handle inside a synthetic
verification URL — never a patient identifier, session token or download
authority; the handle cannot resolve or download the record. Rendering uses the
installed `qrcode` package and an explicitly synthetic deterministic PDF writer
(`synthetic-pdf-v1`): A4, Helvetica 11pt, glyph-width wrapping that reserves
the first-page QR block, no timestamps or unstable identifiers, so identical
frozen input produces byte-identical output. The task-6 register records `pdf_rendering` as
unavailable: no approved renderer exists, the synthetic renderer refuses to run
outside synthetic data mode, and malformed output or a missing QR dependency
fails closed without writing an artifact. These artifacts are not signed
prescriptions and cannot unlock task 34 signing or task 35 distribution.

## Privacy-limited verification and delivery

Task 35 publishes document status without exposing content. The anonymous
`GET /prescription/verify/<handle>/` route bypasses tenant middleware and calls
`clinic_app.prescription_verify`, a resolver-owned SECURITY DEFINER function
that returns only a fixed minimal projection: status, document version,
issuance time, digests and the frozen issuer/clinic labels. Unknown, malformed
and unissued handles are indistinguishable (`unavailable`); stored bytes whose
digests no longer match return `invalid`; revocation and supersession are
published as `revoked`/`superseded` without rewriting the signed bytes. The
resolver verifies the signature inside its privileged boundary
(`prescription_verify_synthetic`) and publishes only the verdict — signed
bytes never leave the resolver, so the runtime role cannot read them through
the public handle. A disabled verifier fails closed to `unavailable` and
tampered signed bytes to `invalid`. Lookups are rate-limited per probe key by
`prescription_verify_allowance` against `prescription_verificationprobe`, a
table the runtime role cannot read or write at all. The QR handle never grants
download: it resolves only to the status page.

Full document access stays behind two authorized boundaries. The issuer
releases a completed document to the patient with `release_document`
(one active release per document, issuer-bound insert/update policies plus a
binding trigger), and the patient lists/downloads released signed bytes only
through `prescription_patient_documents`/`prescription_patient_document_bytes`,
which re-validate the live `records` session on every call. Downloads append a
`prescription.document.downloaded` audit event through
`prescription_document_viewed`. `revoke_document_release` restores denial and
`revoke_document` records an insert-only revocation row; neither rewrites the
signed artifact.

Delivery uses the shared outbox: `deliver_document` enqueues a
`prescription.document.delivery` operation that the registered
`DocumentDeliveryAdapter` sends as a link-only email (the verification URL,
never an attachment), gated on a completed signature, an active release, a
verified patient email and no revocation. A subject recheck
(`document_delivery_eligible`) re-validates all of that at send time, so a
revoked or unreleased document can never be delivered.

QA: `uv run --frozen --no-sync --no-env-file pytest --reuse-db -q
tests/renewal/test_prescription_drafts.py tests/infra/test_module_boundaries.py`, then
`uv run --frozen --no-sync --no-env-file python -m ops.testing.renewal_runner
browser --suite prescription-draft` in the owned database environment.
Verification and delivery: `uv run --frozen --no-sync --no-env-file pytest
--reuse-db -q tests/renewal/test_document_verification.py`, then
`uv run --frozen --no-sync --no-env-file python -m ops.testing.renewal_runner
browser --suite document-verification`.
