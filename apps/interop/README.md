# Interoperability contract - governed import, partner API, webhooks and export

Interop owns the governed migration path from spreadsheets and exports into a
clinic: upload, profile, map, validate, dry run, sign-off, batched commit,
reconciliation and rollback. Its public entrypoints are in `services.py`. It
adds no browser surface yet; every command is a keyword-only, clinic-scoped
service checked again by PostgreSQL (FORCE RLS, one guard trigger, resolver
functions). `exchange_clinical_record` (RNDS, todo 68) still raises
`NotImplementedError("Phase >=1")`.

Competitor-specific layouts are out of scope: no mapping is invented for a
named product until owner-supplied EG-15 sample exports exist. EG-15 stays
open.

## Contract files

- `docs/interop/import-mapping-v1.json` - the JSON Schema (Draft 2020-12) for
  mapping documents (`$defs/mapping`) and profile reports (`$defs/profile`).
  `apps/interop/contract.py` holds the same schema; a test requires the two
  to be equal.
- Archive layout `clinic-import-archive-v1`: a ZIP holding exactly
  `manifest.json`, `patients.csv`, optional `history.csv` and flat
  `files/<name>` attachments. The manifest lists every other member with its
  `sha256` and `bytes`; any missing, extra or mismatched member fails the
  upload (`manifest_mismatch` / `checksum_mismatch`).

## Sources and limits

| Kind | Content |
| --- | --- |
| `csv` | one `patients` dataset; UTF-8 (BOM allowed) or Windows-1252; `,` `;` tab or `|` |
| `xlsx` | first worksheet's cached text; formula cells are row-level `formula_cell` rejects; DTDs are refused |
| `api` | `{"datasets": {"patients": [{column: text or null}], "history": [...]}}`, no duplicate keys |
| `zip` | the archive layout above; `history` is allowed only for `zip` and `api` |

Sources are at most 20 MiB, 50,000 rows and 64 columns per dataset; archive
members at most 10 MiB, 64 MiB in total, a 100:1 compression ratio above
64 KiB, no symlinks, no encryption and no paths outside the flat layout
(threat model IE-E2). Nothing is extracted to disk.

## State machine and authority

`ImportJob` is an immutable header; state lives in append-only
`ImportTransition` rows with a dense `sequence` (every command names the
`expected_sequence` it observed; a stale or replayed command is
`interop_stale`):

```
uploaded -> profiled -> mapped -> dry_run -> approved -> committed
                        mapped <- dry_run (re-map)     any non-terminal -> rolled_back
```

| Command | Authority | Notes |
| --- | --- | --- |
| upload, profile, suggest/save/apply mapping, approve, rollback, read job | import: `staff.clinic` (clinic manager, legacy clinic admin) or `staff.organization` (org admin, owner) at the exact clinic | approve signs the cutover/rollback plan |
| dry run, dry-run report and export, commit, batch run | import **and** `demographics.read` | dry runs compare with existing identities, commits write them |
| imported history and documents | patient-scoped `clinical.read` | never the import authority |

Organization admins hold no demographics access by default (RP matrix; the
intake duplicate guard refuses them too), so a clinic manager runs the dry run
and the commit; an org admin may prepare, sign off and roll back. Commit and
every post-approval rollback need this actor's fresh persisted step-up session
(`session_key`), verified in Python and again by the guard through
`clinic_app.identity_session_verified`. Unknown, foreign and unauthorized
selectors share `ImportAccessDeniedError`; refusals carry closed machine
codes (`access.Code`), never source values.

## Validation and dry run

