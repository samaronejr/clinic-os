# SC-5 single-run evidence

`junit.xml` is the native pytest JUnit file produced by one serial invocation
of the entire non-browser suite on `6b84a4faf05183d3ac622d212c51bbdeb58987f6`.
Its suite name binds the tested SHA and systemd `INVOCATION_ID`. There are
no retries in this invocation. No primary record was recovered for the
old `76376101` invocation; neither it, the superseded partial run, the
focused tests, nor the isolated harness rerun contributes any manifest row.

## Identity and reproduction

At the exact commit, independently run `python -m pytest --collect-only -q
-p no:cacheprovider --ignore=tests/renewal/browser tests` under the locked
fixture environment and pipe stdout to `python reference.py /new/reference.txt`.
The public ID is the static `tests/file.py::[Class::]test_function` address.
If the raw pytest ID has `[...]` suffixes, replace everything from the
first `[` with `[sha256-H]`, where H is the lowercase hex SHA-256 of the
**entire unmodified UTF-8 node ID** (no newline). This is one-to-one on this
collection and avoids publishing fixture values. `reference.txt` is sorted
ASCII with one public ID per LF-terminated line. Removing the 14 new
posture tests recovers all 16,618 original test identities; `reference.py`
also verifies that historical raw-node-ID fingerprint.

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

## Gates and limitations

The exact-SHA full command was `python -m pytest -q --reuse-db --tb=line
--ignore=tests/renewal/browser --junitxml=<private>/p1/junit.xml -o
junit_family=xunit1 -o junit_suite_name=sc5:<sha>:<run_id> -o junit_logging=no
-o junit_duration_report=total -p sc5_report <all 26 committed coverage
targets> --cov-fail-under=90 --cov-report=term:skip-covered tests`.
Startup independently checked the commit, parent, tree, clean status, actual
Redis binary and target list. Umask was 022 with private 0700 containing
directories. The main invocation was a niced systemd user unit waited on
through completion; `run.json` records the held unit result.

Earlier, the superseded `76376101` census was interrupted at user direction;
all installed teardown guards were rechecked, and the test database was
rebuilt. An initial scratch projection-check launcher misquoted a Python
path before any full execution started. One scratch mutation wrapper
refused an owner fixture's SQL before reaching its intended public canary;
the corrected wrapper only mutates the tagged producer after fixture setup.
These attempts are not evidence for a passing body. On the successor, a
separate targeted mutation of the recursive role-family query removed the
membership edge: the real grant/revoke regression then failed at the denied
posture expectation, as required. No test exercised elevated logging or file
authority. This is not deployment, hosted CI, or independent review approval.
