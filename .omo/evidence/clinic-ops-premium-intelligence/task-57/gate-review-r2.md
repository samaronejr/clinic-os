recommendation: REJECT
verdict: needs-fix
reviewDate: 2026-11-06
reviewer: omo-native-gate-reviewer / st_01a1b279
reviewType: Read-only adversarial source and evidence review; no implementation changes.

originalIntent: Deliver todo 57's recorded PIX/card PSP integration and statement reconciliation on corrected todo 56, authenticate callbacks before scope lookup, preserve exact financial authority and journal identity, and recover ambiguous remote effects without another send. Recheck both prior findings using real red/green regressions.

desiredOutcome: Patients receive explicitly non-payable rehearsal instructions and a receipt only after independently verified collection. Finance matches references, gross centavos, currency and fees, posts each economic fact once, and retains unknown outgoing reservations. Real providers remain BLOCKED-ON-EG-1/EG-5.

userOutcomeReview: Both prior production defects are corrected in the supplied commit. The retained runtime receipts cover successful correction, reversal protection, reader/write separation, statement replay, refund replay and broker recovery. One expressly required evidence item is missing: a captured failing regression for the pre-fix same-version statement defect. The supplied red run exercises only the evidence-authority finding. This is an evidence-completeness rejection, not a finding that the corrected statement code is still defective. After that proof is supplied, browser/full-R/upgrade remain deferred as instructed.

scope:
- Candidate: 6a3c7666c9eb5f7736d21afa58e2a7be95425876, branch ulw/cpi-e-t57-r2.
- Parent independently confirmed: 63240e99aad2fa749decefa9e557e4916a00084b.
- Candidate tree: 640557d398505b946326971f9d478931196802a4.
- Worktree: /home/samarone/Documents/clinic-ops-new-roadmap/clinic-os-wt/cpi-e-t57-r2.
- Evidence directory, abbreviated E below: /home/samarone/Documents/clinic-ops-new-roadmap/clinic-os-wt/cpi-e-t57-r2/.omo/evidence/clinic-ops-premium-intelligence/task-57.
- Plan: /home/samarone/Documents/clinic-ops-new-roadmap/clinic-os/.omo/plans/clinic-ops-premium-intelligence.md:104-131 and :1268-1278.
- Reviewed all 64 changed paths against the stated parent, including the semantic inventory changes. The tracked worktree was clean.
- Toolkit status was queried with the requested SDK through Bun because no JS-eval tool is exposed here. It returned ULW_LOOP_PLAN_MISSING. This report uses the task-57 path expressly requested by the caller.

blockers:
- id: T57-R2-B1
  severity: medium
  violatedCriterion: The explicit request to verify BOTH prior findings with real red/green regressions; the plan's captured failing-test-first requirement for money and authorization at .omo/plans/clinic-ops-premium-intelligence.md:104, supported by SC-12 (:123).
  evidencePointer: E/takeover/commands.jsonl:20; E/takeover/red-evidence-authority.log:1-17; E/takeover/green-review.junit.xml:1; tests/billing/test_psp_review.py:123; E/SOURCE_REVIEW_RESOLUTION.md:14-19.
  observation: The only expected-red command is red-evidence-authority. Its two failures are the clinic_manager and owner cases of test_financial_read_authority_cannot_fabricate_another_actors_evidence, both failing with DID NOT RAISE DatabaseError. Across every retained JUnit file, test_corrected_statement_at_the_same_provider_version_can_post appears only as PASS in green-review.junit.xml and affected.junit.xml. No pre-fix failure, mutation receipt, or command for that second regression is retained.
  requiredAction: Capture the existing statement-correction regression failing for the intended reason against the pre-fix SQL/posting owners in an isolated test snapshot, with command, exit code, source/schema identity and JUnit/log paths. Pair it with the already retained corrected-source green receipt and reference both from SOURCE_REVIEW_RESOLUTION.md. This reviewer did not modify source or create a database to manufacture that missing proof.

priorFindingsReview:
- T57-B1 source fix confirmed at apps/billing/migrations/_psp_guard_sql.py:128. Binding and evidence INSERTs now require intent.requested_by_id to equal the actor GUC before the broader read predicate can authorize them. The existing read scope remains useful without becoming another actor's write authority. tests/billing/test_psp_review.py:72-120 exercises binding and evidence inserts for known and unknown intents, asserting the same SQLSTATE 42501 and billing access denied message. Its final three-role cases distinguish a manager reader, an owner, and a different Finance collector. The two-reader pre-fix red log and three-role corrected green JUnit are present and consistent with the test.
- T57-B2 source fix confirmed at apps/billing/migrations/_psp_guard_sql.py:158 and apps/billing/psp_posting.py:153. Both freshness comparisons exclude exactly source=statement, decision=quarantined. A corrected distinct statement line at the same authoritative version can therefore match and post. The excluded row still retains its actual mismatching statement amount. tests/billing/test_psp_review.py:123-153 asserts bank 9900, fees 100, clearing 0 and one receipt after correction. The separate newer authoritative-reversal control at :156 prevents a stale verified fact from posting. Both controls have green runtime receipts; the missing red receipt is T57-R2-B1.
- SOURCE_REVIEW_RESOLUTION.md accurately describes the changed predicates and the narrower red proof it actually names. Its 33-test focused green claim and final three-role claim agree with JUnit. It does not supply a second pre-fix red proof, so it cannot establish the full standard requested in this review.
- The rotation correction at apps/billing/psp_gate.py:44-72 separates new effects from lookup-only use of the originally approved version. Fresh sends still require the current approved fixture version; lookup requires the stored version to remain approved sandbox, with the correct capability, clinic/global scope, provider, environment and account. A revoked stored version fails the state check. The green rotation regression retains one effect and one send attempt. This is source/retained-evidence confirmation, not a newly executed database test.

