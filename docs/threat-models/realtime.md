# Threat model: realtime event channel

- Status: design baseline, 2026-09-24 (todo 2). Mitigations are planned work
  owned by the listed todos unless a current source path is named. The Tests
  column maps each mitigation id to the tests that prove it (todo 73,
  checked by `tests/infra/test_threat_model_coverage.py`). A mitigation that
  starts with **Proposal:** goes beyond the owning todo's plan text; that
  todo accepts or rejects it and isn't bound by it until then.
- Decisions: [ADR-002](../adr/ADR-002-realtime-sse.md),
  [ADR-018](../adr/ADR-018-connection-pooling.md)

## Scope

Server-sent events that tell staff and patient browsers to refetch the agenda,
reception queue, inbox, messages and AI job status. Covers the ticket
endpoint, the separate ASGI process, Redis pub/sub and the browser client.
Refetching data goes through the normal tenant request path and is covered by
existing tenancy controls.

## Assets

- Topic membership: which clinic, user or job a stream follows.
- Event timing and volume, which can hint at clinical activity.
- Session cookies and single-use stream tickets.
- Database connections in the realtime process.

## Trust boundaries

1. Browser to the ticket endpoint (session, CSRF, OTP).
2. Browser to the stream endpoint (opaque ticket only).
3. Realtime process to PostgreSQL (short tenant transaction per check).
4. Web and worker processes to Redis pub/sub (publish on commit).

## Data flow

```mermaid
flowchart LR
  subgraph Browser["Browser (untrusted)"]
    Client["realtime.js EventSource"]
  end
  subgraph Web["Web process (WSGI, TenantMiddleware)"]
    Ticket["POST ticket endpoint"]
    Views["HTMX fragment views"]
    Commit["transaction.on_commit publish"]
  end
  subgraph RT["Realtime process (ASGI, no TenantMiddleware)"]
    Stream["GET stream with opaque ticket"]
    Authz["authorize_topics in short tenant txn"]
  end
  subgraph Data["Data stores"]
    PG[("PostgreSQL, FORCE RLS")]
    Redis[("Redis pub/sub, opaque channels")]
  end
  Client -- "topics in POST body" --> Ticket
  Ticket -- "single-use 60 s ticket" --> Client
  Client -- "ticket" --> Stream
  Stream --> Authz
  Authz --> PG
  Commit -- "topic hash, kind, version" --> Redis
  Redis --> Stream
  Stream -- "event without record data" --> Client
  Client -- "refetch" --> Views
  Views --> PG
```

## Threats and mitigations

