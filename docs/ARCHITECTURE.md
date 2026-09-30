# Clinic OS architecture

This document describes the implemented synthetic-data surface. It is not a
production, pilot, or live-data claim. The Phase 1A foundation — patient
registration/search, staff availability, booking, agendas, rescheduling, and
cancellation — exists, and the renewal domains (clinical records, consent,
teleconsultation, prescriptions, billing, messaging, retention) are
implemented and verified in synthetic mode on labelled synthetic adapters.
Real issuance, automatic retention destruction and clinical-record exchange
remain deferred stubs; every provider-backed slice stays `waiting_external`
until its capability record is approved.

See [SECURITY.md](SECURITY.md) for trust boundaries,
[RUNBOOK.md](RUNBOOK.md) for operations,
[CONTRIBUTING.md](CONTRIBUTING.md) for change rules, and
[the renewal roadmap](plans/clinic-os-renewal-roadmap.md) for task status and
the external prerequisites of each provider slice.

## Runtime shape

Clinic OS is a Django 5.2 application backed by PostgreSQL 16. Django owns the
HTTP and session layer; PostgreSQL owns the authoritative tenant boundary and
the append-only audit ledger. The project exposes both WSGI and ASGI entry
points, but all current tenant middleware is synchronous. Redis backs the
Celery broker for the comms reminder worker: `apps/comms/tasks.py` ships
`comms.execute_operation` (one stored outbox operation through the trusted
worker boundary) and `comms.dispatch_due_reminders` (claims due outbox rows),
which `config/celery.py` schedules through beat every 60 seconds. DRF remains
a dependency/configuration foundation only: there is no shipped domain API.

The URL set includes the shell, `workspace/`, `sw.js`, `/healthz`, `/readyz`,
identity, clinic intake, availability, booking, agenda, reschedule, and
cancellation routes, plus the renewal domain mounts for ehr, prescription,
retention, billing, consent, and teleconsult. Debug-only identity showcase
routes are added only by `config.urls_dev`.

## Domain module map

The 13 registered domain apps have explicit `AppConfig` classes. Five contain
implemented Phase 1A behavior:

| App | Phase 1A responsibility |
| --- | --- |
| `identity` | UUID users, organizations, clinics, canonical role assignments, password-session authentication through database resolvers, TOTP enrollment/verification, and recent-verification step-up |
| `tenancy` | transaction-scoped tenant context, middleware, RLS DDL helpers, membership resolvers, and the schema-policy sentinel |
| `audit` | typed semantic events, RFC 8785 canonical hashing, trusted-context append functions, immutable per-organization chains, and chain verification |
| `intake` | organization-scoped patient identity, clinic enrollment, body-only search, pagination, and idempotent registration |
| `scheduling` | practitioner availability, booking, day/week agendas, terminal cancellation, rescheduling, and audited staff-only screens |

The `comms` domain implements durable, independently gated synthetic appointment
reminders through the shared integration boundary; real providers remain blocked.
See [communications](../apps/comms/README.md) for the scheduling, actor, retry and
receipt contract.

The renewal waves implemented the remaining domain apps in synthetic mode:
`ehr` (encounters, SOAP notes, problems/allergies, quarantined attachments,
amendments), `teleconsult` (scoped sessions, waiting room, clinician
workspace), `consent` (versioned capture and revocation), `billing`
(invoices, synthetic PIX, reconciliation, receipts), `retention` (policies,
legal holds, controlled export) and `prescription` (drafts, `synthetic-pdf-v1`
documents, synthetic signing, public verification). Each provider-backed slice
uses a labelled synthetic adapter that refuses non-synthetic mode; real
integration stays `waiting_external` on its task-6 capability record.

Three service entrypoints remain deferred stubs that raise exactly
`NotImplementedError("Phase >=1")`:

| Deferred entrypoint | Reserved service boundary |
| --- | --- |
| `prescription.issue_prescription` | real prescription issuance (awaits the signing capability) |
| `retention.apply_retention_policy` | automatic retention-policy destruction (no approved disposal policy) |
| `interop.exchange_clinical_record` | clinical-record exchange (RNDS/SNCR preparation only) |

The `apps.core` package supplies the project shell, readiness, privacy headers,
and health endpoints; it is not one of the 13 registered domain apps.

### Renewal prescription drafts

Task 32 adds [synthetic prescription drafts](../apps/prescription/README.md),
separate from the still-deferred issuance boundary above. Drafts bind an open
encounter, patient and issuer, retain immutable item snapshots, compare expected
versions, and enforce current assigned-physician authority in services and FORCE
RLS. Active prescription drafts block encounter closure. The native no-store
workspace is reached from the encounter; clinical selectors stay in POST bodies.
No real category has a confirmed task-6 issuance contract. The visibly synthetic
category permits draft rehearsal only and never infers medication safety.

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

External timestamping or anchoring is not part of Phase 1A. The exact threat
boundary is documented in [SECURITY.md](SECURITY.md).

## Delivery and extension boundaries

- The local database surface is Docker PostgreSQL 16; migrations run as
  `clinic_owner`, while the application connects as `clinic_app`.
