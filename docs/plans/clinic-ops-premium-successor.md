# Clinic Ops premium successor contract

Status: **binding successor contract**, adopted 2026-09-24 against `main`
`b39ae436590c8a9c00ad8fc93d619875d1822827`. It records product decisions and
their owners. It doesn't claim that any of them are built, approved or live.
The machine-readable ledger is
[clinic-ops-premium-successor.supersession.json](clinic-ops-premium-successor.supersession.json),
and `tests/infra/test_successor_contract.py` checks it.

## Mandate

Clinic Ops becomes a premium outpatient SaaS for Brazilian clinics, with
governed AI embedded in the work. Staff, clinicians, patients and delegates
use one workspace for the whole outpatient day: scheduling rooms and
professionals, arrivals, a longitudinal chart, recorded consultations turned
into draft notes the clinician reviews, results and follow-ups that never
drop, prescriptions and documents, video visits, a patient portal, one
inbox for official messaging channels, and the money side (payments,
insurance claims, fiscal documents, stock and reports).

The audience assumption is 3 to 30 clinicians per clinic with reception and
finance staff, a simple solo configuration and a credible multi-unit path.
It's a planning assumption, not customer research. The customer-facing brand
is **Clinic Ops**; code identifiers keep `clinic-os` and `clinic_*`.

The Django/PostgreSQL monolith stays the system of record. Tenant isolation,
row-level security, the audit chain, envelope encryption, signatures,
no-hard-delete triggers, PHI-free telemetry and recovery requirements carry
over unchanged and extend to every new table, stream, worker and agent.

AI helps inside each task. A person approves anything clinical or financial.
AI never diagnoses, prescribes, finalizes a note or messages a patient about a
clinical matter on its own. Every external provider moves through a visible
lifecycle and reaches real patients or real money only after its external
gates clear.

## Authority and precedence

1. This contract supersedes the product-scope bans listed in the ledger below.
   Historical documents keep their text. Each one gains a single line pointing
   here, so readers see both the old decision and its successor.
2. `docs/plans/clinic-os-phase1a-approved.md` and its `.sha256` sidecar stay
   frozen byte for byte (SD-13). Nothing in this contract edits them.
3. The executable plan (`.omo/plans/clinic-ops-premium-intelligence.md`, local
   to the planning workspace) owns todo text, ordering and acceptance commands.
   Todo numbers below refer to that plan.
4. A class-3 obligation (safety, security, live data or provenance) can only
   be preserved or added. No successor decision weakens one.
5. Where a historical doc and this contract disagree about product scope, this
   contract wins. Where they disagree about a safety obligation, the stricter
   rule wins.

## Scope

Every requirement of the owner specification
(`clinic-ops-ambitious-product-plan.md`, version 1.0, evidence checked
2026-09-24) is in scope:

| Area | Requirement IDs |
| --- | --- |
| Identity and access | ID-01 |
| Patients and delegates | PAT-01 |
| Scheduling and operations | SCH-01, OPS-01 |
| Clinical record | EHR-01, EHR-02, RES-01 |
| Prescribing, documents and video | RX-01, TEL-01 |
| Communication and follow-up | MSG-01, CARE-01 |
| Revenue and operations | FIN-01, INS-01, INV-01, CRM-01, BI-01 |
| Data and platform | DATA-01, SAAS-01 |
| AI and agents | AI-01, AI-02, AI-03, AI-04, AI-05, AI-06, AI-07, AI-08, AI-09, AI-10, AI-11, AI-12 |
| Premium quality contract | UX-01, UX-02, UX-03, UX-04, UX-05, UX-06 |
| Cross-cutting sections | sec.6 architecture and ADRs, sec.7 performance and reliability, sec.8 security, privacy, regulatory and clinical governance, sec.9 waves A to G, sec.10 economics and staffing, sec.11 scorecard, sec.12 acceptance scenarios, sec.13 handoff and non-goals |
| Completeness checks | G1, G2, G3, G4, G5, G6, G7 |
| Journeys | J1, J2, J3, J4, J5, J6, J7 |

