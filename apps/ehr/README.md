# EHR

Task 21 implements assigned-physician encounters, immutable specialty-template
versions and explicit SOAP draft saves. Open an appointment from the physician
agenda; PostgreSQL uniqueness and locking converge parallel starts on one encounter
and SOAP version. Saving compares the submitted revision atomically and retains
prior data on failure. Clinical content is never stored in browser storage;
autosave is server-side only (below). Selection uses server-side session state
and POST bodies, not patient URLs.

Appointment lifecycle v2 (todo 22, D-9): a scheduled-bound encounter opens only
when its appointment is `arrived` or `in_progress`; the service predicate and
the database binding guard changed in the same migration. Appointment
corrections never touch finalized notes.

`services.record_clinical_note` requires an exact version and expected revision.
Services and FORCE RLS both enforce canonical clinical roles and care relationships;
reception and administrators have metadata access only. Submitted intake references
point to immutable `QuestionnaireEvent` receipts, not mutable response answers.
Clinical audit events contain record identifiers and fixed reason codes only.

The binding lifecycle is `docs/clinical/record-contract.md`. Publishing a
specialty template is an admin/owner/physician service, exposed through the clinic settings
screen rather than the encounter screen. Its four SOAP prompts and title accept
only bounded plain text; HTML, scripts and CSS are rejected by the service and
database. Each publication appends fixed metadata-only audit evidence. Existing
drafts and finalized versions keep their original template reference.
External interoperability remains the separate, deferred `apps.interop` boundary.

Todo 29 adds schema-validated v2 definitions alongside the exact four-key v1
contract. The v2 schema is `docs/clinical/template-schema-v2.json`; publication
accepts typed fields, units/ranges, explicit optional sections and clinic or
publisher-bound clinician phrases, never defaults or executable content.
`draft`, `published` and `retired` are immutable version snapshots; only
published snapshots bind new notes. Typed definitions never populate findings.
The template-specific trigger is isolated from the encounter binding guard.
The detailed definition and branching contracts are in
`docs/clinical/questionnaires.md`.

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

Plan item 27 (ADR-004, SD-8a) adds durable server autosave, conflict compare,
episodes and unscheduled encounters.

- Autosave: `autosave.autosave_draft(*, clinic_id, version_id,
  expected_revision, editor_command_id, editor_session_id, sections,
  request_handover=False)`, exposed as `POST /api/ui/v1/ehr/autosave/`
  (`{clinic_id, version_id, expected_revision, editor_command_id,
  editor_session, sections, handover}` returns `{revision, saved_at, lock}`, or
  409 `{current_revision, diff}`). It is authorized exactly like an explicit
  save and writes only through the unchanged `record_clinical_note` (H-2).
  `editor_command_id` is idempotent: a receipt binds it to the SHA-256 of the
  request, so a retry replays the acknowledgement and a reused key with other
  content is refused (`idempotency_mismatch`). A stale revision writes nothing
  and returns a per-section comparison; the author merges explicitly
  (`action=merge` without JavaScript). There is no last-write-wins.
- One editor session (tab) holds the draft lock; each autosave renews it for
  two minutes. Another tab gets `locked_by_other` and may request a handover,
  which the holder's next autosave grants after saving its own text.
