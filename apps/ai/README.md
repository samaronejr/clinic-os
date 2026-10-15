# Governed synthetic AI gateway

This is the binding contract for todo 38. All inference is offline simulation.
No model configuration, organization opt-in, command, or test satisfies an
external gate. Real adapters remain `real_enabled=False`. Live, sandbox, paid,
and real-provider legs are **BLOCKED-ON-EG**.

## Admission and authority

`invoke(*, clinic_id, purpose, principal, inputs_ref, budget)` accepts an
encrypted, immutable input artifact reference, never a prompt body. `principal`
must equal the current human user GUC. It is an assertion, not authority.
Clinical purposes require current patient-scoped `clinical.read`; front desk
requires `appointment.read`; analysis requires `charge.read`. Unknown, foreign,
expired, and another user's artifact all return the same access denial.

This gateway uses todo 7's human-attributed `ActionOperationRequest` transport.
The existing machine principal grant set permits only `appointment.read` and
cannot impersonate a human, read clinical artifacts, or enqueue this action.
The agent execution platform owns future machine action authority; this app
does not widen those grants.

Inputs and outputs are purpose-bound `EncryptedTextField` envelopes with explicit
retention classes and expiries: one day for synthetic transient input, seven
days for synthetic review drafts. These are synthetic rehearsal defaults, not
an approved live retention policy. Artifacts are immutable and are never
included in logs, task arguments, metering, audit payloads, or error messages.
The retention workflow owns eventual purge and legal-hold decisions.

## Approved configurations

`ModelConfiguration` is platform data: owner-written, runtime SELECT-only.
It stores purpose, capability key, provider/model, region and jurisdiction,
retention/training terms, integer micro-BRL prices, version, review reference,
primary/fallback route, timeout, and state. The DB permits only
`draft -> approved -> active -> retired`, with retirement also possible before
activation. Terms never mutate; changes require a new version. Model review
references are synthetic configuration approvals, not provider/EG approvals.

Every load reads the provider lifecycle's current version and refuses missing,
revoked, degraded, or region-incompatible capabilities. The owner-approved
global region list and the fixed region-to-jurisdiction map must agree.
The only registrations are `synthetic` and `recorded_fixture`. Neither has a
network client. Both enforce the transaction/egress boundary in their base.
The recorded provider has one code-versioned recording, `fixture-v1`, not a
caller-selected filesystem path.

An open primary circuit permits selection of an active, approved fallback only
in the same jurisdiction class. It does not silently cross a region policy,
change a queued invocation's configuration, or retry an unknown outcome.
Timeout opens an organization/configuration circuit. Other organizations'
circuits are independent. A new reviewed model version starts closed; retained
circuit rows also support owner-only closure without deleting their history.
Only one circuit may be open per organization/configuration; another timeout
after closure creates a new retained circuit rather than overwriting history.
The reset uses the same organization send lock and records a system audit event.

Prompts live in `prompts/<purpose>/v<N>.md`. Admission records SHA-256 of the exact
bytes; preparation checks it against the outbox digest before dispatch.
Extraction accepts exactly `{"text": <string>}`, without duplicate keys.
All output remains untrusted draft data; the gateway executes no tools and
never changes a clinical or financial record.

## Budgets and metering

Organization-wide opt-in, capability enablement, and organization/purpose monthly
caps are stored in append-only `ClinicConfiguration.ai_policy`. The latest
organization snapshot governs every clinic. Ordinary clinic settings saves
carry the current organization policy forward under the same organization lock.
Changing it requires `configuration.organization` in every clinic.

Admission takes a per-organization advisory transaction lock and reserves the
worst input/cache mix plus bounded output and whole audio seconds. A DB trigger
repeats the cap arithmetic, so raw runtime inserts cannot bypass reservation.
All prices and aggregation use integer micro-BRL. Cached tokens are a subset of
input tokens and are charged once. `cost_minor` is the conservative ceiling to
whole BRL centavos; budgets aggregate unrounded `cost_micros`, never rounded rows.
Outstanding reservations count even across a month boundary. Completed usage
belongs to its settlement month in UTC.

