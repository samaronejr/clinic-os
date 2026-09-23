# Intake

Phase 1A implements organization-scoped patient identity, clinic enrollment,
idempotent registration, and paginated search through staff-only screens.
Patient search values stay in request bodies rather than browser URLs.

Contacts and messaging preferences extend the same boundary: destinations
are organization-level patient data verified per destination version,
preferences are clinic-scoped versioned opt-in states per purpose and
channel, and every change appends to an immutable history. Destinations
render masked everywhere except the explicit edit screen; the enrollment
travels in POST bodies, never in URLs. Automated sends re-check the
preference and the destination verification under the operation's
subject lock immediately before the external call, and preference or
destination mutations hold the matching transaction lock until they
commit. Sends and mutations also serialize on a stable patient-channel
boundary lock, so a preference created while a destination change is
in flight cannot escape the invalidation: a committed revocation can
never be overtaken by an in-flight send and an unverified or opted-out
destination can never receive an automated message. Verification is the
explicit staff-attested synthetic method; no identity is inferred from a
shared destination and no legal basis is inferred from a preference.

Patient access adds enrollment-bound invitations and sessions: staff issue
a single-use 24-hour code whose SHA-256 hash is the only persisted form,
redemption through `clinic_app.redeem_patient_invitation` consumes the
grant and mints a session bound server-side to organization, clinic,
patient, enrollment and the granted operations. Patient requests never
enter the staff tenant context — no tenant GUC is set, so tenant policies
fail closed — and patient reads go through resolver functions that
re-validate the session row (30-minute idle, 8-hour absolute, revocable).
The policy defaults live in
[docs/clinical/patient-access.md](../../docs/clinical/patient-access.md).

The public service facade is `services.py`; HTTP routes are in `urls.py`.
Questionnaires now retain immutable typed template versions, validate patient
saves/submissions, and preserve append-only clinical reopen history. Sensitive
response RLS is patient-session/physician scoped; reception uses a separate
completion-only resolver. See [questionnaire policy](../../docs/clinical/questionnaires.md).
All current patient workflows are
synthetic-only; see [the architecture](../../docs/ARCHITECTURE.md) and
[the live-data gate](../../docs/compliance/LIVE-DATA-GATE.md).
