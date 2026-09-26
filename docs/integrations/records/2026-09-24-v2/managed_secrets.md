# Managed secret loading and rotation

| Record field | Value |
| --- | --- |
| Capability | `managed_secrets` |
| Record version / reviewed date | 2026-09-24-v2 / 2026-09-24 UTC |
| Supersedes | [2026-09-12-v1/managed_secrets.md](../2026-09-12-v1/managed_secrets.md) |
| Status | unavailable; documentation only |
| Selected provider | None |
| Candidate / reference | Unselected; AWS Secrets Manager is a reference example. |
| Accountable owner / decision | Unassigned; infrastructure/security owner and rotation operator required. |
| Approval / contract | None supplied; provider API/profile/package version unverified unless stated below |
| Sandbox / executed provider evidence | Unavailable; none executed |
| Residency / subprocessors / retention | Unverified; owner-approved region, processing agreement, retention and legal-hold handling required |
| Costs / budget | Unverified; historical prices are not approval or current quotes |
| Required packages | Unapproved: selected secret-manager SDK/workload-identity provider, pinned version and deployment integration. |
| Dependent tasks | Successor todos 73; superseded record: renewal tasks 13 external effects; 43 implementation; 44 evidence; 46/47 live use |
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

AWS documents [secret encryption](https://docs.aws.amazon.com/secretsmanager/latest/userguide/security-encryption.html) and [rotation](https://docs.aws.amazon.com/secretsmanager/latest/userguide/rotating-secrets.html). Current prod.py reads required settings from environment; this does not demonstrate approved secret-manager retrieval or rotation.

## Contract required before use

Inventory runtime/owner DB credentials separately, Django signing key, Redis/provider/API/webhook keys, signing-service credentials and KMS/backup access. Use approved workload identity and least privilege with environment/tenant/service scoping; never put secret values in records. Define startup/reload, bounded cache lifetime, expiry, revocation, rotation overlap, rollback and outage behavior. Missing/expired access fails closed with redacted errors; no repository or hardcoded fallback. Key recovery must respect historical encrypted data and signing/session policy.

## Required future fixtures

Authorized sandbox retrieval, correct version/role, successful staged rotation and revocation; deny wrong identity/environment, expired cache, manager outage and missing version. Prove logs do not contain values.

These are acceptance requirements, not executed sandbox results. Fixtures may
contain only synthetic or explicitly provider-authorized data.

## Task 43 implementation boundary (2026-09-22)

`apps.core.secrets` implements the closed loading contract: a `SecretStore`
protocol, the `synthetic-file` backend (one mode-0600-or-stricter regular
file per named 64-hex secret under `CLINIC_SECRET_DIR`), and a fail-closed
factory selected only by `CLINIC_SECRET_BACKEND`. There is no default, no
environment fallback and no plaintext path; unknown backends, missing files,
loose permissions and malformed values all raise `SecretUnavailableError`.
The tenant KEK for the envelope boundary is fetched per call through this
surface. A managed secret-manager adapter, workload identity and rotation
procedure remain unapproved and unimplemented.

## Unavailable branch

Task 43 real secret-manager adapter and task 44 evidence wait for security/operator ownership, identity/store/SDK decisions and observed retrieval/rotation. Local private env files remain synthetic tooling, not production readiness.

Follow the [register's versioning and approval rules](../../capabilities.md).
