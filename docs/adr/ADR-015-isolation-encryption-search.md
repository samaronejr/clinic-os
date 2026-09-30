# ADR-015: FORCE RLS, envelope encryption and blind-index search

- Status: accepted 2026-09-24
- Recorded by: todo 2

## Context

Every current tenant table has `ENABLE` and `FORCE ROW LEVEL SECURITY`, and
posture tests pin the exact policy set. PHI and PII live in
`EncryptedTextField` and `EncryptedJSONField` (`apps/tenancy/fields.py`) under
per-tenant keys wrapped by a KEK. Encrypted columns can't be filtered or
ordered in SQL, so exact patient search goes through HMAC blind indexes
(`apps/intake/patient_search.py`). The successor scope adds many tables and a
patient base of 100k per tenant in the performance profile.

## Decision

Isolation, encryption and search keep the current model and extend it to
every new table.

- FORCE RLS on every tenant table.
- Envelope encryption for PHI and PII.
- Exact search through blind indexes only.
- No plaintext duplicates of protected fields, in shadow columns or anywhere
  else.

## Consequences

- New apps follow SC-2 and SC-5: RLS target allowlists, posture test updates
  and encrypted fields from the first migration.
- Fuzzy or prefix search over protected fields isn't available. Features that
  need it have to use non-protected attributes or blind-indexed tokens.
- Search performance is measured on the P-100 dataset in todo 71 before anyone
  argues for a different approach.

## Rejected

- A search service holding plaintext. It creates a second copy of PHI outside
  RLS and outside the key hierarchy.

## Revisit trigger

A measured p95 miss on patient search with blind indexes in the todo 71
harness.

## Owning todos

17, 71. Applies to every todo that adds a tenant table (SC-2, SC-5).
