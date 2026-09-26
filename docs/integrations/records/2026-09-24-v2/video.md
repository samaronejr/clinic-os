# Video consultation

| Record field | Value |
| --- | --- |
| Capability | `video` |
| Record version / reviewed date | 2026-09-24-v2 / 2026-09-24 UTC |
| Supersedes | [2026-09-12-v1/video.md](../2026-09-12-v1/video.md) |
| Status | unavailable; documentation only |
| Selected provider | None |
| Candidate / reference | Daily is a historical candidate; no provider selected. |
| Accountable owner / decision | Unassigned; clinical and video-service owners required. |
| Approval / contract | None supplied; provider API/profile/package version unverified unless stated below |
| Sandbox / executed provider evidence | Unavailable; none executed |
| Residency / subprocessors / retention | Unverified; owner-approved region, processing agreement, retention and legal-hold handling required |
| Costs / budget | Unverified; historical prices are not approval or current quotes |
| Required packages | Unapproved: Daily/browser client or another selected SDK; exact version and local asset policy unset. |
| Dependent tasks | Successor todos 4, 37; superseded record: renewal tasks 13, 27, 28, 29, 30, 43, 44 |
| Review trigger | Provider selection, protocol/package change or release; no approval expiry exists |

## Successor note

This record is carried forward from set 2026-09-12-v1 under the
[successor contract](../../../plans/clinic-ops-premium-successor.md). Its source
observations were retrieved on 2026-09-12 and weren't fetched again for this
set, so re-verify them before any approval. Task numbers in the body below are
renewal-plan tasks; the header lists the successor todos. Status, owner and
approval fields are unchanged: nothing here authorizes a provider, sandbox,
spend or live data.

Recording and transcription stay disabled in code and at the database layer.
The successor contract (SD-4) allows them only through todo 40's consent-bound
recording session, the [`asr` record](asr.md) in this set and gates EG-2, EG-3
and EG-5. Until then, the "Recording/transcription stay disabled" rule below
remains binding.

## Source-backed observations

Daily documents [private rooms](https://docs.daily.co/reference/rest-api/rooms/create-room), scoped [meeting tokens](https://docs.daily.co/reference/rest-api/meeting-tokens/create-meeting-token), and [webhooks](https://docs.daily.co/reference/rest-api/webhooks). The webhook documentation describes HMAC-SHA256 validation with timestamp/signature headers, event-ID deduplication and unordered delivery. It states a credit card is required for webhooks; no card/account action is authorized.

## Contract required before use

Pin room privacy, exact room_name binding, participant role, token expiry, revocation/recovery and return/callback schemas before task 27. Authenticate original callback bytes before looking up the stored operation; never trust an external tenant claim. Restrict two-persona access to the consented encounter. Recording/transcription stay disabled. Confirm negotiated media/control transport, processing locations and subprocessors; a BAA claim alone is not local clinical/privacy approval.

## Required future fixtures

Authorized sandbox room with two synthetic participants, expiry and wrong-room token, revoked session, HMAC mismatch, replay/out-of-order event and reconnect/outage. Provider media success requires actual two-persona evidence.

These are acceptance requirements, not executed sandbox results. Fixtures may
contain only synthetic or explicitly provider-authorized data.

## Unavailable branch

Tasks 27-30 external video slices wait for video/clinical owners, contract/privacy approvals, SDK and authorized sandbox/webhook access. Synthetic room states can proceed without acquiring paid access.

Follow the [register's versioning and approval rules](../../capabilities.md).
