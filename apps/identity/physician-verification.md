# Physician verification boundary (task 31)

Only synthetic data is authorized. The task-6
`docs/integrations/records/2026-09-12-v1/physician_registration.md` record has no
selected provider, machine API, owner approval or sandbox. `registry_capability()`
therefore always returns `real_enabled=False`. There is no configuration switch
that approves live verification. The live-data gate remains UNAPPROVED.

## Storage and authority

`PhysicianProfile` is an owner-provisioned identity binding per organization,
authenticated user and jurisdiction. Registration number and signing subject are
not staff roles. Runtime staff cannot create profiles or change those bindings;
only verification snapshot columns can be updated. `PhysicianEvidence` is a
normalized, timestamped response or unavailable-attempt record, append-only for
`clinic_app`. Neither the profile snapshot nor old evidence authorizes signing.
FORCE RLS requires the current tenant and physician; evidence additionally requires
the assigned encounter, matching organization/physician and clinic jurisdiction.

There is intentionally no staff self-attestation or provider-credential picker.
Synthetic profile provisioning requires visibly synthetic numbers (`SYNTHETIC-`)
and subjects (`synthetic:`) to pass the adapter. Real provisioning and approved
identity proofing remain external prerequisites, not a new administrative UI.

## Issuance consumer contract

The prescription `issue_prescription` service remains an unimplemented,
fail-closed stub owned by later tasks. Before a future signing side effect, call
`verify_physician_for_signing` inside the authenticated tenant transaction with
the request, exact clinic/encounter and a **verified signing-adapter**
`SigningIdentity`. Never construct that identity from posted subject/user fields.
This task does not implement certificate trust/revocation or a signing provider.

Each call derives the active actor from the database context, requires the
canonical exact-clinic physician role, both encounter/appointment assignment,
matching request user, recent exact-device TOTP, matching profile/signer subject
and issuer, and a fresh registry lookup. Do not reuse the result for a later
attempt or expose a cached `eligible` flag. Later issuance must keep these checks
adjacent to its own document-version/signing transaction boundary.

Synthetic use requires both `PHYSICIAN_SYNTHETIC_REGISTRY=True` in trusted settings
and `synthetic=True` on the service call. Its five-minute recheck interval is a
synthetic test policy, not a clinical-owner-approved real policy. Unknown expiry
is represented by NULL, bounded by mandatory recheck. Evidence is marked synthetic
and is never an authorized sandbox receipt.

Failures raise `PhysicianVerificationRequired.reason_code` (recent TOTP retains
`StepUpRequired`). The registry is queried on every attempt, even after success.
An outage records `registry_unavailable` and replaces the current profile status
with unavailable; it never uses old evidence. Revoked, suspended, unknown, expired,
stale/future, wrong-jurisdiction/registration/subject and malformed normalized
responses deny with distinct states. Restore the missing authority/binding or
refresh through the registry; every retry runs all checks again. Catch the error
inside the tenant transaction to retain failed-response evidence; an outer
transaction rollback correctly rolls back its bookkeeping too. A failed identity
or step-up check makes no registry call and stores no provider response.

## Verification and CI

`tests/renewal/test_physician_verification.py` exercises actual PostgreSQL policies
and services as `clinic_app`, using owner authority only for fixture provisioning.
The existing renewal CI discovers `tests/renewal/` and already owns coverage for
`apps.identity`; no new browser suite or coverage exclusion is needed. Required QA:

```sh
uv run --frozen --no-sync --no-env-file pytest --reuse-db -q tests/renewal/test_physician_verification.py tests/auth/test_stepup_policy.py
```

Real/sandbox acceptance remains waiting_external: accountable clinical owner,
licensed approved registry API/schema/status mapping, professional identity
binding and signing-subject proof, approved freshness/expiry/outage policy,
authorized sandbox records/access, and all live-data-gate approvals are absent.
