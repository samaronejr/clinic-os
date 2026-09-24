# AES-256 storage encryption

| Record field | Value |
| --- | --- |
| Capability | `data_at_rest` |
| Record version / reviewed date | 2026-09-12-v1 / 2026-09-12 UTC |
| Status | unavailable; documentation only |
| Selected provider | None |
| Candidate / reference | Unselected; managed PostgreSQL/object storage such as RDS/S3 are reference examples. |
| Accountable owner / decision | Unassigned; infrastructure/security owner must approve complete deployment inventory. |
| Approval / contract | None supplied; provider API/profile/package version unverified unless stated below |
| Sandbox / executed provider evidence | Unavailable; none executed |
| Residency / subprocessors / retention | Unverified; owner-approved region, processing agreement, retention and legal-hold handling required |
| Costs / budget | Unverified; historical prices are not approval or current quotes |
| Required packages | Unapproved: selected storage/provider clients and cryptographic integration; versions/policies unset. |
| Dependent tasks | 43 implementation; 44 evidence; 46/47 live use |
| Review trigger | Provider selection, protocol/package change or release; no approval expiry exists |

## Source-backed observations

[RDS encryption](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Overview.Encryption.html) describes AES-256 protection of encrypted instance storage and associated backups/snapshots/logs. The repository's Terraform skeleton has not been applied; it is not deployment evidence.

## Contract required before use

Preserve the project's AES-256 commitment across the [protected inventory](../protected-data-v1.md): database/WAL/replicas, object versions, PDFs, exports, backups, volumes, approved logs and disk-backed staging. Pin actual cipher/key settings per surface and classify ephemeral memory separately. Pair infrastructure encryption with tenant_key_management, authorized decryption and managed_secrets; backup-only encryption is insufficient. No plaintext fallback when a provider/key is missing.

## Required future fixtures

Task 43 must inspect actual selected storage/key policy and prove encrypted writes/reads, copy/restore coverage and rejection of unencrypted target configuration; inspect synthetic plaintext/ciphertext boundaries without logging payloads.

These are acceptance requirements, not executed sandbox results. Fixtures may
contain only synthetic or explicitly provider-authorized data.

## Task 43 implementation boundary (2026-09-22)

The application-level AES-256 envelope boundary is implemented through the
tenant_key_management record (pgcrypto `pgp_sym_encrypt_bytea`, MDC on,
compression off, tenant-bound wrapped DEKs). The restore rehearsal now covers
every domain table, the wrapped-key store, attachment object bytes (manifest
digest verification against the restored object store) and signing/payment
reference rows. Infrastructure storage encryption (volumes, WAL, backups,
object store at rest) is still unselected and unproven; this record's
provider requirements are unchanged.

## Unavailable branch

Task 43 actual storage integration and task 44 separate data_at_rest evidence require infrastructure/security approval, region/inventory, package/algorithm decisions and observed encrypted storage. Environment variables or an unapplied Terraform flag cannot satisfy readiness.

Follow the [register's versioning and approval rules](../../capabilities.md).
