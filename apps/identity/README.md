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
agenda read-only). `tests/identity/legacy_guards.json` inventories all 47 existing
current-actor guard call sites; parity tests exercise all four legacy role
truth tables and DRF role gates. Existing services retain their assignment,
step-up, audit and RLS contracts until their owning feature migrates them.

Migration 0011 is additive, with table creation, default-DML revocation and
FORCE RLS in one DDL transaction. Protected columns are bytea from creation;
there is no plaintext conversion, backfill or plaintext rollback. Existing
protected-field conversion migrations remain non-atomic and irreversible.
Reversing this additive migration removes only the new tables/helper; role
values already stored in UserClinicRole are not rewritten. Once new authority
history exists, use an additive forward correction rather than dropping it.
