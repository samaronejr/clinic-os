# Organization export v5

Format id: `clinic-organization-export-v5`. An organization administrator
(`configuration.organization` on every clinic) requests the export through
`POST /api/interop/v1/` (`exports.request`). It covers every domain record of
the organization in every subscription state, including `offboarded`. This
version is frozen: its relation set lives in
`apps/interop/migrations/_export_v5_sql.py` (the export v4 set plus todo 73's
delegate-access log) and a new relation needs a new export version. Exports
built after migration `identity.0052_delegate_access_log` are v5; export v4
(`export-v4.md`), v3 (`export-v3.md`), v2 (`export-v2.md`) and v1
(`export-v1.md`) are frozen and unchanged.

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
| `v` | `clinic-organization-export-v4` |
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
| `identity_scimresource` | `external_hash`, `search_hash` | a digest of the identity provider's external id and the directory search index |
| `interop_importfile` | `data` on rows of role `source` | a stored import source, which the governed import confines to the pipeline of an in-flight job |

## Attachments

Each `ehr_clinicalattachment` row in the `available` state contributes its
decrypted bytes as `attachments/<id>.<extension>`, after the bytes match the
row's `sha256` and detected type. Quarantined and failed attachments appear
only as rows.

## Import compatibility

The [v5 import coverage matrix](export-v5-import-coverage.md) classifies all
255 datasets against the todo 66 governed importer. **Eight are importable as
projections; 247 are export-only.** This is a synthetic, single-target-clinic
migration path, not a backup restore, a multi-clinic clone or a live import
authorization. The caller is responsible for bringing an authorized source
export and selecting the target clinic for these records.

`apps.interop.services.upload_v5_import` takes the complete v5 ZIP, the
archive SHA-256 from the source receipt, the explicit list of relations to
import (including `intake_patient`), and the target clinic and idempotency
key. It verifies the version, manifest digest, exact member set, byte
lengths and every member digest before staging a `clinic-import-archive-v1`
ZIP. An unsupported relation in the selection is refused; the mere presence
of export-only datasets in the full ZIP is normal. The upload and projection
are subject to the governed import's existing size, archive and row limits,
not the larger download limit. Each serialized SOAP note must fit in one
20,000-character import cell, even if its individual EHR sections fit their
separate limits. Checksum verification checks integrity, not
the identity of whoever supplied the export.

The operator profiles the upload, saves and applies `v5_import_mapping()`,
then follows the normal dry-run, sign-off, step-up commit and reconciliation
flow. The sealed staging archive binds the selected source counts and
canonical values, including document title, type, size and byte digest.
Reconciliation is false if validation or mapping loses rows or values, or if
the committed counts, record hashes, linkages, or file checks differ. A
governed commit with acknowledged rejects is not a reconciled round trip.
The import job's source digest and idempotency key bind the staged
archive, not the exporting receipt; retain that receipt separately. The
projection uses JSONL, not the
spreadsheet-neutralized CSV. It keeps source timestamps with explicit offsets
and sub-minute precision.

| Target projection | Dataset | Not replayed |
|---|---|---|
| Registry name and birth date for patients with a non-hidden enrollment; source patient id, creation time and original registrar label when the first correction receipt exists | `intake_patient` | Source primary keys, enrollments and registration authority |
| Current legal/social names and sex at birth on the new patient registration | `intake_patientdemographics` | Other demographics fields, unknown-field reasons, old versions and correction receipts |
| Latest non-retired CPF/CNS values per patient and kind | `intake_patientidentifier` | Other identifier kinds, issuers, retired/older versions, indexes and source row ids |
| RFC 8785 SOAP content as note text, with source version id, original author id label and finalization time | `ehr_clinicaldocumentversion` | Draft/discarded versions, template, encounter, amendment links and live SOAP state |
| Code, value, unit and code-set version with source id, performer id label and effective time | `ehr_observation` | Reference ranges, derivation links, confirmation and live observation state |
| Available filename, type, size and SHA-256 plus exact rescanned bytes, with source id, uploader id label and creation time | `ehr_clinicalattachment` | Unavailable/rejected files, object keys and source scan state |
| Original patient source label, author and time; imported note, observation and document content and provenance | `interop_importedrecord` | Pending/rolled-back facts, hidden enrollments and the old job/approval |
| Bytes, media type, size and checksum of clean files referenced by eligible imported documents | `interop_importfile` | Sealed source uploads and unreferenced/rejected files |

Select both prior-import datasets together; an archive with eligible prior
records refuses their omission. Source identity labels are kept separately
from unique staging keys, so repeated labels from unrelated prior jobs do
not collide.