No MVP subset is carved out. Waves are dependency and review boundaries, not
permission to ship shallow modules.

### Non-goals

- No inpatient hospital information system, hospital pharmacy or
  controlled-drug dispensing.
- No autonomous diagnosis, prescribing, treatment change, clinical
  finalization or unmediated clinical communication by AI.
- No custom foundation-model training, model-written SQL or arbitrary
  SQL/shell/HTTP/filesystem/browser tools for agents.
- No microservice or Kubernetes proliferation, unofficial WhatsApp automation,
  automatic patient merge or silent provider fallback across jurisdictions.
- No production spend, contracts, credentials, `terraform plan`/`apply`, real
  patient data or provider activation without the matching external gate.
- No fabricated approvals, customers, interview results, testimonials,
  pricing validation, certifications or competitor-absence claims.

## Supersession ledger

Classes: **1** product scope, **2** implementation mechanism, **3** safety,
security, live-data or provenance obligation. Dispositions: SUPERSEDED,
CONDITIONALLY SUPERSEDED, REPLACED, REWRITTEN, EXTENDED, PRESERVED, ADDED.
The table copies the planning draft's supersession map. The JSON ledger holds
the same rows as `{id, source_path, source_lines, class, disposition,
replaced_by_todo}`: one tracked file and line range where the item lives in
this tree, an integer class, a disposition written as `KEYWORD: detail` (the
keyword is machine-checked, the detail copies this table), and the list of
todos that deliver or guard the disposition. It splits the mixed SD-8 row into SD-8a (autosave ban, class 1)
and SD-8b (browser storage ban, class 3) so each part carries one class.

