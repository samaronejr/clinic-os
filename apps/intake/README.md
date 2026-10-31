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

## Duplicate review and retained lineage

`DuplicateCandidate` retains an ordered same-clinic enrollment pair with
`exact_identifier`, `dob+name_prefix` or `contact` signals. Creation and updates
compare purpose-bound tenant HMAC snapshots, including every active DEK version;
name/DOB prefixes use six normalized characters. Migration `0016` seeds existing
encrypted registrations. Signals are hints, never a block or an automatic merge.

`adjudicate_duplicate(*, clinic_id, candidate_id, decision,
survivor_patient_id, field_choices, reason)` requires a manager or physician
assignment, `demographics.read` and a genuinely persisted, fresh verified session.
Legacy clinic-admin assignments retain manager eligibility; an owner assignment
alone does not gain demographics access. `duplicate_review_session` binds the
framework-owned session key, and the services and triggers revalidate its hash.
Each field choice names one of the pair's patients and the exact version reviewed.
Only a new survivor demographics version receives the choices. Its existing
same-transaction mirror guard remains the sole permitted registry UPDATE path.

`MergeDecision` is immutable, with encrypted reason and choices. Active
`PatientAlias` edges resolve through `clinic_app.patient_alias_set(patient uuid)`;
the workspace, search, finalized timeline, released portal history and exports
retain original bindings and show their unified date. Search matches retained
identities but selects the current surviving enrollment. Clinical care, same
clinic, explicit release and live patient-operation gates still apply. Consent
history projects aliases for reads only; a historical receipt never supplies a
new consent or a new permission.

New appointments, encounters and invoices reject an aliased target with
`patient_aliased` in services and a native insert trigger. Replaying an existing
operation still returns its historical identity. Historical encounters,
finalized SOAP digests, invoice references, signed/rendered documents, consent
receipts and audit rows are never repointed. Invalid, unknown and foreign merge
selectors share the `merge_cross_org` refusal without a record-existence hint.

The human must hold review authority in every affected enrolled clinic before a
merge begins. Per-app handlers cancel/recreate open unencountered bookings and
open waitlist entries with immutable links. Contacts retain originals; fresh
copies are unverified. Old preferences are opted out without inferring a new
opt-in. Old grants and sessions are revoked; fresh codes are returned once in
memory, never persisted or audited. Existing survivor choices are retained.

`unmerge(*, clinic_id, merge_decision_id, reason)` deactivates an edge under the
same role and session proof. Later survivor rows stay on the survivor.
`merge_review` lists those appointments, encounters, invoices and retained
forward links through their unchanged domain RLS for individual review. Native
forms live at the clinic's `patients/duplicates/` and `patients/timeline/` routes;
all selectors and reasons remain in POST bodies.

## Patient portal v2 (todo 50)

`/patient/` is a mobile-first hub built by `portal.py`. Each destination in
`DESTINATIONS` names exactly one session operation, and a destination whose
operation the live session does not hold is absent, never a placeholder. The
home keeps the v1 element contract (`#patient-name`, `#patient-clinic` and the
`#*-link` destination ids). Its summaries read only through the existing
patient-session resolvers: the next upcoming appointment in clinic time
(`patient_appointments`), unsubmitted questionnaires (`patient_responses`),
open released charges (`patient_ledger`), and whether the session acts for a
represented patient (`clinic_app.patient_is_delegate`). A resolver that
refuses between the overview read and a summary read hides that summary. The
portal stores nothing in the browser, and the service worker contract is
unchanged. `PATIENT_PORTAL_V2=false` serves the retained v1 home as the
rollback path.

The patient destinations (booking and waitlist offers, forms, teleconsult,
charges and one charge, documents, records, consent) render from views other
apps own, so their templates read the same switch through the
`{% patient_portal_v2 %}` tag (`templatetags/patient_portal.py`). Under v2 each
opens with `intake/partials/portal_back.html` (`[data-portal-back]`: one
`[data-portal-back-link]` to `/patient/`, or to the charge list from one
charge, and one `[data-portal-help-link]` to `#portal-help`), ends with
`intake/partials/portal_help.html` (the home's non-AI support copy as
`#portal-help[data-portal-help]`, which the access gate also shows), and drops the legacy
eyebrow and bottom back links; actions are full width below 48rem, record
lists keep the prose measure, and digests wrap. With the switch off, each
template renders its retained v1 bytes. Every id, `data-*` hook, form, action
name and heading is unchanged in both modes.

