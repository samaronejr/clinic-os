# Tasks and versioned workflows

This app owns the single operational task entity (ADR-008, todo 26) and the
care-plan entities (todo 52, D-16). Care records never become a second task
store: an administrative care action owns exactly one `Task`.

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
(critical results, then urgent tasks), so a later critical result is never
cut behind older normal ones. The selected owner and due filters reach each
feed before its bound, so narrowing finds matching work beyond an unfiltered
page; `truncated` says the listed items are not every matching item. Counts,
`total`, `overdue` and `critical` cover every item the page knows (each feed's
unfiltered page plus filtered items found beyond it), so no summary contradicts
a listed item. Whenever a feed has more work than a page they are all lower
bounds: `N+` on Inbox and Today, `counts_complete: false` in the API. Bulk
preview uses the same filtered selection the board lists. A refresh posts the
keys it shows; keys no longer visible come back as `removed` and the page says
so.
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
pre-visit brief slot uses todo 44's default-off, source-cited assistant. Its
patient authority is separate from the own-appointment permission; an
unavailable assistant never prevents manual chart work.
Commits that change inbox inputs, and appointment bookings, moves and status
changes, publish a `clinic:<id>:inbox` refetch hint after commit, so a
connected Today board refetches its own day without a clinic-wide agenda
grant; subscribers still need the operator-issued topic lease from todo 8, and
without it the page refreshes through the polling fallback.

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

## Care plans, referrals, recalls and PROs (todo 52)

Code lives in `apps/workflows/care/` and is exported only by
`apps.workflows.care.services` (keyword-only, explicit `clinic_id`, never an
acting user). The authority census (`tests/workflows/guard_discovery.py`,
`decision_sites.py`) recurses into subpackages, so every care guard has a seeded
case in `tests/workflows/care_guard_cases.py`, built on the committed fixture in
`care_world.py` (the guard actor is the plan clinician, so a refused role is
refused by its permission, never by row visibility). Services check permission
before any record lookup; unknown and foreign records share one denial.

`create_care_plan_from_version` turns one *finalized* note version into one plan
(a version has at most one plan) of typed actions: `return_window`, `order`,
`referral`, `questionnaire`, `monitoring_task`, `patient_instruction`,
`staff_coordination`. The author needs patient-scoped `clinical.write`. Clinical
kinds, including every patient-facing instruction, are approved by that
clinician (`approved_by`) and staff cannot add them; `return_window` and
`staff_coordination` may also be added by staff with `tasks.assign`. A patient
instruction is encrypted clinician content with no staff task; every other kind
owns one follow-up `Task` whose owner and completion the action mirrors. Free
text exists only in the encrypted instruction and the encrypted referral target.

Action states are open, assigned, overdue, done and cancelled. Overdue is marked
from the database clock past the window end and is never an execution. When the
source version leaves `finalized`, the plan becomes superseded and its pending
actions are flagged `review_state=required`: they refuse assignment and
completion until the clinician confirms or cancels each one. The clinician may
cancel only task-less actions; cancelling a staff task is a manager power
(todo 26).

A `Referral` coordinates a referral action (requested, scheduled, completed,
cancelled) and carries the nullable one-to-one link to the todo-33 referral
document (`PrescriptionDraft` of kind referral, same encounter). Referral
documents that predate care plans are linked once by migration
`0007_referral_backfill` as `documented` rows with an unspecified target,
created by the document's issuer; nothing about their destination or progress is
inferred. The migration is trusted operator code (the care guard is lifted for
its one statement inside the migration transaction); runtime code creates only
planned referrals.

`schedule_recall` stores a due instant computed from the injected clock; the
scanner turns each due recall into one role-owned follow-up task. `assign_pro`
accepts only calculator-catalog instruments whose licence status permits use
(PHQ-9, GAD-7) and creates a bounded series of slots. The first slot is never due
before the injected clock's now, because such a slot could never be answered; an
identical replay still returns the existing series. A recorded response always
creates a review task for the assignment's response role; the instrument's own
published item (PHQ-9 item 9) only raises that task to urgent. A slot unanswered
three days after its due instant becomes `no_response`, shown as "Sem resposta".
Absence is never read as wellness, refusal or a score. `staffing_policy_ref` is an
opaque reference; the staffing policy content is external gate EG-3.

**Stale source fails closed.** Assigning, starting, completing or reassigning a
care action is refused in the same transaction when its plan's source version is
no longer the finalized one, unless the clinician has confirmed the action. The
SQL guard enforces it for every writer and the services check the same predicate
before any task effect, so the scanner interval is never a window. Cancelling,
overdue marking and the review decision itself stay allowed.