acceptanceReview:
- PaymentProvider has all five requested effect/query operations plus exact raw-byte authentication at apps/billing/psp_contracts.py:102. RecordedPaymentProvider is the only accepted implementation; runtime registration rejects other implementations. PIX creation returns a SYNTHETIC-NOT-PAYABLE code, cards use payments.invalid, and both remain pending. Clinic policy freezes installments, with PIX restricted to one. No creation path writes a settlement or receipt.
- Callback ordering is explicit at apps/billing/psp_flow.py:61-70: authenticate, resolve the stored provider/reference pair, query independent status, classify, then record. apps/billing/psp_fixtures.py:83-133 verifies HMAC over exact raw bytes before JSON parsing and projects only allowlisted financial fields. Payload tenant/organization/clinic claims do not enter the stored scope. Signed amount/currency/status/version disagreements cannot verify. Raw-body and malformed-input tests were independently rerun.
- Status and statement queries occur outside transactions. Prepared intents/outbox rows commit before dispatch; the adapter reloads immutable scope and the digest, then rechecks dispatch eligibility. Evidence locks use clinic/audit/intent ordering, and provider/source/event identity is unique. Stale and contradictory authoritative observations cannot silently replace newer facts.
- Exactly-once financial lineage is shared with todo 56. apps/billing/psp_posting.py:32 calls observe/verify/post/allocate using the intent identity. collections.py reuses the settlement evidence identity and allocation command identity; its guarded SQL writer owns collection posting. Statement transfer calls the public post_entry facade at psp_posting.py:87 with a stable uuid5 fact per intent. Refund/payout confirmation uses the existing transition writers and their shared guarded posting core, rather than a second ledger implementation. Receipt/settlement/allocation uniqueness and stable source identities prevent reconciliation replay from posting again.
- The actual journal assertions in test_psp_statement.py:35-77 and test_psp_refunds.py:41-139 establish one clearing/fee transfer and one refund/repasse reversal. E/pix.json, E/refund.json and their matching Manual-QA JUnit/test source agree. These are stronger than the in-memory EventStore's self-implemented replay behavior.
- apps/core/integration.py:611-628 treats only registered in-progress ACTION recovery as lookup recovery, without consuming another send attempt. :919-922 catches AmbiguousSendError without resetting the operation to pending. :1037-1044 selects the registered recovery adapter. The PSP recovery branch calls lookup and no mutating method, retaining the original key even for not-found/unknown results. Dedicated operation/subject/boundary locks remain in the flow. LSP found the changed _send_claimed definition and both inspected call sites.
- The required broker receipt is a real Redis/PostgreSQL Celery gate, not the pure multiprocessing substitute. tests/billing/psp_broker_app.py:33-55 fsyncs an external fixture effect before SIGKILL. The real broker test subscribes before publish, waits on the result, and asserts one create attempt, one outbox send attempt, success and one verified fact. The final environment binding fixes the prior memory-transport timeout without skipping the test. E/takeover/psp-broker.junit.xml contains one passing, unskipped test; commands.jsonl records exit 0 and the required-gate runner sets CLINIC_BROKER_GATE=required. This gate was inspected, not rerun after its private resources were removed.
- Synthetic/live exclusion holds. The setting defaults off, PspCapability factories return real_enabled=False, the rehearsal gate rejects live/invalid modes, and both new environment variables are refused by live activation. The existing provider README:86 documents psp_pix as the canonical pix alias; cards use psp_card. No network provider, paid call, credential provisioning, lifecycle approval or EG clearance was added. BLOCKED-ON-EG-1/EG-5 remains explicit.
- Patient output uses the released-invoice/session/delegate resolver and encrypted instruction envelopes. A sealed PspReceipt determines paid/receipt presentation. The template labels the code/link as non-payable and displays clinic installments. The native partial checks and patient alias test are present. The four browser cases cover PIX/card at desktop/mobile widths, including a native-form baseline, but have no successful browser qualification here.

censusAndPostureReview:
- Independently reran identity.legacy_guard_inventory.discover: its 835 symbol/signal pairs exactly match the delivered manifest.
- Compared the manifest semantically with git show 63240e99:tests/identity/legacy_guards.json. All 824 inherited Python records and role_calls are identical. All 465 inherited SQL classifications retain every non-discovery field. The 11 Python additions are apps.billing.psp_* only. The seven SQL additions are the six PSP functions plus billing_patient_psp_instructions.
- Existing discovery differences contain only added PSP policies/callers: billing_patient_charges, identity_session_verified, billing_book_access, billing_finance_session and billing_lock_book. No inherited dependency was removed and no non-PSP caller was added.
- Read the discovery generator rather than trusting its receipt. It invokes repository Python discovery, deployed SQL discovery, and the live relation classifier; refuses other-lane additions/removals; and emits separate generated artifacts. Independently confirmed byte equality of all three generated outputs with tests/identity/legacy_guards.json, ops/testing/restore_contract.py and tests/isolation/test_backup_restore_guard.py. Fresh SQL/relation discovery was not rerun because the private database was removed.
- The retained catalog contains 223 classified domain relations, including exactly five PSP tables. Independently asserted from E/takeover/live-catalog.json that all five are FORCE RLS and owner-owned; clinic_app has exactly SELECT/INSERT, clinic_resolver exactly SELECT, clinic_agent no table grants. All seven PSP functions are SECURITY DEFINER, owned by clinic_resolver, use pg_catalog,clinic_app,pg_temp search_path, and have the exact callable/trigger ACLs. No clinic_owner EXECUTE remains. Source and independent test pins agree.
- Migration 0012 is additive and transactional. It flushes deferred FKs before helper-dependent policies, temporarily removes FORCE on four owner-owned referenced tables, and restores it inside the transaction. Runtime mutation/delete/truncate grants are not expanded. Immutable trigger coverage includes all five new tables.
- The known billing_definition_guard inactive-actor probe gap remains an inherited todo-56 item. Todo 57 does not change tests/identity/test_sql_inactive_actor.py. No new PSP SQL source reads is_active. The only occurrence in changed production Python is the unchanged core _actor_has_clinic_authority helper at integration.py:555; the diff adds no such reader. No todo-57 registration defect was found in this class.

