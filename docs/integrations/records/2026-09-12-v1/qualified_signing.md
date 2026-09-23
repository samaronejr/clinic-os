# Qualified document signing

| Record field | Value |
| --- | --- |
| Capability | `qualified_signing` |
| Record version / reviewed date | 2026-09-12-v1 / 2026-09-12 UTC |
| Status | unavailable; documentation only |
| Selected provider | None |
| Candidate / reference | Historical candidates BirdID (Soluti), VIDaaS (Valid), optionally Memed for prescription workflows; none selected. |
| Accountable owner / decision | Unassigned; clinical/signature owner and technical integration owner required. |
| Approval / contract | None supplied; provider API/profile/package version unverified unless stated below |
| Sandbox / executed provider evidence | Unavailable; none executed |
| Residency / subprocessors / retention | Unverified; owner-approved region, processing agreement, retention and legal-hold handling required |
| Costs / budget | Unverified; historical prices are not approval or current quotes |
| Required packages | Unapproved/unknown: selected signing SDK, formats, provider client and required certificate software. |
| Dependent tasks | 31, 32, 33, 34, 35, 36, 44 |
| Review trigger | Provider selection, protocol/package change or release; no approval expiry exists |

## Source-backed observations

[Soluti integration portal](https://idtech.soluti.com.br/portal-de-integracoes) and [BirdID product](https://soluti.com.br/bird-id-pro/) establish integration/product availability only. The [older manual](https://manuais.soluti.com.br/bird-id/dicas-e-duvidas/perguntas-frequentes) and [current product FAQ](https://soluti.com.br/perguntas-respondidas/posso-assinar-documentos-diretamente-atraves-do-aplicativo-bird-id/) give conflicting app-signing descriptions; neither is a backend signing contract. VIDaaS/Memed formats and API semantics remain unverified.

## Contract required before use

Obtain signed provider/version contract for supported PAdES/CAdES/CMS profiles, byte-versus-hash request semantics, hash algorithm, signed attributes, timestamps, signer/certificate chain binding and qualified ICP-Brasil evidence. Define physician authorization/step-up, document-version binding, asynchronous results, cancellation, idempotency and ambiguous-success recovery. Preserve exact source/rendered/signed bytes and separate signer success from independent verification.

## Required future fixtures

Authorized test certificate and service sandbox; exact reviewed document, altered bytes, wrong signer, expired/revoked certificate, callback forgery, duplicate operation and timeout after signing. No real physician credentials in repository/evidence.

These are acceptance requirements, not executed sandbox results. Fixtures may
contain only synthetic or explicitly provider-authorized data.

## Unavailable branch

Task 34 real signing and tasks 35/36 verified issuance require both this approval and signature_verification. Missing verified format or independent verifier keeps documents unverified; a success JSON or unsigned PDF never satisfies issuance.

Follow the [register's versioning and approval rules](../../capabilities.md).
