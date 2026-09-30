# NFS-e fiscal documents

| Record field | Value |
| --- | --- |
| Capability | `nfse` |
| Record version / reviewed date | 2026-09-24-v2 / 2026-09-24 UTC |
| Supersedes | None; new capability in this set |
| Status | unavailable; documentation only |
| Selected provider | None |
| Candidate / reference | The NFS-e Padrao Nacional API (gov.br/nfse) direct is the plan's proposed primary. Focus NFe, eNotas, PlugNotas and NFE.io are alternatives. Proposal only; no provider selected. |
| Accountable owner / decision | Unassigned; fiscal/accounting reviewer (EG-9) and technical integration owner required. |
| Approval / contract | None supplied; provider API/profile/package version unverified unless stated below |
| Sandbox / executed provider evidence | Unavailable; none executed |
| Residency / subprocessors / retention | Unverified; certificate custody, fiscal document retention and municipal adherence per clinic required |
| Costs / budget | Unverified; no price is an approval or a current quote |
| Required packages | Unapproved: reviewed HTTPS client or selected provider SDK; exact version unset. |
| Dependent tasks | Successor todos 4, 58 |
| External gates | EG-5, EG-9 |
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

The plan records the national API documentation as verified and municipal adherence per clinic as UNVERIFIED. CBS/IBS applicability is partially verified and needs the EG-9 reviewer.

## Contract required before use

Fiscal documents stay distinct from receipts. Emission checks the clinic's municipal adherence first, is idempotent with lookup after timeout, and supports cancellation and substitution states. Certificates never enter the repository or logs.

## Required future fixtures

Emission, rejection, cancellation, substitution, duplicate request, timeout followed by lookup and non-adherent municipality refusal.

These are acceptance requirements, not executed sandbox results. Fixtures may
contain only synthetic or explicitly provider-authorized data.

## Unavailable branch

Todo 58 builds the fiscal document model and adapter against fixtures. Live emission waits for EG-9 and EG-5.

Follow the [register's versioning and approval rules](../../capabilities.md).