| SD | Historical item (path) | Class | Disposition |
| --- | --- | --- | --- |
| SD-1 | Renewal roadmap exclusions: transcription/recording, automated clinical recommendations, TISS/insurance, controlled Rx, full SNCR/RNDS, inventory, native apps, advanced BI (docs/plans/clinic-os-renewal-roadmap.md:56-60 at b39ae43) | 1 product scope | SUPERSEDED by this contract and ADR-000; the roadmap keeps its history and gains a 'superseded in part' line |
| SD-2 | 'Patient merging by demographics' forbidden | 3 safety | PRESERVED (the specification also forbids auto-merge); human-adjudicated merge added |
| SD-3 | 'Fake signature verification' forbidden | 3 safety | PRESERVED |
| SD-4 | Teleconsult recording/transcription DB trigger (apps/teleconsult/migrations/_teleconsult_sql.py:113,116) | 1 scope via 2 mechanism | REPLACED by an additive migration: recording/transcription allowed only with a RecordingSession bound to consent and capability; the trigger is rewritten, not dropped |
| SD-5 | `real_enabled=False` permanent in capability modules (comms, teleconsult, billing, prescription, identity) and the tests asserting it | 2 implementation | REPLACED by the provider-lifecycle registry (researched to revoked); the real path is reachable only when state is `activated` AND the live gate is satisfied; tests are rewritten to assert the gate, not the constant |
| SD-6 | Rx CHECK `category=synthetic_non_controlled` (apps/prescription/models.py:45-51) and policy.py | 1+2 | REPLACED by a prescription-class taxonomy (simple, controle especial, antimicrobial/retention, notificacao A/B/B2, retinoids, thalidomide) with per-class eligibility gates; the synthetic category stays for rehearsal |
| SD-7 | Deferred stubs `issue_prescription`, `apply_retention_policy`, `exchange_clinical_record` and their pin (tests/infra/test_module_boundaries.py:86-105; AGENTS.md) | 2 | REPLACED per stub when its todo lands; the pin is updated in the same commit |
| SD-8 | No autosave / no browser storage (apps/ehr/AGENTS.md:31, static/AGENTS.md:30) | mixed | Autosave SUPERSEDED (server-side durable only); no browser storage of clinical text or audio PRESERVED |
| SD-9 | No npm/bundler/JS lint, no client bundle (static/AGENTS.md:4; PRODUCT.md:77-78 at b39ae43) | 1 | CONDITIONALLY SUPERSEDED: a Node toolchain (npm, bundler, JS lint) is allowed only under `frontend/`, for islands, and only if the agenda vertical slice selects them (ADR-001); committed build plus drift CI; the HTMX JS-off baseline is PRESERVED |
| SD-10 | Questionnaire branching excluded (docs/clinical/questionnaires.md:22-23) | 1 | SUPERSEDED with declarative, deterministic branching (no expressions, no eval) |
| SD-11 | Consent purpose only teleconsultation (apps/consent/models.py:40-45) | 2 | EXTENDED with a purpose taxonomy (recording, AI processing, messaging, marketing, research) |
| SD-12 | PRODUCT.md 'non-controlled prescriptions', 'no richer client'; capabilities.md 'transcription remains excluded' (docs/integrations/capabilities.md:54-57) | 1 | REWRITTEN by this contract's todo |
| SD-13 | Frozen docs/plans/clinic-os-phase1a-approved.md and .sha256; isolation ledger snapshot in every CI job | 3 provenance | PRESERVED byte for byte |
| SD-14 | Live-data gate, LIVE-DATA-GATE boxes, synthetic evidence never satisfies live, audit payload keys, no hard deletes, RLS, encryption, PHI-free logs | 3 | PRESERVED and extended to every new domain |
| SD-15 | 'No microservices/Kubernetes' | 1 but aligned | PRESERVED as the default (monolith plus a separate ASGI realtime process and workers); revisit triggers documented |
| SD-16 | Chromium-only browser verification (tests/renewal/browser/conftest.py:61-65) | 2 | EXTENDED to Firefox/WebKit and a real-device matrix |
| SD-17 | CFM Res. 2.454/2026 (in force August 2026) | 3 new obligation | ADDED: patient information and right to refuse AI, human decision, AI-use record in the chart, risk classification, support for the clinic's AI and Telemedicine commission |

## Evidence levels

Levels are never conflated. A todo claims only the level it produced.

| Level | Meaning | Example |
| --- | --- | --- |
| L0 | Source present in the tree | a file, function or test exists at a commit |
| L1 | Local test run | command, exit code and junit report from a workstation |
| L2 | Hosted CI run | a GitHub Actions run id green on the exact commit |
| L3 | Provider sandbox receipt | an executed call against an owner-approved sandbox |
| L4 | Production-authorized approval record | an external, accountable approval; never produced by an agent |

Synthetic evidence never satisfies L3 or L4. A higher level doesn't imply the
lower ones were recorded; each receipt names its own level.

## External gates

Only the owner or an accountable human clears a gate. Todos deliver synthetic
or sandbox slices and tests proving that activation refuses without the gate's
record. No todo marks a gate satisfied.

