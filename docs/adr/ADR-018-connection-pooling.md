# ADR-018: pgbouncer transaction pooling with a session alias for locks

- Status: accepted 2026-09-24
- Recorded by: todo 2

## Context

The runtime connects as `clinic_app` with
`options="-c search_path=clinic_app,public"` in `config/settings/base.py`.
Tenant context lives in transaction-local GUCs, which work under transaction
pooling. The outbox holds session-level advisory locks
(`apps/core/integration.py:416-420,625,697`), which don't: under transaction
pooling a session lock can land on a connection another client reuses. Web,
workers and the realtime process (ADR-002) together will need more
connections than PostgreSQL should hold open.

## Decision

- Runtime traffic goes through pgbouncer in transaction mode.
- A session-pool or direct alias serves the outbox advisory locks.
- `ALTER ROLE clinic_app SET search_path` so the path doesn't depend on
  startup options that transaction pooling may drop.
- psycopg runs with `prepare_threshold=None`.
- `idle_in_transaction_session_timeout` is set on `clinic_app`.

## Consequences

- Code that needs session state (advisory locks) must use the named alias;
  posture tests pin the role settings.
- Server-side prepared statements are off, which costs some per-query parse
  time.
- A stuck transaction is killed by the server rather than holding a pooled
  connection forever.

## Rejected

- Session pooling everywhere. It caps concurrency at one server connection per
  client and doesn't scale to the web, worker and realtime process mix.

## Revisit trigger

A pgbouncer version in use that supports prepared statements in transaction
mode.

## Owning todos

8. Consumer: 9.
