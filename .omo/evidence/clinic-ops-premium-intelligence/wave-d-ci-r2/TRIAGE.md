# PR 30 hosted CI triage, round 2

Date: 2026-11-06.
Requested head: `d3bb5356ed874a5d299897c1dcfb2f1c1b9a6924`.
Fix branch: `ulw/cpi-d-ci-fix`.

## Artifact findings

The messages failures are in the accessibility test harness, before the urgent
conversation's POST-selection journey. They are not failures of the new
`action=show` transport. Chromium and WebKit pass all 16 messages tests in both
runs. Firefox passes 15 and fails the same remaining test in both runs.

Artifacts were downloaded with `gh run download` from
`samaronejr/clinic-os`: `renewal-browser-16` from both runs and
`renewal-browser-12` from both runs, including the passing comparison. Job logs
were retrieved with `gh api repos/samaronejr/clinic-os/actions/jobs/<job>/logs`.
The cancelled jobs explicitly report that no suite artifact was uploaded.
The acceptance jobs publish no separate JUnit or browser trace.

These browser artifacts contain redacted server/access logs, JUnit, axe JSON,
and synthetic screenshots/videos; they contain no Playwright trace archive.
The report uses the JUnit and redacted logs rather than claiming a trace was
available. Downloaded media and complete logs remain in the private triage
directory and are not committed. Only reviewed, redacted excerpts and digests
are included here. `hosted-sources.json` records artifact IDs and SHA-256
digests of the inspected JUnit and job logs.

The push run tested `d3bb5356ed874a5d299897c1dcfb2f1c1b9a6924`; the pull-request
run tested merge commit `305361b6a06d6d3b4ac7f9b3b0dffdd9fdcb6408`.
Their report files identify the same source tree:
`c77b4702853c67fa03b4154a47b42a8c08f1934e`.

| Run | Job | Classification | Evidence and disposition |
| --- | --- | --- | --- |
| 42234616332 | 127948902573, messages on three engines | Real test-harness defect | Firefox's urgent-queue test rejects the axe script's HTTP 304 response. Chromium and WebKit are 16/16. Fix the shared loader. |
| 42234626845 | 127948937332, messages on three engines | Same real test-harness defect | Same Firefox test, stack, and terminal HTTP 304. Chromium and WebKit are 16/16. |
| 42234626845 | 127948937824, WebKit agenda and clinical shard | Intermittent accessibility failure | `test_board_zoom_reflow_and_reduced_motion[4]` reports 12 serious color-contrast findings in the dark compact day view. The identical tree passes this scene and all 117 shard tests in run 42234616332. Preserve the assertion and recommend rerunning this job. |
| 42234616332 | 127948902201, scribe-capture on three engines | Hosted environment/download timeout | Cancelled while `playwright install --with-deps` was still downloading Ubuntu packages. No test ran and no suite artifact exists. Recommend rerun. |
| 42234616332 | 127948902659, primitives/workspace and related WebKit suites | Hosted environment/download timeout | Cancelled during the same package-download step, with repeated archive download attempts. No test ran and no suite artifact exists. Recommend rerun. |
| 42234616332 | 127981026552, RC acceptance | Downstream aggregate failure | Reports the unsuccessful browser job group and missing reports for Firefox messages plus the nine suites in the two cancelled jobs. No independent RC defect. |
| 42234626845 | 127975251909, RC acceptance | Downstream aggregate failure | Reports the unsuccessful browser job group and missing `agenda-grid@webkit` and `messages@firefox` reports. No independent RC defect. |

### Both Firefox messages failures

The exact failing test is
`test_an_urgent_portal_reply_reaches_the_accountable_urgent_queue`:

```text
tests/renewal/browser/test_messages.py:1369
    _publish_coverage(page, base, staff, root)
tests/renewal/browser/test_messages.py:1114
    report = check_page(page, base, root, f"messages-urgent-coverage-{width}")
tests/renewal/browser/a11y_support.py:101
    assert axe_response.value.status == OK_STATUS
E   AssertionError
```

The pull-request run's last application response is a successful
`workflows:urgent-coverage` POST at `2026-11-06T14:38:04.510986Z`. It is followed
by an unmatched GET with status 304 at `2026-11-06T14:38:04.761845Z`. The redacted
access log ends with:

