# PIX charging and reconciliation

| Record field | Value |
| --- | --- |
| Capability | `pix` |
| Record version / reviewed date | 2026-09-12-v1 / 2026-09-12 UTC |
| Status | unavailable; documentation only |
| Selected provider | None |
| Candidate / reference | Asaas is a historical candidate; no provider selected. |
| Accountable owner / decision | Unassigned; billing/finance owner and technical integration owner required. |
| Approval / contract | None supplied; provider API/profile/package version unverified unless stated below |
| Sandbox / executed provider evidence | Unavailable; none executed |
| Residency / subprocessors / retention | Unverified; owner-approved region, processing agreement, retention and legal-hold handling required |
| Costs / budget | Unverified; historical prices are not approval or current quotes |
| Required packages | Unapproved: selected billing SDK or reviewed HTTPS client; exact version unset. |
| Dependent tasks | 13, 37, 38, 39, 40, 43, 44 |
| Review trigger | Provider selection, protocol/package change or release; no approval expiry exists |

## Source-backed observations

Asaas [webhook creation](https://docs.asaas.com/docs/create-new-webhook-via-api) and [event reception](https://docs.asaas.com/docs/receive-asaas-events-at-your-webhook-endpoint) describe a shared asaas-access-token header, not HMAC, and at-least-once delivery with event-ID deduplication. Its [sandbox](https://docs.asaas.com/docs/sandbox-1) separates production credentials and confirms receipt through the UI; no dedicated API confirmation endpoint is documented.

## Contract required before use

Pin API/version and exact charge/QR/expiration/cancellation/reconciliation schemas, BRL decimal-to-integer minor-unit rules and settlement states. Require a separately configured webhook token and authenticate before operation lookup. Verify provider account/environment, amount/currency and stored invoice mapping; ignore callback tenant claims. Obtain endpoint-specific charge-creation idempotency/lookup semantics, including timeout after creation; webhook deduplication alone is insufficient.

## Required future fixtures

Owner-authorized sandbox charge and UI-confirmed receipt, duplicate/out-of-order callbacks, wrong token/account/amount/currency, expired charge, unknown operation and timeout/reconciliation. Exactly one settlement receipt per accepted payment.

These are acceptance requirements, not executed sandbox results. Fixtures may
contain only synthetic or explicitly provider-authorized data.

## Unavailable branch

Task 38 real charge adapter and task 39 real settlement/receipt wait for finance owner, provider/endpoint/package approvals and sandbox. Generic invoices and synthetic PIX journeys can proceed without creating charges or claiming settlement.

Follow the [register's versioning and approval rules](../../capabilities.md).
