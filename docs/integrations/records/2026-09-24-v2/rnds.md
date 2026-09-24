# RNDS national health data exchange

| Record field | Value |
| --- | --- |
| Capability | `rnds` |
| Record version / reviewed date | 2026-09-24-v2 / 2026-09-24 UTC |
| Supersedes | None; new capability in this set |
| Status | unavailable; documentation only |
| Selected provider | None |
| Candidate / reference | Direct DATASUS credentialing is the plan's proposed route: Portal de Servicos request, digital certificate, homologation, then production. No alternative is recorded. |
| Accountable owner / decision | Unassigned; interoperability owner, privacy reviewer and certificate custodian required. |
| Approval / contract | None supplied; provider API/profile/package version unverified unless stated below |
| Sandbox / executed provider evidence | Unavailable; none executed |
| Residency / subprocessors / retention | Unverified; certificate custody, exchange logs without PHI, retention and legal basis per exchange required |
| Costs / budget | Unverified; no price is an approval or a current quote |
| Required packages | Unapproved: reviewed HTTPS/FHIR client and certificate handling; exact version unset. |
| Dependent tasks | Successor todos 4, 68 |
| External gates | EG-2, EG-7 |
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

The plan records the credentialing process outline as verified from the RNDS guide on 2026-09-24. Profiles, endpoints and authentication details are unverified here.

## Contract required before use

`exchange_clinical_record` keeps raising `NotImplementedError("Phase >=1")` until todo 68 lands. Profiles and versions are pinned per release. Each exchange records its legal basis and consent where required. Authentication uses the approved certificate flow; payload tenant claims are never read.

## Required future fixtures

Homologation exchange with synthetic records, schema rejection, expired certificate, duplicate submission and outage with retry.

These are acceptance requirements, not executed sandbox results. Fixtures may
contain only synthetic or explicitly provider-authorized data.

## Unavailable branch

Todo 68 builds the adapter against fixtures. RNDS live waits for EG-7 and EG-2.

Follow the [register's versioning and approval rules](../../capabilities.md).
