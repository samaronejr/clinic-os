# ADR-008: DB-persisted workflows on Celery with digest-bound approvals

- Status: accepted 2026-09-24
- Recorded by: todo 2
- Threat model: [agents](../threat-models/agents.md)

## Context

Tasks, recurring operational sequences, care plans and agent actions need
durable state, retries and human approval. Celery with Redis already runs the
comms worker (`config/celery.py`), and the outbox in `apps/core/integration.py`
already claims work under advisory locks. The audit chain already hashes
RFC 8785 canonical JSON, and `rfc8785` is a runtime dependency.

An approval is only meaningful if it binds the exact thing approved. A payload
that changes after approval, or an approver who loses authority before
execution, must block the action.

## Decision

Workflows and approvals are persisted in PostgreSQL and executed on Celery.

- `WorkflowRun` and `ActionProposal` live in the database; active runs keep
  their definition version.
- Workflow and AI work use dedicated Celery queues (todo 9).
- An approval binds `SHA-256(RFC 8785(payload))` of the canonical action
  payload.
- At execute time the digest is recomputed and authority, budgets and
  invariants are revalidated with database time before any effect.

## Consequences

- Every step is visible and queryable, with the same RLS as other tenant data.
- Payload changes, stale sources and revoked approvers all fail closed at
  execution.
- Long timers are beat-driven rows, so timer precision is bounded by the beat
  interval.
- Retry and compensation logic is ours to maintain, which is why the revisit
  trigger watches that cost.

## Rejected

- Temporal now. It's a new stateful service to run and secure, and the
  current workflow count and timer needs don't justify it.

## Revisit trigger

Any of: more than 15 workflow types with multi-day timers, more than 20% of
engineering time spent on retry bugs, a timer SLA under 60 s, or more than
50k waiting runs.

## Owning todos

26, 39. Consumers: 52, 53, 54, 60.
