# LLM inference for drafting and assistance

| Record field | Value |
| --- | --- |
| Capability | `llm_inference` |
| Record version / reviewed date | 2026-09-24-v2 / 2026-09-24 UTC |
| Supersedes | None; new capability in this set |
| Status | unavailable; documentation only |
| Selected provider | None |
| Candidate / reference | Amazon Bedrock in sa-east-1, in-region models only (no cross-region inference profile), is the plan's proposed primary. Azure OpenAI in Brazil South (regional deployment only) and Vertex AI in southamerica-east1 are alternatives. Proposal only; no provider selected. |
| Accountable owner / decision | Unassigned; clinical safety owner (EG-3), privacy reviewer (EG-2), regulatory reviewer for AI-08 (EG-4) and technical integration owner required. |
| Approval / contract | None supplied; provider API/profile/package version unverified unless stated below |
| Sandbox / executed provider evidence | Unavailable; none executed |
| Residency / subprocessors / retention | Unverified; in-region processing, no cross-jurisdiction fallback, no provider retention or training use of prompts and outputs beyond approved terms |
| Costs / budget | Unverified; no price is an approval or a current quote |
| Required packages | Unapproved: selected provider SDK or reviewed HTTPS client; exact version unset. |
| Dependent tasks | Successor todos 4, 38, 42, 44, 45, 46, 48 |
| External gates | EG-1, EG-2, EG-3, EG-4, EG-5, EG-11, EG-13 |
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

The plan's provider matrix lists model availability per region as UNVERIFIED. Uses: note drafting (AI-01), brief and chart assistant (AI-02), extraction (AI-03), agents (AI-04 to AI-07) and decision support (AI-08, behind EG-3, EG-4 and EG-13).

## Contract required before use

Calls go through the provider-neutral gateway (ADR-006) with a per-purpose primary and approved fallback, no cross-jurisdiction fallback, an egress allowlist, per-tenant budgets and kill switches. Outputs are validated against typed schemas; invalid output is refused, never repaired by guesswork. No model-written SQL, no arithmetic for doses or scores, no hidden chain-of-thought storage. Prompts, outputs and errors never reach logs, traces or metrics labels.

## Required future fixtures

Deterministic fake model; prompt-injection corpus; schema-violating output; timeout and rate limit; budget exhaustion; revoked capability mid-request.

These are acceptance requirements, not executed sandbox results. Fixtures may
contain only synthetic or explicitly provider-authorized data.

## Unavailable branch

Todo 38 ships a deterministic fake model as the only runnable provider. Every AI feature has a complete manual path. Real inference waits for EG-2, EG-3 and EG-5 (plus EG-4 and EG-13 for AI-08), the lifecycle record reaching `approved_to_test` and EG-1 for spend.

Follow the [register's versioning and approval rules](../../capabilities.md).
