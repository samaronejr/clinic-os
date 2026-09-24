# Clinical record ownership and lifecycle contract

This document is the binding contract for clinical records. It is written
before the schema and service work it governs (renewal tasks 21 through 30);
the code must match these rules exactly. It defines who may act on a record,
which states records move through, what each action requires and produces, and
what happens when an action fails. It creates no runtime behavior by itself.

Only synthetic data is authorized. The live-data gate remains binding; this
contract does not approve real patient data, and no fixture is an approval
record.

## Objects and ownership

- **Appointment** (existing, unchanged): `scheduled` or `cancelled` in
  `apps/scheduling/models.py`. Cancellation is terminal. This contract adds no
  appointment states and changes no appointment behavior.
- **Encounter**: exactly one per appointment. Its identity binds the
  appointment, patient, clinic, organization and assigned physician. States:
  `open`, `closed`. Closed is terminal; corrections happen through document
  amendments, never by reopening.
- **Clinical document**: a versioned record inside one encounter. The first
  kind is the SOAP note; later kinds follow the same lifecycle. Each version is
  `draft`, `finalized`, `superseded` or `discarded`. A document's display state
  is `draft` until a first version is finalized, `finalized` while the current
  version is the first finalized one, and `amended` once a later version is
  current. A document whose only versions are discarded still displays `draft`;
  it has no finalized content. At most one draft version exists per document
  at a time.
- **Document release**: a separate record naming one finalized or superseded
  version released to the patient. Releases are never implicit.
- **Amendment**: a new draft version linked to the finalized version it
  corrects, carrying a reason, its author and its time. Finalizing it
  supersedes the base version. The base is never overwritten. An amendment
  draft is a new version of an existing document, not a new document; closing
  an encounter forbids only new documents, so amendments remain possible on a
  closed encounter.

State separation rule: appointment state and record state are independent.
Cancelling an appointment never changes, hides or deletes an encounter or
document, and record state never changes an appointment. An existing record
survives later cancellation.

## Principals and access predicates

Every staff predicate requires an authenticated user with a current
`UserClinicRole` assignment for the exact clinic, inside the existing tenant
boundary (`app.current_tenant`, FORCE RLS). Role checks read the canonical
assignment table only; client-supplied clinic, user or role claims are never
authority. Predicates are enforced in services and in PostgreSQL policies, the
same double check used for questionnaires.

| Principal | Predicate | Clinical content | Logistics |
| --- | --- | --- | --- |
| Assigned physician | `physician` role for the clinic AND `user_id = appointment.practitioner_id` | read and write the encounter and its documents, all non-discarded versions; discarded versions expose metadata only, never content | yes |
| Same-clinic physician with care relationship | `physician` role for the clinic AND (assigned to any appointment of this patient at this clinic OR author of a record for this patient at this clinic) | read finalized and superseded versions and history; no drafts; no writes to another physician's encounter | yes |
| Receptionist | `receptionist` role for the clinic | none | appointment status, encounter existence and state, document workflow state; never content |
| Clinic admin / owner | `clinic_admin` or `owner` role for the clinic | none | same logistics as receptionist |
| Patient | live patient session bound to this patient and clinic, holding the `records` operation | released document versions only | own appointments under scheduling policy |
| Any other principal | no matching predicate | none | none |

Three consequences follow. Owner or admin authority never confers clinical
access, matching the questionnaire rule. Drafts are author-only: no other
principal, including a same-clinic physician, reads a draft. Patient access
requires an explicit release per version; a later amendment does not extend an
earlier release, and a superseded released version stays readable, marked
superseded.

## Action contract

Every action lists its principal predicate, preconditions, transition, audit
event and failure behavior. Successful transitions append the named event to
the tenant audit chain. Denials that reach a clinical object append
`ehr.access.denied` with a fixed `reason_code`; failures before the tenant
boundary (anonymous, malformed, cross-tenant) fail closed with no event, as
today. Audit payloads stay metadata-only: record ids and reason codes, never
clinical content or patient identifiers. Cross-clinic failures are
non-enumerating: they return the same response as "does not exist".

### Open encounter

- Principal: assigned physician.
- Preconditions: the appointment exists in this clinic, its status is
  `scheduled`, `appointment.practitioner_id` equals the actor, and the
  patient's enrollment belongs to this clinic.
- Transition: create the encounter `open`, or return the existing one.
  Parallel opens converge on one row; one encounter per appointment is a
  database guarantee, not a convention.
- Audit: `ehr.encounter.opened`.
- Failure: cancelled appointment is a conflict (`precondition_failed`), no
  encounter is created and nothing is deleted. Any other principal is denied
  (`role_denied`, `not_assigned` or `cross_clinic`).

### View encounter or document content

