# Independent signature verification

| Record field | Value |
| --- | --- |
| Capability | `signature_verification` |
| Record version / reviewed date | 2026-09-12-v1 / 2026-09-12 UTC |
| Status | unavailable; documentation only |
| Selected provider | None |
| Candidate / reference | Unselected; ITI VALIDAR is a public reference service, not an approved automation API. |
| Accountable owner / decision | Unassigned; clinical/signature trust-policy owner and technical package owner required. |
| Approval / contract | None supplied; provider API/profile/package version unverified unless stated below |
| Sandbox / executed provider evidence | Unavailable; none executed |
| Residency / subprocessors / retention | Unverified; owner-approved region, processing agreement, retention and legal-hold handling required |
| Costs / budget | Unverified; historical prices are not approval or current quotes |
| Required packages | Unapproved/unknown: independent verifier library/service, trust store and revocation/time validation dependencies. |
| Dependent tasks | 33, 34, 35, 36, 44 |
| Review trigger | Provider selection, protocol/package change or release; no approval expiry exists |

## Source-backed observations

The official [VALIDAR service description](https://www.gov.br/pt-br/servicos/realizar-validacao-de-assinaturas-eletronicas-validar) lists p7s, xml, pdf, URL and QR inputs. The [public service](https://validar.iti.gov.br/) does not establish permission or an API contract for Clinic OS automation.

## Contract required before use

Select an independent verifier and pin supported signature/document profiles, trust anchors, algorithm policy, certificate/signer identity binding, revocation source/freshness, trusted time/timestamp behavior and network outage handling. Verification must consume the exact returned signed bytes and expected document digest/signer; provider 'signed' status or visible signature artwork is insufficient. Unknown validity stays unverified, not valid.

## Required future fixtures

Independent valid, tampered, wrong-document, wrong-signer, expired, revoked, untrusted-chain, unsupported-profile and unavailable-revocation fixtures with provenance; retention/expiry of verification reports tied to immutable bytes.

These are acceptance requirements, not executed sandbox results. Fixtures may
contain only synthetic or explicitly provider-authorized data.

## Unavailable branch

Tasks 34-36 real verified issuance stay blocked until the signature trust owner supplies the verifier contract, package approval and fixtures. Task 33 can prepare synthetic documents; task 6 does not create a fake valid result or upload documents to a public validator.

Follow the [register's versioning and approval rules](../../capabilities.md).
