# Consent

Synthetic, purpose-specific consent, notices and disclosure records are
implemented; no live legal approval is implied. Consent here is never the
sole legal-basis claim for care processing. The supported purposes are
`teleconsultation`, `consultation_recording`, `ai_assistance`,
`transactional_messaging`, `marketing` and `research_model_improvement`,
all in `pt-BR`. Channel preferences also remain in intake and are never
inferred from this consent. Care records and explicit record releases do
not depend on consent, and refusing or revoking any purpose never blocks
care.

- Clinic owners/admins publish the complete clinic text as a new immutable
  version per purpose, through the staff consent screen or bounded clinic
  settings. Both use the same publisher and reject HTML, scripts and CSS at
  the service/database boundaries. Publication and acceptance serialize on
  the same clinic/purpose lock. A text changed after presentation causes a
  conflict, not acceptance of unseen terms.
- `NoticeVersion` publishes information-only notices (care data processing,
  consultation recording, use of AI in care, teleconsultation). A notice
  informs; it is never a consent and never satisfies a consent gate.
- Only a live patient session with the `consent` operation can accept,
  refuse or revoke. Signed, 30-minute offer tokens bind the displayed
  version/digest/purpose to the exact session without exposing its
  identifier. Staff cannot impersonate a patient.
- Text is preserved byte-for-byte as UTF-8 with a SHA-256 digest, version,
  language, purpose, clinic, publisher and timestamp. Receipts retain
  patient, enrollment, exact text reference, session authority,
  explicit-action marker and timestamp. No IP address, user agent,
  geolocation, free-form reason or device fingerprint is collected.
  Session identifiers are internal provenance, not receipt UI content.
- A deliberate refusal appends an immutable `RefusalRecord` bound to the
  displayed version. Refusing an actively accepted text is rejected: the
  patient revokes instead. Accepting a previously refused version is
  allowed; each event stays in history.
- Revocation appends an immutable event referencing the acceptance and
  revoking patient session. Retries return the same
  acceptance/revocation/refusal; a revoked version cannot be reactivated
  by replay. A new consent requires a new text version and a fresh
  explicit action. No consent action mutates EHR records, releases or
  messages.
- `ParticipantAcknowledgment` records that the clinician delivered the
  recording notice to a non-patient voice (caregiver, companion,
  interpreter) present in a clinical session, without creating a patient
  record for them. `AIUseDisclosure` is the per-encounter clinician
  attestation that the patient was informed about AI assistance, plus the
  recorded refusal; one immutable row per encounter.
- `consent_for_future_use(*, clinic_id, enrollment_id, purpose)` returns
  the exact current, unrevoked acceptance for any purpose under authorized
  clinic scope, or `None`; a purpose outside the taxonomy is rejected.
  `ai_disclosure_status(*, clinic_id, encounter_id)` returns the recorded
  disclosure or `None`; downstream AI consumers treat `None`, not informed
  or refused as unavailable. Both must be checked at the point of use,
  never cached. Publishing a new version requires fresh acceptance for
  future use, while prior receipts remain readable.
- FORCE RLS and binding triggers independently check roles and patient
  provenance. Runtime UPDATE/DELETE privileges are absent; triggers reject
  maintenance-role edits/deletes too. All record classes have indefinite
  retention and no disposal path, including on revocation or refusal; no
  automatic purge or legal approval is implied.
- Fixed metadata-only publication/acceptance/refusal/revocation/notice/
  disclosure/read audit events join the tenant chain; patient event actors
  are patient-session identifiers, never staff.

Patient surface: `/patient/consent/` (texts, notices, acceptances,
refusals, revocations). Staff surface: `/clinics/<clinic_id>/consent/`
(publish texts and notices, record AI-use disclosures and participant
acknowledgments, inspect receipts and refusals). Patient/enrollment/
encounter identifiers and offer tokens travel in POST bodies, never URLs;
responses are private/no-store. The native pt-BR forms work without
JavaScript and never preselect acceptance.

QA: `uv run --frozen --no-sync --no-env-file pytest --reuse-db -q
 tests/renewal/test_consent.py tests/consent/
 tests/infra/test_module_boundaries.py` then
`uv run --frozen --no-sync --no-env-file python -m ops.testing.renewal_runner
 browser --suite consent` in the task-owned database environment.
