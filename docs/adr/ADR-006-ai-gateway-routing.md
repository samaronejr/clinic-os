# ADR-006: Provider-neutral AI gateway with per-purpose routing

- Status: accepted 2026-09-24
- Recorded by: todo 2
- Threat model: [AI gateway](../threat-models/ai-gateway.md)

## Context

Eight AI capabilities (AI-01 to AI-08) call speech and language models. Model
quality, price and regional availability change often, and several candidate
providers have unverified Brazilian region support (PV table). Cross-border
processing needs a transfer basis under EG-2, and silent fallback to another
jurisdiction is a stated non-goal.

Existing integrations register send adapters and go through the outbox in
`apps/core/integration.py`. Real providers stay closed until the provider
lifecycle (todo 4) reaches `activated` and the live gate passes.

## Decision

All model calls go through a provider-neutral gateway in `apps/ai`.

- Each purpose (scribe ASR, scribe drafting, brief, extraction, front desk,
  analyst, decision support) has one primary configuration and optional
  approved fallbacks.
- Fallback never crosses a jurisdiction class.
- Adapters enforce an egress allowlist.
- Budgets are reserved before dispatch.
- Global and per-organization kill switches are checked on every call.

## Consequences

- Changing a model or provider is a configuration change with an approval
  record, not a code change in each feature.
- Metering lives in the gateway and records no prompt or output content.
- Every AI feature ships a deterministic fake adapter as its only runnable
  provider until EG-1 and an `approved_to_test` record exist (SC-16).
- Kill switches default to disabled until an organization opts in.

## Rejected

- A hardcoded vendor. It ties every feature to one provider's region, price
  and retention terms, and makes a provider outage a product outage.

## Revisit trigger

None recorded. The AD table sets no trigger. Changing this model needs a new
ADR that supersedes this one.

## Owning todos

38. Consumers: 39 to 48, 53, 54, 60, 63.
