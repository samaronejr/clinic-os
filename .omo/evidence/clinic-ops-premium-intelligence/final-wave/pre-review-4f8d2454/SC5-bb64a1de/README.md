# SC-5 single-run evidence

`junit.xml` is the native pytest JUnit file produced by one serial invocation
of the entire non-browser suite on `bb64a1de35504b5582b8d3adf0183f220f472ff7`.
Its suite name binds the tested SHA and systemd `INVOCATION_ID`. There are
no retries in this invocation. Neither historical invocation, the focused tests, mutation controls nor the standalone harness contributes a manifest row.

## Identity and reproduction

At the exact commit, independently run `python -m pytest --collect-only -q
-p no:cacheprovider --ignore=tests/renewal/browser tests` under the locked
fixture environment and pipe stdout to `python reference.py /new/reference.txt`.
The public ID is the static `tests/file.py::[Class::]test_function` address.
If the raw pytest ID has `[...]` suffixes, replace everything from the
first `[` with `[sha256-H]`, where H is the lowercase hex SHA-256 of the
**entire unmodified UTF-8 node ID** (no newline). This is one-to-one on this
collection and avoids publishing fixture values. `reference.txt` is sorted
ASCII with one public ID per LF-terminated line. Removing the nine new round-four cases recovers the complete prior public reference (16,642 IDs); reference.py verifies its SHA-256.

`sc5_report.py` is the complete reporter used with pytest's native
`--junitxml`. It captures each actual setup/call/teardown report and its
duration in a separate lifecycle witness (`phases.json.gz`, UTF-8/JSON).
At native JUnit serialization it removes properties, stdout, stderr and
error text and substitutes public IDs. It **does not create passes or strip
failure/error/skip tags**. A separate synthetic reporter probe checked one
pass, one failure and one skip, and confirmed that fixture markers from
parameters, properties, stdout and diagnostics were absent.

`outcomes.jsonl` is derived from `junit.xml` only after matching both the
independent collection and all actual report phases. Rows, sorted by public
`node_id`, have exactly `node_id`, `outcome`, `duration` (seconds; JUnit
total to 1 ms), `run_id`, and `sha`. No suite properties, call arguments,
URLs, other session statements or exception payloads are retained.
`verify.py` checks the multiset, duplicates, schema, identity binding,
durations, and each successful body/teardown; it re-serializes the manifest
and compares every byte. Run `python verify.py` here and `sha256sum -c
SHA256SUMS` to replay and check the retained artifacts without a database.
`run.json` contains only execution metadata, counts, timestamps, and hashes.

## Gates and mutation controls

The release used the 26 committed coverage targets and a 90% minimum in one serial non-browser pytest invocation, with real broker and PDF prerequisites, private roots and umask 022. `run.json` binds the process-manager exit, product SHA/tree, separate mutation and isolation runs, counts and versions. No local verification is deployment or review approval.

`mutation_probe.py` applies one narrowly scoped omission per selected case, retaining the production code and clean worktree on disk. No privileged routine is invoked; the memory routine receives/revokes an EXECUTE grant only. The UUID-named harmless SQL routine in an independently created extension schema is not an extension member. The machine-only database authenticates a reachable suffixed login although the app has no CONNECT. The owner regression is SET-only with no direct app EXECUTE; only the target database's owner edge makes the grant reachable. All four required negative assertions fail under the corresponding omissions. Removing a `datallowconn`-false database, ignoring initial PG ACLs, and broadening an approved helper's grantees also fail their respective regressions.

`validate_controls.py mutations.json mutation.xml <product-SHA>` independently reconciles 31 sanitized cases: 29 targeted assertion failures, zero errors or skips, two equivalences. Removing only the explicit superuser check does not permit a superuser; removing only the `pg_read_all_stats` role arm does not permit its two effective private PG16 routines. On predecessor `de424fa482adc1346392049fbf2d556cb05c957d`, the first, more restrictive wrapper expected only one redundant check and exited 1; `initial-mutations.json` and `initial-mutation.xml` preserve its unmodified observations. The independent replay with the corrected attribution uses a new diagnostic invocation only; no diagnostic run contributes SC-5 rows. Use the predecessor SHA for the initial files, the final SHA for `mutations.json`.

The separate `tests/isolation` run uses closed database URLs; all its IDs match the full-run subset, including six backup/restore guard checks. Neither this nor the full release is a live restore or deployment. The release gate, not a runtime login, receives the independent credential. Standard template0 is either explicitly provisioned for inspection without PUBLIC access or the gate is red. See the product `docs/SECURITY.md` for operational assumptions and application policy provenance.

The sibling `SC5-de424fa4-rejected` folder archives the **different**, unpublished de424fa4 SC-5 invocation (7 failures). They cannot be supplied to `verify.py` as final data. Its failures identified three changed stored definitions left by test teardowns. Clock and migration fixtures were corrected, not the verifier or the manifest. `run.json` binds both attempts to their distinct SHAs and systemd invocations.

The separately archived `SC5-661483f0-rejected/` ledger also has seven failures. The true per-case verdicts, durations and lifecycle records are independently replayable in each rejected folder. Both run windows contained eight PostgreSQL connection refusals to a sealed scheduling template. The 661483f0 post-suite catalog check was clean after the session released that template. The final candidate has package-boundary scheduling cleanup, not an exception in the verifier. `boundary-red.xml` and `boundary-green.xml` are separate diagnostics: the seal and worker-clone controls pass in both; the third (deployment posture) fails before the fix and passes after it. No earlier run or auxiliary test contributes a release row.
