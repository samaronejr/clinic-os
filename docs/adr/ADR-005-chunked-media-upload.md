# ADR-005: Chunked HTTPS media upload to private encrypted storage

- Status: accepted 2026-09-24
- Recorded by: todo 2
- Related decisions: D-10, SD-8
- Threat model: [scribe media](../threat-models/scribe-media.md)

## Context

The scribe (AI-01) records consented consultation audio. Clinic networks drop,
laptops sleep and tabs close mid-visit. Partial transcripts need p95 <= 3 s,
which rules out long upload slices (D-10). Browser storage of clinical audio is
banned (SD-8b), so the browser can't spool a whole visit.

EHR attachments already go through the `AttachmentStorage` protocol
(`apps/ehr/attachment_storage.py`) into quarantined private storage, and tenant
keys are wrapped by a KEK (`apps/tenancy/envelope.py`).

## Decision

Audio moves by chunked HTTPS upload.

- The browser records 2 s MediaRecorder Opus slices. The slice length is
  tunable from 1 to 5 s after device tests.
- Each chunk is a POST keyed by session, epoch, track and sequence, stored in
  private encrypted object storage through an extension of the
  `AttachmentStorage` protocol, with a per-object data key wrapped by the
  tenant key.
- The browser keeps at most 60 s of audio in a volatile in-memory buffer,
  measured from capture time. When the buffer is exhausted, capture pauses
  explicitly and tells the clinician.

## Consequences

- A network drop costs at most the unsent buffer, and the UI says so; it never
  reports success for audio the server didn't receive.
- The server can detect gaps and duplicates from the chunk key, which feeds
  the transcript coverage manifest in todo 41.
- Per-object keys make crypto-shredding possible for the purge path in
  todo 43.
- Chunk upload rides the normal HTTPS stack, so CSRF, session and CSP rules
  apply unchanged.

## Rejected

- WebSocket media ingest for v1. It adds a second transport with its own auth
  and backpressure story before there's evidence it's needed.
- IndexedDB spool. It would persist clinical audio on the device, which the
  class-3 browser-storage ban forbids.

## Revisit trigger

A requirement for live captions under 2 s that chunked upload can't meet.

## Owning todos

40. Consumers: 41, 43.
