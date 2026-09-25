# ADR-002: Realtime updates over SSE from a separate ASGI process

- Status: accepted 2026-09-24
- Recorded by: todo 2
- Threat model: [realtime](../threat-models/realtime.md)

## Context

Agenda, reception queue, inbox and AI job status need to update across staff
sessions within p95 <= 2 s. `TenantMiddleware` wraps each request in one
durable transaction and rejects streaming responses, so a long-lived stream
can't run inside it. The project already ships `config/asgi.py`, but all
tenant middleware is synchronous. Redis is already a dependency as the Celery
broker.

A stream that held a database transaction or connection per client would
exhaust the pool. Events that carried identifiers or names would leak data to
anything that can read the stream or the broker.

## Decision

Realtime uses server-sent events from a separate ASGI process
(`config/asgi_realtime.py`, `config/settings/realtime.py`, served by uvicorn)
without `TenantMiddleware`.

- Authorization runs in a short tenant transaction when the stream opens and
  again on reauthorization.
- Events have exactly `{topic, kind, version}`, with the topic sent as an
  opaque hash. Todo 8's wire contract names that field `topic_hash`, and the
  implementation follows todo 8. They're published from `transaction.on_commit` through Redis
  pub/sub.
- Clients refetch the affected fragment through the normal HTTP path. The
  event itself carries no record data.
- The stream reauthorizes every 60 s, closes after a 10 min connection cap,
  and closes at once on an `authz:user:<id>` revocation message.

## Consequences

- HTTP application traffic never goes through the ASGI process, so the
  request/transaction contract stays unchanged.
- The realtime process holds no database connection between checks
  (`CONN_MAX_AGE=0`), which ties it to the pooling rules in ADR-018.
- A lost event costs one stale view until the next event or poll, never a
  wrong write, because every write still goes through the tenant request path.
- Clients need a polling fallback flag for when the realtime process is down.
- The process is a new deploy unit with its own settings module listed in
  `config/AGENTS.md`.

## Rejected

- Django Channels. It adds a channel layer and ASGI routing for the whole app
  when the need is one-way notification.
- WebSocket everywhere. Nothing needs client-to-server messages on this
  channel, and WebSockets make session, CSRF and proxy handling harder.

## Revisit trigger

A product need for bidirectional low-latency messaging that HTTP requests plus
SSE can't meet.

## Owning todos

8
