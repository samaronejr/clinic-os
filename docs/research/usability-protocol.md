+++
protocol_id = "usability-v1"
kind = "usability"
state = "pre-registered"
external_gate = "EG-10"
gate_status = "BLOCKED-ON-EG"
results_template = "docs/research/results-template.md"
capture_form = "docs/operations/research-capture-forms.md#usability-observation"
prereg_sha256 = "ac1fe11e5c1ba9999f7e79f3e2fbdc31ac11702f43a5a134754507032a07ed4e"

[[targets]]
id = "unassisted-completion"
label = "proposal"
metric = "share of critical-task attempts completed unassisted"
comparator = ">="
value = 0.95
numerator = "critical-task attempts with outcome code U (unassisted success)"
denominator = "all critical-task attempts started, per role and device stratum"

[[targets]]
id = "critical-errors"
label = "proposal"
metric = "share of critical-task attempts with a severity 4 finding"
comparator = "<="
value = 0.0
numerator = "critical-task attempts with at least one severity 4 finding"
denominator = "all critical-task attempts started, per role and device stratum"

[sample_size]
label = "proposal"
unit = "participant session"
per_stratum = 5
roles = ["clinician", "reception", "clinic_admin", "finance"]
devices = ["desktop", "tablet", "phone"]
rationale = "assumption: five sessions per cell surface most severity 3 and 4 issues; this is a planning heuristic, not a power calculation"
+++

# Scenario-based usability protocol (UX-06, EG-10)

Status: pre-registered protocol, no sessions run. This file holds no
results and no participant counts. Sessions are owner-arranged with
existing clinic contacts under EG-10, which stays BLOCKED-ON-EG until the
owner records that they happened. Nothing here satisfies EG-10 or any other
gate.

Every target, sample size and threshold below is a **proposal**. The owner
may change it before the first session. After the first session, any change
needs a new protocol version and a new pre-registration hash.

<!-- prereg:begin -->
## Pre-registered design

### Critical tasks per role

Each task runs on synthetic data in a synthetic tenant. Participants never
see or enter real patient data.

| Task id | Role | Scenario | Success definition |
| --- | --- | --- | --- |
| UC-01 | clinician | Open today's next patient and start the encounter | Encounter is open on the right synthetic patient within the task window |
| UC-02 | clinician | Review a scribe draft, fix one seeded error, finalize | Seeded error corrected and note finalized by the clinician |
| UC-03 | clinician | Draft and sign a synthetic prescription | Draft reaches the signed state with the intended item |
| UC-04 | reception | Book a returning patient into a free slot | Appointment exists at the requested slot, no double booking |
| UC-05 | reception | Check in an arrival and resolve one schedule conflict | Arrival recorded and conflict cleared |
| UC-06 | clinic_admin | Grant a staff role and revoke another | Both role changes recorded, step-up passed |
| UC-07 | finance | Find an unpaid charge and record the synthetic payment | Charge shows as paid with a receipt |
| UC-08 | any | Recover after the AI assistant is marked unavailable | Task finished through the manual path |

A task window is 10 minutes (proposal). The observer stops the task at the
window and records outcome code T.

### Outcome codes

- U: unassisted success, the success definition is met with no hint.
- A: assisted success, met after one or more observer hints.
- F: failure, participant gave up or reached a wrong end state.
- T: timed out at the task window.

Only U counts toward the unassisted completion target. A, F and T stay in
the denominator.

### Strata

Roles: clinician, reception, clinic_admin, finance. Devices: desktop,
tablet, phone. Each critical task is attempted only by the roles named in
its row. Results are reported per role and device cell, never only pooled.

### Severity scale

| Code | Meaning |
| --- | --- |
| 1 | Cosmetic, no effect on the task |
| 2 | Minor, slows the participant, no wrong outcome |
| 3 | Major, needs a hint or causes a recoverable wrong step |
| 4 | Critical, wrong patient, wrong record, data loss or unsafe clinical state |

### Observation template

The observer fills the usability observation form in
`docs/operations/research-capture-forms.md`. It records session id, task
id, role and device stratum, outcome code, elapsed seconds, hint count and
severity codes. It holds no names, no free-text quotes and no clinical
content.

### Consent to participate

Before the session the owner reads the participation notice to the
participant and records only a yes or no consent flag against the session
id. Participation is voluntary. The participant can stop at any time and
the session is then recorded as withdrawn, with the attempts so far kept in
the denominator. No audio, video or screen recording is made unless the
owner adds it under a separate, written consent; this protocol assumes none.

### Analysis

For each target and each stratum cell, report numerator, denominator and
the proportion with a Wilson 95% interval. A cell with no attempts is
reported as 0 of 0, not omitted. Severity 3 and 4 findings are listed by
task id with their counts and denominators.
<!-- prereg:end -->

## Recording results

Results go only into a copy of `docs/research/results-template.md`, filled
from real sessions. Every row must carry its denominator.
