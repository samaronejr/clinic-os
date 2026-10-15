# Parallel safety of the agenda-grid refusal witnesses

Scope: `tests/core/test_workspace_agenda_grid_refusals.py`, moved by
`30e5d25` from `tests/scheduling/test_agenda_grid_http_refusals.py`.

## Why e93b988 correctly required serial execution

The original test location inherited `tests/scheduling/conftest.py`:

- `scheduling_world` (lines 118-128) is autouse. For a test requesting
  `rbac_graph`, it enters `scheduling_worlds.world(...)`.
- The local `rbac_graph` override (lines 132-134) returns that world.
- The local `_django_db_helper` (lines 92-106) substitutes world teardown
  for Django's normal per-test database cleanup.
- `pytest_collection_modifyitems` (lines 54-72) therefore requires a
  `server-state` classification for every such scheduling test.

`Worlds.seeded` and `Worlds.world` in `tests/scheduling/worlds.py:71-126`
create a `scheduling_template_<uuid>` and a `scheduling_world_<uuid>`.
`tests/scheduling/world_database.py:19-72` uses an autocommit superuser
connection to the **postgres database** for `CREATE DATABASE`, database
`CONNECT` ACL changes and `DROP DATABASE ... WITH (FORCE)`.
These are committed cluster-global `pg_database` changes, even when the
HTTP assertions themselves only read rows. The serial entry added by
`e93b988` was necessary for that fixture chain.

## Why the core location has a different state boundary

Pytest fixtures are scoped by the collected test's directory, not by the
module containing an imported helper. The core test inherits
`tests/conftest.py:16-17`, which re-exports
`tests/rbac_fixtures.py:35`'s `rbac_graph`. It does not inherit scheduling's
world, graph override or cleanup override.

The parent graph creates ordinary organization, clinic, user and role rows
through Django's active `default` connection. It does not create/drop a
database or alter a PostgreSQL role. Scheduling's template fixture itself
calls this same parent graph after creating its scratch database
(`tests/scheduling/worlds.py:77-81`).
Its `synthetic_secret_backend` dependency is per-test `tmp_path` storage
(`tests/tenant_key_support.py:35-50`), not shared server configuration.

Host-free `--collect-only --fixtures-per-test` inspection collected all
three core cases alongside a scheduling sibling. Every core case resolves
`rbac_graph` to `tests/rbac_fixtures.py:35` with no scheduling-world fixture;
the sibling resolves it to `tests/scheduling/conftest.py:132` and includes
`scheduling_world` and `scheduling_worlds`. No test body or database fixture
was executed by this inspection.

The unchanged `django_db(transaction=True)` marker uses pytest-django's
ordinary `_django_db_helper` and `TransactionTestCase` cleanup. The pinned
pytest-django 4.12.0 fixture chain is:

`django_db_setup` -> `django_db_modify_db_settings` ->
`django_db_modify_db_settings_parallel_suffix` ->
`django_db_modify_db_settings_xdist_suffix`.

The last fixture applies the actual worker ID, such as `gw0`, to the test
database name. With the canonical database named `clinic`, each worker
therefore uses **test_clinic_gw0**, **test_clinic_gw1**,
**test_clinic_gw2** or **test_clinic_gw3**, not `clinic`, unsuffixed
`test_clinic`, `postgres` or a scheduling scratch world.
`ops/testing/ci_pytest.py:375-443` provisions these exact worker clones
before starting its shared-catalog observation window.

`tests/conftest.py:55-80` includes the `locks` alias for these tests.
`config/settings/test.py:20-21` makes it a test mirror of `default`, so
session advisory locks use the same worker database.

## The HTTP helpers stay inside that worker database

`scheduling.test_agenda_grid._seeded` (lines 362-378) creates availability,
an enrollment, an appointment and a resource using normal services.
It does not call `build_world`, `Worlds`, `clone_database` or an admin URL.
Its imported `_reload` (lines 391-393) uses the ordinary appointment ORM.
`scheduling.appointment_http_support.seed_enrollment` and `seed_appointment`
likewise use the active Django connection.

`patient_http_support.audit_event_types` (lines 93-103) explicitly queries
`django.db.connection`; it does not connect to an unsuffixed database.
`patient_service_support.runtime_role` (lines 13-20) executes session-local
`SET ROLE clinic_app` / `RESET ROLE`, not cluster-global `ALTER ROLE`.
Tenant GUCs are transaction-local.

The tests still commit real fixture, authentication/session, appointment
and audit rows. Those writes are database-local to their worker. No body,
assertion or service was weakened. This is the same placement and parent
fixture mechanism as
`tests/core/test_workspace_late_refusals.py::test_scheduling_service_rechecks`.

## Inventory and partition contract

The serial manifest accepts exact Python-file/node prefixes, not directory
wildcards (`ops/testing/ci_pytest_plugin.py:96-145`). The old file entry was
removed; no entry classifies the new core module. The new host-free
`test_grid_refusal_module_stays_in_the_parallel_partition` derives every
test node from the actual module and checks it against the real manifest.
Moving it back or adding a matching serial entry makes that check fail.

The CI driver still collects `tests`, including the new file, and validates
the disjoint union of parallel and serial populations
(`ops/testing/ci_pytest.py:116-204`). Refusal receipts merge within the
parallel pytest session, not across phases. Keeping these real HTTP
witnesses there satisfies the existing observation mechanism without an
allowlist or guard change.

Host-free verification on this follow-up passed:

- Ruff, formatting and strict mypy for the refusal and CI-driver test modules.
- All 33 cases from the 18 host-free CI-driver/manifest unit functions,
  including the new parallel-partition pin; zero failures or skips.
- Full collection: 8,237 distinct nodes, partitioned into 7,650 parallel and
  587 serial nodes. All 235 serial prefixes matched; the sets are disjoint
  and their union is the full corpus. All three grid-refusal nodes are in
  parallel and the removed scheduling path is absent.

The four database-dependent CI-driver test functions were not selected.
Database URLs used an unavailable local endpoint and the broker was in
memory. Collection ran no application test body; no database, container or
browser was provisioned for these checks.

This is a source and host-free collection/unit proof. A fresh real-role
HTTP run and four-worker shared-catalog/refusal-guard certification remain
queued after todos 27 and 18; no runtime success is claimed here.
