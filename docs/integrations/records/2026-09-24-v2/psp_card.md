# Card payments, installments and split

| Record field | Value |
| --- | --- |
| Capability | `psp_card` |
| Record version / reviewed date | 2026-09-24-v2 / 2026-09-24 UTC |
| Supersedes | None; new capability in this set |
| Status | unavailable; documentation only |
| Selected provider | None |
| Candidate / reference | The plan's shortlist is Asaas, Pagar.me, Stone, Iugu and Efi, with no primary. PIX stays covered by the carried-forward [`pix` record](pix.md). Proposal only; no provider selected. |
| Accountable owner / decision | Unassigned; billing/finance owner and technical integration owner required. |
| Approval / contract | None supplied; provider API/profile/package version unverified unless stated below |
| Sandbox / executed provider evidence | Unavailable; none executed |
| Residency / subprocessors / retention | Unverified; processing agreement, card-data scope, retention and legal-hold handling required |
| Costs / budget | Unverified; no price is an approval or a current quote |
| Required packages | Unapproved: selected PSP SDK or reviewed HTTPS client; exact version unset. |
| Dependent tasks | Successor todos 4, 56, 57 |
| External gates | EG-1, EG-2, EG-5 |
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

The plan's provider matrix marks every shortlisted PSP as UNVERIFIED.

## Contract required before use

Card data is tokenized by the provider; card numbers and security codes never touch Clinic Ops servers, logs or evidence. Charges, installments, refunds, chargebacks, split and payouts post to the operational journal (todo 56) in integer centavos. Webhooks are authenticated with the provider's exact scheme before operation lookup; tenant claims in payloads are ignored.

## Required future fixtures

Authorized sandbox charge, installment plan, partial and full refund, chargeback, forged and replayed webhook, out-of-order events and timeout reconciliation.

These are acceptance requirements, not executed sandbox results. Fixtures may
contain only synthetic or explicitly provider-authorized data.

## Unavailable branch

Todo 57 builds the adapter and reconciliation against fixtures. Real card processing waits for EG-1, EG-2 and EG-5.

Follow the [register's versioning and approval rules](../../capabilities.md).