**The action follows its task on every path.** Staff work an administrative
action where they see it: the Tasks and Inbox workspace, bulk reassignment and
the care services all drive the one `Task`, and `care.mirror` mirrors each task
transition onto the action in the same transaction (owner; open to assigned;
done; cancelled; an overdue action stays overdue until its task ends). The task
services refuse to assign, start or complete a care task whose action awaits
review or whose source is stale; bulk preview refuses it too. The database
repeats both rules on `workflows_task`: a BEFORE trigger refuses the transition,
and a deferred check refuses any commit in which a care task and its action
disagree. An action never changes owner or state on its own. Every path locks
the task before its action. A care command derives its key from the action
revision, so the same command from either surface replays as one.

**Keyed command digests.** Every care command digest and creation fingerprint
binds the actor and the request terms, which can carry encrypted content from
small spaces (PHQ-9 answers, instruction text, referral targets). They are
HMAC-SHA256 under the tenant's data keys through `protected_blind_index`
(purpose `workflows.care.command.v1`), never a plain hash that could be inverted
offline. Writes store the newest key version's digest and a replay matches any
version, so a key rotation never turns an identical retry into a conflict.
Scanner transitions bind only the record id.

**Referral link.** `Referral.document` points at the `PrescriptionDraft` of kind
referral, not at a rendered `PrescriptionDocument`: the draft is the stable
identity for one encounter and kind, while rendered documents are immutable
per version and re-approval creates new ones, which would orphan the link.

**Clinical content stays with the patient's clinicians (RP matrix).** Managers
and organization admins see routing metadata only: plans, administrative and
clinical-kind actions (no text) and their tasks. A patient instruction, a
planned referral's destination and every PRO assignment and slot are visible
only with patient-scoped `clinical.read` (PROs also `observation.write`); the
referral's coordinating task owner also sees, and advances, that one referral.
A backfilled referral carries no destination and stays staff-visible.

**Response capture.** A PRO response is recorded by the patient's nurse
(`observation.write`) or clinician (`clinical.write`), never by routing
authority, until patient-side capture arrives with todos 50 and 55. The response
role must be one that can read the response (physician, nurse, allied
professional). Recording a response makes the guard emit one source-backed
review task (no staff creator) in that role's queue, admitted by
`workflows_care_system_task_valid` exactly as orders and retention tasks are;
concern only raises its priority.

**Clinical confirmation pending (EG-3).** The only concern signal is the
instrument's own published item (PHQ-9 item 9, any non-zero answer). It raises
the priority of a review task and nothing else; every response still goes to a
human queue. The item list and the staffing policy content need clinical-owner
confirmation under EG-3. This change never marks that gate satisfied.

**The scanner never stalls.** The beat `workflow-care-scanner` lists due records
by opaque `(kind, record)` ids: due recalls, actions past their window (never a
flagged one, which waits for its clinician), PRO slots past their grace period
and stale plans, from one predicate (`workflows_care_items`) that the per-record
services repeat exactly. It takes at most 25 records per organization and 500 in
total, oldest first, so one tenant's backlog never starves another. Each record
is its own job on the bulk queue: the job reopens the record's stored actor
(creator or plan clinician), rechecks current authority and transitions exactly
that record. A record the actor cannot process (revoked, narrowed, no longer
allowed to see it) is not swallowed: `workflows_care_park` records the refusal
in `workflows_carerefusal` (who, how often, when), readable by clinic managers,
and the record is listed again only after 60 minutes, so a restored authority
resumes the work and a stuck record never fills the batch. Seven tables
(`workflows_careplan`, `careaction`, `referral`, `recall`, `proassignment`,
`prooccurrence`, `carerefusal`) have FORCE RLS and no DELETE; the six care
tables have column grants limited to command/state columns and one SECURITY
DEFINER transition guard, the refusal table only its resolver's writes.
Migration `0006` is protected-schema style: irreversible, rollback = restore.
Live legs (real PRO delivery, patient release, staffing policy) stay
BLOCKED-ON-EG; none is reachable here.

## Result release and urgent concerns (todo 55)

Code lives in `release.py` and `urgent.py` (models in `release_models.py` and
`urgent_models.py`, staff pages in `coordination_views.py`). Every service is
keyword-only with an explicit `clinic_id` and derives the actor from the
database context. A missing permission raises the actor error; unknown and
foreign selectors share the payload-free `WorkflowAccessDeniedError`, and a
stale notice version or revision is a `WorkflowConflictError`.

**Release with approved wording.** `release_result_to_patient(*, clinic_id,
result_version_id, template_version, audience, delegate_grant_id=None)` composes
the orders release (`apps.orders.review.release_result`, rules unchanged: the
version's own acknowledgment; the patient, or an active delegate grant scoped for
records and care processing) with the clinic's current release notice. The
plan's sketch has no clinic; SC-1 requires the explicit scope, so the signature
adds it. A notice (`ReleaseNoticeTemplate`) is one clinic-wide sentence of at
most 300 characters without digits or `@`, immutable and versioned, approved by
a physician through the clinic-level `configuration.propose` cell
(`result.release` is patient-scoped: it needs an enrollment, so no clinic-level
check can grant it); the approving clinician is its author
(`publish_release_notice`). Each release still needs patient-scoped
`result.release` and `clinical.read` for the result's enrollment. `template_version` must equal the current version,
so the clinician releases with the wording they saw: a stale version is a
conflict, and without an approved notice the results page cannot release. The
notice is queued on the patient's portal care thread through the todo-51 inbox
(`open_conversation` and `send_message`, staff actor, outbox after commit) and
never carries result content, values, dates or identifiers. A repeated release
returns the original delivery and sends nothing twice: the message command id
is derived from the release id.