| ID | STRIDE | Threat | Mitigation | Todos | Tests |
| --- | --- | --- | --- | --- | --- |
| RT-S1 | Spoofing | A stolen or replayed stream URL opens another user's stream. | Tickets are opaque, single-use and expire after 60 s. **Proposal:** bind the stream to the session that minted the ticket. | 8 | `tests/realtime/test_tickets.py::test_ticket_is_consumed_once_under_concurrent_redemption`<br>`tests/realtime/test_tickets.py::test_ticket_binds_session_and_expiry_without_a_sleep`<br>`tests/realtime/test_tickets.py::test_forged_ticket_is_refused` |
| RT-S2 | Spoofing | A cross-site page mints a ticket with the victim's cookie. | Ticket creation is a POST with CSRF and OTP checks, and the ticket is returned only in the response body. | 8, 10 | `tests/realtime/test_tickets.py::test_ticket_http_denials_csrf_and_private_headers`<br>`tests/realtime/test_authorization.py::test_privileged_subscription_keeps_exact_totp_policy` |
| RT-T1 | Tampering | A client forges events or injects messages into Redis. | The stream is one-way; channel names are `rt:<sha256(topic+secret)>`; Redis is private to the deployment. Events only trigger a refetch that goes through normal authorization. | 8 | `tests/realtime/test_transport.py::test_event_schema_is_closed_and_topic_is_opaque`<br>`tests/realtime/test_transport.py::test_broker_boundary_rejects_extra_fields_and_forged_metadata`<br>`tests/realtime/test_stream.py::test_bad_broker_messages_never_reach_the_browser` |
| RT-R1 | Repudiation | Nobody can tell who subscribed to what after an incident. | Ticket issuance and denials record reason codes and request ids through the audit and telemetry allowlists, with no topic contents. | 8, 11 | `tests/security/test_audit_attribution.py::test_subscription_authorization_audits_clinic_metadata_only`<br>`tests/realtime/test_contracts.py::test_realtime_log_templates_are_registered_telemetry_messages`<br>`tests/realtime/test_failures.py::test_failed_publication_is_observable_without_echoing_exception` |
| RT-I1 | Information disclosure | Events carry patient names, ids or text. | Event schema is exactly `{topic_hash, kind, version}`; a PHI-sentinel scan covers SSE output. | 8, 73 | `tests/realtime/test_transport.py::test_event_schema_is_closed_and_topic_is_opaque`<br>`tests/realtime/test_transport.py::test_unregistered_kind_or_invalid_version_cannot_carry_text`<br>`tests/security/test_sentinel_sinks.py::test_request_sentinels_never_reach_any_sink` |
| RT-I2 | Information disclosure | A user subscribes to another clinic's or patient's topic. | `authorize_topics` runs `require_permission` in a tenant transaction; unknown and forbidden topics return the same 403 shape. | 6, 8 | `tests/realtime/test_authorization.py::test_unknown_and_forbidden_topics_have_identical_denial`<br>`tests/realtime/test_authorization.py::test_topics_recheck_permission_and_close_database`<br>`tests/realtime/test_topics.py::test_patient_topics_bind_exact_enrollment_and_operation` |
| RT-I3 | Information disclosure | Record ids appear in the stream URL and leak through logs or referrers. | Topics travel in the POST body; the GET carries only the opaque ticket. | 8 | `tests/realtime/test_tickets.py::test_ticket_http_denials_csrf_and_private_headers`<br>`tests/realtime/test_failures.py::test_bad_stream_requests_have_one_denial` |
| RT-D1 | Denial of service | Many open streams hold database connections or transactions. | The realtime process uses `CONN_MAX_AGE=0`, checks run in short transactions, and connections are capped at 10 min. | 8 | `tests/realtime/test_disconnect.py::test_asgi_disconnect_leaves_no_database_or_pubsub_connection`<br>`tests/realtime/test_stream.py::test_idle_receive_cannot_outlive_the_absolute_connection_cap`<br>`tests/realtime/test_stream.py::test_stream_has_no_database_backend_while_idle_and_filters_payloads` |
| RT-D2 | Denial of service | A publish storm from one tenant delays others. | Publishing happens on commit only; fairness quotas throttle bursty queues; clients fall back to polling. | 8, 9, 71 | `tests/realtime/test_publication.py::test_publication_is_after_commit_and_rollback_discards_it`<br>`tests/core/test_fairness.py::test_token_bucket_enforces_per_org_quota`<br>`tests/realtime/test_failures.py::test_disabled_transport_reports_unavailability`<br>`tests/renewal/browser/test_realtime.py::test_two_sessions_refetch_and_fail_closed_without_realtime` |
| RT-E1 | Elevation of privilege | A revoked user keeps receiving events on an open stream. | Reauthorization every 60 s and an immediate close on the `authz:user:<id>` message published by revocation, logout and live-halt. | 8, 15 | `tests/realtime/test_stream.py::test_stream_closes_on_revocation_or_deadline`<br>`tests/realtime/test_publication.py::test_role_revocation_emits_only_after_commit`<br>`tests/realtime/test_publication.py::test_logout_emits_the_same_revocation_channel` |
| RT-E2 | Elevation of privilege | App HTTP traffic is routed through the ASGI process and skips `TenantMiddleware`. | The realtime settings module mounts only the stream routes; a test proves other routes aren't served there. | 8 | `tests/realtime/test_pooling.py::test_realtime_settings_mount_only_stream_without_tenant_middleware` |

## Residual risk and external gates

- Event timing still reveals that something changed in a clinic. The design
  accepts that because topics are coarse and hashed.
- Load behavior is L1 evidence from todo 71 until a hosted environment exists.
