# Video consultation

| Record field | Value |
| --- | --- |
| Capability | `video` |
| Record version / reviewed date | 2026-09-12-v1 / 2026-09-12 UTC |
| Status | unavailable; documentation only |
| Selected provider | None |
| Candidate / reference | Daily is a historical candidate; no provider selected. |
| Accountable owner / decision | Unassigned; clinical and video-service owners required. |
| Approval / contract | None supplied; provider API/profile/package version unverified unless stated below |
| Sandbox / executed provider evidence | Unavailable; none executed |
| Residency / subprocessors / retention | Unverified; owner-approved region, processing agreement, retention and legal-hold handling required |
| Costs / budget | Unverified; historical prices are not approval or current quotes |
| Required packages | Unapproved: Daily/browser client or another selected SDK; exact version and local asset policy unset. |
| Dependent tasks | 13, 27, 28, 29, 30, 43, 44 |
| Review trigger | Provider selection, protocol/package change or release; no approval expiry exists |

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
