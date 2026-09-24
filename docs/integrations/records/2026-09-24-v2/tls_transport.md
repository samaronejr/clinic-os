# TLS transport policy

| Record field | Value |
| --- | --- |
| Capability | `tls_transport` |
| Record version / reviewed date | 2026-09-24-v2 / 2026-09-24 UTC |
| Supersedes | [2026-09-12-v1/tls_transport.md](../2026-09-12-v1/tls_transport.md) |
| Status | unavailable; documentation only |
| Selected provider | None |
| Candidate / reference | Unselected ingress/hosting/provider stack; TLS 1.3 is the project policy. |
| Accountable owner / decision | Unassigned; infrastructure/security owner required for endpoint inventory and exceptions. |
| Approval / contract | None supplied; provider API/profile/package version unverified unless stated below |
| Sandbox / executed provider evidence | Unavailable; none executed |
| Residency / subprocessors / retention | Unverified; owner-approved region, processing agreement, retention and legal-hold handling required |
| Costs / budget | Unverified; historical prices are not approval or current quotes |
| Required packages | Unapproved where new: selected terminator/client/CA tooling; existing locked clients do not prove deployment. |
| Dependent tasks | Successor todos 73; superseded record: renewal tasks 27, 38 external transports; 43 implementation; 44 evidence; 46/47 live use |
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

[RFC 8446](https://www.rfc-editor.org/rfc/rfc8446) specifies TLS 1.3. Current production settings require secure redirects/cookies and the DB URL validator requires verified transport; deployment negotiation, certificate renewal and every upstream endpoint remain separate evidence.

## Contract required before use

Inventory browser-to-ingress, ingress-to-app, PostgreSQL, Redis/broker, storage, KMS/secrets, webhooks and provider control/media paths. Require TLS 1.3 where applicable, hostname/chain verification and renewal/expiry handling; document each Unix-socket/internal and media protocol boundary rather than calling all traffic TLS. Any provider protocol/compatibility exception needs explicit security approval and evidence, not silent downgrade. No verify=False or plaintext fallback.

## Required future fixtures

Task 43/44 record actual endpoint protocol/cipher/peer verification and expiry/renewal rehearsal; reject wrong hostname, untrusted/expired certificate and prohibited version. Baseline local DB TLS evidence is not hosted ingress/provider evidence.

These are acceptance requirements, not executed sandbox results. Fixtures may
contain only synthetic or explicitly provider-authorized data.

## Unavailable branch

Task 44 tls_transport approval and dependent live actions wait for endpoint inventory, certificate/renewal ownership, approved exceptions and observed selected deployment. Task 1 local TLS success alone cannot close this capability.

Follow the [register's versioning and approval rules](../../capabilities.md).
