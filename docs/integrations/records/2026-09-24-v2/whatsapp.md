# WhatsApp delivery

| Record field | Value |
| --- | --- |
| Capability | `whatsapp` |
| Record version / reviewed date | 2026-09-24-v2 / 2026-09-24 UTC |
| Supersedes | [2026-09-12-v1/whatsapp.md](../2026-09-12-v1/whatsapp.md) |
| Status | unavailable; documentation only |
| Selected provider | None |
| Candidate / reference | Unselected; direct Meta or a business solution provider (BSP). Twilio is a reference example. |
| Accountable owner / decision | Unassigned; clinic operations and messaging decision required. |
| Approval / contract | None supplied; provider API/profile/package version unverified unless stated below |
| Sandbox / executed provider evidence | Unavailable; none executed |
| Residency / subprocessors / retention | Unverified; owner-approved region, processing agreement, retention and legal-hold handling required |
| Costs / budget | Unverified; historical prices are not approval or current quotes |
| Required packages | Unapproved: selected BSP/Meta client and callback validator; API/package versions unset. |
| Dependent tasks | Successor todos 4, 51, 54; superseded record: renewal tasks 13, 14, 19, 41, 44 |
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

[Twilio messaging callbacks](https://www.twilio.com/docs/messaging/guides/webhook-request) is a BSP example. Direct Meta documentation retrieval in the source intake did not establish a usable contract; direct Meta API behavior remains unverified.

## Contract required before use

Select direct Meta or BSP explicitly. Record business/sender approval, opt-in requirements, allowed template names/languages/versions, conversation/category restrictions, API version and callback authentication. A provider template approval is separate from the patient's current channel preference. Pin send/delivery/status payloads, idempotency and duplicate/out-of-order handling; template revocation must stop pending sends.

## Required future fixtures

Owner-authorized business sandbox/test numbers and approved pt-BR templates; delivered/rejected/expired/template-revoked, opted-out destination, callback forgery and retry duplication.

These are acceptance requirements, not executed sandbox results. Fixtures may
contain only synthetic or explicitly provider-authorized data.

## Unavailable branch

Task 19 WhatsApp provider slice and task 41 real template configuration wait for the messaging owner, business/template/API/package approvals and sandbox. Generic reminders/settings remain runnable with clearly synthetic receipts.

Follow the [register's versioning and approval rules](../../capabilities.md).