privacyReview:
- New outbox rows carry scoped IDs, fixed provider/subject vocabulary, the original idempotency key and a digest. They carry no raw callback, payment code/link, bank/card data, secret or clinical content. AmbiguousSendError stores no exception message. Registered PSP audit events use the existing fixed metadata contract, and there is no new product logging sink.
- Read Manual-QA's real probe source and the matching four result artifacts. It sends a synthetic clinical sentinel in an extra callback field and checks audit, outbox and captured logs; the projection ignores that field. It also checks foreign-organization reads and runtime/privileged immutable-history refusals. These receipts support that tested scenario, not a claim that every runtime sink was exhaustively scanned.
- Independently scanned 148 retained text artifacts for credential-bearing PostgreSQL URLs and private-key headers. Twelve URL-shaped hits were all existing literal negative-test parameters in affected.junit.xml; each matched the original test_live_activation.py fixture literal. They were not runtime credentials. No private-key header or runtime-password leak was found by this scan. The retained private-runtime file is absent; the cleanup receipt records its removal and zero password leaks. No secret value was copied into this report.

independentChecksExecuted:
- PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -q --tb=short --no-showlocals tests/billing/test_psp_adapter.py tests/billing/test_psp_policy.py tests/renewal/test_live_activation.py::test_live_preflight_rejects_invalid_environment tests/infra/test_module_boundaries.py::test_psp_human_boundaries_derive_authority_from_current_context: exit 0, 52 passed.
- git diff --check 63240e99 6a3c7666: exit 0.
- .venv/bin/ruff check --no-cache .: exit 0, all checks passed.
- .venv/bin/ruff format --check .: exit 0, 2396 files already formatted.
- .venv/bin/mypy .: exit 0, no issues in 2396 source files.
- Programming skill check-test-junk.ts over every changed Python test path: exit 0, no regex-detectable findings. A separate direct semantic test review was performed.
- SHA256SUMS verification: all 148 listed files exist and match. This confirms retained bytes, not the truth of arbitrary success prose.
- Parsed every retained JUnit file. The correction test has two green receipts and no red receipt. Authority has two pre-fix failures and final three-role greens. Reconstructed 366 unique affected cases from the 365-case run plus the three-role run, and 109 unique serial passes from 108 initial passes plus the xunit1 retry. The initial strict-warning fixture error is retained and explained, not counted as an initial pass.
- Confirmed the component checkpoint 68fb3ad73ae18d39d4666bae5de2ee86670ee5da has no application/config/template/static/locale/activation delta from the candidate. This supports the reused component receipts after the corrected todo-56 rebase. Metadata/test deltas have the later affected/serial/static receipts.
- Read the compiled GNU translation catalog: all four new PSP msgids have translations. Frozen foundation plan and its correctly named .sha256 sidecar, pyproject.toml and uv.lock are byte-identical to the parent.
- LSP diagnostics: psp_posting.py returned no diagnostics. psp_worker.py returned pluginless BasedPyright unknown Django-generated FK attributes and related warnings. They were not suppressed or represented as clean; the project's Django-aware strict mypy gate passed.
- Two inspection scripts initially failed because of overly narrow discovery-delta assumptions and a misnamed foundation sidecar. Adjusted checks established PSP-only policy/caller additions and checked the actual .sha256 path. The credential-pattern scan's initial hits were resolved against public test literals. None of these inspection failures was a product test failure.

programmingAndRemoveAiSlopsReview:
- Read both installed skill files, Python reference, testing authoring gate and junk-class table. Applied them directly to the candidate diff, production owners and tests. Read-only scope excludes cleanup, test cuts, production mutations and refactoring.
- Existence/reuse: the PSP layer uses the existing journal, finance scope, step-up, outbox, provider registry and patient resolver. No parallel ledger, network stack, generic extraction framework, speculative transport or unrelated parsing/normalization layer was introduced. HMAC/JSON parsing is at the provider boundary; exact financial checks remain at their trust boundaries.
- Obvious comments: no consequential new vague TODO/debug leftover found. Comments explaining non-payability, lock ordering, authoritative evidence and lookup-only recovery carry real contract context.
- Excess defense/complexity: the exact-type, currency, bound, signature and scope checks are boundary checks, not proven redundant guards. String variant branching and guarded casts remain consistent with the existing Django contract; they are modeling notes, not a basis for an unrelated rewrite in this gate.
- Abstraction/boundaries: the pure provider/store protocols have real orchestration and database adapters. The new recovery registration has a production PSP caller. Single-use collection/transfer helpers express distinct financial operations; no test-only production export or flag was added.
- Dead code/duplication/performance: no new dead provider transport or redundant production normalization found. The existing patient projection does make additional per-invoice resolver calls; no performance criterion here supplies a failing measurement. This is not an approval blocker.
- Tests: no tests were deleted, weakened, made deletion-only, or added merely to certify a requested removal. Exact catalog/signature/security pins are retained independent contracts. No tautological or implementation-mirroring test is the sole proof of the shipped money outcome. Every newly introduced declaration is retained under its contract as recorded below. No D/C proof or test deletion is claimed.
- Direct overfit note: test_psp_adapter.py:189 exercises match_statement, but its following asdict/fee assertions simply inspect input fixture values. The real writer/fee preservation keeper is test_psp_statement.py:35. Likewise, EventStore's replay implementation is test code; its duplicate counts are not accepted as proof of database idempotency. The real refund/statement/outbox keepers supply that proof.
- Size: all new PSP production modules are below 250 pure lines. psp_services.py is 207 and _psp_schema.py is 201. Existing enlarged integration/presentation/catalog modules remain a maintenance note, not an invented financial success criterion. No scope-expanding split is requested.
- Existing review coverage confirmed: E/source-review.md explicitly contains programmingAndRemoveAiSlopsReview, all ten slop categories, test-ledger coverage, fixture-value/mock replay limitations, no deletion/skip/weakening claims, and missing owner coverage for both original findings. It reviews the superseded snapshot, not this commit; this report directly checked the added regressions and final diff rather than treating that prior prose as approval.

