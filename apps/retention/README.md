# Retention

Retention policies, legal holds, record releases and controlled exports.

- `RetentionPolicy` is versioned per clinic and record class; only an
  `approved` policy can make a record disposal-eligible, and approving a new
  version retires the previous one atomically. A missing or unapproved policy
  fails closed: `request_disposal` denies and audits the refusal, and no
  automatic purge exists — `apply_retention_policy` remains a deferred stub.
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
