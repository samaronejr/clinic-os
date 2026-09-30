# ADR-014: Allowlist-redacted observability

- Status: accepted 2026-09-24
- Recorded by: todo 2

## Context

Operators need latency, error, queue and provider SLIs. Sentry is inert
without a DSN and is configured with PII and local-variable capture disabled.
PHI must never reach logs, traces, metric labels, Sentry or evidence (SC-7).
Vendor auto-instrumentation typically records URLs, headers, SQL text and
request bodies, any of which can carry clinical content.

## Decision

Logs, traces and metrics are structured and pass through an allowlist
redactor. Clinical content is never exported.

- Log records drop every key not on the allowlist.
- Span attributes use their own allowlist.
- Metrics label routes by name, not path, and never by patient or clinic
  name.
- The Sentry scrubber reuses the same allowlist.
- OpenTelemetry export is disabled by default.

## Consequences

- A new log field needs an allowlist change that review can see.
- Debugging uses request ids and reason codes rather than payloads.
- A redaction corpus with synthetic CPF, names and SOAP text guards the
  filter in CI.
- The internal metrics endpoint is token-protected and network-restricted.

## Rejected

- Vendor auto-instrumentation defaults. They capture URLs, SQL and bodies by
  default, which is exactly where PHI leaks.

## Revisit trigger

None recorded. The AD table sets no trigger. Changing this model needs a new
ADR that supersedes this one.

## Owning todos

11. Consumers: 71, 72, 73.