Integration boundaries: the messages destination (operation `messages`, added to
new invitations and to the delegate operation vocabulary by todo 50) opens
`/patient/messages/`, which the comms app owns together with todo 51's portal
threads; its contract is the Portal section of
[`apps/comms/README.md`](../comms/README.md). Like results, it is a v2-only page.
PSP payment links arrive inside the existing charge screens with todo 57. The
non-AI path to a person is on the home, on every destination and on the access
gate.

Results (`/patient/results/`, orders app) list only what
`clinic_app.orders_patient_sources` releases to the session's audience: the
current acknowledged version, with a "corrected version" mark after the first.
A download posts the version in the body, re-checks the resolver, verifies size
and SHA-256 and chains `orders.result.patient_downloaded` bound to the live
session (`clinic_app.orders_patient_download_audit`); unknown, unreleased and
malformed selectors render the same gate.

## Data-subject requests (LGPD art. 18, IS-19)

`DataSubjectRequest` (`data_request_models.py`) records access, correction,
portability, sharing information and objection requests. Patients and verified
delegates file them at `/patient/data-requests/` through
`clinic_app.submit_data_request`, which binds the live session, the subject
enrollment and the delegate grant, requires the operation each right needs
(`records` for access and portability, `consent` plus the delegate's purposes
for objection, `enrollment_view` otherwise), stamps `received_at` and a due date
15 days later from `clinic_app.data_request_now()`, and opens one `review` task
owned by the `clinic_manager` role with the same due date. Correction text is a
tenant envelope and the idempotency fingerprint is a tenant HMAC, so no
plaintext or plain digest of it is stored. A delegate sees only requests made
under its own grant; the patient sees every request about them.

Access and portability deliver the deterministic released-record export, so
records restricted from delegates never leave for a delegate. Correction never
edits clinical content: staff review it and any clinical change is a
clinician's amendment. An objection names one optional consent purpose
(`ConsentPurpose`, never care processing): accepted texts of that purpose are
revoked through the consent service and the request is fulfilled at once;
otherwise it waits for staff. Sharing information projects the provider
registry (capability, lifecycle state, `is_live`) without accounts or secrets.

Staff with `privacy.requests` (owner, org admin, clinic admin and clinic
manager; a catalog v2 extension held without a workflow bundle opt-in, as
`reception.queue` is, and narrowable by a v2 remove grant) answer at
`/intake/clinics/<clinic>/data-requests/` (Patients > Data-subject requests):
received -> in_progress -> fulfilled, or rejected with a fixed reason. The
database guard rejects every other transition, deletes and direct writes, and
a terminal answer closes the SLA task. Overdue is database time, and equality
counts as overdue. Every other role gets the same denial as an unknown request.

## Preferences, representation and notifications

`/patient/preferences/` keeps display choices (larger text, higher contrast)
in the server-side Django session only and summarizes the messaging channels
the clinic uses (`clinic_app.patient_channel_preferences`, never a
destination). Channel changes stay with the clinic team, who verify each
contact. The home lists the original identity's active, verified grants
(`clinic_app.patient_delegate_options`) and switches through the existing
POST-only `/patient/delegation/switch/`.

Browser notifications stay unavailable and are shown as such: no portal page
registers a service worker, asks for notification permission or keeps data in
browser storage, and `templates/clinic-os-sw.js` is unchanged. The only copy a
future notification may carry is `PUSH_NOTICE` ("Você tem uma nova mensagem da
clínica", no record data). Delivery needs a push handler in the service worker,
patient-session worker serving and a governed push capability (EG-1/EG-5); the
trigger, an accepted portal message, now exists.

Patients book by service type on the appointments screen (contract:
`tests/renewal/test_patient_service_booking.py`; mechanism in
`apps/scheduling/README.md`). The service choice travels in the POST body
(`action=slots`), never in a URL. Clinics that review self-bookings offer no
service choice: their patient rows are born `requested`, which todo 22 gave
service bookings no edges for.
