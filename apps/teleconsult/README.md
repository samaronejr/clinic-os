# Teleconsult

Scoped teleconsultation sessions (renewal task 27). One `TeleconsultSession`
binds exactly one open encounter, its assigned physician, its patient and the
exact accepted consent text version. States are `waiting`, `active`, `ended`
and `failed`; terminal states never reopen.

Provider rooms are created only through the committed outbox
(`IntegrationOperation`, channel `video`, provider `teleconsult-synthetic-v1`,
subject `teleconsult.session`). The send-time recheck revalidates consent,
encounter and session state before any external effect, so a revoked consent
or closed encounter cancels the operation without a room. Room authority
derives from stored rows, never from external tenant identifiers.

Join access uses short-lived (15-minute) role-scoped credentials; only the
SHA-256 digest is stored. Only the bound physician or patient can exchange a
live credential for its room; expired, revoked, foreign-role or
foreign-participant tokens fail closed, and ended/failed sessions cannot be
re-entered. Recording and transcription are disabled at the database layer.

The synthetic room adapter requires `TELECONSULT_SYNTHETIC_PROVIDER`; the real
video capability remains unavailable pending the task-6 record
(`docs/integrations/records/2026-09-12-v1/video.md`). `TELECONSULT_SYNTHETIC_FAIL`
injects a transient provider failure for QA only.

## Clinician workspace (renewal task 29)

Joining a session as the assigned physician renders
`templates/teleconsult/clinician.html`: the patient's identity as the page
title, the video panel and the encounter notes panel side by side from 64rem
and behind an accessible tab switch below it (the hidden panel stays mounted,
so edits and the local stream survive switching). Note actions
(`note-template`, `note-save`, `note-finalize`, `note-discard`, `note-amend`)
post to the same clinic URL with the session id in the body;
`apps/teleconsult/workspace.py` re-validates the assigned-physician session,
binds the posted version to that session's encounter (a foreign version is
denied, never written) and delegates every write to the EHR services, which
own permissions, revisions, step-up and the document lifecycle. htmx swaps
only the notes panel or the session facts, so a save, a failed save, starting
or ending the video never restarts the camera or drops unsaved text; without
JavaScript the same forms re-render the whole page, and the start/end buttons
submit the notes form itself while a draft is on the page, so the typed text
comes back as unsaved instead of being discarded. A save response that lands
after further typing keeps the later text and shows it as unsaved. The version
shown is read through the EHR clinical read service, which authorizes the
assigned physician and appends `ehr.record.viewed` for exactly that version.
`Registro completo` posts the displayed encounter's identifier (body only) to
the EHR workspace's `show` action, which re-validates the assigned physician
and renders that record in the same response: the binding never passes
through the shared session selection, so another tab joining another room or
opening its own record in between cannot change which patient this action
shows. With unsaved text the exit is guarded; leaving anyway resubmits the
same button, so the chosen action is kept.
Ending or losing the video leaves the encounter open and the draft untouched,
and the surface says so.
