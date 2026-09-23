# Physician registration verification

| Record field | Value |
| --- | --- |
| Capability | `physician_registration` |
| Record version / reviewed date | 2026-09-12-v1 / 2026-09-12 UTC |
| Status | unavailable; documentation only |
| Selected provider | None |
| Candidate / reference | CFM web service is a historical candidate; no approved machine interface. |
| Accountable owner / decision | Unassigned; clinical governance owner must approve status/freshness rules. |
| Approval / contract | None supplied; provider API/profile/package version unverified unless stated below |
| Sandbox / executed provider evidence | Unavailable; none executed |
| Residency / subprocessors / retention | Unverified; owner-approved region, processing agreement, retention and legal-hold handling required |
| Costs / budget | Unverified; historical prices are not approval or current quotes |
| Required packages | Unapproved/unknown until a licensed API/schema and client requirements are supplied. |
| Dependent tasks | 13, 31, 32, 34, 44 |
| Review trigger | Provider selection, protocol/package change or release; no approval expiry exists |

## Source-backed observations

The [CFM portal](https://portal.cfm.org.br/) references physician services, but the intake did not establish a machine API, permitted usage, response schema or sandbox. A public directory entry is not an automated verification contract.

## Contract required before use

Obtain exact CRM/UF lookup identity, active/suspended/cancelled status mapping, professional identity binding, evidence version, freshness interval, review/expiry and outage behavior. Define what an accountable manual evidence route may support before relying on it. Do not scrape or infer active authority from name similarity or an old successful lookup.

## Required future fixtures

Provider-authorized current, absent, suspended and mismatched-identity records; stale evidence, timeout and revoked approval. All fixtures must be synthetic/provider-approved and labeled.

These are acceptance requirements, not executed sandbox results. Fixtures may
contain only synthetic or explicitly provider-authorized data.

## Unavailable branch

Task 31 real professional verification and dependent issuance in tasks 32/34 remain unavailable pending the clinical owner and provider/license/package decisions. Unavailable or stale verification cannot produce an eligible-to-sign result.

Follow the [register's versioning and approval rules](../../capabilities.md).
