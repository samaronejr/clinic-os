Todo 62 implementation commit is `9ecbebd4a5f755c7365be5227f687f1f3dd04c20` over reviewed candidate
`3f3a9fec7b36346c1a3dc11c2671a7514fd7245f` on `ulw/cpi-e-t62`. The worktree is clean.

Marketing preparation resolves the current verified contact and the final locked
eligibility check requires its fingerprint to match the conversation's immutable
recipient identity. Replacement, verification and fresh opt-in cannot revive an
old-address operation. Both before-execution and after-preparation races were
confirmed red, then green, with no old-address transport and a fresh-B success.

Native results: 350 affected comms/CRM cases passed,
including all 37 CRM cases; CRM coverage 94.11%.
The complete applicable CI-serial cohort passed 3667
cases across all 24 required prefixes.
All runs use `junit_family=xunit1`; no skips or warning suppression.
Ruff, formatting and strict mypy passed.

`review-fix-r1/NATIVE-JUDGMENT.json` binds the raw XML and actual collected node
identities. `review-fix-r1/REVIEW-FIX.json` and `COMMIT.json` identify the fix.
Prior claims remain in `review-fix-r1/prior-claim/`; historical browser reports
retain their original lineage and provide no final-current-head clearance.

The authenticated owned review database, anonymous volume, environment link,
credentials and raw logs were removed; unrelated resources were preserved.
Current cleanup is `review-fix-r1/cleanup.json`. Final browser, native R and
upgrade gates remain root-owned and deferred. The review's nonblocking notes and
full dynamic PHI-sentinel sink receipt remain outside this B1 change; the privacy
audit is a credentials check. Real legs remain BLOCKED-ON-EG.

Review history in `gate-review-r1.md` is preserved: needs-fix, the recorded consent fix, then confirmed-pending-R. Approval is not claimed. Current machine
receipts are `DoneClaim.json`, `VERIFICATION.json`, `campaign.json` and
`takeover/canonical-ready.json`.
