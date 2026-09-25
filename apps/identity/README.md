# Identity and authorization

`UserClinicRole` remains the sole staff membership authority. Never infer
clinical authority from Django flags, a professional registration, a care-team
row, or a caller-supplied actor. Offboarding revokes membership; it never deletes
a user (`UserClinicRole.user` retains its legacy CASCADE relationship).

## Permission bundles v1

`permissions.PERMISSIONS` is the closed action vocabulary. `BUNDLES_V1` is an
immutable mapping of stored staff roles to immutable permission sets, following
ADR-003 and the successor RP matrix. Released versions are immutable; a change
requires a new bundle version and migration, not editing v1 SQL in place.

- Nurse and allied-professional defaults are observation/task scope, not SOAP
  narrative or prescribing rights. Their registrations remain profession-specific.
- Scheduler uses the reception bundle; clinic_admin uses clinic_manager; owner
  uses org_admin. These aliases describe **new permission checks**, not a rewrite
  of the legacy service contracts.
- Administrative roles have no default clinical narrative permission.
- `appointment.*_own` is authority only over the actor's agenda/care-team slots;
  consumers must filter the practitioner/slot. It is not `appointment.book`.
- Read, billing-minimum read, route, request, propose and approve are distinct
  actions. Consumers must not treat one as another.
- Restricted notes require author plus an explicit future note grant. No v1
  staff bundle supplies `restricted.read`.
- Patient/delegate, support and service-principal RP cells are **not staff
  bundles**. Their separate grant boundaries belong to todos 19, 65 and 7.
- Break-glass permissions permit requesting a grant, never accessing a chart.

`current_context.require_permission(permission, *, clinic_id,
patient_enrollment_id=None)` returns the current GUC-bound actor or raises
`CurrentActorError` without reflecting selectors. It delegates every decision to
`clinic_app.has_permission(text, uuid, uuid)`. The resolver is fixed SQL,
SECURITY DEFINER, owned by clinic_resolver, revoked from PUBLIC, and pinned to
`pg_catalog,clinic_app,pg_temp`. It reads current membership, active-user state,
exact clinic/organization, active removals and subject scope on every call.
There is no authorization cache. READ COMMITTED observes a committed revocation
at the next statement, including inside an existing request transaction.

`require_current_actor_org_admin(organization_id, roles)` keeps todo 9's
organization-wide coverage requirement. Each clinic needs either a caller-allowed
legacy assignment or a canonical `org_admin` assignment that passes
`require_permission("staff.organization", clinic_id=...)`. Canonical assignments
never bypass remove-only grants, even when `org_admin` appears in `roles`.
A single clinic's assignment, an empty role contract, an empty organization or
a foreign organization cannot authorize an organization-wide operation. Legacy
clinic settings still require their existing exact-clinic roles and TOTP; this
helper does not migrate or bypass those domain guards.

Clinical actions additionally require a current, regular, synthetic professional
registration matching clinic UF and the exact canonical clinical role. They also
require an enrollment in that clinic and either a current care-team membership
for that role or an open encounter assigned to the physician. An unknown or
wrong-clinic enrollment is indistinguishable. Permission eligibility does not
replace domain-level author restrictions, class eligibility, release policy,
approval or recent step-up; prescribing continues to use per-attempt physician
verification. No real council verification or provider activation is introduced.

## Scope records and database posture

- `RoleGrant` is a clinic-and-role subtraction, never a membership or positive
  grant. A database CHECK permits only `effect=remove`, version 1; a second CHECK
  confines its permission to that role's frozen bundle. Half-open validity is
  `[valid_from, valid_to)`, with a nullable upper bound. Rows are immutable.
  Multiple canonical roles compose by union after each role's own removals.
- `CareTeamMembership` binds clinic, enrollment, user and clinical role with the
  same half-open validity convention. It cannot create a staff membership.
- `ProfessionalRegistration` stores council as bounded data (CRM, COREN, CRP,
  CREFITO and other allied councils), encrypted number and specialty, UF, role,
  reused PhysicianProfile status vocabulary, and a bounded validity window.
  An optional legacy PhysicianProfile link must match organization/user/UF and
  is revalidated for current synthetic registry status on use. It does not
  replace or mutate PhysicianEvidence or the signing verification workflow.

These are owner-provisioned authority records, like existing PhysicianProfile.
`scope_provisioning` supplies keyword-only `narrow_role`, `assign_care_team`,
`register_professional`, `revoke_care_team` and `revoke_professional` services.
They require the clinic_owner database connection plus a current exact-clinic
`staff.organization` permission. Call them inside the owner lifecycle's bound
operator context, after its authentication/step-up boundary; they are not web
handlers. Each transition appends a registered metadata-only audit event in the
same transaction. Revocation retries serialize and do not append duplicate events;
a failed audit rolls back the scope change. No runtime provisioning UI is added.
The runtime has SELECT only, no column writes. RLS allows a member to read their
own scope, or an exact-clinic staff administrator to inspect scope. Even an org
administrator needs the explicit clinic assignment. Composite foreign keys reject
cross-clinic/organization links. The database forbids deletes and identity changes;
care/professional records permit only irreversible `revoked_at` transitions.

