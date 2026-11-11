# Organization export v1

Exports built after migration `interop.0003_export_v2` use
[export v2](export-v2.md), which adds the governed-import relations. This
version and its contract below are unchanged.

Format id: `clinic-organization-export-v1`. An organization administrator
(`configuration.organization` on every clinic) requests the export through
`POST /api/interop/v1/` (`exports.request`). It covers every domain record of
the organization in every subscription state, including `offboarded`. This
version is frozen: the relation set lives in
`apps/interop/migrations/_export_sql.py` and a new relation needs a new export
version.

## Download

A ready export is stored as encrypted parts of at most 4 MiB and kept for 7
days. `exports.download` with `{"export_id": ..., "part": n}` returns part `n`
decrypted, with these headers:

| Header | Value |
|--------|-------|
| `X-Export-Part` | `n/total` |
| `X-Export-Part-Sha256` | SHA-256 of this part |
| `X-Export-Archive-Sha256` | SHA-256 of the whole archive |
| `Content-Disposition` | `organizacao-<export id>.zip`, or `organizacao-<export id>-parte-<n>.bin` when there are several parts |
| `Cache-Control` | `no-store` |

Concatenate parts `1..total` in order. The result is one ZIP archive whose
SHA-256 equals `X-Export-Archive-Sha256` and the receipt's `archive_sha256`.
After expiry the receipt stays listed with its digests, and the parts no
longer hold bytes.

## Archive layout

```
manifest.json
data/<relation>.jsonl
data/<relation>.csv
attachments/<attachment id>.<pdf|png|jpg>
```

The archive is deterministic apart from `created_at`: deflated entries dated
1980-01-01 with mode 0600, ZIP64 enabled, datasets in alphabetical relation
order and rows ordered by their key column. Every relation of the version has
both files, even with zero rows.

## Manifest

`manifest.json` is RFC 8785 canonical JSON:

| Field | Meaning |
|-------|---------|
| `v` | `clinic-organization-export-v1` |
| `export_id`, `organization_id` | the receipt and the exporting organization |
| `created_at` | UTC build time, ISO 8601 with `Z` |
| `dataset_count`, `record_count`, `attachment_count` | totals over the archive |
| `datasets[]` | `relation`, `domain` (the app label), `records`, the `jsonl` and `csv` paths, and `columns[]` of `{name, type, encrypted_at_rest}` |
| `files[]` | `path`, `sha256`, `bytes` and `kind` (`jsonl`, `csv` or `attachment`) of every entry except the manifest |
| `excluded[]` | `relation`, `reason` and `why` for every domain relation left out |
| `manifest_sha256` | SHA-256 of the RFC 8785 encoding of the manifest without this field |

## Verification

1. Check each part against `X-Export-Part-Sha256` and the concatenation against
   `X-Export-Archive-Sha256`.
2. Open the ZIP. Every entry except `manifest.json` must appear exactly once in
   `files[]` with a matching SHA-256, and `files[]` must name no other entry.
3. Recompute `manifest_sha256` from the manifest without that field.

`apps.retention.services.verify_export_package(archive_bytes)` implements
steps 2 and 3 without any database; it is the verifier the retention export
already uses.

## Rows

JSON Lines files hold one UTF-8 object per row with sorted keys, separated by
`\n`. Values keep their database meaning:

| Column type | JSON value |
|-------------|------------|
| `uuid`, `text` | string |
| `timestamp` | ISO 8601 string with offset |
| `date`, `time`, `interval` | PostgreSQL text form |
| `integer`, `number`, `decimal` | number |
| `boolean` | `true` or `false` |
| `json`, `array` | nested JSON |
| `base64` | base64 of the stored bytes |

Columns encrypted at rest (`encrypted_at_rest: true`) are decrypted with their
field's envelope purpose and carry their plain type: `text`, `date`, `json`, or
`base64` for encrypted bytes. Money is integer centavos, as stored.

CSV files follow RFC 4180 with CRLF line ends, UTF-8 without BOM and a header
row of the sorted column names. NULL is an empty cell, booleans are `true` and
`false`, and JSON or array values are compact JSON. A `text` cell starting with
`=`, `+`, `-`, `@`, a tab or a carriage return is prefixed with `'` so a
spreadsheet never evaluates it; the JSON Lines file keeps the original value.

