# Unit economics, packaging, guardrails, staffing and critical path

Status: proposal for owner decision. Nothing in this file is a price, a quote,
a commitment or a schedule.

This file explains the cost model in `ops/economics/model.py` and the inputs in
`ops/economics/variables.yaml`, and it sets out what the owner has to decide
before spending. Every input carries a unit, a source, and either the date the
source was read or an `assumption: true` label. Inputs nobody has a sourced
number for are `null` and marked `owner_input_required`. Any cost that depends
on one of them comes out as `null`, so the model can't produce a total it
can't back. The test `tests/business/test_economics_model.py` enforces both
rules, and it also rejects negative inputs.

Run the model:

```bash
uv run python -m ops.economics.model --output economics.json
```

## COGS formula

Per clinic and month, with every term computed for the low, expected and high
scenarios:

```
COGS = fixed_per_clinic_month                      (core infra, DB/backup, monitoring, support, licensed content)
     + billable_audio_minutes x p_asr
     + captured_visits x (1 + retry_overhead) x tokens_per_visit x p_llm / 1M   (input, output, cached)
     + visits x teleconsult_share x capture_minutes x video_participants x p_video
     + visits x messages_per_visit x p_message
     + visits x attachments_per_visit x attachment_gb x p_storage
     + visits x fees_per_visit                      (PSP, NFS-e, signing)

audio_minutes_per_day = captured_visits_per_day x capture_minutes
                        x (1 + long_share x (long_factor - 1) + caregiver_share x (caregiver_factor - 1))
billable_audio        = audio x (1 + retry_overhead)
provisioned_capacity  = audio x burst_factor        (sizing only, not billed usage)
```

Messages use one blended price for now. Splitting by template category is a
change to `p_message` once the BSP price list is on file. Storage counts only
the gigabytes a month adds; retention keeps them, so stored volume grows month
over month.

## P-100 reference arithmetic

The P-100 profile comes from the plan's PF section: 100 clinics x 10 clinicians
x 25 visits a day = 25,000 visits a day, and 60% of them captured for 20
minutes gives 300,000 audio minutes a day. The model reproduces that figure
exactly in every scenario, then layers the labelled assumptions on top:

| Output (P-100, per day) | Low | Expected | High |
|---|---|---|---|
| Reference audio minutes | 300,000 | 300,000 | 300,000 |
| With long and caregiver-heavy visits | 309,000 | 337,500 | 457,500 |
| Billable, with 15% retries | 355,350 | 388,125 | 526,125 |
| Provisioned capacity, 2x burst | 618,000 | 675,000 | 915,000 |

The 15% retry overhead and the 2x burst factor come from the plan's EC section.
Working days, long-visit and caregiver mix, attachments, teleconsult share and
messages per visit are modelling assumptions. They're labelled as such in the
variables file and should be replaced by todo 63 metrics once real usage
exists.

Every price is `null` today, and so are LLM tokens per visit. Todo 48's
benchmark (`docs/ai/provider-selection-2026-10-29.md`) ran only on synthetic
adapters with `provider_leg: BLOCKED-ON-EG`, so it measured no real token
counts or prices. The COGS totals are therefore `null` in all three scenarios.
That's the honest output until the owner records dated prices under EG-1 and
EG-5.

Owner input required: `p_asr`, `p_llm_input`, `p_llm_output`, `p_llm_cached`,
`p_video`, `p_message`, `p_storage`, `fees_per_visit`,
`fixed_per_clinic_month`, and the three `llm_*_tokens_per_visit` values.

## Packaging proposal

These are the three plan codes todo 64 already seeds as `provisional` in
`apps/subscriptions`. The proposal sets no price and estimates no willingness
to pay.

| Package | Contents |
|---|---|
| `core_operations` | Agenda, patients, records, intake, consent, billing and comms. AI meters are off. |
| `intelligence_usage` | Core plus metered AI: scribe audio, LLM tokens, assistant. Quotas and alerts apply. |
| `network_governance` | Intelligence plus multi-clinic governance, enterprise identity and network reports. |

Every package includes security controls, record read and export, reconciliation
and clinical continuity. Restriction or nonpayment never removes them
(`CORE_FEATURES` in `apps/subscriptions/models.py`).

