# R4h — birth-time pidfd custody, source audit and single-SC-5 evidence

From a Git repository containing the product, its parent and the published
r4g/r4e evidence commits, run:

```
python -I -B /path/to/SC5-ee79bbe2/replay_r4h.py
python -I -B /path/to/SC5-ee79bbe2/verify.py
python -I -B /path/to/SC5-ee79bbe2/replay_rejected_r4h.py /path/to/SC5-ee79bbe2
```

`sha256sum --strict --check SHA256SUMS` in this directory verifies the
stored files. `verify.py` rebuilds the five-field manifest in memory from the
one native full-run JUnit; it checks the independent collection, uniqueness,
per-ID setup/call/teardown and durations. None of the supplemental invocations
contributes a release row. The interpreter receipt comes from the actual
pytest process, not just a version check in its caller.

The first r4h candidate, `9f44663bc3b2f4dd18241aae086b947663053117`,
was rejected and not published. Its separate full invocation had 16,700
passing IDs and two failed calls. `rejected-*` preserves its native reports,
unit/interpreter evidence, and attribution. The guard and listener-handoff
assertions were not removed: the corrected listener test exercises a real
owned private process, and only the native module now knows the storage ABI.
`corrections.xml` and its phases prove both cases passed under coverage on the
successor; neither diagnostic nor historical rows are counted toward SC‑5.

### Rejected invocation: tests, reference set and kernel window

Invocation `2839b83898a8428cbeedf40e779a0614` collected every one of the
16,686 r4g IDs, plus 16 new IDs, each once. Two inherited calls failed;
16,684 inherited and all 16 new calls passed. There were zero errors or
skips. Setup and teardown passed for both failed calls.

| Failed ID | Rejected log and cause |
| --- | --- |
| `tests/browser/test_browser_server_controller.py::test_controller_keeps_port_bound_through_gunicorn_handoff` | `ProcessLookupError: [Errno 3] No such process`. The fixture handed the real pidfd-pinning supervisor a `Mock` with PID `58419`; there was no child to pin. This was an exception, not a failed port-binding assertion. |
| `tests/core/test_workspace_guard_static.py::test_no_test_reaches_or_rewrites_the_refusal_guard` | `assert not unexpected` raised `AssertionError: Refusal guard tampering constructs:`. The three `tests/infra/` modules `test_posture_birth_custody.py`, `test_posture_signal_custody.py` and `test_posture_signal_native.py` each reported `uses ctypes (x2)`. This was the existing refusal guard, not the new signal audit. |

Neither failure reproduced in the exact `26a2a86b` full invocation
`005b0a75465f475e9ef8382a4737317d`: its JUnit and per-ID phases show
passing setup, call and teardown for both IDs. The rejected private pytest
log SHA-256 was `dd4adf94fa2f84c43c477328212c870636c32a84492cfa78877958d9ee34e6f2`;
the sanitized JUnit locates the failures, not their exception text.

The rejected unit ran from 10:24:24.801390 to 14:36:54.641307 UTC on
2026-11-27. In that exact interval `journalctl -k` recorded one OOM kill:
at 12:31:24.321163 UTC the kernel killed `docker` (PID 1332887) in a
COSMIC Files cgroup, not pytest or the SC-5 unit. Pytest completed the
entire reference set and the unit exited normally with status 1. Host
pressure was real; neither recorded failure was an OOM kill, an interpreter
error, or evidence that a numeric signal fallback was needed.

## Automatic source scope

`signal-audit.json` and `parent-signal-audit.json` are the output of the same
analyzer on two exact source trees. It enumerates the paths changed across the
lane's history from the fixed base and intersects them with each frozen tree;
it is not a hand list of modules. Python, shell, Make and supported extensionless
shebang sources are included. The default working-tree command additionally
checks staged, renamed and untracked sources. `check_source_receipt.py` rebuilds
that inventory from Git objects independently, including its per-source hashes.
Aliased and indirect numeric signal methods and implicit subprocess timeout
paths have lint-only negative controls; snippet strings are never executed.
These are static checks of the stated source scope, not an arbitrary-Python
whole-program security proof.

## What the negative tests observe

`parent_local.py` imports the exact reviewed r4g launch and orphan-driver
modules before collecting the new tests. `birth_oracle.py` binds the native
phase outcomes to their original assertion and, where applicable, the
observed custody counters. The four children are real private subprocesses:
EOF, malformed receipt, unreadable receipt, and silent live child. A pipe
handshake establishes a child exists and the test then interrupts the constructor.
The test's independent pidfd is a witness, not a handle supplied to production
cleanup. Only after taking its verdict does the harness reclaim a leftover on
the failing parent implementation. Any numeric signal attempt fails locally;
a handle signal is forwarded only if it identifies the exact test child.

On r4h the native glibc call publishes into the already-protected owner slot.
No Python return value or secondary socket receipt is needed to signal and
reap an unpublished child. The receipt remains mandatory before workload
admission. Old SIGCHLD/PGID and sampler-model evidence is distinct from these
native tests. A pre-release parent diagnostic once produced a pytest setup
error from a premature finalizer registration; only the corrected six genuine
parent call failures are used here.

## Mutation accounting and limits

`mutations.json` and `mutation.xml` exercise all inherited diagnostic selectors
plus the r4h additions. The old lost-publication arm now drops the authoritative
birth slot. `receipt-compat.json`, `compat.xml` and its phases separately
exercise the **literal original** r4g `_receive` omission without adapting it:
normal admission is refused, but a partial birth is still reaped without a
receipt. These are complementary requirements, not a claim that unavailable
SCM_RIGHTS must cause a leak. The sole equivalent deletion in the omission
matrix remains explicit `rolsuper`; both committed recorder controls still
fail at the original final assertion.

The seven posture sequences, full 608-entry migration-origin replay, R5-P1
source identity, strict 15-second success cutoff, per-mode handoff traces,
the independent 3,308 isolation cases and six restore guards are replayed.
The Linux host must provide glibc `pidfd_spawn`, usable kernel creation-time
pidfds and group-scoped signalling where required. There is no numeric fallback.
The synthetic main PG16.14 instance alone had fsync off. Stored evidence
contains no raw report payloads, command lines, database URLs or credentials.
The local post-cleanup receipt is intentionally outside the evidence commit.
These results are local verification, not independent approval or deployment.
