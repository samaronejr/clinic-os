+++
protocol_id = "scribe-effort-v1"
kind = "scribe-crossover"
state = "pre-registered"
external_gate = "EG-10"
gate_status = "BLOCKED-ON-EG"
results_template = "docs/research/results-template.md"
capture_form = "docs/operations/research-capture-forms.md#scribe-encounter-timing"
prereg_sha256 = "1c60ccbfce938fa0392131608f2c2b92eba7aed72a3381e9bcfdf4dfd9250555"

[[targets]]
id = "documentation-effort-reduction"
label = "proposal"
metric = "relative reduction in mean total documentation plus correction time per encounter, scribe arm vs manual arm"
comparator = ">="
value = 0.30
numerator = "per-clinician mean manual-arm minutes minus scribe-arm minutes"
denominator = "per-clinician mean manual-arm minutes, over clinicians completing both periods"

[[targets]]
id = "significant-omission-rate"
label = "proposal"
metric = "share of scribe-arm finalized notes with a clinically significant omission or hallucination found on review"
comparator = "<="
value = 0.0
numerator = "scribe-arm finalized notes with at least one significant omission or hallucination"
denominator = "all scribe-arm finalized notes reviewed"

[sample_size]
label = "proposal"
unit = "clinician completing both periods"
design = "two-period two-sequence crossover, paired analysis"
alpha_two_sided = 0.05
power = 0.80
z_alpha = 1.96
z_beta = 0.8416
baseline_minutes_assumption = 10.0
target_reduction_fraction = 0.30
sd_paired_difference_minutes_assumption = 4.0
dropout_assumption = 0.20
encounters_per_period = 10
required_completers = 14
enrollment_target = 18
+++

# Scribe documentation-effort study protocol (sec.11 scribe target, EG-10)

Status: pre-registered protocol, no sessions run. This file holds no
results and no participant counts. Sessions are owner-arranged with
existing clinic contacts under EG-10, which stays BLOCKED-ON-EG until the
owner records them. Nothing here satisfies EG-10, EG-11 or any other gate.

The 30% reduction target from sec.11, the sample size and every number in
the assumptions are **proposals**. None of them is a measured value.

<!-- prereg:begin -->
## Pre-registered design

### Design

Paired two-period crossover. Each clinician documents a block of
encounters in the manual arm and a block in the scribe arm. Sequence (manual
first or scribe first) is assigned by a coin flip the owner records against
the session id before period 1. A one-day washout separates the periods
(proposal).

Encounters are scripted synthetic role-plays on synthetic patients, unless
and until EG-11 provides a lawful corpus. The scribe arm uses the drafting
path; the clinician always reviews and finalizes, and the manual path is
always available.

### Primary outcome

Total documentation plus correction time per encounter, in seconds, from
the end of the conversation to note finalization. In the scribe arm this
includes draft review and every correction. It's measured by the observer
with a stopwatch and cross-checked against the app's draft and finalize
timestamps for the same session id.

### Secondary outcomes

- Clinically significant omission or hallucination rate in finalized
  scribe-arm notes, judged by a second clinician against the role-play
  script.
- Count of corrections per scribe draft.
- Medication name or dose, allergy and negation errors left in finalized
  notes, per arm.
- Participant workload score (NASA-TLX raw, proposal) per period.

### Sample size

The unit is a clinician who completes both periods. With a paired
difference, n = ((z_alpha + z_beta) x sd / delta)^2, where delta is the
target reduction fraction times the assumed baseline minutes. Using the
front matter assumptions (alpha 0.05 two-sided, power 0.80, baseline 10
minutes, 30% reduction so delta 3 minutes, paired SD 4 minutes) gives 14
completers. Allowing for 20% dropout gives an enrollment target of 18. Every
input is an assumption to revisit with the owner; none came from data.

### Analysis plan

Per clinician, take the mean primary outcome per arm. Test the paired
difference with a paired t-test, report the mean difference, its 95%
interval and the relative reduction against the manual-arm mean. Report a
period effect and a sequence effect as secondary checks. Clinicians who
don't finish both periods are listed with their count and excluded from the
paired analysis, and the denominator says so. Secondary outcomes are
reported as counts over their denominators with Wilson 95% intervals.

### Stopping and safety rules

- Stop the scribe arm at once if any finalized note carries a clinically
  significant error that would reach a patient. With synthetic role-plays
  no patient is exposed, but the finding is still recorded as severity 4.
- Stop the whole study if the AI provider or drafting path is unavailable
  for a period. The affected period is recorded as void, not imputed.
- A participant may withdraw at any time without giving a reason.
- No interim efficacy analysis. The analysis runs once, after the last
  period.
<!-- prereg:end -->

## Recording results

Results go only into a copy of `docs/research/results-template.md`, filled
from real sessions, with a denominator on every row.
