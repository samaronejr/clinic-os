# Clinic OS foundation architecture

This document describes the code that exists in the Phase 0 foundation. It is
not a roadmap claim. Product workflows such as prescribing, consent capture,
clinical records, scheduling, billing, messaging, retention execution, and
interoperability are deliberately unavailable until a later phase.

See [SECURITY.md](SECURITY.md) for trust boundaries,
[RUNBOOK.md](RUNBOOK.md) for operations, and
[CONTRIBUTING.md](CONTRIBUTING.md) for change rules.

## Runtime shape

Clinic OS is a Django 5.2 application backed by PostgreSQL 16. Django owns the
HTTP and session layer; PostgreSQL owns the authoritative tenant boundary and
the append-only audit ledger. The project exposes both WSGI and ASGI entry
points, but all current tenant middleware is synchronous. Redis, Celery, and
DRF are dependency/configuration foundations only: there are no project Celery
tasks and there is no shipped Phase 0 domain API.

The production URL set is intentionally small: the shell, `/healthz`,
`/readyz`, and identity routes. Debug-only identity showcase routes are added
only by `config.urls_dev`.

## Domain module map

The 13 registered domain apps have explicit `AppConfig` classes. Three contain
implemented Phase 0 behavior:

| App | Phase 0 responsibility |
| --- | --- |
| `identity` | UUID users, organizations, clinics, canonical role assignments, password-session authentication through database resolvers, TOTP enrollment/verification, and recent-verification step-up |
| `tenancy` | transaction-scoped tenant context, middleware, RLS DDL helpers, membership resolvers, and the schema-policy sentinel |
| `audit` | typed semantic events, RFC 8785 canonical hashing, trusted-context append functions, immutable per-organization chains, and chain verification |

Ten apps are registered extension seams whose public service entrypoints raise
exactly `NotImplementedError("Phase >=1")`:

| Deferred app | Reserved service boundary |
| --- | --- |
| `scheduling` | appointment creation |
| `intake` | intake submission |
| `ehr` | clinical-note recording |
| `teleconsult` | teleconsultation start |
| `prescription` | prescription issuance |
| `consent` | consent recording |
| `billing` | invoice creation |
| `comms` | message delivery |
| `retention` | retention-policy execution |
| `interop` | clinical-record exchange |

The `apps.core` package supplies the project shell and health endpoints; it is
not one of the 13 registered domain apps. The deferred modules contain adapter
interfaces and phase-boundary stubs, not working product features.

## Request, session, and tenant transaction

Authenticated tenant requests use a signed Django session containing the user
identifier and `active_org_id`. `TenantMiddleware.__call__` rejects missing or
malformed identifiers before domain code runs. It then opens the outermost
durable transaction and performs this ordered sequence:

1. set transaction-local `app.current_user_id`;
2. call the fixed `clinic_app.user_has_org(org_id)` resolver, which derives the
   user from that GUC rather than accepting an arbitrary user argument;
3. set transaction-local `app.current_tenant` only after membership succeeds;
4. execute authentication rehydration and all downstream ORM work inside the
   same transaction;
5. roll back responses with status 500 or greater, reject streaming responses,
   and reset both GUCs on every exit path.

Health, login, static, and shell routes are explicit bypasses and have their
connection GUCs cleared before and after execution. Django
`ATOMIC_REQUESTS` is disabled so the tenant boundary owns the full transaction
lifetime.

PostgreSQL applies `ENABLE ROW LEVEL SECURITY` and `FORCE ROW LEVEL SECURITY`
to every current tenant table. Policies compare the row's organization column
to `NULLIF(current_setting('app.current_tenant', true), '')::uuid`; an unset or
empty tenant therefore returns no rows instead of becoming a global scope.

## Identity and relational model

The core relationship is:

```text
Organization 1 --- * Clinic
Organization 1 --- * UserClinicRole * --- 1 User
Clinic       1 --- * UserClinicRole
```

`Organization` is the tenant root. Each `Clinic` belongs to one organization.
`UserClinicRole` joins one user, one clinic, one organization, and one stored
role (`owner`, `physician`, `receptionist`, or `clinic_admin`). A composite
foreign key from `(organization_id, clinic_id)` to the clinic's
`(organization_id, id)` prevents a membership from naming a clinic in a
different organization.

`UserClinicRole` is the sole RBAC authority. Permission classes and query
helpers query those stored assignments in the active tenant; Django staff or
superuser flags are not an application authorization substitute. New roles or
permissions must extend this model and its tests rather than introduce a
second authority.

## Authentication, TOTP, and recent verification

The custom authentication backend never directly queries `identity_user` as
the runtime role. Before a session exists it calls `auth_lookup(username)`;
inside a tenant request it calls `load_current_user()`, which is bound to
`app.current_user_id`. Password login selects a deterministic organization
from the user's memberships and stores it as `active_org_id`.

Privileged-role routes use the baseline `privileged_totp_required` guard.
Enrollment and verification bind a confirmed TOTP device to the current user.
Successful verification rotates the session key, calls the django-otp login
binding, and records a freshness timestamp.

Sensitive future operations can use `assert_step_up(request)` at a service
boundary or `require_recent_verification()` at a view boundary. The default
window is 300 seconds, inclusive. The check also requires the exact confirmed
persistent device for the active user and fails closed for malformed, future,
or stale timestamps. The step-up guard is role-neutral: a future prescription,
consent, or document-issuance workflow must apply it to every authorized role
that can perform that operation. Those workflows are not implemented here;
only the reusable guard and `/auth/step-up/` challenge exist.

## Audit boundary

Application code constructs a bounded semantic event and a payload restricted
to the audit vocabulary. It derives organization and actor from transaction
GUCs, canonicalizes versioned content with RFC 8785, applies a domain-separated
SHA-256 content hash, and calls the database `audit_append` function.

The database serializes each organization's chain, combines the content hash
with the previous hash, and inserts the row. Ordinary application code can
read only the tenant-filtered view and cannot insert, update, delete, or
truncate the base ledger. A separate all-zero organization chain is reserved
for owner-only system events. `verify_chain()` rebuilds semantic hashes and
the linked chain without returning sensitive payloads in failure evidence.

External timestamping or anchoring is not part of Phase 0. The exact threat
boundary is documented in [SECURITY.md](SECURITY.md).

## Delivery and extension boundaries

- The local database surface is Docker PostgreSQL 16; migrations run as
  `clinic_owner`, while the application connects as `clinic_app`.
- CI runs the same bootstrap, migrations, posture, lint, type, test/coverage,
  and dependency-audit gates on Python 3.12 and 3.13.
- `terraform/` is a validation-only AWS `sa-east-1` RDS skeleton. No resource
  has been provisioned and no plan/apply belongs to foundation validation.
- PITR procedures, external audit anchoring, user mutation definers, password
  reset/admin editing, APIs, background jobs, and all ten deferred product
  domains require later design, authorization, tests, and operational review.

Source anchors: [settings](../config/settings/base.py),
[tenant transaction](../apps/tenancy/db.py),
[tenant middleware](../apps/tenancy/middleware.py),
[identity models](../apps/identity/models.py),
[step-up policy](../apps/identity/stepup.py), and
[audit service](../apps/audit/services.py).