- Principal: assigned physician (all non-discarded versions); same-clinic
  physician with a care relationship (finalized and superseded versions only);
  patient (released versions only).
- Transition: none.
- Audit: `ehr.record.viewed` for clinical content reads. Receptionist and
  admin logistics reads use the existing scheduling view events and produce no
  clinical event.
- Failure: denied (`role_denied`, `no_care_relationship`, `not_released`,
  `cross_clinic`); no content returned.

### Create document draft

- Principal: assigned physician.
- Preconditions: encounter is `open`.
- Transition: new document, version 1, state `draft`.
- Audit: `ehr.document.draft_created`.
- Failure: closed encounter is a conflict (`encounter_closed`).

### Save draft

- Principal: the draft's author, who must still satisfy the assigned-physician
  predicate.
- Preconditions: the version is `draft` and the client's expected revision
  matches the stored one.
- Transition: content stored, revision incremented atomically.
- Audit: `ehr.document.saved`.
- Failure: stale revision is a conflict (`stale_revision`) and writes nothing;
  a non-draft version is a conflict (`precondition_failed`).

### Finalize document version

- Principal: the draft's author, who must still satisfy the assigned-physician
  predicate.
- Preconditions: the version is `draft`, recent step-up verification holds
  under the existing 300-second window, and required content is present.
- Transition: the version becomes `finalized` with a fixed content digest and
  timestamp; if it is an amendment, its base version becomes `superseded`.
- Audit: `ehr.document.finalized`.
- Failure: missing or stale step-up is denied (`step_up_required`); missing
  required content is a conflict (`missing_required_content`) and the version
  stays `draft`; a `discarded` or `superseded` version is a conflict
  (`precondition_failed`); repeating finalize on the same
  version returns the existing finalized result, never a second version; a
  stale amendment base is a conflict (`stale_revision`).

### Amend document

- Principal: assigned physician.
- Preconditions: the document has a finalized current version, no `draft`
  version exists for the document, and the encounter may be open or closed.
- Transition: a new draft version linked to the current version, carrying
  reason, author and time. It then follows the normal save and finalize path.
- Audit: `ehr.document.amended`.
- Failure: no finalized version is a conflict (`precondition_failed`); an
  open draft is a conflict (`draft_in_progress`); a stale base is a conflict
  (`stale_revision`). Amendment by anyone other than the assigned physician is
  not in the synthetic contract; delegated amendment needs clinical-owner
  approval first.

### Discard draft

- Principal: the draft's author, who must still satisfy the assigned-physician
  predicate.
- Preconditions: the version is `draft`.
- Transition: the version becomes `discarded`, a terminal housekeeping state.
  Discarded versions are retained for audit and never served as content.
- Audit: `ehr.document.discarded`.
- Failure: a non-draft version is a conflict (`precondition_failed`).
- Rationale: this gives a deterministic way to close an encounter without
  finalizing an unwanted draft. Whether discard is permitted at all is a
  clinical-owner decision listed below.

### Release document version to patient

- Principal: assigned physician.
- Preconditions: the version is `finalized` or `superseded`; the encounter may
  be open or closed.
- Transition: a release record is created for that exact version.
- Audit: `ehr.document.released`.
- Failure: a draft or discarded version is a conflict (`precondition_failed`);
  any other principal is denied.

### Revoke release

- Principal: assigned physician.
- Preconditions: the release exists.
- Transition: the release is marked revoked; patient access to that version
  ends. The document itself is unchanged.
- Audit: `ehr.document.release_revoked`.
- Failure: an already revoked release returns the existing revoked release
  unchanged; an unknown release id is denied (`not_found`), the same
  non-enumerating response as a nonexistent record, and nothing changes; any
  other principal is denied.

### Close encounter

- Principal: assigned physician.
- Preconditions: the encounter is `open` and no live draft remains; every
  document version is `finalized`, `superseded` or `discarded`.
- Transition: the encounter becomes `closed` with a timestamp.
- Audit: `ehr.encounter.closed`.
- Failure: an open draft is a conflict (`draft_in_progress`); repeating close
  returns the existing closed encounter. After close, creating a new document
  is a conflict (`encounter_closed`); amendment drafts on existing finalized
  versions and releases remain allowed, and the encounter stays `closed`
  throughout.

### Cancel appointment (existing action, unchanged)

- Principal: the existing scheduling authority in
  `apps/scheduling/appointment_cancellation.py`: a staff actor holding a
  manager role (`clinic_admin`, `owner` or `receptionist`) for the
  appointment's clinic, or a live patient session whose clinic, organization
  and patient match the appointment.
- Preconditions: the appointment exists inside the actor's tenant and clinic
  scope; for the patient path it belongs to the session's patient. The reason
  is one of the fixed `Appointment.CancellationReason` values; the patient
  path always uses `patient_request`.
