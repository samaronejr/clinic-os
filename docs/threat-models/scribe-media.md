# Threat model: scribe audio capture and media storage

- Status: design baseline, 2026-09-24 (todo 2). Mitigations are planned work
  owned by the listed todos unless a current source path is named. The Tests
  column maps each mitigation id to the tests that prove it (todo 73,
  checked by `tests/infra/test_threat_model_coverage.py`). A mitigation that
  starts with **Proposal:** goes beyond the owning todo's plan text; that
  todo accepts or rejects it and isn't bound by it until then.
- Decisions: [ADR-005](../adr/ADR-005-chunked-media-upload.md),
  [ADR-004](../adr/ADR-004-server-side-drafts.md),
  [ADR-016](../adr/ADR-016-recovery-with-key-escrow.md)

## Scope

Consented consultation audio from the browser (and the teleconsult recording
path) through chunk upload, encrypted object storage, transcription and
drafting, to retention and purge. Covers `apps/scribe`, the capture script,
the object storage capability `object_storage_media` and the ASR queue.

## Assets

- Raw consultation audio (highest sensitivity).
- Transcripts, draft suggestions and the AI-use record in the chart.
- Consent and recording refusal records.
- Per-object data keys and tenant keys.
- Chunk ordering and coverage manifests (integrity of what was said).

## Trust boundaries

1. Browser microphone and memory to the chunk upload endpoint.
2. Web process to private object storage.
3. Worker to the AI gateway and ASR provider.
4. Retention worker to object storage and backups.

## Data flow

```mermaid
flowchart LR
  subgraph Browser["Clinician browser (untrusted)"]
    Mic["MediaRecorder, 2 s Opus slices"]
    Buffer["60 s volatile memory buffer"]
  end
  subgraph Web["Web process (tenant txn)"]
    Upload["POST chunk: session, epoch, track, seq, sha256"]
    Consent["Consent and capability check"]
  end
  subgraph Store["Private storage"]
    Obj[("Object storage, per-object keys")]
    PG[("PostgreSQL: RecordingSession, AudioChunk")]
  end
  subgraph Worker["Workers (ai-interactive, ai-batch)"]
    ASR["Transcription via AI gateway"]
    Draft["Draft suggestions"]
    Purge["Retention purge and crypto-shred"]
  end
  Mic --> Buffer
  Buffer -- "chunk POST over HTTPS" --> Upload
  Upload --> Consent
  Consent --> PG
  Upload -- "encrypted object" --> Obj
  PG --> ASR
  Obj --> ASR
  ASR -- "transcript segments" --> PG
  PG --> Draft
  Draft -- "suggestions for review" --> PG
  Purge --> Obj
  Purge --> PG
```

## Threats and mitigations

