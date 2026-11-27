# R4f — replay of synthetic PostgreSQL posture and child-lifetime evidence

The product commit is the parent of the evidence-only commit. In a Git repository
containing the product, reviewed parent, and r4e evidence commit, run:

```
python -I -B /path/to/SC5-6820766c/replay_r4f.py
python -I -B /path/to/SC5-6820766c/verify.py
```

In the bundle directory, `sha256sum --strict --check SHA256SUMS` checks the
archived bytes. `verify.py` rebuilds the five-field manifest from the single
native full-run JUnit and checks independent collection, exact identity,
durations, and one passing setup/call/teardown. The separate diagnostic
invocations supply zero release rows. `interpreter.json` was written by
pytest itself after opening and closing a real kernel pidfd.

`parent.xml`, `parent.json` and `parent-phases.json.gz` identify each assertion
on the unmodified r4e verifier and trace worker, imported from their exact Git
blobs before collection of r4f tests. This is a comparison of the same new
regressions against the two implementations, not a claim that the new test
files were already in the parent. The parent's existing schema guard refuses
even after ordinary ACL revocation; the old implementation cannot truthfully
be described as failing that behavioral test. The broad catalog omission
matrix also tests the actual scope of a table's implicit row type and array.
No routine was executed and no data or delegation was accessed through a
canary. The model used for deterministic PGID recycling never sends an OS
signal. A separate real private child group demonstrates that a successful
leader can leave an owned child that must still be killed and reaped.

`gate-exits.json` distinguishes expected nonzero assertion runs from the
gate/validator exits. The sole equivalent omission is the already documented
explicit `rolsuper` arm: the independent restricted-routine oracle continues
to refuse a superuser on PG16. We did not weaken either assertion to manufacture
a kill. Two additional committed recorder controls are independent of this
48-case matrix. Seven real posture sequences, all 608 migration-lineage
records, the strict boundary and reaped-timeout trace, and all 3,308 isolation
IDs (six restore guards) are validated again. Raw test streams, SQL, secrets,
process arguments and unsanitized parameters are not part of the evidence.

The setup was local and synthetic, with fsync off only on the disposable main
PG16 instance, Redis 7.4.11, native PDF tools, private loopback high ports,
and the normal 90% full-suite coverage floor. This is implementing-lane
evidence, not hosted CI, independent code review or deployment approval.
