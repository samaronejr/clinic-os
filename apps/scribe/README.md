# Consultation audio capture and ingest

The capture slice records consented consultation audio. Todo 41 adds optional,
separately consented synthetic transcription through the governed Model Gateway.
Neither slice generates a clinical note or authorizes drafting.
Manual clinical care does not require recording consent.

## Authority and state

The public service surface is `issue_encounter_token`, `create_recording`,
`control_recording`, `ingest_chunk`, `read_transcript`, `review_segment`, and
`finalize_transcript`. Services receive explicit keyword-only
scope. The current database principal supplies authority, never a caller's
patient, organization, or actor claim.

Only the encounter's currently assigned, authorized clinician can capture.
Recording requires current `consultation_recording` consent, an informed and
unrefused `AIUseDisclosure` for this exact encounter, and the selected
`object_storage_media` capability version. A missing disclosure or one from a
different encounter cannot authorize capture. These checks apply to admission,
control, ingest, and teleconsult recording flags, including direct SQL writes.
They do not authorize ASR or drafting. Manual care remains available after AI
refusal. The recording retains the exact consent reference and immutable
provider-version snapshot.

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

## Optional synthetic transcription

Each accepted chunk publishes a reference-only `apps.scribe.tasks.transcribe`
task on `ai-interactive` after commit. The worker fetches ciphertext outside a
transaction, rechecks assignment, decrypts under the tenant key, and admits one
purpose-bound encrypted input through `apps.ai.services.invoke`. No audio enters
task arguments or integration-operation rows. A unique retained
`ChunkTranscription` binds the chunk, exact `ai_assistance` acceptance and
governed invocation; matching replays do not reserve another invocation.
The durable AI input contains object ciphertext and opaque binding metadata,
not raw audio. Only gateway preparation materializes raw bytes into the
repr-hidden, ephemeral provider request. Destroying the chunk's sole wrapped
object key therefore leaves no independently decryptable raw-audio shadow.

Current recording consent and an informed, unrefused encounter disclosure are
necessary but insufficient. Admission and final provider eligibility also
require separate current `ai_assistance` consent, the ASR capability registry,
approved model configuration, both AI switches and the existing exact budget.
Consent publication and withdrawal take the gateway's organization send fence;
no purpose lock is acquired while a worker holds that fence.
The registered transcript projection runs after metering is settled but before
the first AI audit append, inside the same transaction. Media acceptance takes
the recording row before its chunk row, preserving recording/chunk/audit order.

The only runnable ASR is the deterministic synthetic recording in
`fixtures/asr_v1.json`. It exercises partial replacement, repeated words,
inaudibility and the uncertain dose "5 ou 15 mg"; it makes no accuracy claim.
AWS Transcribe Streaming, Azure Speech and Google Speech-to-Text v2 request
options and result codecs are executable on offline fixtures in `asr.py`.
Their execution stays `BLOCKED-ON-EG`, with `real_enabled=False`; there is no
SDK, credential loader or network transport to activate accidentally. AWS's
Ogg Opus requirement and Azure's unqualified compressed-audio dependency are
explicit rather than pretending every browser container interoperates.

`TranscriptVersion` is an immutable linear successor chain. `TranscriptSegment`
stores text and uncertainty spans only in tenant-encrypted fields. Stitching
orders source ranges by time and immutable identity, never by text, and never
deduplicates repeated words. Every version retains declared epoch/track/sequence
coverage, including gaps, pending recognitions and completed chunk references.
Content-free milliseconds measure received-chunk-to-partial latency.
Snapshot roots retain source identities; only changed source text rows are
appended. Each version does copy its full coverage metadata, not its source
text. A version is limited to 1 MiB of coverage JSON and a recording to 64 MiB
of retained coverage JSON. Recognition stops at 63 MiB, reserving 1 MiB for
finalization or human review; reaching capacity is a clean transcript refusal
and leaves retained audio and manual care available. These database-enforced
limits bound metadata growth; they are not a claim of linear storage.
Unknown voices remain `unknown`; role labels are reviewable proposals.

The native POST-only screen opens from the encounter and provides pending,
partial, final, disabled, empty, conflict and denial states, marked uncertainty,
speaker review, text corrections and a manual-note link. Every review creates a
new version and metadata-only audit event. Human reviews survive later
hypotheses. Finalization requires a stopped recording and full coverage or
explicit, unchecked-by-default acknowledgment of incompleteness. Late model
results never change a final; correcting one creates a new final successor.

An ASR refusal/outage retains audio under its existing policy and leaves
`transcription_pending`. There is no invented text or automatic replay of an
unknown charged outcome. The queue action admits only not-yet-requested chunks.
Existing transcripts do not authorize new processing after consent loss.

`0003_transcripts` is additive, non-atomic and irreversible because it introduces
protected columns; rollback = restore. All three tables have FORCE RLS,
SELECT/INSERT-only runtime grants, binding and immutability guards, no truncate
grant, and recovery-manifest classification. Real-role SQL, migration-upgrade,
restore-rehearsal and browser acceptance require the separately owned DB window.
Source-only verification does not satisfy those gates or any EG.

Vendor codec references, retrieved October 26, 2026:

- AWS: https://docs.aws.amazon.com/transcribe/latest/APIReference/API_streaming_StartStreamTranscription.html
- Azure: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/get-speech-recognition-results
- Google: https://cloud.google.com/speech-to-text/v2/docs/reference/rest/v2/projects.locations.recognizers/recognize
