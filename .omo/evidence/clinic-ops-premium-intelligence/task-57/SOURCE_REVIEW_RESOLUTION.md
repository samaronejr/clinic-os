Native re-review of `6a3c7666` confirmed both production fixes and their green
receipts. Its only remaining finding, `T57-R2-B1`, requested the missing
pre-fix statement-correction red proof. That proof is now recorded below.
This evidence update does not claim a new reviewer approval.

The root's first finding allowed a financial reader to insert another actor's
binding or evidence. `_psp_guard_sql.py` now requires the actor stored on the
immutable intent before either insert can proceed. The regression uses both a
clinic-manager reader and a different policy owner. Both failed against the
original schema. Against the amended migration, known and unknown intents
produce the same `42501` and `billing access denied` result.
The final regression also includes a different Finance collector and asserts
that only that actor has `charge.collect`. All three cases passed with the same
opaque denial; see `takeover/reader-writer-regression.log`.

The second finding let rejected statements poison valid facts at the same
provider version. Both the SQL evidence guard and the posting freshness query
now exclude exactly quarantined statement observations. The regression proves
that a corrected statement can post at the same version. A separate regression
proves that a newer independently observed reversal still prevents the older
verified fact from posting.

The missing red run loads the archived `7193de92` guard SQL into migration
`0012_sandbox_psp` and the archived `psp_posting` implementation only in the
isolated test process. It changes no production or test source file. The live
PostgreSQL guard body was checked against the archived implementation.
`test_corrected_statement_at_the_same_provider_version_can_post` then failed
with exit `1`: the corrected statement returned `superseded`, not `matched`.
The corrected guard was restored after the run, and the private PG, volume and
secrets were removed.

See `takeover/red-statement-correction.log`,
`takeover/red-statement-correction.junit.xml`,
`takeover/red-statement-runtime.json` and `takeover/commands.jsonl` (the
`red-statement-correction` entry records the actual exit and full output).
`takeover/statement-red-green.json` pairs this failure with the same test's
existing passes in `affected.junit.xml` and `green-review.junit.xml`.
No green or serial run was repeated.

An isolated source review also found that provider rotation prevented
lookup-only recovery. New effects still require the current approved fixture
version. Recovery and local journal application may query the intent's original
approved sandbox version; the version must still be sandbox, approved, scoped
to the clinic or its global capability, and the recorded fixture provider.
Revocation still closes it. A native regression rotates the capability after
the recorded effect and proves recovery without a second send.

The focused green run passed 33 tests, including those regressions,
authenticated refund callback replay, statement matching, patient projection,
adapter authentication and outbox recovery. See `takeover/green-review.log` and
`takeover/green-review.junit.xml`. The explicit two-case red proof remains in
`takeover/red-evidence-authority.log`.

After rebasing onto the qualified todo 56 census commit, the affected modules
passed 365 tests. All 18 required serial selectors are covered by 109 unique
passing tests, including the native authority census. The initial JUnit-format
fixture error and its compatible `xunit1` rerun are preserved and reconciled in
`takeover/serial-summary.json`. Generated metadata preserves all 824 inherited
Python entries and 465 SQL classifications; only PSP boundaries were added.

The required real Redis/PostgreSQL gate passed with a prefork SIGKILL after the
recorded effect, one send attempt and lookup-only redelivery. The earlier
timeout was a gate configuration defect: Celery's environment still selected
the parent test transport. The test fixture now binds the real broker and
result-backend environment before the lazy client connects. Its failed receipt
is retained alongside the passing `broker-r3-psp-broker.log`.

No migration was deployed outside the task's private databases. The still
unshipped additive migration `0012_sandbox_psp` was amended as authorized. It
validates deferred foreign keys before installing helper-dependent RLS
policies, restores FORCE RLS inside the migration transaction, and does not
grant runtime resolver authority to `clinic_owner`.

Recorded fixtures remain the only provider implementation accepted by this
lane. Real provider legs remain `BLOCKED-ON-EG-1/EG-5`. Browser and full native R
qualification are deferred to the root's exclusive host window.
