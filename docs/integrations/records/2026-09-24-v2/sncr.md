# SNCR controlled-prescription integration

| Record field | Value |
| --- | --- |
| Capability | `sncr` |
| Record version / reviewed date | 2026-09-24-v2 / 2026-09-24 UTC |
| Supersedes | None; new capability in this set |
| Status | unavailable; documentation only |
| Selected provider | None |
| Candidate / reference | The Anvisa SNCR API with its training environment is the plan's proposed primary; integration through a prescription partner is the alternative. Proposal only; no integration selected. |
| Accountable owner / decision | Unassigned; prescription legal reviewer, clinical owner and technical integration owner required. |
| Approval / contract | None supplied; provider API/profile/package version unverified unless stated below |
| Sandbox / executed provider evidence | Unavailable; none executed |
| Residency / subprocessors / retention | Unverified; credential custody, retention of submission receipts and legal-hold handling required |
| Costs / budget | Unverified; no price is an approval or a current quote |
| Required packages | Unapproved: reviewed HTTPS client for the Anvisa API or a partner SDK; exact version unset. |
| Dependent tasks | Successor todos 4, 34, 36 |
| External gates | EG-5, EG-6 |
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

The plan records SNCR availability dates as verified from gov.br (21/09/2026): from 30/09/2026 Notificacoes A, B and B2, retinoids and thalidomide can be issued electronically only through integrated systems; electronic white prescriptions under special control, including antimicrobials, must integrate from 30/10/2026; paper remains valid (RDC 873/2024, Portaria SVS/MS 344/1998). This record didn't re-fetch those pages, and the legal reading belongs to the EG-6 review.

## Contract required before use

Per-class eligibility is date-aware (D-21). Until EG-6 clears, electronic issuance of the regulated classes is refused and the product offers the clearly labeled paper-printing path. Issuance binds the exact approved bytes and a qualified signature (ADR-010). Submission is idempotent with lookup after timeout. Credentials never enter the repository, logs or evidence.

## Required future fixtures

Training-environment submissions for each class, rejected submission, duplicate submission, timeout followed by lookup, expired credential and date-boundary cases on 30/09/2026 and 30/10/2026.

These are acceptance requirements, not executed sandbox results. Fixtures may
contain only synthetic or explicitly provider-authorized data.

## Unavailable branch

Todo 34 builds the class taxonomy with fail-closed gates; todo 36 builds the adapter against a fixture. Electronic issuance of regulated classes waits for EG-6 and EG-5.

Follow the [register's versioning and approval rules](../../capabilities.md).