newTestLedger:
| File / declarations | Mark | Independent contract / credible regression |
| --- | --- | --- |
| test_psp_adapter.py: forged raw body; raw-byte signature; signed malformed input is separately in policy suite | R | Raw-byte authentication before resolution; parsing/reencoding or moving resolve earlier breaks the contract. |
| test_psp_adapter.py: callback tenant/replay; callback disagreements | R | Tenant-field projection and independent terms/version classification. Fake replay alone is not DB proof. |
| test_psp_adapter.py: recovery not-found; pending creation; card link | R | No mutating recovery, no paid-on-create, invalid-host card instructions. The card test's name alone does not establish all installment persistence. |
| test_psp_adapter.py: installment rejection; PIX single installment | R | Exact clinic-policy and method boundaries. |
| test_psp_adapter.py: three-field statement mismatch; gross/fee matcher success | R | Reference/amount/currency matching; fixture-only assertions are noted above. |
| test_psp_adapter.py: SIGKILL process recovery | R | Real process death with retained key/effect; distinct from full broker lifecycle. |
| test_psp_policy.py: refund callback; lookup timeout; exact money inputs; malformed signed events | R | Refund classification and provider-boundary refusals, with real DB refund keeper elsewhere. |
| test_psp_outbox.py: timeout recovery; lifecycle/real exclusion; live callback refusal | R | Real outbox state/attempts and configuration security boundaries. |
| test_psp_reconciliation.py: gross once; amount mismatch | R | Actual collection/allocation identity and nonposting of mismatches. |
| test_psp_statement.py: reimport once; actual mismatch amount | R | Actual journal balances, stable transfer fact and retained rejected observation. |
| test_psp_refunds.py: authenticated replay | R | Finance reservation/confirmation and one refund/repasse reversal. |
| test_psp_posture.py: exact catalog; absent/foreign organization | R | Migration/RLS/ACL/trigger and tenant-read security. |
| test_psp_patient.py: native partial cases; survivor/unmerge | R | Shipped non-payable UI/i18n and canonical released-patient authority. |
| test_psp_review.py: three reader roles; same-version correction; newer reversal; rotated recovery | R | Exact original security/money regressions and lifecycle control. Missing captured red for correction is T57-R2-B1, not a reason to cut the test. |
| test_psp_broker.py: required SIGKILL/redelivery | R | Real broker/PG/prefork lifecycle with a durable fake remote effect. |
| browser/test_billing_psp.py: PIX/card desktop/mobile cases | R | Real patient surface, native baseline and receipt transition. Execution deferred. |
| test_module_boundaries.py: nine PSP signature cases | R | Required public authority/signature architecture invariant. |

notes:
- tests/conftest.py:130-144 still does not copy _ACTION_RECOVERY_ADAPTERS with the other per-test registrations. The original source review recorded this isolation risk. No order-dependent failure was reproduced, and it is not promoted to a financial acceptance blocker.
- Existing broad modules and the two pure tests' fixture/fake replay limitations are maintenance notes. Stronger real-boundary keepers are present; no cleanup is required by this review.
- The caller supplied no notepad path, and none was found in task-57 evidence. No success conclusion relies on a notepad or unstated parent-session history.

standardContractDisposition:
| Criterion | Disposition |
| --- | --- |
| SC-1 | Human financial writers derive exact clinic authority from GUCs and use keyword-only scope. Unknown/wrong-clinic service queries use the same denial; known/unknown evidence inserts have the exact opaque-denial regression. Stored callback/worker entrypoints use recorded scope. |
| SC-2 | Five TenantScopedModel tables, FORCE RLS, app/global catalog integration, resolver ownership and exact restricted final grants are present and consistent with retained catalog/test receipts. |
| SC-3 | No new app. |
| SC-4 | Four PSP event definitions and same-commit catalog pins; journal/outgoing state transitions use existing registered audit writers. |
| SC-5 | Immutable insert history, delete/truncate guards and encrypted instruction fields; additive forward-only schema. Upgrade/restore qualification is not claimed. |
| SC-6 | Authenticated callback ordering and commit-before-send outbox flow confirmed; stored-actor write fix closes the prior alternate write path. |
| SC-7 | No new PHI/secret sink found; synthetic sentinel scenario and bounded retained-artifact credential checks inspected. Full sink scan remains deferred with R. |
| SC-8 | Native patient partial, English msgids, pt-BR source/compiled catalog and registered browser source present. Visual/a11y/browser evidence deferred. |
| SC-9 | Independent pure/static gates green; retained targeted/fresh-migration/drift/posture/broker receipts inspected. Full R/browser/upgrade explicitly deferred by this request. |
| SC-10 | Existing billing suite extended; no new suite or pinned suite-list change. Existing three-engine shards continue to own the billing suite. |
| SC-11 | Foundation plan and .sha256 byte-identical to parent. |
| SC-12 | Artifact locations, commands, JUnit and cleanup receipts present; missing captured correction red is T57-R2-B1. No screenshot pass claimed. |
| SC-13 | One requested todo commit with Conventional Commit and all Lore trailers; complete release-green status is not claimed while native gates remain deferred. |
| SC-14 | No EG cleared; recorded-only code refuses live processing and new effects require approved current fixture sandbox. |
| SC-15 | Relevant exact posture/authority/idempotency/money/degraded scenarios are covered as described; no fabricated full coverage/browser/recovery qualification. |
| SC-16 | Recorded local adapter only; external sandbox/provider legs BLOCKED-ON-EG-1/EG-5. |
| SC-17 | Upgrade excluded/deferred by the explicit current request; not a todo-57 blocker in this review. |
| SC-18 | Changed private send signature and its callers inspected through LSP; nine PSP public signature pins pass. |
| SC-19 | No dependency additions; manifests unchanged and retained dependency audit reports no known vulnerabilities. |