**Delivery state is derived.** `ReleaseDelivery` is content-free provenance
(release, version, enrollment, audience, delegate grant, notice version and the
comms message). It is `pending` until the message's outbox operation succeeds
(`delivered`) and `failed` when the operation fails or is cancelled.

**Scope is rechecked at send time (scenario 19).** comms runs registered send
checks inside the send's tenant transaction (`register_message_send_check`).
`notice_send_eligible` asks the tenant-only resolver
`workflows_release_send_eligible`: the version must still be current and, for a
delegate audience, `orders_delegate_allowed` must still hold. A guardian
revoked or expired between release and send receives nothing: the operation is
cancelled. Patient result reads keep rechecking the release audience at read
time (`apps/orders/patient_results.py`).

**A failed delivery reaches staff.** When a notice's outbox operation fails or
is cancelled, the outcome bridge (`workflows_release_outcome`, an AFTER UPDATE
trigger on `comms_integrationoperation` for terminal transitions of comms
messages) marks the delivery `scope_revoked` (the send check refused it) or
`send_failed` (any other failure) once, and writes one high-priority follow-up
`Task` for the reception role on the patient's enrollment, due four hours later.
The task guard admits exactly that row as a source-backed system task
(`workflows_release_system_task_valid`), as it does for orders, retention and
care; no staff identity is borrowed, so the task exists even when the releasing
clinician has since lost authority. The outbox's own `comms.operation.failed` or
`.cancelled` audit event records the transition.

**Urgent concerns (scenario 20).** A portal reply that `classify_inbound` marks
urgent opens exactly one `UrgentConcern` in the reply's own transaction through
the patient bridge (`register_urgent_reply_hook` in comms; SQL
`workflows_report_urgent` admits only a message that session wrote in that
transaction), audited once as `workflows.urgent_concern.reported` with the
patient session as actor. The concern stores pointers only; the text stays
encrypted in comms. A concern moves open -> acknowledged -> resolved, each step
by a person holding one of the policy's responsible roles
(`acknowledge_urgent_concern`, `resolve_urgent_concern`, optimistic
`expected_revision`), and the table guard enforces the same transitions.
Without a policy, clinic managers own concerns and no deadline exists. Nothing
replies to the patient automatically; staff answer in the conversation.

**Coverage policy.** `set_urgent_coverage` (`configuration.clinic`) publishes
the next immutable `UrgentCoveragePolicy` version: covered weekdays, opening and
closing time in clinic time, a response window of 1 to 72 business hours and
the responsible roles (nurse, physician, receptionist, clinic manager). The
deadline (`due_at`) adds the window in covered business minutes from the report
time. The patient portal (message list and thread) states exactly the
configured terms, ending with "em emergência, ligue 192"; without a policy it
says the clinic does not watch messages continuously and to call 192, and never
claims coverage. The policy is clinic configuration; clinical safety and
staffing content remain external gate EG-3, which this change never marks
satisfied.

**Overdue escalation.** A concern still open at its deadline is overdue: it
rises above every other item in the queue and in the Clinical Inbox feed
`urgent` (critical), and it becomes visible to clinic managers outside the
responsible roles. Overdue is derived from the policy deadline whenever the queue
is read (`urgent.utc_now`, which tests replace), so escalation never waits for a
scanner interval and needs no machine writer.

**Pages.** Inbox > Urgent concerns (`demographics.read`), Inbox > Result release
notice (`configuration.propose`) and Settings > Urgent message coverage
(`configuration.clinic` and the administrative route roles) are TOTP-protected,
private no-store pages whose selectors travel in POST bodies. Each page decides
its permission before the auth decorators; each form names its action
(`ack`/`resolve`, `publish`), and an unknown action or a malformed selector gets
the same shared 403 page as an unknown or foreign record, without session,
audit or outbox writes.

**Schema.** Migration `0008_release_and_urgent` adds four FORCE RLS tables
without DELETE: `workflows_releasenoticetemplate`, `workflows_releasedelivery`
and `workflows_urgentcoveragepolicy` are append-only for the runtime role (only
the outcome bridge marks a delivery failed), and `workflows_urgentconcern` has
no runtime INSERT and UPDATE only on its state and attribution columns.
One SECURITY DEFINER guard, `workflows_coordination_guard`, enforces authorship,
versions, scope, each delivery's binding to its release and comms message, the
bridge-only failure update and the concern transitions; the migration also
re-creates the task guard with the release follow-up admission. The migration is additive and stores no patient text;
its reverse is for an empty synthetic database only, and once releases or
concerns exist rollback = restore.