Validators are deterministic: CPF check digits and CNS length (intake's
`normalize_identifier`), dates in the declared format (`DD/MM/YYYY`,
`YYYY-MM-DD` or `excel_serial`; no future birth dates), clinic-local
`DD/MM/YYYY HH:MM` or ISO minutes (never in the future), closed vocabularies
(sex at birth, history kinds), the local observation code set with its exact
UCUM-like unit (`ehr.longitudinal_values`), clinic-required demographics,
in-file duplicate source ids and identifiers, and patient names that would be
spreadsheet formulas (`formula_like`, legal and social names). The dry-run
report lists `[dataset, row, field, code, source_id]` rejections, where
`source_id` is the operator's own row label, and reconciles
`rows = accepted + rejected` per dataset. `export_import_report` returns the
same reasons as CSV in which every cell that a spreadsheet would evaluate
(leading `=`, `+`, `-`, `@`, tab, carriage return or their full-width forms,
also after leading spaces) is prefixed with an apostrophe. Existing-patient matches
(`exact_identifier`, `dob+name_prefix`, compared only through tenant HMAC
indexes of visible patients) are reported per row; a commit still creates the
patient and intake's own duplicate detection opens a `DuplicateCandidate` for
human adjudication (todo 18). Nothing is ever merged automatically.

The dry-run digest (RFC 8785 + SHA-256) binds the source checksum, the
mapping digest, every accepted normalized row and every rejection. Approval
binds that digest; `commit_import` recomputes it from the stored source and
refuses any drift (`dry_run_stale`).

Mapping suggestions (`suggest_mapping`) are advisory only: a fixed pt-BR and
English header vocabulary, never patient values, and nothing applies until an
operator saves a mapping version. No model is called; an AI-backed (A1)
suggestion would need its own AI purpose, prompt, evaluation and an
EG-1 `approved_to_test` record first.

## Commit, provenance and reconciliation

`commit_import` records immutable batches of at most 200 rows (patients
first, then history) and queues `apps.interop.tasks.commit_batch` on the
`bulk` queue after commit. The task carries only the batch id; it resolves the
stored scope (`clinic_app.interop_batch_scope`), spends one token of the
organization's `bulk` fairness bucket (deferring with a countdown otherwise)
and commits under the requester's re-checked current authority. Each batch
re-verifies the source checksum and its own row digest before writing.

Patients are registered through intake's own invariants (name index, first
demographics version with its receipt, identifier blind indexes, duplicate
hints) under an enrollment command key the guard recomputes from the job and
row. Every imported row gets an immutable `ImportedRecord` with encrypted
source id, original author label and original time, a content digest and the
label `imported`; history (notes, observations, documents) lives only there
and is never attributed to a current user: the importer is recorded as the
actor of the transition and of intake's registration receipt, never as the
author of imported clinical content. Attachments are stored encrypted with
their SHA-256 and the synthetic quarantine scan verdict (allowlisted type,
matching signature, no active content); only clean ones can be linked.

The last batch appends `committed` with the reconciliation report: per
dataset rows, accepted, rejected and committed counts; per batch digests;
source and attachment bytes; source and attachment checksums re-verified.
Linked attachments are re-verified inside the database
(`clinic_app.interop_attachment_checks`), which returns only counts, bytes and
mismatches, never the bytes.

## Rollback (no hard delete)

`rollback_import` appends the terminal `rolled_back` transition (within the
signed rollback window after commit, step-up after approval) and calls
`clinic_app.interop_hide_reversible`, which hides every imported enrollment
that nothing else references yet. Activity is any row in the relations
listed in `migrations/_import_sql.py` (`PATIENT_ACTIVITY`,
`ENROLLMENT_ACTIVITY`) or anything beyond what the import wrote (another
enrollment, a demographics version above 1, more identifiers); in-use
enrollments are retained and returned for review. A catalog test requires
every foreign key to a patient or enrollment identity to be classified, so a
new relation cannot silently escape the check. Hidden enrollments (and their
duplicate hints) disappear from every `clinic_app`/`clinic_owner` read through
the restrictive `interop_import_visibility` policies; every row stays
physically retained. Imported history of a rolled-back job is invisible.
The clinic gate orders rollback against bookings and batches, so a rollback
may land while a commit is still in flight: batches still pending stay
`pending`, every later delivery of them returns `skipped`, and the rows
earlier batches wrote are hidden like any other unused import.

## Database and recovery obligations

