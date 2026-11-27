# R4g — replay of process-custody, posture and single-invocation SC‑5 evidence

From a Git repository containing the product, its reviewed parent, and the
r4f and r4e evidence commits, run:

```
python -I -B /path/to/SC5-26a2a86b/replay_r4g.py
python -I -B /path/to/SC5-26a2a86b/verify.py
```

In the bundle directory run `sha256sum --strict --check SHA256SUMS`.
`verify.py` regenerates the five-field outcome manifest in memory from the
certifying native JUnit. It checks every duration and exactly one passing
setup, call and teardown for every independently collected public ID. No
focused, parent or mutation invocation contributes a release row.

## What was and was not forced

`parent_local.py` imports the precise r4f Git blobs for the worker and
observer before collecting the three new fault-injection cases. A single
Event-controlled model runs each case against the two implementations. Its
private socket transfers an inert file description; both numeric and pidfd
signal endpoints are replaced by recorders. `parent.json` and `custody.json`
contain the boolean verdicts and counts produced inside the tests, not
reconstructed from the summary. Under ignored and default SIGCHLD the parent
addressed a number that the model had reassigned; the candidate did not.
Under default disposition this deliberately simulates loss to another waiter,
not normal auto-reap. The interrupted constructor on the parent never recovered
its created child; r4g obtained the private receipt, signalled the original
handle, waited, and restored the original disposition. No real foreign process
was signalled and no live PID reuse was induced.

Native controls are separate, unmocked where kernel identity is asserted. They
exercise SIGCHLD inherited across exec, real pidfds, WNOWAIT, recovery when a
real constructor's return value is lost, and an owned live descendant reached
through the outer worker's group-scoped pidfd. The existing successful-leader
and surviving-child test still checks both the live and reaped states. Two
Event-controlled sampling tests verify post-close admission and in-flight
read completion without waiting on a blocked database operation.

For the real handoff, the trace retains only safe process class/counters,
aggregate backend states, monotonic stages, and normalized signal dispositions.
The admitted launcher stays alive and waits for its own fixed workload. On
normal completion it may consume that direct child's wait status; a wait is
not a signal. On abort the dedicated worker uses pidfds for the launcher and
successively adopted children. The outer watchdog drains output without
consuming the wait status before its final handle-bound group operation.
`deadline.json` separately tests the exact `< 15 seconds` success boundary.
The fresh, warm and intentionally stalled probes are not substitutes for each
other. A timeout observation can be later than 15 seconds; no successful
result at or beyond 15 seconds is accepted. The outer worker requires
group-scoped `pidfd_send_signal` support; unavailable capability is a refusal
before workload admission, not a fallback to signalling by PID.

## Other gates and scope

The database verifier, R5-P1 tests and deployed exceptions are unchanged.
Fresh migration replay validates all 608 catalog rows across 259 migrations.
Seven real polarity sequences, both committed recorder controls, all 48 prior
omission probes and five new custody/sampler omissions are reconciled to native
receipts. Only explicit `rolsuper` is equivalent; that behavioral denial is
still enforced independently. `gate-exits.json` distinguishes intentional
nonzero diagnostic pytest exits from release gate exits. The independent
isolation suite includes all 3,308 cases and six restore guards.

The local delivery receipt sits beside this directory and is written only after
atomic non-forced publication, fresh object-store readback, and verified lane
cleanup. Raw logs, raw test parameters, free-form exception payloads, process
arguments, SQL and credentials are not published. Local synthetic verification
does not grant independent code-review, hosted-CI, or deployment approval.
