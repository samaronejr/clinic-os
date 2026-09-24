# Consent

Synthetic, purpose-specific consent is implemented; no live legal approval is
implied. The only supported purpose is `teleconsultation`, with language `pt-BR`.
Message/marketing preferences remain in intake and are never inferred from this
consent. Care records and explicit record releases do not depend on consent.

- Clinic owners/admins publish the complete clinic text as a new immutable version,
  through the staff consent screen or bounded clinic settings. Both use the same
  publisher and reject HTML, scripts and CSS at the service/database boundaries.
  Publication and acceptance serialize on the same clinic/purpose lock. A text
  changed after presentation causes a conflict, not acceptance of unseen terms.
- Only a live patient session with the `consent` operation can accept or revoke.
  Signed, 30-minute offer tokens bind the displayed version/digest/purpose to the
  exact session without exposing its identifier. Staff cannot impersonate a patient.
- Text is preserved byte-for-byte as UTF-8 with a SHA-256 digest, version, language,
  purpose, clinic, publisher and timestamp. Receipts retain patient, enrollment,
  exact text reference, session authority, explicit-action marker and timestamp.
  No IP address, user agent, geolocation, free-form reason or device fingerprint is
  collected. Session identifiers are internal provenance, not receipt UI content.
- Revocation appends an immutable event referencing the acceptance and revoking
  patient session. Retries return the same acceptance/revocation; a revoked version
  cannot be reactivated by replay. A new consent requires a new text version and a
  fresh explicit action. No consent action mutates EHR records, releases or messages.
- `consent_for_future_use` returns the exact current, unrevoked acceptance under
  authorized clinic scope, or `None`. Downstream consumers must check at the point
  of use, never cache the result. Publishing a new version requires fresh acceptance
  for future use, while prior receipts remain readable.
- FORCE RLS and binding triggers independently check roles and patient provenance.
  Runtime UPDATE/DELETE privileges are absent; triggers reject maintenance-role
  edits/deletes too. All three record classes have indefinite retention and no
  disposal path, including on revocation; no automatic purge or legal approval is
  implied. Task 25's EHR policy/export interface is not a consent disposal interface.
- Fixed metadata-only publication/acceptance/revocation/read audit events join the
  tenant chain; patient event actors are patient-session identifiers, never staff.

Patient surface: `/patient/consent/`. Staff surface:
`/clinics/<clinic_id>/consent/`. Patient/enrollment identifiers and offer tokens
travel in POST bodies, never URLs; responses are private/no-store. The native
pt-BR forms work without JavaScript and never preselect acceptance.

QA: `uv run --frozen --no-sync --no-env-file pytest --reuse-db -q
 tests/renewal/test_consent.py tests/test_module_boundaries.py` then
`uv run --frozen --no-sync --no-env-file python -m ops.testing.renewal_runner
 browser --suite consent` in the task-owned database environment.