| ID | STRIDE | Threat | Mitigation | Todos | Tests |
| --- | --- | --- | --- | --- | --- |
| SM-S1 | Spoofing | Chunks are uploaded for a session the uploader doesn't own, or after a patient switch. | Chunks are accepted only for the session bound to the current encounter token and the assigned clinician with active consent; a patient switch forces stop, discard or keep-bound. | 40 | `tests/scribe/test_refusals.py::test_signed_but_unbound_tokens_cannot_upload`<br>`tests/scribe/test_refusals.py::test_cross_session_replay_cannot_retarget_a_chunk`<br>`tests/scribe/test_actor_readers.py::test_capture_switch_is_owner_scoped_and_never_retargets_recording`<br>`tests/renewal/browser/test_scribe_capture.py::test_patient_switch_requires_an_explicit_bound_audio_decision` |
| SM-S2 | Spoofing | Recording starts without patient consent. | `RecordingSession` moves to `authorized` only with consent refs from the purpose taxonomy; a refusal hides capture. | 20, 40 | `tests/scribe/test_ingest.py::test_revoked_consent_stops_new_chunks_and_direct_sql`<br>`tests/scribe/test_ai_disclosure.py::test_capture_refuses_missing_uninformed_or_refused_disclosure`<br>`tests/scribe/test_refusals.py::test_consent_loss_never_accepts_another_slice_or_mutates_on_refusal`<br>`tests/renewal/browser/test_scribe_capture.py::test_recording_refusal_hides_capture_without_blocking_care` |
| SM-T1 | Tampering | Chunks are reordered, replayed or altered, changing what the transcript says. | Chunk key `(session, epoch, track, seq)` plus sha256; conflicting duplicates are rejected; stale epochs are refused; transcripts carry a gap manifest and never fill gaps. | 40, 41 | `tests/scribe/test_ingest.py::test_conflicting_duplicate_quarantines_original_without_replacement`<br>`tests/scribe/test_ingest.py::test_resume_fences_old_epoch_and_commands_replay`<br>`tests/scribe/test_transcripts.py::test_gaps_remain_explicit_until_an_actual_recognition_arrives` |
| SM-T2 | Tampering | A late model result overwrites text the clinician already edited. | Generation fence per draft revision and section edit epochs; suggestions are never auto-applied. | 42 | `tests/scribe/test_draft_runtime.py::test_section_regeneration_fences_late_results_without_invalidating_other_sections`<br>`tests/scribe/test_draft_runtime.py::test_late_result_keeps_manual_edits_and_requires_explicit_replacement`<br>`tests/scribe/test_draft_runtime.py::test_generation_and_three_clinician_decisions_never_finalize_automatically` |
| SM-R1 | Repudiation | A clinician can't show which parts of a note came from AI. | AI-use record and provenance tags persist after acceptance and are visible in the chart. | 42 | `tests/scribe/test_draft_views.py::test_native_review_shows_sources_diff_and_persistent_ai_origin`<br>`tests/ai/test_decision_support.py::test_clinician_choices_record_ai_use_without_changing_the_chart_or_session` |
| SM-I1 | Information disclosure | Audio persists on the device through IndexedDB, localStorage or the service-worker cache. | Memory-only buffer, capped at 60 s from capture time; no browser storage; a browser test inspects storage after capture. **Unproved:** outside the browser suites, the 60 s capture-time cap is checked only as shipped source text, not as running behavior. | 40, 14 | `tests/security/test_browser_storage.py::test_first_party_scripts_never_write_browser_storage`<br>`tests/security/test_browser_storage.py::test_scribe_capture_buffer_is_memory_only_and_capped_from_capture_time`<br>`tests/security/test_browser_storage.py::test_service_worker_stores_only_versioned_static_assets`<br>`tests/renewal/browser/test_scribe_privacy.py::test_capture_and_manual_note_stay_out_of_browser_and_technical_sinks`<br>`tests/renewal/browser/test_scribe_capture.py::test_seventy_second_outage_pauses_drops_expired_audio_and_records_gaps` |
| SM-I2 | Information disclosure | Audio or transcripts leak through logs, URLs or telemetry. | Selectors in POST bodies; allowlist redaction; PHI-sentinel scans over logs, outbox and URLs. | 11, 40, 73 | `tests/scribe/test_sink_sentinels.py::test_ingest_publishes_only_the_outbox_reference_and_no_sensitive_log_values`<br>`tests/scribe/test_ingest.py::test_outbox_stores_only_ciphertext_and_object_keys_are_not_authority`<br>`tests/security/test_sentinel_sinks.py::test_request_sentinels_never_reach_any_sink` |
| SM-I3 | Information disclosure | Stored audio is readable by anyone with bucket access. | Private storage behind the `object_storage_media` capability, per-object data key wrapped by the tenant key. | 40, 72 | `tests/scribe/test_s3_storage.py::test_storage_keys_cannot_escape_the_private_namespace`<br>`tests/scribe/test_ingest.py::test_existing_audio_survives_kek_rewrap_without_rewriting_the_object`<br>`tests/scribe/test_s3_storage.py::test_remote_configuration_is_tls_only_and_has_no_selector_channels` |
| SM-I4 | Information disclosure | Purged audio survives in backups or comes back after a restore. | Purge receipts record the backup expiry date honestly; restore drills re-apply purge tombstones. | 43, 72 | `tests/retention/test_source_contract.py::test_capture_renders_the_dated_purge_receipt_without_loading_audio`<br>`tests/retention/test_audio_purge.py::test_restore_reapplies_object_tombstones_idempotently`<br>`tests/retention/test_restore_cli.py::test_acceptance_replays_source_tombstones_into_its_fresh_media_root` |
| SM-D1 | Denial of service | Network loss makes the UI claim recording continues when it doesn't. | Buffer exhaustion auto-pauses with a visible message; expired chunks are discarded; TTL never resets on reconnect. | 40 | `tests/scribe/test_refusals.py::test_expired_upload_cannot_reset_its_window`<br>`tests/scribe/test_refusals.py::test_epoch_rollback_and_paused_upload_are_read_only`<br>`tests/scribe/test_ingest.py::test_identical_replays_converge_without_resetting_expiry`<br>`tests/renewal/browser/test_scribe_capture.py::test_seventy_second_outage_pauses_drops_expired_audio_and_records_gaps` |
| SM-D2 | Denial of service | ASR provider outage blocks documentation. | Degraded mode falls back to manual documentation with no silent provider switch. | 38, 41, 72 | `tests/scribe/test_transcript_runtime.py::test_asr_outage_retains_audio_and_never_fabricates_transcript_text`<br>`tests/scribe/test_refusals.py::test_storage_disable_does_not_fall_back_or_write`<br>`tests/scribe/test_refusals.py::test_capture_presentation_keeps_manual_care_and_truthful_consent_status` |
| SM-E1 | Elevation of privilege | A draft suggestion finalizes the note or writes as the clinician. | Apply is the clinician's own `record_clinical_note` call with CAS; approval of a suggestion never finalizes SOAP. | 42 | `tests/scribe/test_draft_runtime.py::test_generation_and_three_clinician_decisions_never_finalize_automatically`<br>`tests/scribe/test_draft_views.py::test_modified_review_requires_replacement_before_writing` |
| SM-E2 | Elevation of privilege | Teleconsult recording is switched on without a bound session. | The teleconsult trigger allows recording only while a `RecordingSession` for the room's encounter is authorized, recording or paused. | 40 | `tests/scribe/test_room_capture.py::test_room_bits_require_explicit_live_capture_and_asr_stays_closed`<br>`tests/renewal/test_teleconsult_sessions.py::test_raw_mutation_reopen_and_capture_flag_fail_closed` |

## Residual risk and external gates

- Real-device capture behavior (screen lock, interruptions) is unproven until
  EG-14 device runs.
- Live recording needs EG-2 privacy review and EG-3 clinical safety sign-off.
- Provider retention terms for audio are unverified until EG-5 contracts.