History is limited to a patient's non-hidden source enrollment scopes. The
target creates fresh patient and enrollment ids, versions and indexes. Clinical
history is immutable, labelled `imported` and attributed to no target user;
source ids name evidence, not target authority. Selecting the current
demographics or identifiers enriches a patient row rather than adding another
registration. The normal dry run can still reject values that do not meet
target clinic policy or files that fail scanning.

## Governed import records

Version 2 adds the relations of the governed import (todo 66), which did not
exist when v1 was frozen. `interop_importmapping`, `interop_importjob`,
`interop_importtransition`, `interop_importfile`, `interop_importedrecord` and
`interop_hiddenenrollment` are exported like any other relation: mapping
documents, transition reports and imported content with its original source id,
author and time labels are decrypted. Rows of rolled-back jobs are exported too;
`interop_hiddenenrollment` names the imported enrollments a rollback hid, which
`intake_patientclinicenrollment` still contains. An `interop_importfile` row of
role `attachment` carries its decrypted bytes in `data` (base64), like every
clinical document. A row of role `source` carries no `data`: the export never
reads a stored import source, so a source appears only as its metadata and
checksum. `interop_importbatch`, the commit work units, is excluded as
machinery; the job's transitions carry the committed counts and the
reconciliation report.

## Enterprise identity records

Version 3 adds the relations of identity migration 0051 (todo 65), which did not
exist when v2 was frozen. `identity_ssoconnection`, `identity_scimresource`,
`identity_scimrolemapping`, `identity_scimmembership`, `identity_scimrolebinding`,
`identity_breakglassgrant`, `identity_supportaccessgrant`,
`identity_governancenotice` and `identity_unitgovernancechange` are exported like
any other relation: SSO configuration, directory attributes, break-glass reasons,
support purposes and unit-governance references are decrypted.
`identity_scimresource` is exported without `external_hash` and `search_hash`.
`identity_scimtoken`, the SCIM bearer credentials stored as digests, is excluded
as a credential, like every other authentication secret.

## RNDS submissions

Version 4 adds `interop_rndssubmission` (todo 68), which did not exist when v3
was frozen. Each row is one laboratory-result document the clinic sent to the
RNDS, exported like any other relation. `bundle` is the exact sealed FHIR bundle
(RFC 8785 JSON), decrypted and base64-encoded; `bundle_sha256` and
`request_digest` let a reader verify it; `state`, `rnds_identifier`,
`answer_code` and `decided_at` record the RNDS answer; `result_version_id`,
`enrollment_id`, `requested_by_id` and `capability_version_id` name what it was
sent for, by whom and under which provider version. An accepted row's
`rnds_identifier` is the only copy of the identifier RNDS assigned, which a
later system needs to correct the document RNDS holds. No column is left out.

## Delegate access

Version 5 adds `identity_delegateaccessevent` (todo 73), which did not exist
when v4 was frozen. Each row is one read by a delegate acting for a patient, the
same entry that patient's access log shows: `grant_id` names the delegate grant,
`enrollment_id` the represented enrollment, `record_type` is `clinical_record`
or `result`, and `accessed_at` is when the read happened. The row holds no
record content. No column is left out.

## Excluded relations

`excluded[]` lists every left-out relation with one of these reasons:

| Reason | Relations |
|--------|-----------|
| `credential` | authentication secrets, sessions and device material: `comms_provideraccount`, `identity_authenticatorreset`, `identity_authenticatorretirement`, `identity_passkey`, `identity_recoverybatch`, `identity_recoverycode`, `identity_scimtoken`, `identity_sessionrecord`, `identity_signupregistration`, `identity_totpsecret`, `intake_patientaccessgrant`, `intake_patientsession`, `interop_apiaccesstoken`, `interop_apiclientsecret`, `otp_static_staticdevice`, `otp_totp_totpdevice`, `scheduling_displaydevice`, `teleconsult_teleconsultcredential`, `tenancy_tenantdatakey` |
| `platform` | platform-wide records without a tenant owner: `ai_gatewayswitch`, `ai_modelconfiguration`, `identity_physicianevidence`, `prescription_medicationcatalogentry`, `prescription_medicationcatalogimport`, `prescription_verificationprobe`, `subscriptions_entitlement`, `subscriptions_plan` |
| `integrity_ledger` | the hash-chained audit ledger, verified by its own procedure: `audit_event` |
| `machinery` | delivery, retry, projection and receipt machinery: `ai_aicircuit`, `analytics_readmodelrefresh`, `comms_inboundevent`, `comms_integrationoperation`, `comms_messagemeter`, `ehr_addendumsavereceipt`, `ehr_drafteditstate`, `ehr_draftsavereceipt`, `ehr_timelineprojection`, `ehr_timelinerefresh`, `insurance_commandreceipt`, `interop_importbatch`, `inventory_commandreceipt` |
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