checkedArtifactPaths:
- E/source-review.md, E/SOURCE_REVIEW_RESOLUTION.md, E/DoneClaim.md, E/DoneClaim.json, E/Manual-QA.md, E/SHA256SUMS.
- E/pix.json, E/refund.json, E/timeout.json, E/adversarial.json.
- E/takeover/commands.jsonl, red-evidence-authority.log/.junit.xml, green-review.log/.junit.xml, reader-writer-regression.log/.junit.xml.
- E/takeover/affected-summary.json, affected.log/.junit.xml, serial-summary.json, serial.junit.xml, serial-census-retry.log/.junit.xml.
- E/takeover/broker-r3-psp-broker.log, psp-broker.junit.xml, broker-before-env-fix.junit.xml, broker-r3-remove-private-broker.log, broker-resource.json, broker-r3-broker-unit.txt.
- E/takeover/manual_qa.py, qa_runner.py, manual-qa.log/.junit.xml, manual-qa-refund.log/.junit.xml, native-presentation.log/.junit.xml, psp-signature-pins.log/.junit.xml.
- E/takeover/gates.py, generate_psp_inventory.py, discovery-receipt.json, base-preservation.json, legacy_guards.generated.json, restore_contract.generated.py, backup_restore_guard.generated.py, live-catalog.json.
- E/takeover/component-source-reuse.json, delivery-audit.json, cleanup-receipt.json, resource-pointer-reconciliation.json, canonical-ready.json, canonical-billing-chromium.log, dependency-audit.log.
- E/takeover/review-fresh-migrate.log, review-db-posture.log, review-django-check.log, review-migration-drift.log; final integrated/role static logs referenced by commands.jsonl. All 148 manifest-listed artifact hashes were checked, including the remaining earlier receipts, without treating their mere presence as approval.
- Production, migrations, tests, templates, audit/catalog/recovery/runner/config deltas: every path in git diff --name-only 63240e99 6a3c7666. Supporting owners read include billing collections/journal/refunds/payouts, provider lifecycle/models/services and the authority discovery implementation.

exactEvidenceGaps:
- Blocking: no captured pre-fix red run for the same-version statement correction. This is separate from the present green runtime proof and the present authority red proof.
- Expected deferrals: canonical billing/locale engines, rendered desktop/mobile screenshots, browser axe/CSP checks, full native R/coverage/full sink scan and migration-upgrade. No approval is inferred for these.
- This read-only review did not recreate the removed private PostgreSQL/Redis or rerun database/broker scenarios. Their exact test source, logs, JUnit, catalog, command receipts and source reuse were inspected. Fresh runtime reproduction is not claimed.
- No external PSP sandbox receipt, real-provider call, hosted qualification or live approval exists or is claimed. L1 fixture evidence does not satisfy L3/L4 or EG-1/EG-5.

No implementation fix, test deletion, git commit, provider call, message to another person, or deployment was performed by this reviewer. The only intentional file write is this gate report.


followUpReview: T57-R2-B1 only, 2026-11-06, st_01a1b279
verdict: confirmed-pending-R
recommendation: APPROVE for this focused source/evidence gate; native qualification remains deferred.
blockers: none remaining from T57-R2-B1.

This appended verdict supersedes the initial needs-fix verdict and its missing-red finding above. The earlier review is retained as history. The follow-up checked evidence commit 8eea8b34b72dc70853c7726af8314584431267f2, whose parent is exactly 6a3c7666c9eb5f7736d21afa58e2a7be95425876.

