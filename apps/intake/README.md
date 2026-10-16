# Intake

Phase 1A implements organization-scoped patient identity, clinic enrollment,
idempotent registration, and paginated search through staff-only screens.
Patient search values stay in request bodies rather than browser URLs.

The workspace palette uses `search_patients_exact`, not the independent legacy
substring registry. Registration stores a tenant-DEK HMAC in `full_name_index`;
case, accent and whitespace folding are versioned by the
`intake.patient.full_name.exact.v1` purpose. Lookup computes a digest for every
DEK version, selects by indexed ciphertext metadata, orders by enrollment id,
and reveals only bounded exact matches. Migration `0012_patient_name_index`
backfills existing encrypted names one at a time in the owner transaction.
No plaintext shadow, decrypted predicate, or decrypt-and-filter fallback exists
on the palette path. Raw owner fixture inserts must supply the digest to be
searchable by the palette; production registration always supplies it.

Name search keys are exactly legal name and social name: the staff
registry substring page matches `full_name` and the latest demographics
`social_name` only. `preferred_name` is a display/demographics field and
is not a search key; a preferred-name-only query returns no rows by
contract, not by defect.

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

Patient representation is owned by `identity.PatientDelegateGrant`, not patient
contacts. Its fixed database commands require staff verification of scanned
legal evidence before activation. Patient sessions retain their original
identity and bind `acting_for_enrollment_id` to a verified, expiring grant.
`clinic_app.current_patient_authority` supplies the effective enrollment and
operation intersection to every patient surface. Revocation and expiry deny
already-minted sessions on their next use.

The POST-only `/patient/delegation/switch/` trusts only the stored grant.
Expired representation offers a signed renewal request at
`/patient/delegation/renew/`; the new grant is pending verification and does not
restore access. Restricted releases and questionnaire responses default to
deny for delegates, and consent remains purpose-specific. Legal authority and
confidentiality exceptions require EG-2 human review; synthetic evidence is not
legal approval. The complete contract is in
[`docs/clinical/patient-access.md`](../../docs/clinical/patient-access.md).

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

Provenance surfaces: the demographics screen labels every correction row
with its source (`patient_reported` renders "Patient-reported") and marks a
current patient-reported version "Reported by the patient; not verified by
staff." Any authorized reader, physicians included, sees the same labels; the
physician encounter view carries document provenance (template version,
document version, revision, amendment chain, finalization timestamp, content
digest and linked intake submission references) and no separate physician
provenance panel exists for demographics — that is the contract, not a gap.
