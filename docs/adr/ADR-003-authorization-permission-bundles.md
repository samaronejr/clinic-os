# ADR-003: One permission-bundle authorization model

- Status: accepted 2026-09-24
- Recorded by: todo 2
- Related decisions: D-16

## Context

`UserClinicRole` is the only RBAC authority today, with four stored roles
(`owner`, `physician`, `receptionist`, `clinic_admin`). Views and services
check roles through `require_current_actor_clinic_roles`, and RLS scopes rows
by tenant GUCs.

The successor scope adds nurses, allied professionals, schedulers, managers,
finance staff, organization admins, patient delegates, support staff and
service principals, plus care-team and professional-scope limits. Four role
names checked in views can't express the role matrix (RP table in the plan).

## Decision

Authorization uses one permission-bundle model in
`apps/identity/permissions.py`.

- Bundles map roles to permission strings and are versioned in code.
- Clinic overrides can only narrow a bundle, never widen it.
- Grants cover membership, unit, care team or encounter, professional scope,
  delegates and service principals. Per D-16, `identity` owns every grant
  kind; there's no separate platform app.
- Enforcement happens in services (`require_permission`) and in RLS through a
  `SECURITY DEFINER` helper, so both layers agree.

## Consequences

- New tables' policies call the same permission helper that services use.
- Existing guards keep their current behavior; a parity test lists each one
  before any call site moves.
- Admin roles don't get clinical-note access by default.
- Every new capability declares its permission strings in one registry, which
  makes review and audit mapping simpler.

## Rejected

- Frontend-side or agent-side authorization. A browser island or an agent
  prompt can be bypassed or manipulated; only the service layer and the
  database see trusted context.

## Revisit trigger

None recorded. The AD table sets no trigger. Changing this model needs a new
ADR that supersedes this one.

## Owning todos

6. Grant kinds are extended by 7 (service principals), 19 (delegates) and 65
(break-glass, support access).
