# Hosted encrypted backup and point-in-time recovery

| Record field | Value |
| --- | --- |
| Capability | `hosted_pitr` |
| Record version / reviewed date | 2026-09-24-v2 / 2026-09-24 UTC |
| Supersedes | [2026-09-12-v1/hosted_pitr.md](../2026-09-12-v1/hosted_pitr.md) |
| Status | unavailable; documentation only |
| Selected provider | None |
| Candidate / reference | Unselected; Amazon RDS is a reference example. |
| Accountable owner / decision | Unassigned; infrastructure/recovery owner and clinic continuity owner required. |
| Approval / contract | None supplied; provider API/profile/package version unverified unless stated below |
| Sandbox / executed provider evidence | Unavailable; none executed |
| Residency / subprocessors / retention | Unverified; owner-approved region, processing agreement, retention and legal-hold handling required |
| Costs / budget | Unverified; historical prices are not approval or current quotes |
| Required packages | Unapproved: selected provider SDK/CLI, recovery automation and database client versions. |
| Dependent tasks | Successor todos 72; superseded record: renewal tasks 43, 44, 46, 47 |
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

RDS documents [point-in-time recovery](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_PIT.html) and a [restore API](https://docs.aws.amazon.com/AmazonRDS/latest/APIReference/API_RestoreDBInstanceToPointInTime.html). These describe a possible service; the current logical rehearsal provides no encrypted provider-PITR evidence.

## Contract required before use

Approve account/region, database/version/extensions, backup coverage/retention, encryption/key recovery, permitted restore targets and observed RPO/RTO acceptance bounds. Include application objects, signed artifacts, audit lineage and historical key versions; database-only restore is insufficient. Allocate disposable owned targets, establish network/roles/RLS, verify counts/digests and retain source resources. Define cleanup authority before running a restore.

## Required future fixtures

Owner-authorized encrypted recovery target with measured time/loss, object/key restoration and clinic_app runtime verification; wrong target/account, missing key/object, corrupt archive and unavailable history must fail safely.

These are acceptance requirements, not executed sandbox results. Fixtures may
contain only synthetic or explicitly provider-authorized data.

## Unavailable branch

Task 43 provider PITR and dependent live/pilot gates wait for recovery/continuity owners, provider account, key policy, package approval and actual authorized restoration. A dump or synthetic restore cannot satisfy this record.

Follow the [register's versioning and approval rules](../../capabilities.md).