- CI runs the same bootstrap, migrations, posture, lint, type, test/coverage,
  and dependency-audit gates on Python 3.12 and 3.13. Renewal acceptance adds
  a sharded real-Chromium matrix over every registered suite, a real
  Redis/Celery worker-integration gate, a fresh-install plus merge-base
  upgrade rehearsal, mandatory Poppler inspection, and an `if: always()`
  `Renewal RC acceptance` aggregate verdict; see
  [releases/2026-09-24-renewal-rc-m1.md](releases/2026-09-24-renewal-rc-m1.md).
- Renewal verification adds a current-source route:
  `ops/testing/renewal_runner.py` snapshots the working tree, provisions a
  task-owned PostgreSQL container, serves through Gunicorn as `clinic_app`,
  and drives registered real-Chromium suites. Its `ci` subcommand runs the
  static, migration, coverage, dependency, image/TLS and browser gates
  against uncommitted source. `make ci` remains the committed-source gate;
  prerequisites and failure behavior are in
  [RUNBOOK.md](RUNBOOK.md#renewal-verification-runner).
- `terraform/` is a validation-only AWS `sa-east-1` RDS skeleton. No resource
  has been provisioned and no plan/apply belongs to foundation validation.
- PITR procedures, external audit anchoring, user mutation definers, password
reset/admin editing, APIs, real provider integrations, the three deferred
  service entrypoints, and any background job beyond the delivered comms
  reminder worker require later design, authorization, tests, and operational
  review.

The disposable logical recovery rehearsal is a synthetic integrity check, not
a live backup design. Live use remains blocked by
[LIVE-DATA-GATE.md](compliance/LIVE-DATA-GATE.md).

## ADR index

The architecture decisions for the Clinic Ops successor plan are recorded in
[`adr/`](adr/), one file per decision, each with context, decision,
consequences, rejected options and a revisit trigger. They're decisions, not
implementation claims: everything above still describes the running system
until the owning todo lands. `tests/infra/test_adr_index.py` checks the
record structure and that ids run from 000 to 019 without gaps. Threat models
are indexed in [SECURITY.md](SECURITY.md#adr-index).

| ADR | Decision | Owning todos |
| --- | --- | --- |
| [ADR-000](adr/ADR-000-successor-product-contract.md) | Successor product contract supersedes renewal scope bans | 1 |
| [ADR-001](adr/ADR-001-frontend-htmx-first-islands.md) | HTMX first; bounded React + TypeScript islands only if the agenda slice selects them | 23 |
| [ADR-002](adr/ADR-002-realtime-sse.md) | Realtime over SSE from a separate ASGI process | 8 |
| [ADR-003](adr/ADR-003-authorization-permission-bundles.md) | One permission-bundle authorization model | 6 |
| [ADR-004](adr/ADR-004-server-side-drafts.md) | Server-side durable drafts with compare-and-swap | 27 |
| [ADR-005](adr/ADR-005-chunked-media-upload.md) | Chunked HTTPS media upload to private encrypted storage | 40 |
| [ADR-006](adr/ADR-006-ai-gateway-routing.md) | Provider-neutral AI gateway with per-purpose routing | 38 |
| [ADR-007](adr/ADR-007-deterministic-retrieval.md) | Deterministic patient-scoped retrieval, no vector store at start | 44 |
| [ADR-008](adr/ADR-008-workflows-and-approval-binding.md) | DB-persisted workflows on Celery with digest-bound approvals | 26, 39 |
| [ADR-009](adr/ADR-009-official-messaging-adapters.md) | Official messaging adapters and purpose-separated consent | 51 |
| [ADR-010](adr/ADR-010-prescription-signature-trust.md) | Prescription class taxonomy and qualified signature trust | 35, 36 |
| [ADR-011](adr/ADR-011-financial-journal.md) | Integer-centavo double-entry journal | 56 |
| [ADR-012](adr/ADR-012-tiss-rnds-adapters.md) | TISS and RNDS adapters behind the interop boundary | 59, 68 |
| [ADR-013](adr/ADR-013-offline-and-native.md) | No offline clinical mode; native companion only on a failed criterion | 69 |
| [ADR-014](adr/ADR-014-observability-redaction.md) | Allowlist-redacted observability | 11 |
| [ADR-015](adr/ADR-015-isolation-encryption-search.md) | FORCE RLS, envelope encryption and blind-index search | 17, 71 |
| [ADR-016](adr/ADR-016-recovery-with-key-escrow.md) | Recovery drills include keys and objects | 72 |
| [ADR-017](adr/ADR-017-additive-migrations.md) | Additive expand/contract migrations | 17, 22, 66 |
| [ADR-018](adr/ADR-018-connection-pooling.md) | pgbouncer transaction pooling with a session alias for locks | 8 |
| [ADR-019](adr/ADR-019-service-principals.md) | Service principals on a dedicated `clinic_agent` role | 7 |

Source anchors: [settings](../config/settings/base.py),
[tenant transaction](../apps/tenancy/db.py),
[tenant middleware](../apps/tenancy/middleware.py),
[identity models](../apps/identity/models.py),
[step-up policy](../apps/identity/stepup.py), and
[audit service](../apps/audit/services.py).
