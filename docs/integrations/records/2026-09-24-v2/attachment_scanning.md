# Attachment malware scanning

| Record field | Value |
| --- | --- |
| Capability | `attachment_scanning` |
| Record version / reviewed date | 2026-09-24-v2 / 2026-09-24 UTC |
| Supersedes | [2026-09-12-v1/attachment_scanning.md](../2026-09-12-v1/attachment_scanning.md) |
| Status | unavailable; documentation only |
| Selected provider | None |
| Candidate / reference | Unselected; ClamD is a reference example. |
| Accountable owner / decision | Unassigned; security/scanner operations owner required. |
| Approval / contract | None supplied; provider API/profile/package version unverified unless stated below |
| Sandbox / executed provider evidence | Unavailable; none executed |
| Residency / subprocessors / retention | Unverified; owner-approved region, processing agreement, retention and legal-hold handling required |
| Costs / budget | Unverified; historical prices are not approval or current quotes |
| Required packages | Unapproved: selected scanner/client, engine/signature database versions, update and transport dependencies. |
| Dependent tasks | Successor todos 4, 40; superseded record: renewal tasks 23, 25, 43, 44 |
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

The [ClamD protocol](https://docs.clamav.net/manual/Usage/ClamdProtocol.html) documents INSTREAM with size limits and errors. Its TCP interface has no built-in authentication/encryption; an exposed TCP daemon is not an acceptable default.

## Contract required before use

Approve isolated local socket or authenticated/protected service transport, maximum size/time, archive/nesting policy, engine/signature freshness, clean/infected/error mapping and update ownership. Scan the same digest later promoted/served. Timeout, unsupported input, stale signatures and scanner outage retain quarantine. 'Clean' reduces risk and is not proof a document is safe or clinically valid.

## Required future fixtures

Approved harmless scanner test artifact, clean file, oversize/archive-limit/error/timeout, unavailable scanner, changed bytes after scan and stale signatures; no quarantined file becomes retrievable.

These are acceptance requirements, not executed sandbox results. Fixtures may
contain only synthetic or explicitly provider-authorized data.

## Unavailable branch

Task 23 real scan acceptance waits for scanner/transport/package/update policy approval and owned execution environment. Storage success alone cannot satisfy scanning; synthetic scan results remain visibly synthetic.

Follow the [register's versioning and approval rules](../../capabilities.md).