- Verified the archived source hashes in takeover/red-statement-runtime.json against git show 7193de9245b32a5ec2c67b2c13ae5a0f2205921a for 0012_sandbox_psp.py, _psp_guard_sql.py and psp_posting.py. The archived guard SQL hash and the hash of its function body independently match the recorded live PostgreSQL pg_proc body. The pre-fix guard lacks the quarantined-statement exclusion.
- Read takeover/red_statement_snapshot.py:44-68 and :87-111. It compiles the archived guard into the loaded guard module, regenerates the migration SQL, replaces the migration's SQL operation, and compiles the archived posting owner into the production posting module before pytest collection. The recorded posting code filename identifies that archived revision, whose source also lacks the corrected freshness exclusion. The live-schema fixture asserts exact old-guard body equality after database setup; it cannot accept the corrected guard as its baseline.
- Confirmed the whole tests/billing/test_psp_review.py file is byte-identical in 6a3c7666, 8eea8b34 and the current worktree. Its SHA-256 is f8c6acb58bf635513b7c727293d4fe32284a69a97534c4532efcba3cf509a179. The runner does not rewrite or substitute the regression. Its only function-alias substitution targets the production apply_psp_evidence function.
- takeover/commands.jsonl:64, takeover/statement-red-green.json:7-44, takeover/red-statement-correction.log:1-13 and takeover/red-statement-correction.junit.xml:1-6 agree: the unchanged correction test ran once, exited 1, and failed at test_psp_review.py:141 because reconciliation returned superseded instead of matched. This is the intended pre-fix failure, not a fixture error. The upstream SQL assertion stops this red run before the later posting call; the archived posting owner was loaded, but this is not separate executed coverage of that later branch. The two previously inspected corrected-source green receipts exercise the complete correction/posting flow.
- Independently confirmed those same-name green cases remain unfailed and unskipped in affected.junit.xml and green-review.junit.xml. No already-settled green, serial, browser or R gate was rerun.
- git diff --name-only 6a3c7666 8eea8b34 contains exactly 163 paths, all under .omo/evidence/clinic-ops-premium-intelligence/task-57/. No production, test or other path changed. The proof runner, runtime identity, red log, red JUnit, pairing JSON, command ledger and pre-append gate report all match their committed bytes and manifest hashes.

The retained runtime receipt records corrected-schema restoration and an empty tracked diff against 6a3c7666 at capture time. This follow-up inspected and recomputed the evidence chain without recreating the removed private database. Browser/full-R/upgrade deferrals and BLOCKED-ON-EG-1/EG-5 remain as in the original review; no release or live-provider approval is implied. The only write in this follow-up is this report append.


followUpReview: Todo 57 replay and inactive-actor delta, 2026-11-06, st_01a1b317
recommendation: APPROVE for the requested delta gate
verdict: confirmed-pending-R
blockers: []

originalIntent: Preserve the previously confirmed PSP PIX/card implementation when replayed on confirmed todo 56, close requester self-access for inactive actors at the native SQL boundary, and register executable inactive-actor evidence without changing inherited classifications or exemptions.

desiredOutcome: Deactivating a stored PSP requester immediately denies PSP access, binding INSERTs, evidence INSERTs and both dispatch modes, even with actor/tenant GUCs bound directly as clinic_app. Reactivation restores those same valid operations. The prior payment/reconciliation behavior and todo-56 oracle coverage remain intact.

userOutcomeReview: All five requested delta criteria are satisfied. The requester alternative now requires the current active identity before its OR expression can authorize an intent. The native regression distinguishes the prior bypass from the corrected behavior at the actual access and INSERT boundaries; both dispatch modes already required active book authority. The replay preserves the original implementation patch. No additional unregistered active-actor reader or changed inherited classification was found. This approval preserves the earlier pending-R qualification.

scope:
- Worktree: /home/samarone/Documents/clinic-ops-new-roadmap/clinic-os-wt/cpi-e-t57-r2.
- Confirmed parent: 914dd542e2207c8c53b277c878b51691c15c2a2f.
- Replayed implementation: 23784579; replayed evidence: 7e755511.
- Reviewed fix: 5a52514fe97b750eb8c41f4f7cc902ce1daeaad1, HEAD at inspection.
- E means .omo/evidence/clinic-ops-premium-intelligence/task-57 in that worktree; R3 means E/inactive-actor-r3.
- The pre-existing uncommitted report append and takeover/generate_psp_inventory.py change were present on entry. Only this report was appended by this reviewer.
- Toolkit status was queried through a Bun JS evaluation because this session exposes no JS-eval tool. It returned ULW_LOOP_PLAN_MISSING; the caller's explicit existing report path is used.

criterionReview:
1. User-1, replay equivalence: PASS. Independently ran git range-diff 63240e99..6a3c7666 914dd542..23784579; Git reports the implementation patch equal. Comparing the two resulting trees finds exactly the three inherited todo-56 paths: tests/billing/journal_oracles.py, tests/billing/test_sql_inactive_actor.py and tests/identity/test_sql_inactive_actor.py. There is no PSP production delta between those implementation commits.
2. User-2, native SQL fix and five-path scope: PASS. git show 5a52514f contains exactly apps/billing/migrations/_psp_scope_sql.py, tests/billing/psp_oracles.py, tests/billing/test_psp_inactive_actor.py, tests/identity/legacy_guards.json and tests/identity/test_sql_inactive_actor.py. The only production change is the three-line EXISTS at _psp_scope_sql.py:12-14, conjoined before the requester/book-authority OR at :15-17. Both policy alternatives and the requester alternative therefore require the same active identity. 0012_sandbox_psp.py:9-10 imports the SQL builder and :46-50 executes it; this changes the unshipped 0012 definition rather than adding a later migration.
   - billing_psp_guard's binding/evidence branch at _psp_guard_sql.py:126-132 requires the stored requester and billing_psp_access, so it inherits the corrected predicate. Its policy/intent branches enter billing_lock_book and require billing_book_access; its receipt branch also enters billing_lock_book and requires settlement.post. None provides an alternate unchecked inactive-actor INSERT branch.
   - billing_psp_dispatchable at _psp_scope_sql.py:51-73 requires billing_book_access before every charge/refund/payout state branch, regardless of fresh_session. _journal_core_sql.py:56-67 delegates the required permission to the established has_permission/identity_actor_permission chain, whose active-account check is retained. Its recovery flag bypasses session freshness only, not book authority.
   - SELECT policies for PSP intents, bindings, evidence and receipts use billing_psp_access; policy reads use the book gate. Stored scope/deduplication resolvers do not authorize a write or dispatch and do not bypass the checked trigger. UPDATE/DELETE/TRUNCATE remain refused by the existing immutable history contract.
