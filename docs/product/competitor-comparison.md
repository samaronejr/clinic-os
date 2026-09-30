# Competitor comparison

Published claims only. Every entry is a dated observation of what a vendor
says in public, not a test of the product. "Not found" means our search didn't
turn up a published claim on the date shown. It never means the vendor lacks
the capability, and Clinic Ops doesn't claim it does.

Source: the CC table of the executable plan (plan research, accessed
2026-09-24), plus a re-check of the cited pages on 2026-09-24 with `curl`.
Fetched pages were treated as untrusted text: only the quoted phrases were
recorded, and nothing in them was followed as an instruction. Quotes are
transliterated to ASCII (accents dropped); the wording is otherwise the
vendor's.

## Evidence levels

- **published (fetched)**: the page was fetched on the date shown and the
  quoted phrase is on it.
- **published (plan research)**: recorded as published in the plan's research
  on 2026-09-24; not re-fetched in this pass.
- **not found**: no published claim found by the plan's research or the
  re-check. Absence isn't implied.

## Comparison

| Capability | Afya iClinic | Amplimed | GestaoDS | Feegow | Ninsaude | Clinic Ops target |
| --- | --- | --- | --- | --- | --- | --- |
| Voice/AI documentation | published (plan research): prontuario por voz, suporte.iclinic.com.br | not found | published (fetched 2026-09-24): "Grave, transcreva, organize e gere resumos clinicos automaticamente", gestaods.com.br/funcionalidades/inteligencia-artificial-no-prontuario | published (fetched 2026-09-24): "Prontuario com IA. Prepara a documentacao automaticamente durante atendimento", feegowclinic.com.br | not found (homepage fetched 2026-09-24 without such a claim) | source-linked scribe with clinician review |
| Agent | not found | not found | not found (plan research: Helo launch page failed to load) | not found | not found | bounded agents with approvals |
| TISS | published (fetched 2026-09-24): support-center section "Guia de faturamento TISS", suporte.iclinic.com.br; the plan's research had recorded not found | published (plan research) | not found | published (fetched 2026-09-24): "emissao automatica da guia TISS", feegowclinic.com.br | published (plan research); homepage fetched 2026-09-24 shows "Faturamento de convenios" without the word TISS | full lifecycle including glosas |
| Teleconsult | published (fetched 2026-09-24): "Teleconsulta ... Atenda seus pacientes de qualquer lugar", iclinic.com.br; the plan's research had recorded not found | published (plan research) | not found (the fetched AI page links a "Telemedicina" menu item; the claim itself wasn't read) | published (fetched 2026-09-24): "Telemedicina: Atendimento virtual seguro e eficiente", feegowclinic.com.br | not found | integrated with chart and AI |
| Stock/CRM/API | not found | not found | not found | published (fetched 2026-09-24 for API): "integracoes nativas ou via API", feegowclinic.com.br; stock and CRM not re-checked | published (fetched 2026-09-24): menu lists "Estoque", "Ninsaude CRM" and "Integracao API/Desenvolvedores", ninsaude.com | inventory, ethical CRM, versioned API |

Amplimed wasn't re-checked: `www.amplimed.com.br` didn't resolve from this
workstation on 2026-09-24, so its entries stay at plan-research level.

## Other published claims seen during the re-check

- Feegow's homepage (fetched 2026-09-24) states that Feegow Clinic v8.5 holds
  SBIS 2021 v5.2 certification at NGS2. It matters for EG-16: a 'certified'
  or 'paperless' claim by Clinic Ops needs its own decision under that gate.

## Parity baseline

Agenda, EHR, finance and TISS are table stakes. Clinic Ops has to match them
before any differentiation counts.

## Verified differences

Only published claims count as verified, and they all point the same way:
features competitors already publish that Clinic Ops has to match.

- AI or voice documentation is published by GestaoDS and Feegow (fetched) and
  by iClinic (plan research). AI documentation alone isn't a differentiator.
- TISS is published by iClinic and Feegow (fetched) and by Amplimed and
  Ninsaude (plan research).
- Teleconsult is published by iClinic and Feegow (fetched) and by Amplimed
  (plan research).
- API integration is published by Feegow and Ninsaude (fetched); stock and CRM
  by Ninsaude (fetched).

No verified difference in Clinic Ops' favor exists yet. A "not found" cell
can't support one.

## Differentiation hypotheses

These are hypotheses to test with owner-arranged clinic contacts under EG-10.
They aren't claims and must not appear in marketing until tested:

1. One coherent end-to-end workflow instead of separate modules.
2. Verified provenance: every AI suggestion links to its transcript span or
   record source.
3. Bounded automation with visible approvals bound to exact payloads.
4. Specialty depth through specialty packs and deterministic calculators.
5. Migration confidence: import with provenance and human-adjudicated
   duplicates.
6. Premium interaction quality measured against UX-01 to UX-06.