## Legacy compatibility and migrations

No legacy role guard is replaced: none has identical full semantics to the RP
boundary (for example, legacy owners book, whereas the org-admin default is
agenda read-only). `tests/identity/legacy_guards.json` is a reviewed authorization
census, not a list of calls to one role helper. Its AST discovery includes
scope/access functions, membership predicates, SQL resolver calls, denial
branches, decorators, mixins, current-actor checks and tenant boundaries.
The parity suite rejects unclassified candidates and inventory drift. Each
candidate names executable probes, explicitly delegates to probed guards, or
has a reviewed non-staff/infrastructure/presentation/state-only classification.
New permission-bundle boundaries remain covered by their dedicated tests.

The `legacy_*boundaries.py` adapters call real Python boundaries and PostgreSQL
resolvers for owner, physician, receptionist and clinic_admin, using valid and
foreign/missing subjects, unauthenticated requests, stale verification and
revoked/unassigned scope as appropriate. Every named Python probe must actually
enter its target callable; role-tuple inference cannot satisfy the test.
Positive fixtures contain real records, memberships, confirmed devices and
synthetic provider results. Savepoint rollback isolates each decision. Only the
verification clock and asynchronous transport handoff are controlled; authority
is never mocked. Denial-only helpers are paired with successful service probes.
Existing services retain their assignment, step-up, audit and RLS contracts
until their owning feature migrates them.

Migration `0013_permission_bundles`, after `0012_queue_quotas`, is additive, with table creation, default-DML revocation and
FORCE RLS in one DDL transaction. Protected columns are bytea from creation;
there is no plaintext conversion, backfill or plaintext rollback. Existing
protected-field conversion migrations remain non-atomic and irreversible.
Reversing this additive migration removes only the new tables/helper; role
values already stored in UserClinicRole are not rewritten. Once new authority
history exists, use an additive forward correction rather than dropping it.

## Service principals (ADR-019, task 7)

`ServicePrincipal` is a machine identity, never a UserClinicRole or a clinician.
Its organization, exact clinic, machine slug name/purpose, database login and
version are immutable; only `active=True -> False` is allowed. `db_identity` is
unique across organizations: sharing a credential between caller-selectable
principals would defeat the boundary. The initial login is `clinic_agent`;
separate owner-provisioned `clinic_agent_*` logins can inherit that role's narrow
privileges, never staff/resolver privileges. Bootstrap creates no such child
logins and supplies no default machine password.

`service_principals` exposes keyword-only `register_principal`, `grant_principal`,
`revoke_principal` and `revoke_principal_grant`. Like `scope_provisioning`, these
require an authenticated owner lifecycle transaction and the current operator's
exact-clinic `staff.organization` permission. Every transition records a fixed
metadata-only audit event atomically; revocation retries do not duplicate events.
Names/purposes are machine slugs, not personal names or free-text instructions.
Neither clinic_app nor clinic_agent can read or write the underlying authority
tables. Resolver-owned fixed functions expose only the authorization decision.

`service_principal_context(*, principal_id, clinic_id)` owns an outermost
transaction on `DATABASES['agent']`; consumers explicitly use `.using('agent')`.
It verifies `session_user == db_identity`, an active registration and active
exact-clinic grant before setting `app.current_principal` and
`app.current_tenant`. It never sets `app.current_user_id`. Nesting, mixed
staff/patient context and repeatable-read snapshots are refused. The
`clinic_app.principal_has(permission, clinic)` RLS helper checks the stored
identity and active grants at every statement, so committed revocation is
visible even within an existing READ COMMITTED transaction. All four context
GUCs are cleared on exit. Staff guards reject an active machine context and
non-staff database roles even when a physician GUC has been forged.

V1 grants intentionally permit only `appointment.read`, with `subject_scope`
exactly `clinic`. Availability is the sole initial table exposed, under an
agent-only restrictive policy in addition to the unchanged tenant policy.
Clinical, financial, staff-management and wildcard grants are not supported;
future domains must add reviewed grant versions, explicit `AGENT_GRANTS` and
RLS, not silently reinterpret a v1 scope. Audit/key tables have no agent SELECT.
Machine action approvals and execution receipts remain owned by the later
action gateway, not a fabricated human outbox actor.

Migration `0014_service_principals` is additive and installs FORCE RLS, exact
ACLs, immutable history triggers and composite organization foreign keys in
one DDL transaction. Rehearsal reversal is for an empty synthetic database;
after authority history exists, retain the tables and use forward corrections.