| EG | Gate | Blocks |
| --- | --- | --- |
| EG-1 | Owner spend authorization per provider, sandbox or device cloud (hosted CI already runs; run 36045032066 is green on the baseline) | any paid provider, sandbox or device-cloud call |
| EG-2 | Qualified Brazilian privacy review: ROPA/DPIA per feature, international transfer basis, processor contracts | real data, real AI provider use |
| EG-3 | Clinical safety owner (physician) plus CFM 2.454/2026 mapping, AI risk levels, safety messaging, escalation staffing policy | scribe, assistant and decision support live; shadow evaluation on real data; urgent-concern workflow live |
| EG-4 | Anvisa SaMD intended-use and classification assessment | AI-08 and any diagnostic claim |
| EG-5 | Provider contracts and DPAs (ASR, LLM, video, messaging/BSP, PSP, storage/KMS/scan, ICP-Brasil signing, Rx partner) | `production_authorized` |
| EG-6 | SNCR integration credentials, homologation and per-class legal review | regulated Rx electronic issuance |
| EG-7 | RNDS credentialing (DATASUS Portal de Servicos, digital certificate, homologation to production) | RNDS live |
| EG-8 | TISS payer connectivity and contracts per operadora | insurance submission live |
| EG-9 | Fiscal/accounting reviewer for NFS-e and CBS/IBS applicability per clinic | clinic fiscal documents live |
| EG-10 | Owner-arranged scenario-based usability and clinical acceptance sessions with existing clinic contacts (results with denominators) | UX-06 acceptance, launch |
| EG-11 | Lawful representative evaluation corpus | real-world AI performance claims |
| EG-12 | Independent security assessment, incident and recovery approvals, provider PITR | live-data gate, launch |
| EG-13 | Licensed clinical content (medication database and interactions, scales, growth references, terminology) | calculators, interaction checks, automatic class mapping |
| EG-14 | Owner-supplied physical devices or an approved device cloud, plus a manual assistive-technology audit by a qualified tester | real-device evidence (scribe capture, patient PWA) and manual accessibility evidence |
| EG-15 | Authorized competitor export samples from partner clinics | competitor-specific import mappings, acceptance scenario 16 with real formats |
| EG-16 | SBIS/CFM record-validity or certification decision (CFM 1.821/2007, Lei 13.787/2018) | any 'paperless' or 'certified' claim |
| EG-17 | CFM/CRM (and other councils) registry data-access agreement | real professional verification |
| EG-18 | Clinic Ops' own merchant account and fiscal setup | real SaaS subscription collection and Clinic Ops invoices |

## Capability record set 2026-09-24-v2

Per decision D-11, record set `2026-09-12-v1` is never edited.
[`records/2026-09-24-v2/`](../integrations/records/2026-09-24-v2/) supersedes it:
the 16 existing records are carried forward with a supersedes link and
successor todo numbers, and eight new records cover `asr`, `llm_inference`,
`sncr`, `rnds`, `tiss`, `nfse`, `psp_card` and `object_storage_media`. Every
record stays **unavailable**. Code and app docs cite the v2 set.

## Recorded successor decisions

Two decisions change how repository guidance reads before any code changes:

- **SD-5, provider capability lifecycle.** A provider moves through
  `researched`, `selected_in_plan`, `approved_to_test`, `sandbox`,
  `production_authorized`, `activated`, `degraded` and `revoked`. Todo 4
  builds the registry. Until it lands, the `real_enabled=False` constants in
  code remain the enforced behavior.
- **ADR-001, frontend.** HTMX stays first. Bounded React + TypeScript islands
  (agenda grid, encounter with AI review, inbox) are allowed only if todo 23's
  agenda vertical slice selects them. The Node toolchain (npm, bundler, JS
  lint) then lives only under `frontend/`, with Node pinned by image digest, a
  committed lockfile, `npm ci --ignore-scripts` and a committed build with
  drift CI. Nothing outside `frontend/` gains npm, a bundler or JS lint. Until
  the slice selects islands there's no npm, bundler or JS lint in the tree.

## CFM 2.454/2026 obligation (SD-17)

CFM Resolution 2.454/2026 (approved 11/02/2026, in force August 2026) adds
class-3 duties for every AI capability: the patient is informed and may refuse
AI use, a human makes the clinical decision, AI use is recorded in the chart,
each capability carries a risk classification, and clinics with their own AI
get support for their AI and Telemedicine commission. Todos 3, 20, 42 and 47
deliver the records and controls; EG-3 owns the clinical mapping.

## Changing this contract

Change it only through a reviewed commit that updates this file, the JSON
ledger and, where a historical doc is affected, that doc's single pointer
line. Never delete a ledger row. Mark a changed row with a new disposition and
say why in the commit's Lore trailers.