- The lock is advisory by design: it coordinates the author's tabs, and the
  revision compare-and-set protects the text. An explicit save (or an
  autosave naming another tab's session) can write past the lock. That write
  still needs the current revision, so any other tab then gets the compare
  and cannot overwrite it.
- `section_edit_epochs` counts one accepted edit per changed section. It is
  advanced inside `record_clinical_note`, the single revision writer, so
  explicit saves, autosaves and merges all count (todo 42 relies on this).
- `static/js/ehr-autosave.js` debounces 1.5 s, resends an unacknowledged
  command unchanged after a network failure ("Não salvo - tentando
  novamente"), shows "Salvo às HH:MM (revisão N)" only after the server's
  acknowledgement, and marks a back-forward cache restore unconfirmed.
- `services.open_unscheduled_encounter(*, clinic_id, enrollment_id, reason)`
  opens a walk-in, phone follow-up or documentation-only encounter without an
  appointment. It needs `encounter.open_unscheduled` (bundle v2, registered
  physicians only) through `has_permission`; the insert policy re-checks it and
  the binding guard admits only an appointment-bound shape (arrived or in
  progress, no reason) or an unscheduled shape (closed reason, enrolled
  patient). Parallel starts converge on one open unscheduled encounter per
  physician and patient. `ehr_assigned` treats its opener as the assignee, so
  drafts, history, attachments, finalization and releases work unchanged;
  prescribing and teleconsult sessions refuse unscheduled encounters.
- `episodes.open_episode`, `close_episode` and `link_encounter` group one
  patient's encounters under a titled episode (title enveloped). They need
  `clinical.write` (reads `clinical.read`) for the patient's enrollment through
  `has_permission`; linking also needs the encounter's assigned physician, the
  same patient and clinic, and an open episode, re-decided by RLS and the
  binding trigger.
- Addendum drafts (`addenda.py`; `POST /api/ui/v1/ehr/addendum/open/` and
  `/addendum/autosave/`) carry other clinicians' contributions to an
  encounter:
  - Anyone other than the assigned physician who holds `clinical.write` for
    the encounter's patient in its clinic (a registered care-team physician
    today) opens one addendum draft per encounter, stored in
    `EncounterAddendum`. It never touches the main draft.
  - Autosave has the same CAS, idempotency receipts
    (`AddendumSaveReceipt`) and compare as the main draft. There is no lock,
    because the author is the only writer.
  - RLS (`has_permission`, author) and `ehr_addendum_guard` (same
    organization, clinic, patient; open encounter; never the assignee)
    re-decide every write. A new save command on a closed encounter is
    refused (`encounter_closed`) before its receipt, even when the text is
    unchanged; the guard binds receipts to an open encounter too (`0012`).
    Replaying an already acknowledged command stays a read.
  - Only the draft state exists. Later todos extend it: finalizing,
    releasing and showing addenda to readers, and a UI entry point from the
    patient workspace and timeline (todo 28). Typed addendum fields and
    templates come with todo 29, and nurse observations with todo 28
    (`observation.write`).
- Every writer decides every refusal, including the read scope of what its
  response renders, before its first write (the read audit included). The
  request transaction commits any response below 500, so a later refusal
  would keep the write. `services.author_scope` decides clinic scope,
  assignee and authorship with reads only before a draft is written;
  `services.assignee_scope` does the same for the amendment, which the
  assignee writes. Every handler (encounter, history and teleconsult notes
  workspaces, native or htmx) decides those scopes and any posted binding
  before `view_version` or `read_history` appends a read audit.
  `episodes.open_episode_for_encounter` opens and links an episode in one
  savepoint after all of its checks. The only row a refusal leaves is the
  fixed `ehr.access.denied` record.
- Migrations `0010`, `0011` and `0012` are additive and rehearsed forward
  and backward; `0012` only replaces the guard body and its reverse
  restores the `0011` body exactly. The reverse refuses while an unscheduled encounter exists, or while
  any of their tables holds a row (a `*_rollback_is_restore` probe), so
  rollback is a restore and never a silent drop.

Verification: `tests/renewal/test_encounters.py`, `tests/ehr/test_autosave.py`,
`tests/ehr/test_addenda.py`, `tests/ehr/test_write_ordering.py`,
`tests/ehr/test_refusal_matrix.py`,
`tests/renewal/test_attachments.py` and renewal browser suites `encounter` and
`attachments`. All fixtures and browser evidence are synthetic-only.

## Longitudinal patient workspace (todo 28)

The keyword-only public commands in `apps.ehr.longitudinal` take an explicit
clinic and enrollment. Observation entry allows current clinical-write or RP
observation scope; medication statements, reconciliation and confirmation
require clinical-write scope. Nursing/allied observation scope never grants
SOAP, medication reconciliation, manager or task-reassignment authority.

Medication details and observation values/reference ranges are enveloped from
creation. Local observation code-set version 1 binds each kind to one UCUM-like
unit and either an exact decimal string or a closed coded answer. Commas and
points are decimal separators, never mixed or inferred as thousands. BMI uses
only weight and height entered together, owned Decimal precision and half-even
rounding to 0.01. Encrypted fields are never SQL filters or sort keys.
`ProvenanceTag` is shared; immutable `origin` survives explicit confirmation.
Creation never confirms reported, observed, imported or AI-drafted information.
Medication status remains active/stopped/unknown.

### Projection and query-time authority

Root authorized the EHR durable-queue fallback: the existing workflow worker
re-enters its manager starter and requires reassignment, and no clinical
machine principal exists. Workflow schemas and clinician bundles stay closed.
Source triggers commit a scoped revision fence. A content-free on-commit wake
and periodic Beat scan recover broker loss. The fixed resolver-owned refresh
function locks a fence, upserts only source ids/kinds/timestamps/provenance,
and acknowledges that revision atomically. It cannot choose another source
scope or decrypt clinical text. Staff/patient/principal tenant contexts cannot
become the worker.

`timeline(*, clinic_id, enrollment_id, filters)` counts and pages only rows
admitted by current source RLS, including professional, care and restricted
scope. Filters are type, clinic-local date period and episode. Revocation never
waits for refresh. Rollback leaves no intent; duplicate workers cannot lose a
later revision.

### Restricted notes and reviewed reuse

`RestrictedNote` retains a document restriction across its versions.
`NoteAccessGrant` is an audited, revocable extra read requirement for non-authors,
not membership, professional eligibility, care scope or draft access. The
current author controls grants while retaining ordinary clinical read and
write scope. No role bundle supplies restricted read. Source version and
projection RLS hide both content and counts. Revocation is terminal and retained.

`reuse_prior_note` accepts `CopyNoteCommand` (section, reviewed revision, replay
id). A currently readable finalized/superseded source must match the draft's
patient and clinic. Copy uses the existing CAS/section-epoch writer, retains a
source receipt and inserts the translated `Copiado de <data>` marker. A
restricted source cannot enter an unrestricted draft. Every copied block needs
explicit author review at the current draft revision before finalization,
enforced in Python and SQL. Later edits invalidate review. Reuse never reviews
itself or changes its source.

### Surface, migration and verification

The native-form/HTMX workspace follows IA, with vitals, source badges, a
trend/table alternative, encounter/notes and contributions, and real existing
attachment and questionnaire entrypoints. Navigation uses g then a section
letter. Future results/care-plan/message captions are visibly disabled and
their selectors are refused, not backed by fabricated data or no-op commands.
Selectors remain POST-only; clinical values never enter browser storage.
English msgids ship with compiled pt-BR. Rejected forms retain input, roll back
read-audit effects and do not change patient selection.

Migration 0014 is non-atomic and irreversible; rollback = restore. Its explicit
bounded DDL transaction creates tables and installs their complete privilege/RLS
contract before visibility. The seven relations are in the exact live
posture/resolver/recovery census. Verification uses
`tests/ehr/test_longitudinal.py`, its timeline/reuse/posture companions and the
registered `patient-workspace` browser suite on all three engines. Root retains
full canonical, push and landing authority.
