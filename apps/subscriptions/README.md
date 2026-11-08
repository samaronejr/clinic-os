# Clinic Ops subscriptions

Todo 64 owns Clinic Ops-to-customer commercial metadata. It never imports or
writes the clinic's `billing` journal, invoices, settlements or cash sessions.
No customer payment, merchant account or fiscal invoice is implemented here.
Real collection, sandbox collection and Clinic Ops fiscal documents remain
**BLOCKED-ON-EG**, particularly EG-18. The only adapter is an offline,
deterministic, nonpayable fixture with `real_enabled=False`.

`Plan` and `Entitlement` are immutable packaging versions. The three EC packages
are `core_operations`, `intelligence_usage` and `network_governance`. All initial
versions are `provisional=True`, with no published price. Todo 74 publishes the
owner-approved economics into these same models. The finite rehearsal limits
(10 million input/output/cached tokens, 360,000 audio seconds, 1 billion
micro-BRL, 360,000 video participant-seconds, 10,000 messages, 10 GiB ingested
attachment bytes and 10,000 membership seats per organization) are **assumptions,
not commercial offers or unlimited AI promises**. Core operations disables AI.
The generous seat rehearsal limit supports the existing exhaustive role census;
it does not set a proposed paid seat allowance.

Every organization receives one 30-day provisional intelligence trial, including
organizations that predate the additive migration. The organization INSERT
trigger and migration backfill use the same bootstrap function. Rebuilding
missing provisional defaults never overwrites a published package version.
Trial creation has an immutable `trial_started` receipt. Subsequent commands
append `SubscriptionEvent` and the registered metadata-only
`subscriptions.subscription.changed` audit event atomically through services.

The database owns the lifecycle: trial -> active or restricted;
active -> past_due -> grace -> restricted; past_due/grace/restricted -> active
on cure; any live state -> cancelled -> offboarded. A failed payment starts one
seven-day grace deadline. Advancing past_due publishes the durable in-product
`payment_grace_deadline` notice, exposed with its deadline by the commercial API.
Expiry is checked at use even before a maintenance command advances the state.
An active subscription cancels at the next UTC calendar month boundary; other
states cancel immediately, so cancellation cannot cure an expired allowance.
Offboarding retains every record, membership and usage fact.
It never deletes tenant data or changes access to records, export,
reconciliation, consent, manual notes or continuity of care.

Services are keyword-only and clinic-scoped. `subscription_scope` derives the
organization from the stored clinic and transaction GUCs, calls `user_has_org`,
and requires current `configuration.organization` on **every** clinic for
commercial management. The `org_admin`/owner permission bundle is the authority;
no Django flag, seat or caller-supplied organization grants authority. Unknown
and foreign selectors receive the same denial. Runtime table access is SELECT
only, with FORCE RLS and active-actor tenant membership. A package version or
quota cannot be forged by POST or direct runtime SQL.

`Seat` is an immutable one-to-one binding to `UserClinicRole`, not a second RBAC
store. Each unrevoked role assignment occupies one seat, including multiple
assignments for one user. Account deactivation retains its allocation; revoking
the assignment releases it immediately. New assignments serialize against the
subscription row and cannot exceed the seat allowance. A plan downgrade cannot
silently strand existing seats. Restricted or cancelled subscriptions do not
revoke clinical staff.

AI admission checks the server allowance in both Python and a BEFORE INSERT
database trigger. Outstanding worst-case token/audio/cost reservations consume
the same allowance, under the subscription row lock; concurrent admissions
cannot oversubscribe the last allowance. Dispatch checks current subscription
availability again. Quota exhaustion, expiry or restriction returns the existing
manual-workflow result and never changes a clinical document. Other source
meters remain transparent consumption/alert facts: in-progress care, reconciled
message receipts and accepted clinical uploads are never rejected after their
effect merely to fit a commercial quota. The commercial API reports available,
used, quota, reason and an 80-percent spending/usage alert for each meter.

Usage comes from database-owned source hooks, not a browser quantity:

- settled `AIInvocation`: input/output/cached tokens, audio seconds and integer
  micro-BRL cost, including conservative unknown-cost failures;
- an ended/failed started teleconsultation: elapsed seconds rounded up, times
  its two fixed participants, explicitly participant-seconds;
- an outbound message reaching sent/delivered/read: one send, never another
  charge for a subsequent delivery status;
- an ingested EHR attachment: accepted `size_bytes`. This meter measures bytes
  ingested during a period, not a claim about current occupied storage.

`UsageEvent` has `(organization, meter, source_ref)` uniqueness. An identical
source replay is a no-op; different quantity/time for that source conflicts.
Only the resolver-owned source hooks may call the writer. `UsageRollup` updates
exactly once in the same transaction, in the source's UTC occurred-at calendar
month, including late facts. There are no patient IDs, text, audio, provider
payloads, email addresses, credentials or arbitrary source strings in this app.
`source_ref` is an opaque UUID. No clinical content reaches audit or evidence.

`POST /api/subscriptions/v1/` is a private, CSRF/TOTP-protected commercial API.
Its body contains `clinic_id` and action `read`, `change_plan` or `cancel`.
Mutations also require `command_id` and `expected_revision`; plan changes name
only `plan_code`. Unknown extra fields, including entitlements, are refused.
Authorization for the response is decided before a write. Refusals never write
the session or cookies. There is no provider callback route or arbitrary
event/entitlement write API.

An operator uses an authenticated owner database connection:
`subscription_event bootstrap-synthetic` explicitly creates only a researched
`saas_collection` fixture registry record; it is not an approval.
`subscription_event apply --operator-id ... --organization-id ... --clinic-id
... --subscription-id ... --event-id ... --action paid|payment_failed|advance|
offboard` binds that operator transaction-locally and calls the owner-only
SECURITY DEFINER platform resolver. The stored subscription supplies scope;
the simulated receipt supplies no tenant claim. Current missing, revoked,
degraded, foreign or non-synthetic provider versions refuse the fixture path.
The runtime role cannot execute the platform resolver, even with forged GUCs.
Duplicate receipts return their original revision/state; changed replay terms
conflict. No command or fixture satisfies an external gate.

The first migration installs all grants, FORCE RLS, retained history guards,
source hooks and defaults atomically. It is additive; local reverse/forward is
an empty synthetic database rehearsal. Once commercial history exists, retain
it and use a forward correction or a verified restore. All seven relations are
classified in the recovery manifest. Exact catalog pins are regenerated by
`tests.subscriptions.catalog.discover_catalog` (imported as `subscriptions.catalog`
under pytest). Canonical browser gates remain deferred to the host quiet batch.
