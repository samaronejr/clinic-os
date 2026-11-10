# RNDS national health data exchange

| Record field | Value |
| --- | --- |
| Capability | `rnds` |
| Record version / reviewed date | 2026-09-24-v2 / 2026-09-24 UTC |
| Supersedes | None; new capability in this set |
| Status | unavailable; documentation only |
| Selected provider | None |
| Candidate / reference | Direct DATASUS credentialing is the plan's proposed route: Portal de Servicos request, digital certificate, homologation, then production. No alternative is recorded. |
| Accountable owner / decision | Unassigned; interoperability owner, privacy reviewer and certificate custodian required. |
| Approval / contract | None supplied; provider API/profile/package version unverified unless stated below |
| Sandbox / executed provider evidence | Unavailable; none executed |
| Residency / subprocessors / retention | Unverified; certificate custody, exchange logs without PHI, retention and legal basis per exchange required |
| Costs / budget | Unverified; no price is an approval or a current quote |
| Required packages | Unapproved: reviewed HTTPS/FHIR client and certificate handling; exact version unset. |
| Dependent tasks | Successor todos 4, 68 |
| External gates | EG-2, EG-7 |
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

The plan records the credentialing process outline as verified from the RNDS guide on 2026-09-24. Profiles, endpoints and authentication details are unverified here.

## Contract required before use

`exchange_clinical_record` keeps raising `NotImplementedError("Phase >=1")` until todo 68 lands. Profiles and versions are pinned per release. Each exchange records its legal basis and consent where required. Authentication uses the approved certificate flow; payload tenant claims are never read.

## Required future fixtures

Homologation exchange with synthetic records, schema rejection, expired certificate, duplicate submission and outage with retry.

These are acceptance requirements, not executed sandbox results. Fixtures may
contain only synthetic or explicitly provider-authorized data.

## Unavailable branch

Todo 68 builds the adapter against fixtures. RNDS live waits for EG-7 and EG-2.

## Todo 68 update (2026-11-10)

This is a dated note, not a new record version: no provider was selected, no
protocol is approved, and every field above stays as recorded.

- Read on 2026-11-10 from rnds-guia.saude.gov.br: passo-a-passo, ambientes,
  servicos, definicoes, conhecer, homologar, requisicoes, erros, autenticacao,
  certificado and conector. The only supported use case is the laboratory
  result (REL, COVID-19 and Monkeypox), sent as one `document` Bundle by
  `POST /api/fhir/r4/Bundle` with profiles BRResultadoExameLaboratorial-1.1,
  BRDiagnosticoLaboratorioClinico-1.0 and BRAmostraBiologica-1.0. Auth is
  `GET /api/token` over mutual TLS with an ICP-Brasil certificate (30-minute
  token); homologation hosts are `ehr-auth-hmg.saude.gov.br` and
  `ehr-services.hmg.saude.gov.br`, production EHR hosts are per state.
- The definitions package zip returned HTTP 404 (http and https), so profile
  validation is pinned to the guide's documentation example (dated 2020-10-02).
- `exchange_clinical_record` no longer raises `NotImplementedError`. It builds
  and validates REL bundles from a typed exam document and sends them only
  through the recorded fixture transport (provider `rnds-fixture`, environment
  `homologation`), behind `INTEROP_RNDS_HOMOLOGATION` (off by default), an
  approved `sandbox` lifecycle version, the patient's `rnds_exchange` consent
  and `result.release` authority. The mutual-TLS transport is tested only
  against a local CA. See `apps/interop/README.md`.
- Still unavailable: credentialing, the homologation evidence PDF, production
  access, the per-exchange legal basis (LGPD art. 11, open for the owner's
  compliance reviewer) and any executed provider evidence.

Follow the [register's versioning and approval rules](../../capabilities.md).
