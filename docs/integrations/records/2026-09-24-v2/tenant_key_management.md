# Tenant-scoped pgcrypto/KMS envelope encryption

| Record field | Value |
| --- | --- |
| Capability | `tenant_key_management` |
| Record version / reviewed date | 2026-09-24-v2 / 2026-09-24 UTC |
| Supersedes | [2026-09-12-v1/tenant_key_management.md](../2026-09-12-v1/tenant_key_management.md) |
| Status | unavailable; documentation only |
| Selected provider | None |
| Candidate / reference | PostgreSQL pgcrypto plus an unselected KMS; AWS KMS is a reference example. |
| Accountable owner / decision | Unassigned; security/cryptography reviewer and data-migration owner required. |
| Approval / contract | None supplied; provider API/profile/package version unverified unless stated below |
| Sandbox / executed provider evidence | Unavailable; none executed |
| Residency / subprocessors / retention | Unverified; owner-approved region, processing agreement, retention and legal-hold handling required |
| Costs / budget | Unverified; historical prices are not approval or current quotes |
| Required packages | Unapproved: maintained envelope/KMS client and object encryption library; exact primitives, profiles and versions require review. |
| Dependent tasks | Successor todos 72; superseded record: renewal tasks 43 implementation; 44 evidence; 46/47 live use |
| Review trigger | Provider selection, protocol/package change or release; no approval expiry exists |

## Successor note

This record is carried forward from set 2026-09-12-v1 under the
[successor contract](../../../plans/clinic-ops-premium-successor.md). Its source
observations were retrieved on 2026-09-12 and weren't fetched again for this
set, so re-verify them before any approval. Task numbers in the body below are
renewal-plan tasks; the header lists the successor todos. Status, owner and
approval fields are unchanged: nothing here authorizes a provider, sandbox,
spend or live data.

## Source-backed observations

[PostgreSQL 16 pgcrypto](https://www.postgresql.org/docs/16/pgcrypto.html) documents PGP AES-256 support; its cipher default is AES-128. It warns that raw encryption lacks integrity checks and that server-side cryptography trusts DB/system administrators and needs protected transport. [KMS concepts](https://docs.aws.amazon.com/kms/latest/developerguide/concepts.html) and [key rotation](https://docs.aws.amazon.com/kms/latest/developerguide/rotate-keys.html) distinguish KMS key material from application data keys; KMS rotation does not itself reencrypt existing application data.

## Contract required before use

The [protected inventory and migration contract](../protected-data-v1.md) binds this record. Require reviewed AES-256 primitive/profile, integrity protection, tenant-bound data encryption keys (DEKs), wrapping-key (KEK) identifiers/versions and authenticated tenant/object context. Bind authorized decryption to trusted organization/actor context; preserve clinic RLS. Do not hand-compose raw AES/HMAC/IV schemes or equate storage SSE-KMS with application envelopes. Resolve search compatibility and shared-user key scope before migration.

## Required future fixtures

Task 43 must prove encrypted round trips, altered ciphertext/context rejection, wrong-tenant/missing-key denial, query semantics, wrap-key and data-key rotation, backup history/key restoration and interrupted backfill without plaintext fallback.

These are acceptance requirements, not executed sandbox results. Fixtures may
contain only synthetic or explicitly provider-authorized data.

## Task 43 implementation boundary (2026-09-22)

The envelope boundary is implemented and rehearsed: `tenancy_tenantdatakey`
stores only wrapped DEK versions behind FORCE RLS with no runtime table
grants; `clinic_app.tenant_encrypt`/`tenant_decrypt` (SECURITY DEFINER,
owner-owned) encrypt with pgcrypto `pgp_sym_encrypt_bytea` AES-256 + MDC,
no compression, and bind organization, purpose and key version inside the
integrity-protected frame. `tenant_dek_issue`, `tenant_dek_rewrap`,
`tenant_reencrypt` and `tenant_key_status` are owner-only. The KEK arrives
per call through `apps.core.secrets.secret_store()`; no backend configured
means every call fails closed. The restore rehearsal proves a source
envelope decrypts on the restored target under the same KEK.

This is the synthetic/rehearsal boundary, not a production deployment claim:
the KEK backend is `synthetic-file`, and a managed KMS, production KEK
rotation runbook and task-44 evidence remain required before live use.

## Unavailable branch

Task 43 real encryption waits for security owner approval of exact primitive/library, KMS permissions, search/leakage analysis and resumable migration/rollback; task 44 requires separate current evidence. Synthetic adapters may exercise denial contracts but cannot claim pgcrypto/KMS deployed.

Follow the [register's versioning and approval rules](../../capabilities.md).
