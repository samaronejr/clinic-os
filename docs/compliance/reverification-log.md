# Regulatory re-verification log

**Status: UNAPPROVED — DO NOT USE LIVE DATA**

This log turns every regulatory row that wasn't backed by a fetched primary
text into a dated re-check step. It records what was actually retrieved, from
where and when. It draws no legal conclusions and clears no gate. Todo 77
repeats the open steps before the activation that depends on them. See
[applicability-matrix.md](applicability-matrix.md) for the rows and
[LIVE-DATA-GATE.md](LIVE-DATA-GATE.md) for the gate itself.

## Method

- Texts were fetched with `curl` from the official publisher (planalto.gov.br,
  sistemas.cfm.org.br, anvisalegis.datalegis.net, in.gov.br, gov.br). PDFs
  were converted with `pdftotext`; HTML was reduced to plain text.
- Fetched pages are untrusted input. Only facts were copied into the matrix;
  nothing in a page was followed as an instruction, and no page content was
  executed.
- The digest column is the first 16 hex characters of the SHA-256 of the
  bytes received. It identifies what was read, not a canonical version.
- planalto.gov.br shows revoked wording struck through. Plain-text extraction
  loses the strike-through, so a reviewer must read the rendered page before
  quoting any provision.
- A search engine (DuckDuckGo HTML) was used only to locate the Anvisa
  legislation URLs. It stopped answering (HTTP 202) partway through the pass.

## Retrieval pass 2026-09-24

