# Speech recognition (pt-BR)

| Record field | Value |
| --- | --- |
| Capability | `asr` |
| Record version / reviewed date | 2026-09-24-v2 / 2026-09-24 UTC |
| Supersedes | None; new capability in this set |
| Status | unavailable; documentation only |
| Selected provider | None |
| Candidate / reference | AWS Transcribe in sa-east-1 is the plan's proposed primary. Azure Speech (Brazil South), Google Speech-to-Text v2 (southamerica-east1), Deepgram and Speechmatics are alternatives. Proposal only; no provider selected. |
| Accountable owner / decision | Unassigned; clinical safety owner (EG-3), privacy reviewer (EG-2) and technical integration owner required. |
| Approval / contract | None supplied; provider API/profile/package version unverified unless stated below |
| Sandbox / executed provider evidence | Unavailable; none executed |
| Residency / subprocessors / retention | Unverified; in-region processing, no cross-jurisdiction fallback, no provider retention or training use of audio beyond approved terms, and deletion/legal-hold handling required |
| Costs / budget | Unverified; no price is an approval or a current quote |
| Required packages | Unapproved: selected provider SDK or reviewed HTTPS/streaming client; exact version unset. |
| Dependent tasks | Successor todos 4, 38, 41, 48 |
| External gates | EG-1, EG-2, EG-3, EG-5, EG-11 |
| Review trigger | Provider selection, protocol/package change or release; no approval expiry exists |

## Successor note

This record is new in set 2026-09-24-v2 under the
[successor contract](../../../plans/clinic-ops-premium-successor.md). The
research lines below come from the plan's provider and regulatory matrices
dated 2026-09-24. No provider page or API was fetched or called while writing
this record, and nothing here authorizes a provider, sandbox, spend or live
data. Todo 4 may record the proposed primary as `selected_in_plan`; only owner
approval moves it to `approved_to_test`.

## Research observations

The plan's provider matrix lists region, pricing and retention as UNVERIFIED for every candidate. Transcription serves AI-01 (ambient scribe) only through a clinician-reviewed draft.

## Contract required before use

Streaming partial and batch final pt-BR transcripts with speaker labels, word timestamps and confidence. Audio arrives as 2 s slices keyed by session, epoch, track and sequence (D-10). Partial p95 <= 3 s and final draft p95 <= 30 s after stop are proposals until measured. Callbacks are authenticated on raw bytes before any scope lookup. Job names, URLs, logs and metrics carry no PHI. Provider retention, training opt-out and deletion terms must be contractual (EG-5).

## Required future fixtures

Recorded synthetic audio with accents, background noise, caregiver or child voices and interruptions; provider timeout; duplicate and out-of-order partials; region-mismatch refusal; budget exhaustion.

These are acceptance requirements, not executed sandbox results. Fixtures may
contain only synthetic or explicitly provider-authorized data.

## Unavailable branch

Todos 38 and 41 ship a deterministic fake adapter as the only runnable provider. The manual documentation path stays complete. Real transcription waits for EG-2, EG-3 and EG-5, the lifecycle record reaching `approved_to_test` and EG-1 for spend.

Follow the [register's versioning and approval rules](../../capabilities.md).
