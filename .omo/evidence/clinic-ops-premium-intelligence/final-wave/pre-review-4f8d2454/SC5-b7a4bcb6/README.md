# SC-5 single-run evidence

`junit.xml` is the native pytest JUnit file produced by one serial invocation
of the entire non-browser suite on `b7a4bcb62f84965d84616ab22bf7dd9695bd1df5`.
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
ASCII with one public ID per LF-terminated line. Removing the 10 new round-three cases recovers the complete prior public reference (16,632 IDs); reference.py verifies its SHA-256.

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

The full command used the non-browser test path and all 26 committed coverage targets, --reuse-db, --cov-fail-under=90, junit_family=xunit1 and -p sc5_report. PostgreSQL 16.14, redis-server 7.4.11, umask 022 and both real-broker and PDF gates were enabled. See run.json and the parent claim for the unit and exact gates.

mutation.xml and mutations.json are from the separate diagnostic invocation. They have no stdout, properties, free-form exception payloads or fixture arguments. mutation_probe.py maps each tested omission to its intended test; validate_controls.py documents the criterion used on the private execution paths (the two input files are retained here as mutations.json and mutation.xml). Twenty-one controls tripped their corresponding assertion, including all seven added privilege entries and both database scope controls. The extra superuser-only mutation was redundant by design: the test still rejected the role because effective EXECUTE on a forbidden routine is sufficient. This is recorded as a survivor, not a claimed kill. An earlier overly strict diagnostic wrapper returned 1 for this reason; the original outcomes were preserved. The two sets of JUnit cases are never combined. This is not live-data approval, external review, or a browser run.
