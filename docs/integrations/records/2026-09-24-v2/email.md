# Email delivery

| Record field | Value |
| --- | --- |
| Capability | `email` |
| Record version / reviewed date | 2026-09-24-v2 / 2026-09-24 UTC |
| Supersedes | [2026-09-12-v1/email.md](../2026-09-12-v1/email.md) |
| Status | unavailable; documentation only |
| Selected provider | None |
| Candidate / reference | Unselected; SES is a reference example, not a historical selection. |
| Accountable owner / decision | Unassigned; clinic operations and messaging decision required. |
| Approval / contract | None supplied; provider API/profile/package version unverified unless stated below |
| Sandbox / executed provider evidence | Unavailable; none executed |
| Residency / subprocessors / retention | Unverified; owner-approved region, processing agreement, retention and legal-hold handling required |
| Costs / budget | Unverified; historical prices are not approval or current quotes |
| Required packages | Unapproved: selected provider SDK or reviewed HTTPS client; exact distribution/version unset. |
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

[SES production access](https://docs.aws.amazon.com/ses/latest/dg/request-production-access.html) describes a sandbox and a separate production-access request. This is an example capability, not evidence that Clinic OS has an account, verified sender or permission to send.

## Contract required before use

Approve sender/domain, recipient verification, purpose and pt-BR templates with minimum appointment logistics. Pin send/request/response and bounce/complaint/delivery schemas, regional endpoints, authentication and callback validation. Record per-endpoint idempotency support; local operation IDs alone do not make a provider retry safe. Specify timeouts, ambiguous-send reconciliation and bounded retries. Provider acceptance is distinct from recipient delivery.

## Required future fixtures

Owner-authorized synthetic recipient, accepted/delivered/bounced/complaint fixtures; duplicate callback, wrong signature/token, timeout after acceptance, invalidated destination and opt-out.

These are acceptance requirements, not executed sandbox results. Fixtures may
contain only synthetic or explicitly provider-authorized data.

## Unavailable branch

Task 19 email provider slice waits for the named messaging owner, sender approval, versioned contract, package approval and authorized sandbox. Tasks 13/14 and synthetic email behavior may proceed; no message is marked delivered from a mock.

Follow the [register's versioning and approval rules](../../capabilities.md).
