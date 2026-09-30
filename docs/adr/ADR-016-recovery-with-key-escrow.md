# ADR-016: Recovery drills include keys and objects

- Status: accepted 2026-09-24
- Recorded by: todo 2

## Context

`make restore-rehearsal` moves a reviewed table and sequence manifest between
two task-owned PostgreSQL containers and compares rows and posture. It's a
logical synthetic check, not a backup design: it doesn't cover object storage,
encryption keys or provider PITR. Encrypted fields are useless without their
tenant keys, and attachments and audio live outside the database. A restore
that brings back rows but not keys or objects looks green while being unusable.

## Decision

Recovery combines PostgreSQL PITR, object versioning and KEK/DEK escrow, and
the restore drill exercises all three together. The drill fails when keys or
objects are missing.

## Consequences

- The drill reports missing keys or objects as a failure, never as a partial
  success.
- Purge tombstones are re-applied after restore, so purged audio doesn't come
  back (todo 43).
- Provider PITR and encrypted provider backup stay unverified until EG-12;
  local drills are L1 evidence only.
- RPO and RTO are recorded from measured drills, not from targets.

## Rejected

- SQL-only restore. It restores rows whose encrypted fields and object
  references may be unreadable.

## Revisit trigger

None recorded. The AD table sets no trigger. Changing this model needs a new
ADR that supersedes this one.

## Owning todos

72. Consumer: 43.
