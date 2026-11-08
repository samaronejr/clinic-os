# Task 63 native Reports QA

Recorded on November 6, 2026, on the lane based on corrected journal commit
`63240e99aad2fa749decefa9e557e4916a00084b`. All data and provider behavior are
synthetic. This is engineering evidence, not clinical, commercial or live
provider qualification.

Native `clinic_app` Django response fixtures were rendered in Chromium using
the shipped styles, HTMX and application scripts. Every HTTP request was
intercepted locally; the check opened no listener. Transient CSRF tokens were
removed from the rendered copies. The native role and command tests separately
exercise real PostgreSQL, requests, CSRF enforcement and authorization.

Reports, saved filters, stale, null and partial data, typed proposal review, errors and the
workflow metric-step editor were inspected at 1280, 640, 375 and 320 pixels.
The pages remain readable without page-level horizontal overflow. The numeric
table retains its own bounded scrolling region. Metric definitions can be
expanded without losing the selected query, and review displays the exact
metric, version, period, timezone, grouping and weekdays.

The 33 screen/viewport checks found no axe violations and no measured report/editor controls
smaller than 44 by 44 pixels. Keyboard navigation from metric to start date
works. Actual HTMX requests expose the busy state, focus the replacement
result, show and focus server errors, recover from an aborted connection with
an enabled retry button, and replace prior report content with an opaque
denial. Native forms retain the manual path when AI or JavaScript is unavailable.

Two observed defects were corrected: the HTMX settle callback retained the
detached pre-swap node, and workflow navigation/summary targets measured
24.8 pixels high. Focus now targets the current report element, and scoped
workflow styles use the existing minimum-control-size tokens.

The retained workspace artifacts are under
`.omo/evidence/clinic-ops-premium-intelligence/task-63/`. The exact axe arrays,
geometry and interaction results are in `manual-qa/report.json`; screenshots
are in the same directory. Native xunit1 reports and catalog/census receipts
are separate artifacts and are not browser attestations.

The canonical `reports` browser suite and full native R are
**deferred-to-host-quiet-batch** by the root coordinator. Earlier canonical
listener-ownership refusals remain retained. No deferred run is reported as
passed. Independent device qualification, real AI providers and cross-clinic
benchmarking remain **BLOCKED-ON-EG**.
