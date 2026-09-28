# Tasks and versioned workflows

This app owns the single operational task entity (ADR-008, todo 26). Care-plan
entities remain a later extension of this app, not a second task store.

## Authority and privacy

Services are keyword-only and receive an explicit clinic, never an acting user,
request or organization. The actor comes from transaction-local GUCs and every
operation rechecks `has_permission`. Task visibility and mutations are also
bounded by FORCE RLS. Missing and foreign subjects share one payload-free denial.
Denials write neither audit nor session, enqueue nothing and emit no cookies.

Version 2 staff bundles add `tasks.view`, `tasks.assign`, `tasks.complete`, and
manager-only `tasks.reassign`; version 1 remains frozen. A removal of a legacy
permission remains effective after the upgrade. Task owners are an exact-clinic
active user or a canonical role, not both. Creators may claim their unassigned
tasks for themselves; only managers may transfer another owner's task. A role-owned task
can be started/completed by a current member of that role. Manager reassignment
does not grant the manager permission to complete work as its owner.

Subjects, definition steps, run context and completion evidence are closed typed
schemas. They contain only UUID references, bounded enums, booleans and integers.
There are no arbitrary instructions, URLs, provider names, clinical narrative or
executable tools. Comments are encrypted from creation and append-only. Audit
payloads retain only the existing metadata vocabulary. Record selectors travel
in POST bodies, never URLs. Browser storage is not used.

## Task lifecycle

`create_task` records an open task with kind, subject, due date, priority, pinned
escalation policy and optional predecessor. Its idempotency key binds immutable
creation terms; a different replay is a conflict. `assign_task`, `start_task`,
`complete_task` and `cancel_task` require the current revision. A command digest
binds the actor, action, revision and payload; an identical immediate retry returns
the existing result without another transition or audit event. The legal path is
open -> assigned -> in_progress -> done; any nonterminal task may be cancelled.
Assignments can change before completion, with a revision increment. Terminal
records and completion evidence are immutable. Dependencies must be completed
before starting or completing a task. Dependencies cannot be rewritten, so a new
task can depend only on existing history and cycles cannot be constructed.

Evidence is validated per kind: checklist requires exactly `{checked: true}`;
review/follow-up evidence binds a typed record reference and a fixed outcome.
An overdue open/assigned/in-progress task is escalated once under policy version
1 and is visible in the exception queue; escalation never executes clinical work.

Bulk reassignment is a two-stage native-form flow: preview the selected rows and
new owner, then submit the signed preview. The signature binds clinic, actor,
row revisions and the target owner. Apply rechecks permission, ownership and every
revision atomically; stale/tampered previews change no rows.

## Definitions and execution

`publish_definition` appends an immutable, numbered version under a per-clinic/key
lock. Its required idempotency key binds actor, definition key and steps; retries
return the same publication, while altered terms conflict. `start_run` pins that
exact version and immutable typed context references.
Publishing a successor cannot update existing runs, pending steps or their inputs.
There is no implicit migration or in-place template edit. Publication and run
control require manager authority. Starting and executing a run also require the
current `tasks.view` and `tasks.assign` permissions; narrowing any required bundle
permission blocks queued work, even when another role remains on the account.

The closed handler registry supports operational task creation, timers and the
explicit synthetic external action. Internal handler effects and step completion
commit in the same transaction. External effects are never called inside that
transaction: an action operation is enqueued with the step UUID as its stable
idempotency key and an RFC 8785/SHA-256 digest of immutable input. The action
adapter reloads the subject, verifies that digest and rechecks the run actor and
permissions before sending. No real provider is runnable.

A worker owns a dedicated-connection session advisory lock from claim through
completion. Each committed claim increments a fencing token. Applying a claim
locks the row and requires the exact current token; stale workers cannot commit
an effect. A live worker cannot be reclaimed merely because its lease elapsed.
Connection/process death releases its advisory lock; after the stale deadline a
new worker increments the token and reclaims once. A killed effect transaction
rolls back; committed effects cannot repeat. Unknown external outcomes remain
visible for reconciliation rather than being blindly resent.

Beat scans durable due rows every 60 seconds on the bulk queue. Broker loss is
recoverable by the next scan; duplicate deliveries contend on the same lock.
Timers use persisted due instants, not Celery ETA or sleeping workers. Run states
are pending/running/waiting/completed/failed/cancelled. Failed steps and escalated
tasks remain in the exception queue. Revoked/inactive actors cannot execute queued
steps, even when the message was published while authorized.

## Schema, recovery and rollback

All five domain models inherit `TenantScopedModel`. Initial DDL installs FORCE
RLS, exact per-app posture declarations, composite clinic/organization bindings,
immutable identities and no-delete triggers, and revokes bootstrap default DML
in the same migration. No machine-role table access is granted implicitly.
Row visibility is decided by the SELECT/UPDATE policies (owner, role owner,
creator, run starter); insert authority is decided once, by the BEFORE trigger,
which PostgreSQL runs before any INSERT `WITH CHECK`. Every actor-reading SQL
decision, and each permission, helper or actor comparison inside a trigger
branch, is derived from the catalog and exercised both ways in
`tests/workflows/test_sql_decisions.py`. Constraints, defaults, indexes and
views in the workflows closure must not read the actor; the inventory fails
closed if one does.
Every new table is classified as restored tenant data in the recovery manifest;
UUID primary keys introduce no sequences. Protected comment bytes ship encrypted
from their first version; no plaintext conversion or backfill exists.

Reverse/forward rehearsal is only for an empty synthetic database. Once task,
comment or workflow history exists, retain it and use additive forward corrections;
production rollback is restore, never dropping evidence or migrating active runs.

Verification belongs to `tests/workflows/`, the identity authority census, exact
schema/resolver posture, the recovery trio and the registered `tasks` browser
suite on Chromium, Firefox and WebKit. Synthetic evidence is not live approval.
