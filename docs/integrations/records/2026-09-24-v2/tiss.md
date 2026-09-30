# TISS insurance exchange

| Record field | Value |
| --- | --- |
| Capability | `tiss` |
| Record version / reviewed date | 2026-09-24-v2 / 2026-09-24 UTC |
| Supersedes | None; new capability in this set |
| Status | unavailable; documentation only |
| Selected provider | None |
| Candidate / reference | Direct XML webservice or portal per operadora is the plan's proposed route; a clearinghouse is the alternative. Proposal only; no payer connection selected. |
| Accountable owner / decision | Unassigned; revenue-cycle owner and per-payer contract owner required. |
| Approval / contract | None supplied; provider API/profile/package version unverified unless stated below |
| Sandbox / executed provider evidence | Unavailable; none executed |
| Residency / subprocessors / retention | Unverified; payer contracts, retention of guides and batches, and legal-hold handling required |
| Costs / budget | Unverified; no price is an approval or a current quote |
| Required packages | Unapproved: lxml or xmlschema for XSD validation (named in the plan) and a reviewed HTTPS/SOAP client; exact versions unset. |
| Dependent tasks | Successor todos 4, 59 |
| External gates | EG-2, EG-5, EG-8 |
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

The plan records ANS TISS component versions as verified on 2026-07-28: Organizacional 202601, Conteudo e Estrutura 202511, TUSS 202601, Seguranca 202511, Comunicacao 04.03.00/01.06.00. They must be re-verified per release (ADR-012).

## Contract required before use

Guides, batches, glosas and appeals follow a DB-enforced lifecycle. Every outbound document validates against the pinned XSD before submission. Component versions are pinned per component, never hardcoded as one version. Money stays in integer centavos (ADR-011).

## Required future fixtures

XSD-valid and invalid batches, payer rejection with glosa codes, partial payment, appeal round trip and version mismatch refusal.

These are acceptance requirements, not executed sandbox results. Fixtures may
contain only synthetic or explicitly provider-authorized data.

## Unavailable branch

Todo 59 builds the lifecycle and XSD validation against fixtures. Submission to any payer waits for EG-8 per operadora.

Follow the [register's versioning and approval rules](../../capabilities.md).
