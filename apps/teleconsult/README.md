# Teleconsult

Scoped teleconsultation sessions (renewal task 27). One `TeleconsultSession`
binds exactly one open encounter, its assigned physician, its patient and the
exact accepted consent text version. States are `waiting`, `active`, `ended`
and `failed`; terminal states never reopen.

Provider rooms are created only through the committed outbox
(`IntegrationOperation`, channel `video`, subject `teleconsult.session`), with
the provider of the room's `video` capability version (see "Provider adapter"
below; today always `teleconsult-synthetic-v1`). The send-time recheck revalidates consent,
encounter and session state before any external effect, so a revoked consent
or closed encounter cancels the operation without a room. Room authority
derives from stored rows, never from external tenant identifiers.

Join access uses short-lived (15-minute) role-scoped credentials; only the
SHA-256 digest is stored. Only the bound physician or patient can exchange a
live credential for its room; expired, revoked, foreign-role or
foreign-participant tokens fail closed, and ended/failed sessions cannot be
re-entered. Recording and transcription are disabled at the database layer
(the trigger line and the model CHECK are both unchanged by todo 37).
Superseded in part by docs/plans/clinic-ops-premium-successor.md (SD ledger):
SD-4 lets todo 40 relax this additively; it still holds in code today.

The synthetic room adapter requires `TELECONSULT_SYNTHETIC_PROVIDER`; the real
video capability remains unavailable pending its capability record
(`docs/integrations/records/2026-09-24-v2/video.md`). `TELECONSULT_SYNTHETIC_FAIL`
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

## Teleconsult v2 (todo 37)

### Provider adapter

`apps/teleconsult/video_providers.py` defines the `VideoProvider` protocol:
`create_room(RoomSpec)`, `mint_token(TokenGrant)`, `revoke(RevokeSpec)` and
`verify_token(token, room_name=..., now=...)`.
Room names are opaque (`tc-` plus 32 random hex) and the database refuses any
name that spells a stored session, encounter, appointment, patient,
physician, clinic or organization identifier. Participant identities are the
role alone (`physician`, `patient`); join tokens live at most 15 minutes and
never outlive the join credential. No adapter ever requests recording.

Only `SyntheticVideoProvider` runs. `LiveKitVideoProvider` and
`TwilioVideoProvider` build the exact public-API requests and HS256 tokens,
but reach a network only through an injected `Transport`; the default
`BlockedTransport` refuses without opening a socket and `live_provider()`
always raises. Live legs are BLOCKED-ON-EG (EG-1 spend, EG-5 provider
contract); tests use recorded synthetic fixtures (`tests/teleconsult/fixtures`).

Admission requires the current signed grant for the opaque room and role.
Minting a replacement invalidates all earlier grants, even at the same clock;
revocation invalidates the role's grant, and expiry is checked at admission.
Unique random grant nonces prevent same-second token reuse. Synthetic state
is shared by server and worker in `CLINIC_SECRET_DIR/video-grants`, under the
explicit `synthetic-file` backend: private files contain only token digests,
never bearer tokens or clinical identity. Recorded LiveKit/Twilio adapters
exercise this admission contract alongside their exact outbound requests.
Their disconnect receipts alone are not revocation proof. Real provider
admission remains BLOCKED-ON-EG and must satisfy the same lifecycle contract
before a live adapter can be enabled.

`select_room_provider` never falls back. When `providers.is_live("video")`
holds, the current version's own live adapter must serve the room (today
always refused as `provider_unavailable`). Otherwise only the enabled
synthetic provider serves, through its dedicated registry row
(`teleconsult-synthetic-v1`, environment `synthetic`, never approved, seeded
by migration `0004`). No available provider is a `provider_unavailable`
conflict that writes nothing.

### Room binding (H-13)

`teleconsult_binding_guard` is rewritten additively (`CREATE OR REPLACE`,
reverse restores the exact v1 body). A new room stores its `video` capability
version by natural identity (`provider`, `provider_environment`): recovery
re-seeds the registry under fresh surrogate keys, so a UUID reference would
dangle. The version must be the synthetic row (`researched`/
`selected_in_plan`, environment `synthetic`) or an `activated` real version,
of the platform or the session's clinic. The room's outbox operation must use
exactly that provider. The recording/transcription refusal line is unchanged;
todo 40 relaxes it only for a consented RecordingSession.

### Participants

Staff v2 actions (device check, audio only, reconnect, remove the patient,
realtime room topic) go through `participants.clinician_session`: todo 6's
`has_permission('clinical.write', clinic, session patient's enrollment)` and
the session's bound physician. Patient actions bind the validated patient
session to the session's patient, clinic and organization. Unknown,
foreign-clinic, foreign-tenant, unpermitted and unassigned actors all get the
same refusal before any write, event, audit entry, outbox row or hint. The
legacy create/join/start/end guards keep their assigned-physician contract.

The staff write views decide every authority their reply needs before the
first write (`views._participant_action`): the participant guard, then for a
native page the clinical read of the notes through the EHR read service,
then the participant write, then the render. The middleware commits anything
below 500, so no refusal may follow a write. An unknown clinic on the staff
page is refused by the physician guard before any clinic lookup.

- Device check: camera, microphone, speaker and network results as closed
  codes (`TeleconsultDeviceCheck`, insert-only, FORCE RLS). The same
  `teleconsult_clinician` decision guards the insert trigger and policies.
  No device names, media, timings or free text are stored.
- Waiting room: the patient checks devices on the waiting page and waits in
  the room until the physician starts; the physician sees presence, the
  patient's device results and media mode.
- Audio only: `audio_only`/`video_restored` events per role; the camera track
  stops sending; a camera loss with a working microphone switches
  automatically. Each actual change of the physician's mode records
  `teleconsult.audio_only.enabled` or `teleconsult.audio_only.disabled`
  (session record, `clinic_id` and `object_verb` only); repeating the current
  mode records nothing.
- Reconnect: `resume` re-validates the participant, rotates an expired join
  credential, appends `reconnected` and returns the stored media modes and
  peer presence. A revoked (removed, ended or rotated) participant is
  `not_in_room` and returns through the waiting room.
- Remove: revokes the patient's credential, appends `removed`, records
  `teleconsult.participant.removed` and enqueues the provider revoke through
  the outbox (subject `teleconsult.participant`, send-time recheck).

### Realtime

The 5 s status timers are gone. Pages refetch status on todo 8 hints
(`rt:teleconsult`), on realtime.js's 30 s fallback (`rt:poll`) when realtime
is unavailable, on reconnect and on explicit refresh. The physician's topic is
`teleconsult:<opaque room name>` (bound physician with `clinical.write`, per
frame); the patient's is `patient:<enrollment>:teleconsult`. Hints are
scheduled only after commit and carry no identifiers.