```text
POST 200 64620
GET 304 706
```

The failing helper subscribes to the exact vendored axe URL. Its script load
completed, but Firefox exposes the conditional cache response as 304 rather
than the 200 the helper requires. The runner serves static files through
WhiteNoise with `WHITENOISE_AUTOREFRESH=True`; the helper reused one asset URL
across documents. The existing scribe transcript test already strips conditional
headers locally for this same shared-loader requirement.

The fix gives each document's first axe injection a fresh, test-only static
asset URL. Later audits on that document still reuse the loaded script. This
preserves the exact HTTP 200 assertion, CSP, the real supervised server, all axe
rules, and all serious/critical failure checks. The new smoke regression checks
two documents and two audits per document, exactly one fetch per document, two
distinct URLs, identical nonempty script bytes, HTTP 200, and absence of cache
validators. No conversation selector, patient value, credential, or clinical
content enters the cache nonce.

Rejected alternatives: accepting 304 unconditionally would relax the existing
origin-response assertion; retrying or increasing timeouts cannot change a
successful cache revalidation; changing comms views/templates would not address
this failure. SC-7 and all POST-selection assertions remain intact.

### WebKit contrast intermittency

The only failing case among 117 in the pull-request shard is:

```text
tests/renewal/browser/test_agenda_grid.py:663
    assert _axe(page, board.base_url, board.root, name) == []
test_board_zoom_reflow_and_reduced_motion[4]
```

`agenda-grid/axe-day-zoom-400-dark-compact.json` reports `color-contrast`,
impact `serious`, count 12, including `#agenda-title`, the displayed day, and
the unit/professional/resource filter labels. The same scene's JSON in the
push run is `[]`. All other 116 cases in the failed shard pass. The dark-theme
screenshot was inspected and displays a populated board.

This same 12-node dark-theme WebKit finding was previously documented at
`root/hosted-39493763136-webkit/ATTRIBUTION.md` on older Wave B trees. Its prior
passing/failing alternation and this same-tree comparison support classifying
the result as intermittent. The artifact omits computed colors and ratios, so
it cannot prove a harmless axe false positive or exclude a real transient
rendering defect. No CSS change, accessibility suppression, or retry was added.
The existing assertion should be rerun, and a repeat failure should retain
computed color/ratio evidence for a separate rendering diagnosis.

### Environment and aggregate evidence

Job 127948902201 was still downloading `libgstreamer-plugins-bad1.0-0` at
`15:31:22Z` and was cancelled at `15:31:25Z`. Job 127948902659 was retrying
`libgtk-4-common` and downloading `libgtk-4-1` immediately before cancellation
at `15:31:56Z`. Both were in the Playwright prerequisite step, not pytest,
and both check-run annotations explicitly say:

```text
The job has exceeded the maximum execution time of 1h0m0s
```

Both started at `14:31:08Z`; their completion timestamps are `15:31:28Z` and
`15:32:00Z`. Both upload steps state:

```text
No files were found with the provided path:
/home/runner/work/_temp/renewal/. No artifacts will be uploaded.
```

The RC validator's errors are exactly the failed browser group plus missing
success reports. For run 42234626845:

```text
renewal-acceptance: required job renewal-browser did not succeed (failure)
renewal-acceptance: suite agenda-grid@webkit: expected exactly one report, found 0
renewal-acceptance: suite messages@firefox: expected exactly one report, found 0
```

Run 42234616332 additionally lists all three scribe-capture variants and
`locale@webkit`, `onboarding@webkit`, `patient-workspace@webkit`,
`primitives@webkit`, `self-booking@webkit`, and `workspace@webkit`.

## Local verification

Ruff check and format check pass for both changed source files. Strict mypy
passes for both files with exit 0.

The new smoke regression was first run on Firefox with the original loader.
The canonical runner produced 25 passing cases and one failure: the new
regression found one script URL across two documents where two independent
fetch URLs are required. The runner exited 2 for pytest's exit 1. This is a
deterministic baseline failure of the cache-isolation contract; the HTTP 304
failure itself is established by the two hosted messages runs above.

