# Private attachment and document storage

| Record field | Value |
| --- | --- |
| Capability | `attachment_storage` |
| Record version / reviewed date | 2026-09-24-v2 / 2026-09-24 UTC |
| Supersedes | [2026-09-12-v1/attachment_storage.md](../2026-09-12-v1/attachment_storage.md) |
| Status | unavailable; documentation only |
| Selected provider | None |
| Candidate / reference | Unselected; Amazon S3 is a reference example. |
| Accountable owner / decision | Unassigned; storage/security owner and privacy/retention owner required. |
| Approval / contract | None supplied; provider API/profile/package version unverified unless stated below |
| Sandbox / executed provider evidence | Unavailable; none executed |
| Residency / subprocessors / retention | Unverified; owner-approved region, processing agreement, retention and legal-hold handling required |
| Costs / budget | Unverified; historical prices are not approval or current quotes |
| Required packages | Unapproved: object-store SDK, encryption integration and approved content-type handling dependencies. |
| Dependent tasks | Successor todos 40, 72; superseded record: renewal tasks 23, 25, 33, 35, 43, 44 |
| Review trigger | Provider selection, protocol/package change or release; no approval expiry exists |

## Successor note

This record is carried forward from set 2026-09-12-v1 under the
[successor contract](../../../plans/clinic-ops-premium-successor.md). Its source
observations were retrieved on 2026-09-12 and weren't fetched again for this
set, so re-verify them before any approval. Task numbers in the body below are
renewal-plan tasks; the header lists the successor todos. Status, owner and
approval fields are unchanged: nothing here authorizes a provider, sandbox,
spend or live data.

## Source-backed observations

[S3 SSE-KMS](https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingKMSEncryption.html) documents one managed encryption option. It does not prove this deployment's region, keys, access policy, tenant boundary, scanning or recovery.

## Contract required before use

Approve bucket/account/region, exact put/get/version/delete/hold semantics, size/streaming/time limits, checksum verification, signed URL lifetime and denial behavior. Bind opaque object identifiers to authorized tenant/domain records; unguessability alone is insufficient. Separate quarantine and accepted objects; serving requires scan state and permission. Cover attachments, reviewed/signed PDFs and exports with data_at_rest and tenant_key_management. Define retention/legal hold and version preservation before deletion.

## Required future fixtures

Owned synthetic object put/get with matching digest, wrong tenant, missing/truncated object, expired link, held-object delete denial and unavailable KMS. Verify no public or pre-scan retrieval.

These are acceptance requirements, not executed sandbox results. Fixtures may
contain only synthetic or explicitly provider-authorized data.

## Unavailable branch

Task 23 provider storage, tasks 25/35 release and task 43 provider restoration wait for storage/privacy owners, SDK/key/region contract and sandbox. A local synthetic fixture is not approved hosted storage.

Follow the [register's versioning and approval rules](../../capabilities.md).
