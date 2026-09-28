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
  `section_edit_epochs` counts acknowledged edits per section for todo 42.
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
- Migration `0010` is additive and rehearsed forward and backward; once an
  unscheduled encounter exists, rollback is a restore.

Verification: `tests/renewal/test_encounters.py`, `tests/ehr/test_autosave.py`,
`tests/renewal/test_attachments.py` and renewal browser suites `encounter` and
`attachments`. All fixtures and browser evidence are synthetic-only.
