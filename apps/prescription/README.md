# Prescription drafts

Todo 33 adds clinical documents alongside the original prescription path:
`DocumentKind` supports prescriptions, medical certificates, attendance
attestations, referrals, exam requests and patient instructions. The binding
extension contract is
[`docs/clinical/clinical-documents.md`](../../docs/clinical/clinical-documents.md).
It defines encrypted per-kind version snapshots, textual clinic branding,
stale-approval rejection, verified-issuance exam orders and the closed EG-13
diagnosis-source gate. Completed signed history keeps its existing release
behavior after later draft edits or discard.

Task 32 provides an assigned-physician-only, synthetic draft workspace reached
from the encounter. Encounter, patient, clinic, organization and issuer bindings
are immutable and checked by services and FORCE RLS. Current role/assignment is
rechecked on every operation. A same-clinic colleague, receptionist, administrator
or patient cannot read drafts.

Each explicit save compares `expected_version`, increments `version` atomically,
and appends immutable item snapshots. Failed or stale saves preserve both stored
content and bound browser input. Clinician-entered doses are never suggested,
classified or used to generate treatment advice. Free text is retained exactly;
the optional structured mode calculates only the total quantity explicitly
defined by the entered dose, administrations/day and days. Items capture
medication description (240), strength/form (160), dose (160), route (80),
frequency (160), duration (160), quantity (80) and instructions (2000 characters),
with at most 20 items. Metadata-only audit events track creation, viewing, saving
and discard. One resumable draft exists per encounter. Discard retains history,
hides content, and is terminal; live drafts block encounter closure.

## Category boundary

Superseded in part by docs/plans/clinic-ops-premium-successor.md (SD ledger):
SD-6 replaces the synthetic-only category with the todo-34 prescription-class
taxonomy. Every new draft starts with `category=NULL`: there is no selected or
persisted default, including on renewal. Only the assigned physician can choose,
change or clear the class through a versioned save. A catalog selection never
classifies medication.

The [2026-09-24-v2 register](../../docs/integrations/capabilities.md) has no
confirmed real issuance contract. Consequently **no real document category is
supported for electronic issuance**, including ordinary non-controlled
prescriptions. Known classes may be prepared manually; unknown values fail at
the form, service and database boundaries. `PrescriptionClassPolicy` is an
immutable, migration-seeded D-21 reference, not an approval toggle. Notifications
A/B/B2, systemic retinoids and thalidomide require SNCR from the recorded
30/09/2026 boundary. White control-special, antimicrobial and GLP-1 retention
classes require it from 30/10/2026. Electronic requests refuse with
`class_electronic_unavailable` before the recorded boundary and `sncr_required`
from it onward; both remain BLOCKED-ON-EG-6. Simple electronic issuance remains
BLOCKED-ON-EG-5 and the later issuance contract.
The separate `synthetic_non_controlled` / `synthetic-draft-v1` contract is solely
for the plan's synthetic rehearsal. It is not a legal approval or a claim that
entered medication text is non-controlled or safe. This classification is never
inferred from drug text.

`require_issuance_class` runs at signing initiation, preparation, dispatch and
completion. Unset classes refuse with `class_required` before an operation is
created. An immutable `SignatureOperation.category` snapshots the explicit
rendered-version choice; the issued-row CHECK independently requires a known,
non-synthetic, non-null class. A separate database guard keeps every real
electronic class closed until a later gate-backed migration. Paper preparation
is allowed and labeled **not issued**. The only renderer remains synthetic;
its paper preparation output is not a legally issued paper prescription.

## Catalog, favorites and structured directions

`load_medication_catalog --source <file> --sha256 <lowercase-sha256>` is the
owner-only offline refresh job. It never downloads files or uses owner
credentials in a runtime worker. Re-running the command is idempotent; a new
snapshot becomes searchable by retrieval date and load time without changing
historical entries or favorites. Stage an explicitly normalized UTF-8 JSON
snapshot of Anvisa/CMED public terminology:

```json
{
  "schema_version": 1,
  "source_url": "https://www.gov.br/anvisa/pt-br/synthetic-example.json",
  "retrieved_on": "2026-10-20",
  "entries": [
    {
      "code": "SYNTHETIC-001",
      "description": "Synthetic medicine",
      "strength_form": "Synthetic tablet"
    }
  ]
}
```

This example is synthetic, not a downloaded or licensed catalog. Raw CMED
spreadsheets must first be normalized to this closed schema outside the loader.
The loader validates schema, unique source codes, official HTTPS origin,
retrieval date, size and the exact file checksum before inserting any rows.
It retains URL/date/checksum in an immutable import receipt. Classification
fields are rejected. No real dataset or EG-13 approval ships with this task;
regulatory classification remains manual regardless of catalog provenance.

Reference tables hold public data and have runtime SELECT only. Favorites are
clinic/issuer scoped under FORCE RLS, require current physician authority, and
are removed through audited deactivation, never DELETE. Native POST search,
selection and favorite actions preserve the entered draft without saving it.

Structured mode is explicit: a unit plus numeric dose, administrations/day and
duration in days. Decimal-string multiplication computes quantity in the
**same unit**, with no inferred conversion, packaging rule or rounding.
Free-text mode performs no calculation. The structured payload is encrypted
from its first schema version, alongside the existing encrypted item text.
An optional `MedicationStatement` link must belong to the draft's clinic and
patient; linking never confirms, reconciles or mutates the statement.

Renewal explicitly copies a completed, non-revoked source artifact into a new
assigned encounter for the same patient. It retains the source link and copies
only its frozen item snapshot, never its class, signature or issuance
authority. The source bytes and prior snapshots remain untouched.

`issue_prescription()` and live qualified signing remain deferred. Task 31's fresh
professional verification and step-up apply at signing, not ordinary draft
entry. Drafts confer no signing authority, patient release, signature,
RNDS/SNCR exchange or live readiness. Later issuance must separately require the
approved category contract and fresh physician/signer verification.

## Independent signing verification rehearsal

Todo 35 adds the prepare/sign/status/verify `SignatureProvider` protocol
alongside the compatible `CallbackSignatureProvider` contract. The existing
synthetic signer remains the default. An explicitly supplied
`RecordedSignatureProvider` takes explicit synthetic certificate material and
a separate `SignatureVerificationAPIAdapter`. The latter uses locked
cryptography to verify exact-byte RSA-PSS signatures, the certificate path to
a configured synthetic root, a separately signed timestamp proof, certificate
validity/purpose/identity and issuer-signed CRLs. Recorded verdict strings are
never verification evidence. The selected mechanism, fixed fixtures and
restricted synthetic anchor policy are recorded in ADR-010.

An authenticated candidate first commits as `signed_unverified`, retaining
encrypted, immutable bytes. It grants no release, download or delivery.
`reconcile_signature` verifies outside the transaction and revalidates current
document, authority and approval before completion. Unsigned/tampered bytes,
wrong/broken chains, revoked or expired certificates and invalid timestamp
proofs fail closed. An unavailable revocation source returns `reconcile` and preserves
the same candidate for that worker command, never re-signing it. Negative
verification quarantines the candidate in `failed`; it never exposes bytes.
Recorded fixtures reach only `rehearsal_complete`, never `issued`. Neither
the existing synthetic envelope check nor this synthetic PKI establishes
qualified ICP-Brasil/PAdES interoperability. Live signing and
verification both require activated lifecycle records and live-runtime
approval; a real partner/API/trust policy remains BLOCKED-ON-EG-1/EG-5.

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

Patient releases default to `restricted_from_delegates=True`. The issuer may
explicitly choose a shareable release when creating it; that classification is
immutable. All three patient resolvers, including the keyed list and direct byte
export, recheck the active delegate grant and omit restricted releases. Unknown
and restricted document selectors both return the same denial. The patient's
own access and public status-only verification retain their existing boundaries.

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