- Transition: the appointment becomes `cancelled` with the reason and
  timestamp. Cancellation is terminal; no action reopens it.
- Audit: the existing `scheduling.appointment.cancelled` event on the staff
  path; the patient path keeps its existing database-owned receipt.
- Failure: a reason outside the fixed vocabulary is rejected as invalid input
  (`AppointmentCancellationInputError`); an already cancelled appointment
  returns the existing record when the reason matches and is a conflict
  (`AppointmentCancellationConflictError`) when it differs; an unknown,
  out-of-scope or cross-clinic appointment is denied non-enumerating
  (`AppointmentAccessDeniedError`), identical to "does not exist".
- Record effect: none. An existing encounter and its documents survive
  unchanged; the cancelled appointment can never open a new encounter.

## Concurrency and optimistic versions

- Every writable record carries an integer revision. Writes compare the
  client's expected revision; a mismatch is a `stale_revision` conflict and
  writes nothing. The client reloads the current version.
- Uniqueness lives in the database: one encounter per appointment, at most
  one draft version per document, one current version per document.
- Finalize and close are idempotent: a retry returns the first result rather
  than creating a second version or reopening a closed encounter.
- A failed save leaves the prior state intact. Unsaved edits are shown as
  unsaved; the UI never implies an unpersisted note was stored.

## Consent effects

- Consent receipts are immutable. Revocation prevents future use for that
  purpose and never deletes clinical history (task 26).
- Teleconsultation consent gates teleconsult sessions only (task 27).
  Revoking it blocks new sessions; it does not close encounters, hide
  documents or revoke releases.
- Record access does not depend on consent state. Care records rest on the
  care relationship; patient access rests on the physician's release action.
- Staff can never record patient consent on the patient's behalf; consent
  authority is the patient's own action.

## Failure behavior summary

| Case | Outcome |
| --- | --- |
| Receptionist or admin reads clinical content | denied, `role_denied`; logistics metadata only |
| Other-clinic or cross-tenant access | denied, non-enumerating; RLS fails closed |
| Open encounter on a cancelled appointment | conflict `precondition_failed`; no encounter; nothing deleted |
| Appointment cancelled after the encounter opened | appointment `cancelled`; encounter and documents unchanged; assigned physician may still finalize, amend and close |
| Stale revision on save or amend | conflict `stale_revision`; nothing written; client reloads |
| Storage failure during save or finalize | prior state retained; edits shown as unsaved |
| Finalize without step-up | denied `step_up_required` |
| Finalize with missing required content | conflict `missing_required_content`; version stays `draft` |
| Finalize a discarded or superseded version | conflict `precondition_failed`; version unchanged |
| Double finalize or double close | idempotent return of the first result |
| Amend with an open draft or stale base | conflict; existing versions untouched |
| New document on a closed encounter | conflict `encounter_closed`; amendment drafts on existing documents still allowed |
| Revoke an unknown release | denied `not_found`, non-enumerating; nothing changes |
| Repeat cancel with the same reason | idempotent return of the cancelled appointment |
| Repeat cancel with a different reason | conflict `AppointmentCancellationConflictError`; terminal state unchanged |
| Delete any record | not an operation; no principal can delete; retention and holds govern disposal (task 25) |

## Synthetic defaults

- One encounter per appointment, opened only by the assigned physician while
  the appointment is `scheduled`.
- No time-of-day gate on opening in the synthetic contract. A clinic-local
  opening window is a clinical-owner decision.
- Step-up window: the existing 300-second recent-verification default.
- Amendment reason: nonblank, at most 255 characters.
- Patient release: explicit per version; patient sessions need the `records`
  operation; nothing is released by default.
- No automatic purge. Retention defaults to no disposal without an approved
  policy (task 25).
- Attachment defaults (type allowlist, size cap, quarantine states) are owned
  by task 23 and inherit this contract's access predicates.
- All data is synthetic.

## Validation required before live use

This contract is a synthetic default, not a clinical or legal decision. Before
any live use, the accountable clinical and legal owners must review and
approve, at minimum:

- the care-relationship definition for same-clinic history access;
- whether encounters may be completed after appointment cancellation, and any
  opening time window;
- whether amendment is restricted to the assigned physician or may be
  delegated;
- whether draft discard is permitted and how discarded versions are retained;
- release scope, patient-facing wording and revocation semantics;
- retention periods, hold authority and export scope under the live-data
  gate;
- the audit vocabulary additions (`ehr.*` events and denial reason codes).

Each approval must name the accountable owner, scope, date and evidence hash,
matching the live-data gate's requirements. Until then, every record class
here remains synthetic-only.