Known pre-send refusals release the reservation. An unknown provider charge
(timeout, unavailable provider, invalid response) conservatively consumes the
reserved maximum; the reason code identifies that estimate rather than claiming
a verified charge. Synthetic costs are simulated, not money spent.

`AIInvocation` contains organization/clinic/user references, purpose, configuration
version, prompt hash, opaque references, ceilings, units, cost, latency, status,
reason, and timestamps. It contains no input/output text. Aggregate telemetry
uses the existing closed purpose/outcome labels and no tenant label.

## Dispatch and stops

The shared outbox owns after-commit publication, immutable replay identity,
operation/subject session-lock fencing, actor rechecks, and abandoned-send
reconciliation. Its registered action dispatcher routes AI to `ai-interactive`
or `ai-batch` (analysis). Recovery uses the same dispatcher. The AI task applies
organization fairness before claiming an operation. Only opaque operation ids
enter Celery. If core cancels before an adapter recheck, the AI task settles the
unspent reservation as metadata bookkeeping, including after role revocation.

Preparation decrypts within the existing tenant transaction. The inference call
runs with no open transaction on any initialized Django connection. Settlement
opens a fresh tenant context. Global and organization switches are rechecked
at admission and at the final send boundary. Organization policy mutations use
the outbox's stable organization mutation lock.

Every default is disabled: missing global control row and missing organization
or capability opt-in. Non-synthetic data mode refuses inference. The UI is a TOTP/CSRF
protected native form at `/clinics/<clinic_id>/settings/ai/`; it shows disabled,
degraded, validation, conflict, and unconfigured states without displaying
content or provider credentials. Clinical work remains available manually.

## Owner CLI

Run as `clinic_owner`, following the existing owner command environment:

```sh
uv run python manage.py ai_gateway global-switch --disable
uv run python manage.py ai_gateway global-switch --enable --approved-region br-synthetic
uv run python manage.py ai_gateway model-create --purpose front_desk \
  --provider synthetic --model-id synthetic-v1 --region br-synthetic \
  --retention-terms synthetic-no-retention --training-terms synthetic-no-training \
  --input-micros 3 --output-micros 5 --cached-micros 1 --audio-micros 7
uv run python manage.py ai_gateway model-state --configuration-id <uuid> \
  --state approved --approval-ref synthetic-review-v1
uv run python manage.py ai_gateway model-state --configuration-id <uuid> --state active
uv run python manage.py ai_gateway circuit-reset --organization-id <uuid> \
  --clinic-id <uuid> --configuration-id <uuid>
```

## Egress proxy contract

The current adapters make zero network calls. Deployment egress remains deny-all
for these workers. A future real adapter requires its own approved lifecycle and
live-runtime gates, an exact HTTPS hostname/port/path allowlist in its base, and
an outbound proxy which denies every destination by default, permits only the
approved endpoint, disables redirects and cross-region inference profiles,
verifies TLS, and logs metadata only. No wildcard, private-network destination,
caller URL, fallback hostname, or request/response body is permitted. That proxy
configuration is a future EG-bound deployment, not a claim made by this source.

## Schema and verification

`0001_initial` creates the app with exact ACLs, FORCE RLS, composite scope FKs,
reservation/transition guards, no-delete triggers, and no runtime truncate grant. Encrypted
storage makes the migration non-atomic and irreversible; rollback = restore.
The SQL policy installation itself is one explicit transaction. Recovery
includes all five AI relations and the tenant keys. Counts and exact membership
are derived from the live catalog by `tests/infra/test_recovery_manifest.py`.

`tests/ai/test_gateway.py`, `test_controls.py`, and `test_posture.py` are the
real-role runtime gates. `test_units.py` and `test_migration_state.py` need no
database. UI browser coverage extends the existing `clinic-settings` suite.
Root owns the serialized runtime slot, canonical gate, and independent review.
