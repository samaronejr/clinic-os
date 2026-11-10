# Interoperability

This app holds the partner integration surfaces and organization data
portability: the versioned external API under `/api/v1/`, signed webhooks
delivered through the shared action outbox, the private management API under
`/api/interop/v1/` and the organization export v1. Every external exchange is
synthetic: there is no real partner transport, and the webhook transport refuses
to run outside synthetic data mode.

`services.exchange_clinical_record` submits laboratory results to the RNDS
homologation environment behind EG-7 (see [RNDS](#rnds-laboratory-results-behind-eg-7)).
It sits behind `adapters.InteropAdapter` and reuses none of the partner
credentials, scopes or webhook machinery below.

## Records

Ten tenant tables, all `FORCE ROW LEVEL SECURITY`. `interop_guard` (and
`interop_rnds_guard` for RNDS submissions) refuses every `DELETE` and `TRUNCATE`
and allows only the transitions listed here, with a `revision` increment on each.

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
| `interop_rndssubmission` | one sealed RNDS result bundle, its owner approval's capability version and the RNDS answer: `pending`, then `accepted` or `rejected` |

Client secrets (`cs_` + 64 hex) and access tokens (`at_` + 64 hex) carry 256
bits of fresh entropy and are shown once; only their SHA-256 digests are
stored. Webhook signing secrets are derived from the platform secret
`interop-webhook-signing` and the subscription id and version, and are never
stored. External ids are derived with the platform secret
`interop-external-ids`. No table holds clinical text. Webhook subscriptions
(`interop_webhooksubscription`) store their endpoint URL by design; no delivery
or attempt row, outbox row, `last_error`, audit payload or log line holds a
patient name, an endpoint URL or a request body.

## Authority

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

## Management API

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

## External API v1

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

## Webhooks

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

## Organization export v1

The archive format is `docs/api/export-v1.md`. `request_export` records the
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

`export_registry.EXPORT_V1` and `EXCLUDED_V1` partition the recovery manifest's
`DOMAIN_RELATIONS` exactly (`tests/interop/test_org_export.py`). A new relation
that adds an exported dataset needs a new export version migration; the v1
datasets never change. `interop_rndssubmission` (todo 68) joined `EXCLUDED_V1`
as `machinery`: it is the delivery receipt of a document RNDS itself holds, and
the v1 archive gained no dataset. Whether later relations join v1's exclusions
or wait for a v2 is the open integration decision recorded for todo 66.

## RNDS laboratory results (behind EG-7)

The RNDS guide (rnds-guia.saude.gov.br, read 2026-11-10) supports one use case,
the laboratory-result document (Resultado de Exame Laboratorial, REL: COVID-19
and Monkeypox). `rnds_bundle.py` builds it with the profiles
`BRResultadoExameLaboratorial-1.1` (Composition), `BRDiagnosticoLaboratorioClinico-1.0`
(Observation) and `BRAmostraBiologica-1.0` (Specimen). The definitions package
linked from the guide returned HTTP 404, so the validator is pinned to the
guide's documentation example (dated 2020-10-02), recorded with its source and
fetch date in `rnds_fixtures/rel-example.json` and `rnds_bundle.DEFINITIONS`.
Only the codes that example documents are accepted (exam LOINC `94507-1`, SUS
subgroup `0214`, qualitative results `1` and `2`, specimen `SGHEM`); any other
code is `code_not_documented` until a downloaded package extends the sets.
FHIR-shaped data alone is never an RNDS integration.

`exchange_clinical_record(*, clinic_id, result_version_id, exam, command_id)`
takes a typed exam document entered for the result (LOINC code, SUS subgroup,
qualitative result, method, reference range, specimen type, issue time). It
never reads the encrypted free text of an orders result; without a valid
document nothing is built. In order, it refuses with:

| Refusal | When |
|---------|------|
| `RndsUnavailableError` | before any read: data mode is not synthetic, `INTEROP_RNDS_HOMOLOGATION` is off (the default) or the clinic's current `rnds` version is not an approved `sandbox` version of provider `rnds-fixture`, environment `homologation`, account `fixture` |
| `RndsValidationError` | the exam document or the built bundle breaks a recorded rule; it carries every failing element path and a closed issue code, never a value |
| `InteropAccessDeniedError` | unknown, other-clinic or unauthorized result: the actor needs `result.release` for the result's patient, and every miss is the same denial |
| `RndsIneligibleError` | `source_superseded`, `not_acknowledged`, `consent_required` (no unrevoked `rnds_exchange` acceptance), `identifier_required` (no valid CNS or CPF) or `already_submitted` (a pending or accepted submission exists) |
| `InteropConflictError` | the command id was used for other terms; the same terms return the stored receipt |

`interop_rnds_request` re-checks authority, the current and acknowledged
version, consent and the capability version in the database, then stores the
RFC 8785 bundle bytes as a tenant envelope with their SHA-256. One `action`
outbox operation (`interop-rnds-v1`) carries only that digest. At send time the
outbox rechecks the actor and `submission_eligible` rechecks consent, the result
and the capability version, so a revocation before the send cancels it. This
recheck is not a lock: a revocation committed while the request is on the wire
does not recall it.

The only reachable transport is `FixtureRndsTransport`, which answers from the
guide's recorded examples (`rnds_fixtures/responses.json`): an accepted bundle
gets a `location` identifier, a non-final document `EHR-ERR924`, a foreign
requester system `EHR-ERR881`, an unknown requester `EHR-ERR906` and a repeated
bundle identifier `EHR-ERR866`. The answer settles the submission through
`interop_rnds_settle` while its operation is in flight:

- `201` with an identifier: `accepted`, identifier stored.
- `EHR-ERR866`: `accepted` without an identifier. Bundle identifiers are
  submission ids under the establishment's own requester system, so RNDS
  already holds this document; a lost answer never becomes a second document.
- other `4xx`: `rejected` with the `EHR-ERRnnn` code (or `http_<status>`).
- `401 EHR-ERR882`: the token is refreshed once; then the send retries.
- `5xx`, an answer without a readable identifier, network failures and
  timeouts: the same bundle is retried; a copy that already landed answers
  `EHR-ERR866`.
- an operation that fails or is cancelled without an answer: `rejected` with
  `undelivered` or `withdrawn` (`interop_rnds_undelivered` trigger).

A rejected submission frees the result for a new command. Replacing an accepted
result (RNDS "substituir") is not implemented.

`MtlsRndsTransport` implements the guide's flow with the standard library
(`ssl`, `http.client`): `GET /api/token` on the Auth host over mutual TLS with
the establishment's ICP-Brasil certificate (30-minute token), then
`POST /api/fhir/r4/Bundle` on the EHR host with `X-Authorization-Server: Bearer
<token>` and `Authorization: <requester CNS>`. `cryptography`, already locked
and used by `external_ids.py`, only opens the PKCS#12 bundle, which `ssl`
cannot; the key reaches disk only re-encrypted under an ephemeral passphrase in
a private temporary directory, and an expired certificate fails before any
connection. The bundle and passphrase are `rnds-<clinic hex>-pkcs12` and
`rnds-<clinic hex>-passphrase` credential files in the secret store
(`apps.core.secrets.credential_store`), never in the database or evidence.
The transport is tested against a local CA and servers only. Nothing selects
it: `transport_for` refuses every provider but the fixture, and no establishment
(requester id, CNES, requester CNS) exists outside the synthetic fixture one.

BLOCKED-ON-EG-7 (and EG-1, EG-2): DATASUS credentialing, the homologation
evidence PDF, production access per state and the LGPD art. 11 legal basis
(open for the owner's compliance reviewer) all precede a real leg, which also
needs the provider lifecycle record and `ops.release.activation`; its live
preflight refuses an environment that sets `INTEROP_RNDS_HOMOLOGATION`. Submissions
are audited as `interop.rnds_submission.requested`, `.accepted` and `.rejected`;
an answer recorded after the requester lost authority is audited by the outbox
completion event instead. No log, outbox row, `last_error` or audit payload
holds the bundle, the patient identifier or a credential.
