# EHR

Task 21 implements assigned-physician encounters, immutable specialty-template
versions and explicit SOAP draft saves. Open an appointment from the physician
agenda; PostgreSQL uniqueness and locking converge parallel starts on one encounter
and SOAP version. Saving compares the submitted revision atomically and retains
prior data on failure. Clinical content is never autosaved or stored in browser
storage. Selection uses server-side session state and POST bodies, not patient URLs.

`services.record_clinical_note` requires an exact version and expected revision.
Services and FORCE RLS both enforce canonical clinical roles and care relationships;
reception and administrators have metadata access only. Submitted intake references
point to immutable `QuestionnaireEvent` receipts, not mutable response answers.
Clinical audit events contain record identifiers and fixed reason codes only.

The binding lifecycle is `docs/clinical/record-contract.md`. Publishing a
specialty template is an admin/owner service, exposed through the clinic settings
screen rather than the encounter screen. Its four SOAP prompts and title accept
only bounded plain text; HTML, scripts and CSS are rejected by the service and
database. Each publication appends fixed metadata-only audit evidence. Existing
drafts and finalized versions keep their original template reference.
External interoperability remains the separate, deferred `apps.interop` boundary.

Task 24 adds finalization, amendments, discard and encounter closure.
`finalization.finalize_version` freezes a draft with a fixed SHA-256 content
digest over the canonical template binding and SOAP fields; the workspace
requires a recent step-up verification first and audits `step_up_required`
denials. `amend_document` opens a linked draft carrying reason, author and
time; finalizing it supersedes the base, which is never overwritten.
`discard_draft` retires unwanted drafts and `close_encounter` closes the
encounter only when no live draft remains; both finalize and close are
idempotent. Database triggers admit only these transitions, so in-place or
stale writes cannot alter finalized content. Local finalization is not a
provider-verified digital signature; signing is a later capability. Patient
release remains downstream work.

Task 23 adds clinical attachments. Uploads validate declared and detected type
(PDF/JPEG/PNG, at most 10 MiB, active markup and archives refused), store bytes
under an opaque key in the bounded synthetic object store, and stay `quarantined`
until the configured scanner marks them `available` or `rejected`. Downloads are
bounded non-streaming responses authorized by the assigned-physician or
care-relationship predicate; quarantined and rejected bytes are never served and
object keys are never identifiers. Each upload writes a durable pending receipt
before the object and clears it on outermost-transaction commit, so a request
rollback or interrupted cleanup leaves tracked bytes;
`reconcile_pending_uploads` removes receipted objects with no committed row and
must run as `clinic_owner` (or a superuser/BYPASSRLS role) when no upload is in
flight. The synthetic scanner cannot approve production uploads; real use
requires approved storage, encryption and scanning capabilities.

Verification: `tests/renewal/test_encounters.py`,
`tests/renewal/test_attachments.py` and renewal browser suites `encounter` and
`attachments`. All fixtures and browser evidence are synthetic-only.
