# Retention

Retention policies, legal holds, record releases and controlled exports.

- `RetentionPolicy` is versioned per clinic and record class; only an
  `approved` policy can make a record disposal-eligible, and approving a new
  version retires the previous one atomically. A missing or unapproved policy
  fails closed: `request_disposal` denies and audits the refusal, and no
  automatic clinical-record purge exists.
- `LegalHold` binds one record to an authority and reason; release records
  its own authority and reason. Held records are never disposal-eligible and
  every retention table forbids DELETE by trigger.
- `RecordRelease` is the explicit per-version release to the patient; only
  the assigned physician releases or revokes, and at most one active release
  exists per version.
- `RecordExport` is the durable receipt for one export package: a
  deterministic zip of released document versions plus a `manifest.json`
  with per-file SHA-256 digests and a manifest digest. Staff exports require
  a physician with a care relationship; patient exports require a live
  session holding the `records` operation. Drafts, discarded content,
  attachments, staff secrets, audit internals and other-clinic data never
  enter the package.
- The staff status surface lives at `/retention/clinics/<clinic_id>/` and
  the patient surface at `/patient/records/`; both are online
  access-controlled operations, never PWA caches.

## Transient execution (todo 43)

`apply_retention_policy(*, clinic_id, record_class)` now implements only
`raw_audio`, `ai_artifact` and `transcript_partial`. Every clinical record
class remains non-destructive. Call it inside `service_principal_context`;
the owner provisions a new immutable v3 `retention` principal using
`register_retention_principal`, with only `retention.execute`. Existing v1
availability identities gain no authority.

Synthetic clinic configuration defaults to `retention_dry_run=True`,
`raw_audio_retention_days=7`, and `retention_backup_days=35`. A dry run creates
no tombstone, destroys no key, and publishes no worker. Ordinary settings saves
carry these values forward. An approved transient RetentionPolicy overrides
the synthetic period; an approved indefinite policy blocks disposal.
The raw deadline is note finalization plus TTL. Stopped recordings without a
finalized note are ineligible, including under an approved transient policy.
It is never `AudioChunk.expires_at`, which is an upload deadline.
AI artifacts use their stored expiry; partial ASR artifacts are explicitly
classified `transcript_partial` by their producer, not inferred from prose.

Beat schedules `apps.retention.tasks.scan_due` every 60 seconds on the `bulk`
queue. Its resolver discovers only active v3 retention principals and grants
bound to the worker's actual database login. Without a configured agent it is
idle; live mode remains refused. It publishes `apps.retention.tasks.apply_policy`
after commit with only clinic, principal and transient record-class references.
Repeated sweeps resume unfinished receipts. Jobs persist before after-commit
publication and can also be republished with `publish_purge`. There is no implicit
workstation-wide principal. Runnable selection excludes held targets before its
100-record limit. A separate 100-record held batch preserves `blocked_hold`
receipts without consuming runnable capacity; dry-run counts use the same batches.

The retained target moves through `purge_due`, `blocked_hold`, `purging` and
`purged`. A recording receipt covers every chunk, including discarded or
quarantined chunks whose ciphertext may remain in storage. The worker commits
destruction of wrapped per-object audio keys and encrypted staging first,
then deletes each object and pending marker and independently proves absence.
AI ciphertext is removed in place; its provenance row and invocation ledger
remain. Existing tenant-encrypted AI envelopes have no separately retained
per-object key: removing their ciphertext does not crypto-shred old backups.
The receipt honestly records the synthetic maximum backup-expiry horizon.
No final receipt is issued until all targets are confirmed absent.

Legal holds, policy reads, and purge admission serialize per clinic. A hold
on a recording/artifact, or its encounter/document parent, blocks destruction.
After irreversible destruction has started, placing a contradictory hold is
refused rather than pretending the destroyed bytes can still be preserved.
Every table retains no-delete/no-truncate guards. No new runtime table write
grant or machine clinical-content read grant is introduced.

Storage failures leave `purging`, append metadata-only audit, increment
`clinic_retention_purge_failures_total`, and create one urgent clinic-admin
workflow task. The only runnable transport is the filesystem fake. A MinIO,
S3, live or unknown/mismatched storage namespace cannot obtain a purge receipt.
New chunks bind their exact storage namespace at ingest; pre-migration chunks
without that binding fail closed and require operator qualification.

Restore rehearsals capture the current authenticated source tombstone ledger,
import it over an older isolated backup, remove resurrected ciphertext/keys,
and re-delete resurrected media before exposure. Media targets require an
explicit isolated `media_store`; no storage path is guessed. Keep this current
ledger independently of an older backup. Failure blocks restore exposure.
The rehearsal also requires an explicit source media root. It copies that root
into the reserved fresh `media-restored` directory in its private workspace and
binds the ingest namespace to that copy's canonical path and directory identity.
Every target must match the source namespace; unrelated, replaced or foreign
roots cannot use the binding. The source objects remain untouched.
Destruction is irreversible; rollback = restore plus current tombstones.

All live periods and provider execution remain **BLOCKED-ON-EG** (EG-2/EG-3
and applicable provider gates). Synthetic receipts satisfy no external gate.