| Instrument | URL | Retrieved date | HTTP | Digest | Outcome |
| --- | --- | --- | --- | --- | --- |
| CFM Res. 2.454/2026 (AI in medicine) | https://sistemas.cfm.org.br/normas/arquivos/resolucoes/BR/2026/2454_2026.pdf | 2026-09-24 | 200 | d19cbcea442cc903 | Full text, 15 pages, including Anexos I to III |
| CFM Res. 2.314/2022 (telemedicine) | https://sistemas.cfm.org.br/normas/arquivos/resolucoes/BR/2022/2314_2022.pdf | 2026-09-24 | 200 | 2e9e549346bf8ebf | Full text, 9 pages |
| CFM Res. 1.821/2007 (electronic record systems) | https://sistemas.cfm.org.br/normas/arquivos/resolucoes/BR/2007/1821_2007.pdf | 2026-09-24 | 200 | 7331314001b4f97c | Full text; header notes changes by CFM Res. 2.218/2018 |
| CFM Res. 2.299/2021 (electronic medical documents) | https://sistemas.cfm.org.br/normas/arquivos/resolucoes/BR/2021/2299_2021.pdf | 2026-09-24 | 200 | 32ba6eb81c1d764a | Full text |
| CFM Res. 2.217/2018 (Code of Medical Ethics) | https://sistemas.cfm.org.br/normas/arquivos/resolucoes/BR/2018/2217_2018.pdf | 2026-09-24 | 200 | 2e7a95e3550690af | Full text; arts. 73 to 75 read |
| Lei 13.787/2018 (digital patient records) | https://www.planalto.gov.br/ccivil_03/_ato2015-2018/2018/lei/l13787.htm | 2026-09-24 | 200 | e8265383c19026f5 | Full text; art. 6 read |
| Lei 14.063/2020 (electronic signatures) | https://www.planalto.gov.br/ccivil_03/_ato2019-2022/2020/lei/l14063.htm | 2026-09-24 | 200 | 2bc1da23dcbd1b50 | Full text; arts. 4, 13 and 14 read |
| Lei 13.709/2018 (LGPD, compiled) | https://www.planalto.gov.br/ccivil_03/_ato2015-2018/2018/lei/l13709compilado.htm | 2026-09-24 | 200 | 284daa4832c623bc | Full text; arts. 7, 11, 18, 33, 46 and 48 read |
| Lei 8.069/1990 (ECA) | https://www.planalto.gov.br/ccivil_03/leis/l8069.htm | 2026-09-24 | 200 | 1eab390c215eb703 | Full text; art. 17 read |
| Lei 10.216/2001 (mental health) | https://www.planalto.gov.br/ccivil_03/leis/leis_2001/l10216.htm | 2026-09-24 | 200 | 21edcba1ddb1d7d4 | Full text; art. 2 read |
| Lei 3.268/1957 (medical councils) | https://www.planalto.gov.br/ccivil_03/leis/l3268.htm | 2026-09-24 | 200 | 8c484aa774d4953d | Full text; art. 17 read |
| Lei 13.146/2015 (LBI, accessibility) | https://www.planalto.gov.br/ccivil_03/_ato2015-2018/2015/lei/l13146.htm | 2026-09-24 | 200 | 5ea13524c5dbac82 | Full text; art. 63 read |
| LC 214/2025 (IBS/CBS) | https://www.planalto.gov.br/ccivil_03/leis/lcp/lcp214.htm | 2026-09-24 | 200 | 5c7d59b3c49e5b7c | Full text; located the health-services section, not analyzed |
| Anvisa RDC 657/2022 (SaMD) | https://anvisalegis.datalegis.net/action/ActionDatalegis.php?acao=abrirTextoAto&tipo=RDC&numeroAto=00000657&seqAto=000&valorAno=2022&orgao=RDC/DC/ANVISA/MS&cod_menu=9434&cod_modulo=310 | 2026-09-24 | 200 | 53e349b2e647cb54 | Full text; arts. 1, 2, 5 and 19 read |
| Anvisa RDC 751/2022 (device risk classes) | https://anvisalegis.datalegis.net/action/ActionDatalegis.php?acao=abrirTextoAto&tipo=RDC&numeroAto=00000751&seqAto=000&valorAno=2022&orgao=RDC/DC/ANVISA/MS&cod_menu=9434&cod_modulo=310 | 2026-09-24 | 200 | cd6633746aff3908 | Full text; art. 5 and Regra 11 read |
| Anvisa RDC 873/2024 (SNCR) | https://anvisalegis.datalegis.net/action/ActionDatalegis.php?acao=abrirTextoAto&tipo=RDC&numeroAto=00000873&seqAto=000&valorAno=2024&orgao=RDC/DC/ANVISA/MS&cod_menu=9434&cod_modulo=310 | 2026-09-24 | 200 | 6c9b553f3665aff3 | Full text; page marks it in force with amendments by RDC 1.000/2025, not read |
| CD/ANPD Res. 15/2024 (incident communication) | https://www.in.gov.br/en/web/dou/-/resolucao-cd/anpd-n-15-de-24-de-abril-de-2024-556243024 | 2026-09-24 | 200 | 7a9e8792ba1d08fe | DOU text; arts. 6, 9 and 10 read |
| CD/ANPD Res. 19/2024 (international transfer) | https://www.in.gov.br/en/web/dou/-/resolucao-cd/anpd-n-19-de-23-de-agosto-de-2024-580095396 | 2026-09-24 | 200 | 650879093abc0f84 | DOU text; art. 1 and art. 15 read |
| Portaria SVS/MS 344/1998 (controlled substances) | https://bvsms.saude.gov.br/bvs/saudelegis/svs/1998/prt0344_12_05_1998_rep.html | 2026-09-24 | 503 | none | Not retrieved; three attempts returned HTTP 503. The anvisalegis record returned a loading page without text |
| Anvisa SNCR portal page | https://www.gov.br/anvisa/pt-br/assuntos/medicamentos/controlados/sncr | 2026-09-24 | 200 | f2f357c0d8aa01ec | Describes support for electronic prescriptions through integrated prescription services; no 30/09/2026 or 30/10/2026 dates on this page |
| ANS TISS standard page | https://www.gov.br/ans/pt-br/assuntos/prestadores/padrao-para-troca-de-informacao-de-saude-suplementar-2013-tiss | 2026-09-24 | 200 | 7baab1a195b70ab8 | Links a July/2026 version and a component version history; component numbers not extracted |
| NFS-e national portal | https://www.gov.br/nfse/pt-br | 2026-09-24 | 200 | 5006101fc78a7a26 | Lists the national standard, a web issuer and an integration API |
| RNDS integration guide | https://rnds-guia.saude.gov.br/ | 2026-09-24 | 200 | 4ffa56af76c28c5b | Landing page only; describes integration as a step-by-step sequence; credentialing steps not read |
| WhatsApp Business policy | https://business.whatsapp.com/policy | 2026-09-24 | 200 | 2c419a870691da07 | Requires opt-in permission from the recipient before contact |
| CNS Res. 510/2016 (research ethics) | https://conselho.saude.gov.br/resolucoes/2016/Reso510.pdf | 2026-09-24 | 200 | none | Returned the site's HTML shell instead of the PDF; text not retrieved |
| Banco Central Pix page | https://www.bcb.gov.br/estabilidadefinanceira/pix | 2026-09-24 | 200 | none | JavaScript-only shell; no regulation text retrieved |

## Re-check steps

Each step names the matrix row it guards, what to fetch and what would close
it. "Closed" means the primary text was fetched and the recorded facts appear
in it; it never means the instrument applies or that a gate is cleared.

