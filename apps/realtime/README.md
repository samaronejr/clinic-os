# Realtime invalidation (todo 8, ADR-002/018)

This optional, synthetic-verified transport carries refetch hints, never records.
`REALTIME_ENABLED` defaults off. The agenda remains usable without JavaScript and
falls back to 30-second polling when `REALTIME_POLLING_FALLBACK` is true.

## Process and authentication boundary

Ordinary HTTP stays on Gunicorn/WSGI. POST `/rt/stream` mints a 60-second random
single-use ticket after CSRF, a fresh server-side Django session, password hash,
existing privileged-role OTP policy, membership and permission checks. Topics
are in the JSON body, never the URL. GET `/rt/stream?t=<opaque ticket>` is routed
only to `config.asgi_realtime:application`. The ASGI URLconf serves no product
routes. The ticket is bound to the minting session (RT-S1 proposal accepted),
and Redis GETDEL makes consumption atomic, including concurrent replay.

`authorize_topics` uses one `sync_to_async` call. All session/auth/ORM work stays
inside its synchronous worker; staff auth is loaded inside a short
`tenant_context`, never before its GUCs exist. `connection.close()` in `finally`
closes that worker's backend on success and denial. `CONN_MAX_AGE=0` alone would
not close a connection during a long response. Stream idle time uses only Redis.
Subscription ACKs precede a second authority check, closing the revoke/subscribe
race. Reauthorization is every 60 seconds even on busy topics; the absolute
connection cap is 600 seconds. Lost revocation messages are caught by the next
DB check. A revoked role, logout or live-halt closes the stream immediately when
its commit-bound control message arrives. Disconnect releases pub/sub resources.

Unknown, malformed and forbidden selectors have the same 403 JSON body.
Successful authorization appends `realtime.subscription.authorized` in the short
tenant transaction, using only the existing audit metadata vocabulary. Denials
without a trusted tenant are logged with a fixed, selector-free reason, not by
inventing an audit tenant or copying a topic into telemetry.
Redis failure is an observable transport outage, not a failed committed booking.
Tickets/events are not durable workflow state or proof of delivery. No live gate
is satisfied by this implementation.

## Topic authority

- `clinic:<uuid>:agenda` and `clinic:<uuid>:queue` require the exact clinic's
  `appointment.read` in todo 6's authoritative permission resolver, including
  remove-only RoleGrants. `appointment.read_own` is not clinic-wide authority;
  physicians with only that permission retain their normal filtered HTTP agenda
  and polling. A future per-practitioner channel must not widen this scope.
- `clinic:<uuid>:inbox|messages` and `ai_job:<opaque>` are reserved grammar.
  They fail closed until their owning domains add the actual permission and
  durable ownership binding. No v1 permission implies clinical-inbox/message/job
  ownership. Knowledge of an opaque id never grants access.
- A patient session goes through `patient_session_context`, never staff tenant
  GUCs. No patient-wide or clinic-wide stream is currently granted to a patient;
  a future domain must bind its event to the patient's exact authorized subject.
- `authz:user:<uuid>` is an internal control channel, not client-selectable.
  `authz:halt` is the process-wide internal live-halt channel.

Each broker channel is `rt:<sha256(topic + deployment secret)>`. Events are
exactly `{"topic_hash": str, "kind": str, "version": int}`; kinds are a closed
allowlist and versions are non-negative JS-safe integers. Broker input is
validated again before streaming. There are no names, ids, text or URLs in
payloads. A hint's version is an invalidation version, not a global sequence;
clients always refetch and never infer a write result or skip a lower revision.
`publish_on_commit` is the domain entry point; it registers `publish` only with
`transaction.on_commit`. Appointment save and role-delete/logout signals cover
the current ORM paths. Raw owner SQL is recovered by periodic reauthorization or
polling, not falsely claimed as a signal-delivered event.

## Pooling and local deployment

Runtime psycopg connections disable prepared statements and server-side cursors.
The role-setting migration sets `search_path=clinic_app,public` and a 15-second
idle-in-transaction timeout; reversing it RESETs both. Direct connections retain
the explicit startup `options`. PgBouncer ignores those startup options in
transaction mode and relies on the pinned role defaults.

`DATABASES['locks']` must point to direct PostgreSQL or session-mode PgBouncer,
never the transaction pool. Only session advisory locks use it; domain queries
and transaction-local mutation locks stay on `default`. Production enables
`DATABASE_TRANSACTION_POOLING=true` and supplies `LOCKS_DATABASE_URL`; omitting
that URL fails startup. Current direct deployments can use the direct default
for both aliases. Test `locks` is an explicit mirror of the test database.

Docker's default/final `web` target remains the original Gunicorn command;
`--target realtime` uses uvicorn, no proxy-header trust and no access logging.
The optional Compose `realtime` profile supplies a transaction-mode PgBouncer,
private Redis, web, realtime and a same-origin edge. Bootstrap and migrate as
owner before starting it; provision a synthetic secret directory through the
existing secret-store recipe. Never use real patient data with the local stack.
The edge sends only GET `/rt/stream` to ASGI; POST and all product traffic stay
WSGI. Its access log is off so tickets cannot be retained. Hosted deployment
must make the same method/path split, disable proxy buffering, keep a >600-second
read timeout and avoid logging stream query strings.

## Verification

`P tests/realtime/` uses real PostgreSQL roles and real, owned Redis on a random
loopback port. It checks ticket races/expiry/binding, permissions, rollback,
revocation, injected-clock deadlines, three-key payloads and `pg_stat_activity`
showing no idle stream database backend. The `CLINIC_BROKER_GATE=required` variant
runs those same broker-backed proofs. `B realtime` uses two real browser contexts,
a separate uvicorn process and a test-only same-origin reverse proxy; it tests
booking/refetch latency, process death/polling, replay and responsive accessibility.
No patient data or browser storage is introduced; CSP remains `connect-src 'self'`.
