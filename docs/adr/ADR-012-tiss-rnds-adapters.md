# ADR-012: TISS and RNDS adapters behind the interop boundary

- Status: accepted 2026-09-24
- Recorded by: todo 2

## Context

Insurance billing uses the ANS TISS standard, which ships as separately
versioned components. RNDS exchange needs DATASUS credentialing and
homologation (EG-7). `interop.exchange_clinical_record` is a deferred stub
that raises `NotImplementedError("Phase >=1")`. Hardcoding one TISS version
would silently break when ANS publishes a new one.

## Decision

TISS and RNDS adapters sit behind the `interop` boundary. Component
versions are pinned and re-verified on every release. As of the ANS page
dated 2026-07-28, the pins are:

| Component | Version |
| --- | --- |
| Organizacional | 202601 |
| Conteudo e Estrutura | 202511 |
| TUSS | 202601 |
| Seguranca | 202511 |
| Comunicacao | 04.03.00 / 01.06.00 |

## Consequences

- A release checklist item re-reads the ANS page and updates the pins with a
  dated note.
- Guide and batch XML is validated against the pinned XSDs (todo 59 adds
  lxml/xmlschema under SC-19).
- FHIR-shaped data alone is never presented as RNDS integration; only a
  credentialed, homologated adapter counts.
- Per-operadora connectivity stays behind EG-8.

## Rejected

- A hardcoded single version. It fails without warning when ANS publishes a
  new component version.

## Revisit trigger

An ANS publication of a new TISS component version.

## Owning todos

59, 68.
