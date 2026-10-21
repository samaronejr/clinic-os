# Consultation audio capture and ingest

This slice captures consented consultation audio. It does not transcribe audio,
call a model, generate a clinical note, or authorize any of those operations.
ASR and drafting require their own consent, capability, and Model Gateway work.
Manual clinical care does not require recording consent.

## Authority and state

The public service surface is `issue_encounter_token`, `create_recording`,
`control_recording`, and `ingest_chunk`. Services receive explicit keyword-only
scope. The current database principal supplies authority, never a caller's
patient, organization, or actor claim.

Only the encounter's currently assigned, authorized clinician can capture.
Recording requires current `consultation_recording` consent and the selected
`object_storage_media` capability version. The recording retains the exact
consent reference and immutable provider-version snapshot.

`RecordingSession` follows `created -> authorized -> recording <-> paused ->
stopped`, with `stopped_revoked` for cleanup after consent loss. A fresh page
does not imply a live microphone: it must explicitly recover an abandoned
session, then explicitly resume. Recovery replaces token custody; resumption
allocates a new server-issued `CaptureEpoch` UUID and sequence generation.
Terminal sessions do not reopen. Control receipts bind the session, command
key, token digest, expected generation, action, and declared high-water mark.

Four tables use FORCE RLS, exact column grants, immutable bindings, and
database transition guards: `RecordingSession`, `CaptureEpoch`, `AudioChunk`,
and `RecordingCommand`. Unknown and unauthorized record references reveal no
clinical content.

## Chunk protocol

`POST /clinics/<clinic>/scribe/chunk/` accepts the encounter token in
`X-Encounter-Token` and selectors only in the JSON body. The body contains
`session_id`, opaque `epoch`, `track`, `seq`, `sha256`, `duration_ms`, `codec`,
and base64 `bytes`. The browser sends two-second Opus slices. Decoded input
is bounded to 256 KiB, durations to 1-5000 ms, and the request body to 360 KiB.
Authorization and browser/patient binding precede reading an upload body.

The exercised browser server pins synchronous, single-threaded workers with
a thirty-second worker deadline. The slow-body regression keeps a bounded
chunk request incomplete while continuing to send bytes; it requires worker
termination, unchanged capture custody, and a healthy remaining worker. This
is a verified-surface bound, not qualification of production ingress buffering
or deployment-scale denial-of-service resistance.

The identity is `(session, epoch, track, seq)`. Matching immutable byte terms
converge on a duplicate acknowledgement without another row, object write,
or expiry change. A conflicting identity is quarantined, its wrapped key is
destroyed, and the missing sequence becomes an explicit gap. No replacement
content is invented. Old generations, invalid hashes, expired first arrivals,
foreign sessions, and invalid authority are refused.

`AudioChunk.expires_at` is the original **upload** deadline: server epoch start
plus the sequence's two-second offset plus sixty seconds. Reconnect, replay,
and recovery cannot extend it. It is not the raw-object retention deadline;
retention policy and raw-object purge belong to todo 43.

## Volatile client and native care

The client opens no microphone before server authorization and user permission.
Unconfirmed slices live only in JavaScript memory for at most sixty seconds.
It never writes audio to browser storage, a service-worker cache, or a download.
Connection recovery does not resume recording automatically. Expired slices
are discarded and declared as gaps; the microphone stops when the memory
window is exhausted. Every failure state tells the clinician that unconfirmed
audio may be missing and leaves the manual note available.

The encounter has a microphone picker, live level meter, persistent recording
indicator, and start/pause/resume/stop/discard/recovery controls. A patient
change requires keeping the current patient or explicitly stopping/discarding
the bound capture. Native forms retain stop/discard/recovery and manual-care
paths when JavaScript cannot record.

## Encrypted object custody

`MediaStorage` extends the existing `AttachmentStorage` protocol. Ingest creates
a random object key and random per-object data key. pgcrypto seals the audio;
the existing tenant envelope wraps that data key under the tenant DEK/KEK
hierarchy. Only ciphertext is staged in PostgreSQL. An after-commit,
reference-only outbox action writes ciphertext to private object storage
outside the tenant transaction. No audio enters broker messages or logs.

The in-repository filesystem fake is the synthetic default. The S3-compatible
transport uses TLS and SigV4, bounded objects, conditional writes, matching-byte
replay convergence, pending-object reconciliation markers, and no redirects.
It refuses network I/O inside an atomic database transaction. Local HTTP is
permitted only through an explicitly constructed loopback dev/fake adapter;
MinIO is not a CI dependency. A live adapter requires the existing capability
lifecycle and activation gates; no `real_enabled` constant is changed.

For optional local MinIO, use the standalone `docker-compose.media.yml` with
`CLINIC_MINIO_DEV_IMAGE` set to an operator-reviewed local image,
`CLINIC_MINIO_DEV_DATA` set to an isolated synthetic directory, and
`CLINIC_LOCAL_SECRET_DIR` containing owner-only `media-access-key.secret` and
`media-secret-key.secret` files (each 64 hex characters, matching SecretStore).
Create the private bucket in its loopback console. Run the app and worker on
the host with `SCRIBE_SYNTHETIC_CAPTURE=1`, `SCRIBE_DEV_MINIO=1`,
`SCRIBE_S3_ENDPOINT=http://127.0.0.1:9000`, and the selected
`SCRIBE_S3_BUCKET`. The dev selector refuses non-loopback endpoints and never
falls back after a storage failure. Stop with
`docker compose -f docker-compose.media.yml down`; remove only that run's
synthetic data directory. No MinIO service is added to CI or default Compose.

Discard destroys the stored wrapped object keys and encrypted staging while
retaining immutable identity/history. KEK rewrap changes the tenant key
registry rather than rewriting audio objects. Raw-object/backup recovery and
retention acceptance are separate from this slice's relational recovery scope.

## Concurrency and room safety

Human capture commands serialize consent before session/chunk locks. Discard
preclaims affected chunk rows before the first audit append. Context-switch
guards decide and claim their affected rows before applying any discard.
Read-only presentation/status never claims the consent-purpose lock. The
outbox uses the existing consent-boundary lock during external sends and
rechecks stored scope before and after the effect, avoiding purpose/boundary
and chunk/audit lock inversions.

A teleconsult room may enable its recording bit only for the same encounter's
currently recording, consented session and open server epoch. Pause, stop,
consent loss, encounter close, or room termination clears ineligible flags.
Room identity remains immutable. Transcription remains database-disabled and
has no runtime update grant; recording consent alone cannot enable ASR.

## Evidence boundary

Synthetic encoders, fake storage, role tests, screenshots, and local videos are
L1 evidence only. The physical-device protocol remains `BLOCKED-ON-EG`
(EG-14). This implementation satisfies no external legal, provider, residency,
security, device, or live-data gate by agent authority.

The authenticated S3 wire fake checks the local transport contract, but its
SigV4 canonicalization is not an independent interoperability oracle. Actual
S3/MinIO qualification remains pending. Browser storage/sink inspection,
keyboard controls, reduced motion, and 200%/400% physical-window-equivalent
reflow have dedicated `scribe-capture` cases; they do not replace physical
assistive-technology evidence. The repair's manual QA matrix and review notes
belong in the task-40 evidence packet. Root's separate re-review remains the
approval boundary; self-review is not independent approval.