Publication: the website pricing page from todo 70 stays hidden.
`docs/web/claims.yaml` keeps `pricing.status: hidden` and
`owner_approval_ref: null`. This todo publishes packaging only after the owner
approves it, and no approval record exists yet. Once the owner approves, a
follow-up change records the approval reference, sets the owner-approved
prices on a new non-provisional plan version, and turns the pricing page on.

## Usage guardrails

The model proposes per-clinic monthly quotas from the high scenario. Alerts
fire at 80% of quota, the same threshold `apps/subscriptions/services.py`
already applies (`used x 5 >= quota x 4`). When the AI quota runs out, AI is
unavailable and the manual workflow stays open. Clinical core, record read and
export are never blocked.

| Meter | Proposed quota (per clinic-month) | Alert at | Seeded provisional v1 |
|---|---|---|---|
| `ai_audio_seconds` | 7,576,200 | 6,060,960 | 360,000 |
| `video_seconds` | 4,320,000 | 3,456,000 | 360,000 |
| `messages` | 24,000 | 19,200 | 10,000 |
| `storage_bytes` | 45,000,000,000 | 36,000,000,000 | 10,737,418,240 |
| `ai_tokens_in/out/cached` | owner input required | owner input required | 10,000,000 each |

Finding: the seeded provisional audio quota of 360,000 seconds is 100 hours a
month. A 10-clinician P-100 clinic needs about 1,423 hours in the expected
scenario (85,387.5 billable minutes), so it would hit the quota early in the
month. The quota stays as it is in this change. The owner sets real quotas,
along with a per-tenant spend budget in `ai_cost_micros`, when approving
packaging, because quota size drives spend.

## Staffing scenarios

These scenarios list roles and which external gate (EG) each role owns. They
don't estimate headcount cost, since salaries are owner input.

| Role | S1 founder + AI-assisted engineering | S2 small cross-functional team | S3 funded team | EGs owned |
|---|---|---|---|---|
| Engineering | Founder with AI agents | 2 to 3 engineers | Squads per vertical | EG-1 execution, EG-6, EG-7, EG-8 technical legs |
| Product/design | Founder | 1 shared | Dedicated | EG-10 |
| Clinical safety (physician) | External advisor | Part-time physician | Clinical safety lead | EG-3, EG-11, EG-13 selection |
| QA/security | Agents + external assessment | 1 shared | Dedicated team | EG-12, EG-14 |
| Privacy/legal | External counsel | External counsel | In-house counsel | EG-2, EG-4, EG-16, EG-17 |
| Operations/support | Founder | 1 support lead | Support team | EG-9, EG-15, EG-18 |
| Owner | Owner | Owner | Owner | EG-1 spend, EG-5 contracts |

S1 can't clear EG-3 or EG-12 alone. Both need a named qualified human, so S1
still has to buy those two in.

## Critical path

This comes from the plan's dependency matrix. The agent schedule isn't a
commitment, and there are no calendar dates.

- Clinical intelligence: todos 1 -> 5 -> 6 -> 7 -> 38 -> 39 -> 40 -> 41 -> 42 -> 47.
- Agenda, in parallel: 21 -> 22 -> 23 -> 24.
- Revenue, in parallel: 56 -> 59.
- Live activation is held up by external lead times, not code.

| Gate | Lead time | Gates |
|---|---|---|
| EG-5 provider contracts/DPAs | owner-input-required | `production_authorized` for every provider |
| EG-6 SNCR credentials and homologation | owner-input-required | regulated electronic prescriptions |
| EG-7 RNDS credentialing | owner-input-required | RNDS live |
| EG-8 TISS payer connectivity | owner-input-required | insurance submission live |
| EG-1 spend authorization | owner-input-required | any paid call, and any real price in this model |

## Owner decisions

1. Record dated prices for each `owner_input_required` variable, or approve sandbox spend (EG-1) to measure tokens per visit.
2. Approve or change the three packages, then set prices and quotas on a non-provisional plan version.
3. Approve the pricing page publication, with an approval reference.
4. Pick a staffing scenario and name the humans who own EG-2, EG-3 and EG-12.