3. User-3, real red/green proof: PASS. Parsed the actual JUnit cases and command exits, inspected failure messages and the runner, and recomputed every recorded source hash against the relevant Git blobs. R3/red-native-probes-source.json identifies the old scope SQL from 7e755511; the corrected source and every later source receipt match 5a52514f. The test, oracle and global-registry source bytes are identical across red/green; only _psp_scope_sql.py differs.
   - R3/red-native-probes.log:1-21 and red-native-probes.junit.xml:1: five cases, three failures, two passes, zero errors/skips, exit 1. Access failed with assert True is False; binding and evidence failed with DID NOT RAISE DatabaseError. Both dispatch flags passed. These failures exercise the intended bypass rather than a setup failure.
   - R3/green-native-probes.log:1-3 and green-native-probes.junit.xml:1: the same five cases pass, exit 0, zero errors/skips. The native INSERT refusals assert SQLSTATE 42501 and billing_psp_guard in the server error context. The direct-GUC helper in tests/identity/permission_support.py:40-49 has no tenant_context or Python inactive-user admission gate. prepared_payment patches only outbox dispatch during setup.
   - R3/inactive_gates.py:20-46 records source hashes before invoking pytest, and :62-75 runs the same test path for red and green. Its underlying gates.run records the subprocess's actual exit; it does not manufacture results. Pre-fix and corrected sanitized resource receipts identify distinct fresh container IDs and ports. Both private database/volume lifecycles and cleanup are recorded in commands.jsonl.
4. User-4, executable oracles and inherited data: PASS. Independently resolved all three ACTOR_PROTOCOL_ORACLES signatures to the callable native test, resolved every PSP SQL probe name, and collected the exact five pytest parameters. tests/billing/psp_oracles.py:12-19 registers access(uuid), guard() and dispatchable(uuid,boolean); tests/identity/test_sql_inactive_actor.py:47 and :436 combine them into the global registry.
   - The JUSTIFIED assignment's AST is identical to 914dd542. tests/billing/journal_oracles.py and tests/billing/test_sql_inactive_actor.py are byte-identical to 914dd542.
   - Independently compared the delivered JSON with 914dd542: all 824 inherited Python records and role_calls are identical; all 465 inherited SQL records preserve every non-discovery field. Existing probe IDs remain present. Additions are exactly 11 PSP Python candidates and seven PSP SQL roots.
   - The five inherited discovery changes are legitimate additions produced by discovery. Reused the repository's _body_callees against the actual generated PSP SQL and reproduced each edge: billing_book_access <- billing_psp_access/billing_psp_dispatchable/billing_psp_guard; billing_lock_book <- billing_psp_guard; billing_finance_session <- billing_psp_guard; identity_session_verified <- billing_psp_dispatchable; billing_patient_charges <- billing_patient_psp_instructions. The only added policy edges are billing_psppolicy:billing_psp_insert and billing_psppolicy:billing_psp_read -> billing_book_access. No inherited edge was removed. These are the resolvers/policies that discover_sql actually emits, not changed classification labels.
   - All three R3 generated outputs are byte-identical to their delivered owners: legacy_guards.json, restore_contract.py and test_backup_restore_guard.py.
5. User-5, no remaining unregistered active-actor reader: PASS within this delta. The only new production is_active read is the corrected billing_psp_access clause, registered by its full signature. The other is_active occurrence in the touched production area is the unchanged core _actor_has_clinic_authority reader. Independently reran Python discovery: all 835 candidates/signals exactly match the manifest. Independently scanned migration definitions: all 450 discovered definition names and source paths match the delivered SQL inventory. The real corrected-schema global test_every_actor_reader_checks_is_active_or_is_justified case is present and passing in R3/sql-registries.junit.xml; its source receipt matches the reviewed global registry and SQL owner. That test inventories all live clinic_app actor-setting readers and requires executable own/delegating protocols or the unchanged explicit classifications; it is not limited to the three PSP names.

retainedNativeEvidenceChecked:
- R3/commands.jsonl: 21 command receipts, all referenced logs present, all actual exits equal expected exits.
- R3/sql-registries.junit.xml and .log: 99 passes across all five identity SQL registry modules, including the global active-actor coverage test; zero failures/errors/skips.
- R3/lane-serial.junit.xml and .log: 109 passes, zero failures/errors/skips. Independently reconstructed node IDs from xunit1 file/name fields and confirmed all 18 selectors in E/takeover/serial-selection.json are represented.
- R3/psp-affected.junit.xml and .log: 57 passes, zero failures/errors/skips. Its source receipt matches the reviewed delta.
- R3/ruff-check.log, ruff-format.log and mypy.log: recorded full-tree checks pass; 2398 files formatted/typechecked. These are inspected retained runs, not new full-tree executions by this reviewer.
- R3/fresh-migrate.log records billing.0012_sandbox_psp applied successfully; django-check.log reports no issues, migration-drift.log reports no changes, and db-posture.log records the restricted role posture. Fresh migration reports the existing RawSQL full_clean warnings; these were not silently treated as new product failures.
- R3/cleanup-receipt.json, pre-fix-cleanup-receipt.json and corrected-cleanup-receipt.json record removal of owned PostgreSQL/volumes and runtime secrets. The private-runtime file is absent.

