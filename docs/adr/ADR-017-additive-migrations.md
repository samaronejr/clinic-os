# ADR-017: Additive expand/contract migrations

- Status: accepted 2026-09-24
- Recorded by: todo 2

## Context

Protected-field migrations are non-atomic and irreversible
(`apps/tenancy/migrations/_protected_migration.py`); their rollback is a
restore. Audit, appointments, outbox and prescription artifacts have triggers
that block hard deletes. CI runs a fresh install and a merge-base upgrade
rehearsal through `ops/testing/migration_upgrade.sh`. The successor plan
changes the appointment state machine, patient identity and imports, all of
which touch existing rows.

## Decision

Migrations are additive and follow expand/contract.

- New columns and tables land first; readers move; old shapes retire in a
  later release.
- Protected-field migrations are rehearsed through `migration_upgrade.sh`
  before merge.
- No migration rewrites history destructively.

## Consequences

- Schema changes take at least two releases when a shape is replaced.
- Each migration todo runs the SC-17 upgrade gate, and protected-field
  migrations also run `make restore-rehearsal`.
- Imported or corrected data is superseded, never overwritten, so provenance
  survives.

## Rejected

- In-place history edits. They break the audit chain and provenance, and
  protected-field changes can't be rolled back except by restore.

## Revisit trigger

None recorded. The AD table sets no trigger. Changing this model needs a new
ADR that supersedes this one.

## Owning todos

17, 22, 66. Applies to every todo with a migration (SC-9, SC-17).
