# Encrypted object storage for media and AI artifacts

| Record field | Value |
| --- | --- |
| Capability | `object_storage_media` |
| Record version / reviewed date | 2026-09-24-v2 / 2026-09-24 UTC |
| Supersedes | None; new capability in this set |
| Status | unavailable; documentation only |
| Selected provider | None |
| Candidate / reference | Amazon S3 with KMS in sa-east-1 and GuardDuty Malware Protection for S3 is the plan's proposed primary; self-hosted ClamAV is the scanning alternative. Proposal only; no provider selected. EHR attachments stay covered by the carried-forward `attachment_storage` and `attachment_scanning` records. |
| Accountable owner / decision | Unassigned; security/cryptography reviewer, infrastructure owner and retention owner required. |
| Approval / contract | None supplied; provider API/profile/package version unverified unless stated below |
| Sandbox / executed provider evidence | Unavailable; none executed |
| Residency / subprocessors / retention | Unverified; in-region storage, object versioning, retention for raw audio and AI artifacts, legal hold and destruction receipts required |
| Costs / budget | Unverified; no price is an approval or a current quote |
| Required packages | Unapproved: selected storage/KMS SDK; exact version unset. |
| Dependent tasks | Successor todos 4, 40, 43, 72 |
| External gates | EG-1, EG-2, EG-5, EG-12 |
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

The plan's provider matrix marks storage, KMS and scanning as UNVERIFIED.

## Contract required before use

Private storage behind the AttachmentStorage protocol (ADR-005) with tenant envelope encryption on top of provider encryption (ADR-015). Object keys are opaque and carry no PHI. Raw audio is purged by default after note approval under the approved retention policy, with legal hold and destruction receipts (todo 43). Restore fails honestly when keys or objects are missing (ADR-016).

## Required future fixtures

Upload and read-back with envelope encryption, missing key, missing object, legal hold blocking purge, purge receipt, malware verdict and restore drill.

These are acceptance requirements, not executed sandbox results. Fixtures may
contain only synthetic or explicitly provider-authorized data.

## Unavailable branch

Todos 40 and 43 use local synthetic storage. Provider storage waits for EG-1, EG-2, EG-5 and EG-12.

Follow the [register's versioning and approval rules](../../capabilities.md).