An earlier local setup attempt exited before any test because the private
clone's 0700/0600 source permissions prevented the PostgreSQL container user
from reading its initialization bind mounts. Restoring read/traverse permissions
only on the clone's public `ops/db` source directory allowed initialization and
the baseline test run to complete. It is not counted as a browser failure or pass.

The three messages runs are green: 16/16 on each of Chromium, Firefox, and
WebKit, with zero failures, errors, or skips. Agenda-grid is green on all three
engines: 11/11 each, including the previously failing WebKit dark compact
400% scene, whose saved axe report is `[]`. These six canonical reports have
`pytest_exit=0`, `runtime_role=clinic_app`, and source manifest digest
`2d9e28e54a86c3bacdc564850f0d6af73301f137db4dc7b4b87693cbbf56261f`.
The desktop enhanced urgent-thread screenshot (1280 px, Chromium) and the native
mobile screenshot (375 px, WebKit) were inspected: both are populated and the
thread, composer, controls, and internal notes remain readable without overlap.

The initial Firefox smoke run passed 26/26. Chromium then exposed a mistake in
the new regression: it read the first document's response body after navigation,
when Chromium had discarded the network resource. That run was 25 passed and
one failed, with `Network.getResponseBody: No resource with given identifier
found`. The regression now captures each document's bytes before navigating;
the equality, nonempty-body, HTTP 200, unique-URL, and cache-validator assertions
all remain. Only the smoke test changed; the shared loader and the six passing
messages/agenda-grid suite inputs remain unchanged.

The corrected smoke test passes 26/26 on every engine. Its canonical source
manifest digest is
`80292832c8152420f424cc68fa6f12344296e70ca781b3bbcd7ed9a36d254470`.
Ruff, format, and strict mypy checks were rerun for that changed file and passed.

| Suite | Chromium | Firefox | WebKit |
| --- | --- | --- | --- |
| messages | 16 passed | 16 passed | 16 passed |
| agenda-grid | 11 passed | 11 passed | 11 passed |
| smoke, including the corrected regression | 26 passed | 26 passed | 26 passed |

Final qualification: **nine runs, 159 passed, zero failures, errors, or skips**.
Every run has a successful native report, `clinic_app` runtime role, matching
JUnit, and process exit 0. `browser-results.json` records each JUnit digest, both
current-source snapshot identities, and the final changed-file hashes.
The reports name the base revision/tree because this is the supported
uncommitted current-source runner; the changed-file hashes bind the verified
bytes to the fix committed with this evidence. No application code, comms
selector, agenda CSS, or assertion threshold was changed.

Verification used the private clone
`/home/samarone/.cpi-d-ci-r2-st01a1b180/verify`, with its own locked virtual
environment and a fresh tracked-plan isolation ledger. It uses no other lane's
database. Each heavy unit is capped at 8 GiB with `MemorySwapMax=0`; an external
resource-only launcher caps its newly created PostgreSQL container at 3 GiB and
its Redis container at 256 MiB, each with total memory+swap equal to memory.
Combined caps are 11.25 GiB. The canonical runner still owns provisioning,
migrations, source capture, tests, and teardown. All published database ports
are random loopback ports other than 5432. No app_mei, axfix, or axrm resource
is used or modified.

The host-wide lock requirement arrived after five runs had completed while
agenda-grid Firefox was already active. Every run started after that point
enters `flock /home/samarone/.cpi-canonical-browser.lock` before the canonical
runner starts, including agenda-grid WebKit and all corrected smoke runs.
Their logs record `BROWSER_LOCK_ACQUIRED`; completed suites were not restarted.

After all runs, the cleanup audit found no task-labelled container, no named
volume for any of the 13 task-owned PostgreSQL creations, no active task unit,
and no outstanding ledger claim. The runner also removed each of its six Redis
containers with its anonymous volume. Private redacted evidence is retained.

The full `make ci` gate and a hosted run of the fix were not executed. The
targeted source checks and all affected browser engines above are the local
qualification; the existing hosted WebKit intermittency remains explicitly
unresolved beyond this successful local run.

## Hosted follow-up

No hosted job was rerun, no branch was pushed, and no PR comment was posted.
After the fix is integrated, rerun the failed messages jobs, the WebKit shard,
and the two cancelled prerequisite jobs, then let RC acceptance consume the
complete success evidence. Local browser verification does not replace that
hosted RC result.
