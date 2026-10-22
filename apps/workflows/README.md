# Tasks and versioned workflows

This app owns the single operational task entity (ADR-008, todo 26). Care-plan
entities remain a later extension of this app, not a second task store.

## Authority and privacy

Services are keyword-only and receive an explicit clinic, never an acting user,
request or organization. The actor comes from transaction-local GUCs and every
operation rechecks `has_permission`. Task visibility and mutations are also
bounded by FORCE RLS. Missing and foreign subjects share one payload-free denial.
Denials write neither audit nor session, enqueue nothing and emit no cookies.

The unissued version 2 candidate adds `tasks.view`, `tasks.assign`,
`tasks.complete`, and manager-only `tasks.reassign`; version 1 remains frozen
and existing memberships stay on version 1. Explicit synthetic test selection
is not production issuance. Stored permission removals remain effective.
Task owners are an exact-clinic
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

Definitions follow draft -> validated -> published -> retired. Draft editing,
validation, publication and retirement bind the current revision and command
UUID. Published content cannot be edited. `publish_definition` also appends an
immutable, numbered version under a per-clinic/key lock. Its required idempotency
key binds actor, definition key and steps; retries return the same publication,
while altered terms conflict. `start_run` pins that
exact version and immutable typed context references.
Publishing a successor cannot update existing runs, pending steps or their inputs.
There is no implicit migration or in-place template edit. Publication and run
control require manager authority. Starting and executing a run also require the
current `tasks.view` and `tasks.assign` permissions; narrowing any required bundle
permission blocks queued work, even when another role remains on the account.

The closed handler registry supports operational tasks, timers and the
explicit synthetic external action. Task assignment creates a durable waiting
receipt; only the task owner's actual completion evidence releases the next
step. Cancellation fails that step. Internal handler effects and step completion
commit in the same transaction. External effects are never called inside that
transaction: an action operation is enqueued with the step UUID as its stable
idempotency key and an RFC 8785/SHA-256 digest of immutable input. The action
adapter reloads the subject, verifies that digest and rechecks the run actor and
permissions before sending. Accepted sends remain waiting; only authenticated
delivered receipts complete the step. The sessionless synthetic callback uses
the named managed secret `workflow-synthetic-callback`, authenticates raw bytes
before parsing or scope resolution, and is disabled by default. No real provider
is runnable.

The stored starter may explicitly retry a failed local step using a current
revision and command UUID, preserving its published pin and input. A step with
an existing task or external-operation receipt cannot be resent through retry.
The appointment/discharge starting point is an operational checklist, timer
and follow-up task; it does not change a medical discharge record.

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

## Clinical Inbox and clinician Today (todo 32)

The Clinical Inbox is a read model, not a store: `query_inbox(*, clinic_id,
filters, known)` assembles one typed queue on every request from feeds that
owning domains register with `register_inbox_feed(kind, query, *, label,
position)`. This app registers `task` (active tasks the actor owns, decided by
`workflows_owned`, the task row predicate); orders registers `result` (current,
unacknowledged versions the actor may acknowledge for that patient); EHR
registers `draft` (SOAP drafts the actor authors on encounters still assigned
to them). Tasks bound to an orders work item are represented by their result
and never listed twice. Prescriptions (34), routed patient questions (51) and
care actions (52) register their own feeds when they land. There is no table,
so there is nothing to migrate or restore: the queue rebuilds from the domains'
committed, policy-bounded records.

`require_inbox` admits clinic `demographics.read` held through a clinical role
whose bundle carries `result.read` or `order.observe` (physician, nurse, allied
professional). Those permissions are patient-scoped, so each feed rechecks its
own authority per patient; a feed whose permission was narrowed is left out.
Unknown, foreign and refused callers get the shared refusal; the typed API
`POST /api/ui/v1/inbox/query/` instead answers them with the empty page a
clinician with nothing to do receives (same keys, zero counts, the caller's
`known` keys echoed as `removed`), so no answer reveals a clinic or record.

Items carry an opaque `kind:uuid` key, owner, due time, priority and triage
order (critical, priority, overdue, due). A page holds at most `MAX_ITEMS`
items. Each feed decides its own authority before bounding, so rows the actor
may not act on never take a slot, and returns its most urgent items first
(critical results, then urgent tasks). A feed with more than a page of work
keeps only those, marks the page `truncated` and its count as a lower bound
(shown as `N+`), so a later critical result is never cut behind older normal
ones and the page never claims to be complete. A refresh posts the keys it
shows; keys no longer visible come back as `removed` and the page says so.
Results are due on receipt, like their review task. Acknowledgment calls the
orders service unchanged. Bulk acknowledgment is limited to current,
unacknowledged, non-critical results whose every source observation is flagged
exactly `normal`; each selected item is previewed, the confirmation is signed
for the actor, clinic and exact version numbers, and confirmation rechecks every
item under the series fence, acknowledging all as `no_action` or none. Release
is never bulk: it remains a per-version decision in the orders workspace.

Clinician Today reads only the actor's own clinic-local appointments under
`appointment.read_own`, the next patient (in care, arrived, then the next
booked), arrivals, and the inbox counts when the inbox itself would open. The
pre-visit brief slot is reported unavailable until todo 44 supplies it.
Commits that change inbox inputs publish a `clinic:<id>:inbox` refetch hint
after commit; subscribers still need the operator-issued topic lease from
todo 8, and without it the page refreshes through the polling fallback.

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
closed if one does. Checks were removed from the trigger only where row
security already decides them for `clinic_app`; this holds for the runtime role
only. `clinic_owner` owns the tables and can disable triggers, so owner-context
provisioning is trusted operator code, not a guarded path.

Record scope is a guard input too: every comparison of a referenced or target
row's clinic against the call's clinic, in Python lookups and in SQL, is
derived and exercised with a same-clinic and another-clinic record. A wrong-clinic
reference gets the same denial as an unknown id. Organization equality is the
tenant boundary: rows carry composite (organization, clinic) keys, so it is
implied by the clinic comparison and is classified rather than mutated.
Every new table is classified as restored tenant data in the recovery manifest;
UUID primary keys introduce no sequences. Protected comment bytes ship encrypted
from their first version; no plaintext conversion or backfill exists.

Reverse/forward rehearsal is only for an empty synthetic database. Once task,
comment or workflow history exists, retain it and use additive forward corrections;
production rollback is restore, never dropping evidence or migrating active runs.

Verification belongs to `tests/workflows/`, the identity authority census, exact
schema/resolver posture and the recovery trio. Owned loopback browser evidence
is source QA; combined browser-suite registration and all final gates remain
root-held. Synthetic evidence is not live approval.