independentChecksExecuted:
- Replay range-diff and resulting-tree comparison: equal implementation patch; only the three inherited todo-56 test/oracle paths differ.
- Source hashes, JUnit parsing, command/log path checks, inherited manifest comparison, JUSTIFIED AST comparison and generated-output byte comparison: passed.
- Fresh Python discovery, migration-definition census and repository lexical caller discovery against generated PSP SQL: passed.
- Native oracle import resolution and pytest --collect-only -p no:cacheprovider for test_psp_inactive_actor.py: exit 0; all five intended cases collect.
- Ruff check --no-cache on all four changed Python paths: exit 0, all checks passed.
- Programming check-test-junk.ts on both changed test modules: exit 0, no findings; direct semantic review also performed.
- git diff --check 7e755511 5a52514f: exit 0. The broader inherited evidence diff reports only existing trailing spaces in captured failure logs/JUnit (E/takeover/red-statement-correction.log:7, its JUnit:4, and targeted.log:8851-8871). These captured-output spaces are not a failing delta criterion.
- LSP found the actual global-registry consumer of PSP_ACTOR_PROTOCOL_ORACLES. _psp_scope_sql.py has no diagnostics. The new native test has pluginless BasedPyright implicit-test-import and Django-generated-field/Any warnings; these were not suppressed or described as clean. The project's Django-aware full strict mypy receipt passes.

programmingAndRemoveAiSlopsReview:
- Read both installed skills, the Python reference, the testing authoring gate and junk-class table. Applied the existence/reuse ladder, all ten slop categories and the programming criteria directly to the five-path fix, its production caller paths and its tests. The replay's prior source-review.md:78-112 and this report's earlier programmingAndRemoveAiSlopsReview explicitly cover the same skill perspectives, including fixture-only assertions, mock replay limitations, test cuts and source pins. R3/SQL_AUTHORITY_REVIEW.md supplies the current author's SQL assessment but has no separate explicit skill checklist; this direct review supplies current-delta coverage rather than treating that prose as approval.
- Production change: one boundary predicate in the existing shared SQL owner. No extraction, parser, normalization, retry, fallback, helper, logging, dependency or test-only production seam was introduced. The new identity lookup is necessary at the requester OR boundary; delegating callers reuse it rather than copying it.
- Test ledger: R for test_psp_sql_requires_current_active_actor, with five rows for the actual SQL access/dispatch/INSERT contracts. The credible regression is the missing active-identity conjunct; the unchanged test goes red at exactly its three affected boundaries. Dispatch rows retain independently required controls. Active/inactive/active phases establish reactivation without changing command identity or economic state. Existing tests entered tenant_context or used another actor and could not expose requester self-access after direct GUC binding.
- Checked excessive/useless tests, deletion-only tests, removal certificates, tautologies, implementation mirrors, source greps, mock echoes, fabricated postconditions, wrapper replays and unnecessary seams. No such test is the proof of this delta. No test was removed, weakened, skipped, or added merely to verify a requested removal. The table extends one native contract, and the application behavior is exercised in PostgreSQL rather than a copied fake. The live diagnostic-context assertion distinguishes the required guard from an unrelated Python, ACL or RLS denial.
- Maintenance notes only: the new test retains the neighboring Django fixture pattern dict[str, object] and string-based boundary selection at test_psp_inactive_actor.py:29 and :63-76; these are weaker type modeling than the skill's preferred typed variants. They do not defeat an asserted delta criterion. Pure-line counts are 99 for _psp_scope_sql.py, 61 for psp_oracles.py, 80 for the new native test and 698 for the pre-existing global inactive-reader suite. That existing suite's size is not a reason to require an unrelated refactor during this read-only gate. No new production parameter bloat, negative flag naming, redundant post-action production verification or broad exception handler was added.

checkedArtifactPaths:
- Plan: /home/samarone/Documents/clinic-ops-new-roadmap/clinic-os/.omo/plans/clinic-ops-premium-intelligence.md:104-131 and :1268-1278.
- All five paths in 5a52514f; supporting _psp_sql.py, _psp_guard_sql.py, 0012_sandbox_psp.py, _journal_core_sql.py and the identity permission evaluator/session sources.
- tests/identity/legacy_guard_inventory.py, legacy_sql_inventory.py, test_sql_inactive_actor.py, test_lifecycle_sql_readers.py, permission_support.py; tests/billing/journal_oracles.py, test_sql_inactive_actor.py and test_psp_review.py.
- E/gate-review-r2.md, source-review.md, Manual-QA.md, takeover/gates.py, takeover/generate_psp_inventory.py and takeover/serial-selection.json.
- R3/SQL_AUTHORITY_REVIEW.md, WORKING_STATE.md, inactive_gates.py, commands.jsonl, native-results.json, base-preservation.json, discovery-receipt.json and all three generated outputs.
- R3/red-native-probes, green-native-probes, sql-registries, lane-serial and psp-affected: each source JSON, JUnit and log; serial-summary.json.
- R3/full static and database logs named above; sanitized pre-fix/corrected private-resource JSONs and cleanup receipts.

exactEvidenceGaps:
- No blocking evidence gap for User-1 through User-5. Database executions were reproduced from retained source identities, runner, actual failure messages, JUnit cases and command/resource receipts; this reviewer did not recreate the removed private PostgreSQL or rerun the settled native suites. Fresh live pg_proc/discover_sql execution is therefore not claimed. Current Python/source-definition/lexical discovery was independently executed without a database.
- Browser, full native R and migration-upgrade qualification remain deferred as in the preceding confirmed-pending-R verdict. No external PSP sandbox, hosted-CI or live-provider approval is asserted; EG-1/EG-5 remain blocked.
- No notepad path was supplied. R3/WORKING_STATE.md is an intermediate handoff that still describes a running job/private database; later completion and cleanup receipts were used for final state.

This appended verdict covers the delivered replay and 5a52514f delta. Earlier reports remain historical. The only intentional file write in this review is this report append; no implementation, test, inventory, existing external edit, commit or resource was changed.