All seven tables are classified in `ops/testing/restore_contract.py`. The
runtime role holds SELECT/INSERT on mappings, jobs, transitions and batches,
UPDATE only on batch `state`, `committed_count` and `committed_at`, and SELECT
on hide markers, which only the rollback resolver inserts. On stored files and
imported records it holds INSERT and SELECT only on the metadata and checksum
columns (`METADATA_COLUMNS` in `migrations/_import_sql.py`): imported content,
its source id, author and time labels, and stored source and attachment bytes
are never readable through a table, whatever the session's actor. Clinical
readers reach imported history and documents only through
`clinic_app.interop_history` and `clinic_app.interop_attachment`, which apply
`interop_record_visible` (patient-scoped `clinical.read`, live job). The
pipeline reaches the stored source only through `clinic_app.interop_source`:
while the job is in flight (`uploaded` to `approved`), for the current import
authority, with `demographics.read` once approved; it refuses everyone once the
job is committed or rolled back. The importer's own pipeline therefore still
parses the source it uploaded while the job is in flight; the database cannot
tell that step apart from other code in the same session, so it confines the
read to that resolver and those states. No machine grants. The
guard refuses DELETE, TRUNCATE and every UPDATE except the batch step.
Organization export v2 exports these relations except the commit batches
(machinery) and never reads a stored source. Migration
`interop.0001_import_framework` is protected and irreversible;
rollback = restore. It depends only on what it touches when it runs (identity
user, clinic and `has_permission`, intake's latest node, comms participants):
the rollback activity check is plpgsql and resolves the other relations at run
time, and a dependency on a later node of another app would make that app's
reversible migrations irreversible.

Load-bearing private imports: `intake.demographics._apply_demographics`,
`_DemographicsMutation`, `_append_identifier`, `_required_fields`,
`intake.duplicate_detection._current_identifiers`, `_current_indexes` and
`tenancy.envelope._kek`.

## Not yet delivered

- A staff UI and a migration-rehearsal onboarding step (todo 15's registry);
  both are follow-ups, so no browser suite changes here.
- Real attachment scanning, real AI suggestions and competitor layouts stay
  behind EG-1/EG-5 and EG-15.

## Partner integration, webhooks and organization export (todo 67)

Interop also holds the partner integration surfaces and organization data
portability: the versioned external API under `/api/v1/`, signed webhooks
delivered through the shared action outbox, the private management API under
`/api/interop/v1/` and the organization export v1. Every external exchange is
synthetic: there is no real partner transport, and the webhook transport refuses
to run outside synthetic data mode.

`services.exchange_clinical_record` (RNDS clinical-record exchange) stays
deferred and raises exactly `NotImplementedError("Phase >=1")`. The RNDS work
(plan todo 68) lands behind `adapters.InteropAdapter` and must not reuse the
partner credentials, scopes or webhook machinery below.

### Records

Nine more tenant tables, all `FORCE ROW LEVEL SECURITY`. `interop_partner_guard` refuses
every `DELETE` and `TRUNCATE` and allows only the transitions listed here, with
a `revision` increment on each.

| Table | Holds |
|-------|-------|
| `interop_apiclient` | one partner client: clinic, name, sorted scopes, per-minute rate, approving administrator, `active` or `revoked` |
| `interop_apiclientsecret` | SHA-256 digests of client secrets; rotation gives the previous secret a 24-hour expiry |
| `interop_apiaccesstoken` | SHA-256 digests of 15-minute bearer tokens and their granted scopes |
| `interop_apiwritereceipt` | `Idempotency-Key` receipts of external writes, with the request fingerprint and stored result |
| `interop_webhooksubscription` | endpoint URL, events, secret version and the rotation overlap end |
| `interop_webhookdelivery` | one immutable event per subscription: type, subject, subject state, times, payload digest |
| `interop_webhookattempt` | the delivery log: attempt number, outcome, HTTP status, time |
| `interop_organizationexport` | export receipts and their state machine |
| `interop_organizationexportpart` | encrypted archive byte ranges, kept until the archive expires or fails |

Client secrets (`cs_` + 64 hex) and access tokens (`at_` + 64 hex) carry 256
bits of fresh entropy and are shown once; only their SHA-256 digests are
stored. Webhook signing secrets are derived from the platform secret
`interop-webhook-signing` and the subscription id and version, and are never
stored. External ids are derived with the platform secret
`interop-external-ids`. No table holds clinical text. Webhook subscriptions
(`interop_webhooksubscription`) store their endpoint URL by design; no delivery
or attempt row, outbox row, `last_error`, audit payload or log line holds a
patient name, an endpoint URL or a request body.

### Authority

Managing partners and exporting require organization authority:
`configuration.organization` on every clinic of the organization
(`interop_authority`). A clinic-scoped administrator gets the same denial as an
unknown record.

A client acts with the authority of the administrator who approved it.
Issuing a token, resolving a request, recording a write and sending a webhook
delivery each recheck that this administrator still holds organization
authority, so removing that authority stops the client at once.
Rotating a client secret makes the rotating administrator the approver.

Offboarding (the subscription state machine reaching `offboarded`) revokes every
client and webhook subscription of the organization in the same transaction
(`interop_offboarding` trigger, reason `offboarding`). Export stays available in
every subscription state, including `offboarded`.

### Management API

`POST /api/interop/v1/` takes a JSON body `{"clinic_id": ..., "action": ...}`
plus the action's fields; selectors travel only in the body. The view requires
privileged TOTP, sends `Cache-Control: no-store` on every response and caps the
body at 8 KiB.

| Action | Fields | Result |
|--------|--------|--------|
| `clients.list` | - | clients without any credential |
| `clients.create` | `name`, `scopes`, `rate_limit_per_minute`, `command_id` | client id and secret, shown once |
| `clients.rotate` | `client_id`, `expected_revision` | new secret; the old one expires in 24 hours |
| `clients.revoke` | `client_id`, `expected_revision` | revoked client; its tokens stop at once |
| `webhooks.list` | - | subscriptions without secrets |
| `webhooks.revoke` | `subscription_id`, `expected_revision` | revoked subscription |
| `deliveries.list` | `subscription_id` | deliveries with their attempt log |
| `exports.request` | `command_id` | `202` with the export receipt |
| `exports.list` | - | export receipts, newest first |
| `exports.download` | `export_id`, `part` | one decrypted archive part |

Unknown, foreign and unauthorized selectors return `403 {"code":
"not_available"}`; a stale revision returns `409 {"code": "conflict"}`; bad
input returns `400 {"code": "invalid_input"}`. Client creation, rotation and
revocation, webhook revocation, export request, readiness and failure, and each
part download write one audit event from `apps/audit/events.py` with the fixed
payload keys only.

### External API v1

The contract is `docs/api/external-v1.yaml`, regenerated by the command in
`apps/interop/api/schema.py` and checked for drift by
`tests/infra/test_openapi_drift.py`. `/api/v1/` is in the tenant middleware's
bypass list: a request's only authority is what `interop_api_session` resolves
from the bearer token digest, and every data resolver re-validates that digest
and the client's clinic.

- `POST /api/v1/oauth/token` implements the OAuth 2.0 client credentials grant
  (form-encoded; `client_secret_basic` or `client_secret_post`, never both). It
  issues a 15-minute bearer token whose scopes are the requested subset of the
  client's scopes. Every refusal uses RFC 6749 error bodies, and token requests
  are metered at 30 per minute per presented client id.
- Read resources: `appointments` (window list and detail),
  `patients/{id}` (display name and birth date only), `availability` (working
  windows, busy intervals and closures) and `invoices` (issued invoices' status,
  amount in centavos and issue time). Lists are keyset pages over windows of at
  most 31 days.
- `webhook-subscriptions`: list, create, revoke and rotate-secret.

Patient-linked records are addressed only by opaque external ids
(`apt_`, `pat_`, `inv_`, `prc_`, `svc_` plus 52 base32 characters): AES-SIV of
the internal UUID under a per-organization key, with the record kind as
associated data. An internal UUID, a forged id, another organization's id and
another kind's id all fail before any query, so every miss is the same `404`.
No resource exposes clinical narrative.

Every request spends one token of the client's Redis bucket (the Lua script in
`apps/interop/ratelimit.py`; capacity and refill are the client's per-minute
rate). An empty bucket is `429` with `Retry-After`; an unreachable broker is
`503` with `Retry-After` and nothing runs. Each write requires an
`Idempotency-Key` header of 1-64 characters from `[A-Za-z0-9._:-]`; a replay
returns the stored result, and the same key with a different request is `409`.
Refusals are `{code, message_key}`, and an unexpected exception is a JSON
`internal_error` 500 logged as route, exception type and frame locations only.

### Webhooks

Events: `appointment.created`, `appointment.status_changed`,
`appointment.rescheduled` (scope `appointments:read`) and
`invoice.status_changed` (scope `invoices.status:read`, issued invoices only).
Endpoints must be `https` URLs on a DNS host name; IP literals, credentials,
query strings and fragments are refused.

The source transaction captures each event (`interop_capture_event` on
appointments, `interop_capture_invoice` on invoices) as one immutable
`interop_webhookdelivery` plus one `action` outbox operation per matching
subscription, attributed to the client's approver. At send time the
outbox rechecks the approver, and `WebhookActionAdapter` rechecks the
subscription, the client and the event's scope, reloads the delivery, verifies
its payload digest and signs the exact body:

```
Clinic-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>." + body)>
Clinic-Webhook-Id: <delivery id>
Clinic-Webhook-Event: <event type>
```

During the 24 hours after a secret rotation the header carries one `v1=` value
per valid secret. Receivers verify with `apps.interop.webhook_signature.verify`
(the reference verifier): a timestamp more than 300 seconds from the receiver's
clock is refused, so a replayed signature outside the tolerance fails. The body
is RFC 8785 JSON holding the delivery id, type, creation time and opaque ids
with the public status only.

A non-2xx answer, a timeout or a network error returns the operation to the
outbox, and `interop_webhook_backoff` sets `not_before` to 1, 2, 4, 8, 16, 32
and then 60 minutes after attempts 1 to 7; attempt 8 is the last. Each attempt
appends one `interop_webhookattempt` row. Revoking the subscription or client,
losing the approver's authority or offboarding cancels queued deliveries at
send time without a request.

### Organization export

The archive format is `docs/api/export-v2.md`: export v1 (`docs/api/export-v1.md`,
frozen) plus the governed-import relations. `request_export` records the
receipt and, after commit, hands only the export, organization and requester
ids to the fair `bulk` queue. `build_organization_export` claims the receipt,
reads every dataset page in its own `tenant_context` of the requester through
the static reader `interop_export_rows` and decrypts there, so losing authority
stops the build at the next page. The ready archive is stored as encrypted
parts (4 MiB by default, `INTEROP_EXPORT_PART_BYTES`) and expires 7 days after
it is ready. The beat sweeps (`interop-export-dispatch` every 5 minutes,
`interop-export-expiry` every 15 minutes) re-dispatch lost hand-offs, expire
archives, fail builds older than 6 hours or whose requester lost authority, and
null the bytes of expired and failed parts while keeping every receipt.

`EXPORT_V2` and `EXCLUDED_V2` (`migrations/_export_v2_sql.py`) partition the
recovery manifest's `DOMAIN_RELATIONS` exactly, and `EXPORT_V1`/`EXCLUDED_V1`
stay as v1 partitioned it before the governed import existed
(`tests/interop/test_org_export.py`). Version 2 exports the import mappings,
jobs, transitions, files, imported records and hide markers and excludes the
commit batches as machinery; an import file of role `source` is exported without
its stored bytes, which the export never reads. A new domain relation must be
classified in a new export version migration; no published version changes.