## Scope

A relation's rows are those whose `organization_id` is the exporting
organization. `identity_organization` contributes the organization's own row.
`identity_user` contributes the accounts holding any role in the organization,
limited to `id`, `username`, `first_name`, `last_name`, `email`, `is_active`,
`date_joined` and `last_login`; password hashes never leave.

These lookup and credential columns are left out of exported relations:

| Relation | Omitted columns | Why |
|----------|-----------------|-----|
| `identity_staffinvitation` | `token_hash`, `token_nonce`, `email_hash` | invitation credential material |
| `intake_patient` | `full_name_index` | blind search index |
| `intake_patientdemographics` | `registry_full_name`, `registry_full_name_index`, `registry_birth_date` | byte copies of the patient row's encrypted name and birth date, exported decrypted in `intake_patient` |
| `intake_patientidentifier` | `blind_index` | blind search index |
| `orders_labaccount` | `signing_key_digest` | partner credential digest |
| `scribe_recordingsession` | `token_digest` | session credential digest |

## Attachments

Each `ehr_clinicalattachment` row in the `available` state contributes its
decrypted bytes as `attachments/<id>.<extension>`, after the bytes match the
row's `sha256` and detected type. Quarantined and failed attachments appear
only as rows.

## Excluded relations

`excluded[]` lists every left-out relation with one of these reasons:

| Reason | Relations |
|--------|-----------|
| `credential` | authentication secrets, sessions and device material: `comms_provideraccount`, `identity_authenticatorreset`, `identity_authenticatorretirement`, `identity_passkey`, `identity_recoverybatch`, `identity_recoverycode`, `identity_sessionrecord`, `identity_signupregistration`, `identity_totpsecret`, `intake_patientaccessgrant`, `intake_patientsession`, `interop_apiaccesstoken`, `interop_apiclientsecret`, `otp_static_staticdevice`, `otp_totp_totpdevice`, `scheduling_displaydevice`, `teleconsult_teleconsultcredential`, `tenancy_tenantdatakey` |
| `platform` | platform-wide records without a tenant owner: `ai_gatewayswitch`, `ai_modelconfiguration`, `identity_physicianevidence`, `prescription_medicationcatalogentry`, `prescription_medicationcatalogimport`, `prescription_verificationprobe`, `subscriptions_entitlement`, `subscriptions_plan` |
| `integrity_ledger` | the hash-chained audit ledger, verified by its own procedure: `audit_event` |
| `machinery` | delivery, retry, projection and receipt machinery: `ai_aicircuit`, `analytics_readmodelrefresh`, `comms_inboundevent`, `comms_integrationoperation`, `comms_messagemeter`, `ehr_addendumsavereceipt`, `ehr_drafteditstate`, `ehr_draftsavereceipt`, `ehr_timelineprojection`, `ehr_timelinerefresh`, `insurance_commandreceipt`, `inventory_commandreceipt` |
| `presentation` | per-user interface preferences: `identity_savedview`, `identity_userpreference` |
| `discarded` | content a clinician explicitly discarded: `ehr_discarded_content` |
| `transient_media` | raw audio kept only until its purge policy: `scribe_audiochunk` |
| `derived_archive` | the encrypted bytes of export archives themselves: `interop_organizationexportpart` |

## Lifecycle

| State | Meaning |
|-------|---------|
| `requested` | recorded; the bulk worker has not claimed it |
| `building` | claimed; datasets are being read under the requester's authority |
| `ready` | parts stored; downloadable until `expires_at` (7 days) |
| `expired` | past `expires_at`; receipt and digests kept, bytes removed |
| `failed` | `failure_reason` is one of `authority_revoked`, `decrypt_failed`, `attachment_unavailable`, `attachment_mismatch`, `too_large` (above 2 GiB) or `internal_error` (a build older than 6 hours) |

The requester must keep organization authority for the whole build. Losing it
stops the build at the next dataset page, stores no parts, and the expiry
sweep records `authority_revoked`.
