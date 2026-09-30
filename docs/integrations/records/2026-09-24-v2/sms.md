# SMS delivery

| Record field | Value |
| --- | --- |
| Capability | `sms` |
| Record version / reviewed date | 2026-09-24-v2 / 2026-09-24 UTC |
| Supersedes | [2026-09-12-v1/sms.md](../2026-09-12-v1/sms.md) |
| Status | unavailable; documentation only |
| Selected provider | None |
| Candidate / reference | Unselected; Twilio is a reference example. |
| Accountable owner / decision | Unassigned; clinic operations and messaging decision required. |
| Approval / contract | None supplied; provider API/profile/package version unverified unless stated below |
| Sandbox / executed provider evidence | Unavailable; none executed |
| Residency / subprocessors / retention | Unverified; owner-approved region, processing agreement, retention and legal-hold handling required |
| Costs / budget | Unverified; historical prices are not approval or current quotes |
| Required packages | Unapproved: selected SMS SDK/signature validator or reviewed HTTPS client; version unset. |
| Dependent tasks | Successor todos 4, 51; superseded record: renewal tasks 13, 14, 19, 44 |
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

[Twilio callback formats](https://www.twilio.com/docs/messaging/guides/webhook-request) and [request security](https://www.twilio.com/docs/usage/security) provide a provider-specific starting point. No Brazilian sender route, account or callback-validation implementation is approved.

## Contract required before use

Record Brazil destination/sender restrictions, encoding/segmentation, maximum body length, throughput and delivery-status semantics. Pin exact callback method/content type and validation of original public URL/body behind the chosen proxy; do not substitute another provider's HMAC contract. Bind verified destination/preference version at execution. Document provider idempotency, ambiguous-send reconciliation, rate limits and retry bounds.

## Required future fixtures

Authorized synthetic number and provider fixture provenance; accepted/delivered/undeliverable, multi-segment pt-BR, duplicate/out-of-order callback, forged callback and changed destination.

These are acceptance requirements, not executed sandbox results. Fixtures may
contain only synthetic or explicitly provider-authorized data.

## Unavailable branch

Task 19 SMS provider slice waits for route/account, contract, SDK and sandbox decisions by the messaging owner. Email and WhatsApp have independent gates; a working email adapter does not satisfy SMS.

Follow the [register's versioning and approval rules](../../capabilities.md).