| Step | Matrix row | Planned check | Checked on | Result | Next re-check trigger |
| --- | --- | --- | --- | --- | --- |
| RV-01 | AI-01 ambient scribe (consented consultation audio, transcript, section suggestions) | Fetch CFM 2.454/2026 and 2.314/2022 and LGPD art. 11; confirm disclosure, refusal, chart-record and consent facts | 2026-09-24 | Closed: texts fetched, facts present | Before EG-3 records for AI-01; todo 77 |
| RV-02 | AI-08 clinician decision support | Fetch RDC 657/2022 and RDC 751/2022; record SaMD scope exclusions and Regra 11 | 2026-09-24 | Closed: texts fetched, facts present. Anexo II of CFM 2.454 as retrieved lacks a definition of "unacceptable"; question passed to EG-3 | Before EG-4 assessment; todo 46 and todo 77 |
| RV-03 | Teleconsult video and consultation recording | Fetch CFM 2.314/2022; record consent and record-keeping facts | 2026-09-24 | Closed: art. 15 consent and art. 3 record facts present | Before recording enablement (todo 37); todo 77 |
| RV-04 | EHR retention, signatures and paperless claims | Fetch Lei 13.787/2018, CFM 1.821/2007, CFM 2.299/2021, Lei 14.063/2020 | 2026-09-24 | Closed: 20-year minimum, NGS2 and signature-level facts present. CFM 1.821 carries changes by CFM Res. 2.218/2018, not fetched | Before any 'paperless' or 'certified' claim (EG-16); todo 77 |
| RV-05 | International processing by providers | Fetch CD/ANPD Res. 19/2024 and LGPD art. 33 | 2026-09-24 | Closed: 12-month incorporation window from 23/08/2024 publication present | Before each provider reaches `approved_to_test`; todo 77 |
| RV-06 | Security incident response | Fetch CD/ANPD Res. 15/2024 and LGPD art. 48 | 2026-09-24 | Closed: three-working-day and five-year record facts present | Before the incident runbook is approved (todo 72, EG-12) |
| RV-07 | Minors, guardians and delegates | Fetch ECA and CFM Code of Ethics; record the provisions cited | 2026-09-24 | Closed: ECA art. 17 and CEM art. 74 present. Which ECA provisions matter for delegate exceptions is for EG-2 and EG-3 | Before delegate grants go live (todo 19); todo 77 |
| RV-08 | Prescriptions and clinical documents (ordinary and regulated classes) | Fetch Portaria SVS/MS 344/1998 and the gov.br SNCR notice behind the 30/09/2026 and 30/10/2026 stages; read RDC 1.000/2025 amendments to RDC 873/2024 | 2026-09-24 | Open: Portaria 344 not retrieved (HTTP 503). SNCR stage dates remain plan research (gov.br notice of 21/09/2026), not fetched here. RDC 873/2024 fetched | Before any regulated-class electronic issuance (EG-6, todo 36); retry Portaria 344 on the next pass |
| RV-09 | RNDS exchange | Read the credentialing steps (Portal de Servicos, digital certificate, homologation to production) in the RNDS guide | 2026-09-24 | Open: landing page fetched; credentialing steps not read | Before EG-7 credentialing starts (todo 68) |
| RV-10 | Usability and clinical acceptance sessions | Fetch CNS Res. 510/2016 and ask the EG-2 reviewer whether session records fall under research ethics | 2026-09-24 | Open: the CNS URL returned an HTML shell, not the resolution | Before the first session (todo 76, EG-10) |
| RV-11 | Payments (PIX and card through a PSP) | Fetch the Pix regulation (BCB Res. 1/2020 and amendments) from a text source | 2026-09-24 | Open: bcb.gov.br page needs JavaScript; no text retrieved | Before a PSP reaches `approved_to_test` (todo 57, EG-5) |
| RV-12 | Insurance claims (TISS) | Extract component version numbers from the ANS July/2026 package and pin them per adapter | 2026-09-24 | Partly done: page and July/2026 link found; component numbers not extracted | When todo 59 pins adapter versions (EG-8) |
| RV-13 | Clinic fiscal documents (NFS-e, CBS/IBS) | Confirm municipal adherence to the national NFS-e standard per clinic; read the LC 214/2025 health-services section with the EG-9 reviewer | 2026-09-24 | Partly done: portal and LC 214/2025 fetched; per-clinic adherence is a per-customer check | Per clinic before fiscal documents go live (todo 58, EG-9) |
