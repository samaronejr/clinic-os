recommendation: REJECT
verdict: needs-fix
reviewDate: 2026-11-06
reviewType: Read-only source review of todo 57; this is not a completed L1 release qualification.

originalIntent: Deliver recorded PIX/card PSP integration, authenticated payment evidence, statement reconciliation and recovery after an ambiguous send, using todo 56's financial writers. Keep real providers, live money and the external EG legs unreachable.

desiredOutcome: Patients receive clearly synthetic payment instructions and see a receipt only after verified collection. Finance can reconcile exact provider references, centavos, currency and fees without duplicate postings. Read permission cannot manufacture payment evidence.

userOutcomeReview: The normal callback, journal and statement paths are present, and the inspected catalog preserves FORCE RLS and the intended final ACLs. The supplied snapshot nevertheless permits another readable intent's evidence to be fabricated and allows a quarantined statement to prevent later correct reconciliation. Those failures prevent approval of the requested source snapshot.

scope:
- Worktree: `/home/samarone/Documents/clinic-ops-new-roadmap/clinic-os-wt/cpi-e-t57-r2`.
- Reviewed tree: `745f51cad23f4e28a762e888ba0d555e0def184a`.
- Evidence snapshot: `7193de9245b32a5ec2c67b2c13ae5a0f2205921a`; its tree was independently read and equals the reviewed tree.
- Diff base: `ae1b3e4c`, todo 56. The reviewed diff contains 58 changed paths.
- Line references in blockers refer to tree `745f51ca`, not later working-file line numbers.
- Other work changed the working tree during this review. In particular, unstaged changes to the evidence guard, posting filter and provider gate, plus `tests/billing/test_psp_review.py`, appeared. They address some observations below but are outside the supplied staged tree and have not earned approval through this report. No implementation was edited by this reviewer.

blockers:

1. **T57-B1, high: read authority also authorizes fabrication of another actor's PSP evidence.**
   - violatedCriterion: SC-1 (authority from the current actor's exact scope), SC-6 (authenticated integration evidence), SC-15 (authorization/security boundary).
   - evidencePointer: `apps/billing/migrations/_psp_guard_sql.py:126`; supporting paths `apps/billing/migrations/_psp_scope_sql.py:4`, `apps/billing/migrations/_psp_sql.py:22`, `apps/billing/migrations/_psp_sql.py:46`, and `apps/billing/psp_posting.py:149`, all at `745f51ca`.
   - Observation: `billing_psp_access` permits the original requester OR a holder of `journal.read`/`charge.collect`. The INSERT policy and the binding/evidence trigger reuse that read predicate. The trigger does not require the current actor to equal `intent.requested_by_id`. A different actor who can read the intent can therefore INSERT a fabricated `PspBinding` and a `PspEvidence` row with `source='status'`, `status='settled'`, `decision='verified'`, the exact intent amount/currency and a positive version. Nothing on this INSERT path authenticates a provider or calls its lookup. Finance application trusts that retained decision after checking its terms and competing evidence.
   - Source proof: the role has INSERT; the organization and read predicate satisfy WITH CHECK; the trigger's access condition accepts the different readable actor; the remaining verified-evidence checks require matching terms and an existing binding, both supplied by those two INSERTs. This is a mutation-authority defect, not a request to remove legitimate read access.
   - Remedy: give binding/evidence INSERTs a write predicate bound to the stored intent actor and tenant, separate from the broader read predicate. Preserve authenticated worker recording after staff offboarding. Add a runtime-role regression in which another actor can SELECT an intent but receives SQLSTATE `42501` when attempting either INSERT.

2. **T57-B2, high: a rejected statement poisons later matching evidence at the same provider version.**
   - violatedCriterion: Todo 57's statement reconciliation contract (match provider reference, amount and currency); SC-15 (money and reconciliation correctness).
   - evidencePointer: `apps/billing/migrations/_psp_guard_sql.py:154` and `apps/billing/psp_posting.py:151`; supporting paths `apps/billing/psp_storage.py:129` and `apps/billing/psp_policy.py:91`, all at `745f51ca`.
   - Observation: statement evidence deliberately retains the statement's actual amount but the authoritative status's version. The trigger and application compare against every earlier evidence row, including quarantined statement rows. A bad statement amount of 9,999 with authoritative settled amount 10,000/version 2 is quarantined correctly. A subsequent distinct statement line with the correct reference, 10,000 BRL and fee, still at authoritative version 2, passes `match_statement` but the trigger changes its decision to `superseded` because the earlier rejected amount differs. The application filter independently rejects the same correction. Financial history is immutable, so retrying a corrected statement cannot repair that comparison.
   - Remedy: distinguish rejected statement observations from authoritative status facts in both comparisons. Preserve the protection against a genuinely newer reversal or contradictory authoritative status. Add a regression that imports a mismatching line, then a distinct corrected line at the same provider version, and verifies one collection, one receipt and one clearing/fee transfer after replay.

notes:
- `apps/billing/psp_gate.py:49`: the snapshot requires the original version to remain the current version even for lookup-only recovery. Rotation after a timeout makes worker preparation fail and direct reconciliation record unknown without looking up the original key. Separate the policy for new effects from the policy for looking up an already approved request if rotation recovery is part of the intended lifecycle contract. This is recorded as a note rather than an additional blocker because the acceptance examples do not explicitly name rotation during recovery.
- `tests/conftest.py:130`: the registration-isolation fixture copies the action/send/dispatcher/authenticator registries but omits the newly introduced `_ACTION_RECOVERY_ADAPTERS`; `clear_integration_registrations` now clears that shared dictionary. Remedy: isolate the recovery registry alongside the existing registries so one test cannot remove another test's startup recovery registration. This is a test-isolation maintenance finding; no order-dependent failure was reproduced here.
- `tests/billing/test_psp_adapter.py:189`: the statement matcher assertion exercises production behavior, but the subsequent assertions inspect values supplied directly to the fixture. They do not prove preservation by a writer. Remedy: keep the matcher contract and rely on the real journal assertions in `tests/billing/test_psp_statement.py:35` for gross/fee preservation.

sourceChecks:
- Callback order: `psp_flow.receive_event` authenticates raw bytes before calling `store.resolve`. `psp_fixtures.authenticate` checks HMAC over the exact body before JSON parsing, bounds the body, and projects only the six allowed event fields. Tenant/organization/clinic payload claims are never read. `DatabasePaymentEventStore.resolve` uses the stored provider/reference pair. This confirms the public callback path; it does not cure T57-B1's independent database write path.
- Transaction boundary: `prepare_psp_charge`/`prepare_psp_outgoing` enqueue the immutable intent and digest inside the financial transaction. The shared outbox dispatches on commit. The PSP adapter reloads frozen terms during prepare and invokes lookup/create/refund/payout outside the tenant transaction.
- Ambiguity: the new `AmbiguousSendError` branch leaves the operation in progress. Registered action recovery uses the lookup adapter, retains the original key and does not increment send attempts. The existing periodic recovery scan includes in-progress operations. Recovery dispatch itself calls no mutating provider method.
- Economic writers: collection uses todo 56's observe/verify/post/allocate services and its guarded shared posting core. Refund/payout confirmation uses the existing transition services. Statement transfer calls `post_entry` with a stable UUID derived from the intent. Settlement, allocation and receipt identities are stable; repeated statement application cannot mint a second transfer identity. No parallel journal implementation was introduced.
- Receipt/patient surface: creating recorded instructions is pending, not paid. Patient projection reuses released-invoice/session/delegate authority; codes and links are encrypted. The presenter accepts only the non-payable PIX prefix and `payments.invalid` links. A sealed PSP receipt is projected into the existing paid/receipt surface.
- Synthetic/live exclusion: the setting defaults off, `PspCapability.real_enabled` remains False, and the rehearsal gate refuses non-synthetic mode. Runtime registration accepts only the recorded provider class and fixed provider identifier. The selected version must be an approved fixture sandbox. Both new environment variables are excluded by live activation preflight. No network provider implementation or dependency was added. External sandbox and production legs remain **BLOCKED-ON-EG-1/EG-5**.
- Privacy: product outbox input is only scoped identifiers, fixed provider/subject vocabulary, an idempotency key and a digest. Prepared payment terms, raw callback bodies, authentication material and encrypted instructions are not copied into outbox rows or error text. The new ambiguous branch persists no exception message. New audit events use the existing metadata-only builder. No new product logging sink was added. This is a source check, not a completed sentinel scan of all runtime evidence.
- Catalog/fixtures: the five PSP tables are included in the app posture, resolver catalog, global model/policy inventory, recovery manifest and test truncate-guard fixture. Audit events and the four human service signatures are pinned. The browser file extends the already registered billing suite; its existing Chromium/Firefox/WebKit CI shard entries already cover it. The frozen foundation plan and dependency manifests are absent from the diff.
- Deferred FKs: migration `0012_sandbox_psp.py:18` temporarily removes FORCE only from four owner-owned referenced tables, flushes the deferred FK statements, then restores FORCE before installing the new policies. The migration remains transactional. The DDL-only function grants in `_psp_sql.py` are revoked at its end; the fix does not leave runtime helpers executable by `clinic_owner`.

independentEvidence:
- Ran `.venv/bin/python -m pytest -p no:cacheprovider -q --tb=short --no-showlocals tests/billing/test_psp_adapter.py tests/billing/test_psp_policy.py` with bytecode writes disabled: exit 0, **32 passed**. These cover the unchanged pure adapter/policy paths, not the two database defects.
- Ran `.venv/bin/python -m pytest -p no:cacheprovider -q --tb=short --no-showlocals tests/renewal/test_live_activation.py::test_live_preflight_rejects_invalid_environment tests/infra/test_module_boundaries.py::test_psp_human_boundaries_derive_authority_from_current_context`: exit 0, **15 passed**.
- Ran the programming skill's `check-test-junk.ts` on all 17 changed test modules from the requested snapshot: exit 0, no regex-detectable findings. A direct review was also performed; the scanner is not proof of semantic coverage.
- Read the committed GNU `.mo` catalog and compared all four new PSP msgids with their `.po` translations: exit 0, all four match.
- Ran `git diff --check ae1b3e4c 745f51ca`: exit 0.
- Queried the lane PostgreSQL catalog in a transaction forced read-only. All five PSP tables and the four FK-referenced tables have RLS enabled and forced, owned by `clinic_owner`. Each PSP table grants `clinic_app` exactly SELECT/INSERT and `clinic_resolver` exactly SELECT; none grants the checked surface to `clinic_agent`; there are no explicit PSP column ACLs. All seven PSP functions are owned by `clinic_resolver` with the hardened search path. The six callable functions allow `clinic_app`; the trigger guard allows only its owner. `has_function_privilege('clinic_owner', ..., 'EXECUTE')` is false for all seven, and also false for `billing_book_access` and `billing_immutable`. This independently confirms the final grant property of the FK fix, not evidence-write authorization.
- LSP references for the modified `_send_claimed` signature show its two execution call sites; both were inspected.
- LSP diagnostics on `psp_posting.py` report four unknown generated Django FK attributes (`organization_id`/`book_id`). The repository uses the Django-aware mypy plugin; these LSP diagnostics are recorded, not suppressed or claimed clean.
- A later independent `ruff check --no-cache .` ran after concurrent edits appeared and exited 1: D401 in the changed working `psp_gate.py` and five import/line-length findings in untracked `test_psp_review.py`. Its chained format check did not run. This is not a lint verdict on tree `745f51ca`.

programmingAndRemoveAiSlopsReview:
- Loaded both skill files and the Python, testing and junk-class references. Applied their criteria directly to the requested diff, production owners and tests. Per the explicit read-only scope, no cleanup, mutation proof or test cut was performed.
- Checked all ten slop categories: obvious comments, excess defense, complexity, needless abstraction, boundary violations, dead code, duplication, performance cost, missing/junk tests and size. The substantive boundary and evidence-normalization findings are T57-B1 and T57-B2. Protocols separate pure orchestration from the database adapter and are exercised at those real boundaries; the shared outbox extension has a production PSP caller. No speculative parser, transport or unrelated normalization layer was added.
- No tests were deleted, skipped to obtain green, weakened to lower-bound counts, or added merely to certify a requested removal. Existing exact catalog inventories are retained security/storage/architecture contracts, not rejected just because they are static.
- The pure in-memory EventStore implements its own duplicate behavior. Its replay assertions alone are insufficient proof of database deduplication; the real-role refund and statement tests are the production-boundary keepers. No duplicate-test deletion was claimed without the required mutation proof.
- Missing owner coverage explains the two blockers: the snapshot's posture test checks cross-organization reads but not another readable actor's evidence INSERT; the statement tests exercise mismatch and match in separate fixtures but never a correction after a rejected row.
- Measured changed Python files. All new PSP production modules are under 250 pure lines. `psp_services.py` is in the warning band at 207. Existing enlarged modules include `presentation.py` (506), `integration.py` (960), and the existing catalogs/configuration files. Their pre-existing size is a maintenance note, not an invented financial acceptance blocker.
- The skill's Python detector returned exit 1 with eight `object` annotation findings in the new PSP files/tests. The `SendAdapter` object boundary is inherited from the existing protocol; the remaining detections are Django field generic arguments and deliberately untrusted test inputs. Casts from guarded model strings to literals and string-based variant branching remain type-modeling notes. The project's chosen Django/mypy stack was not treated as a reason to demand an unrelated stack migration.

The following ledger marks every new test declaration in the requested snapshot. R means retain its independently named contract; it does not mean the test was executed by this reviewer.

| Test declaration | Mark | Contract |
|---|---|---|
| `test_psp_adapter.py::test_forged_raw_body_cannot_reach_scope_resolution` | R | Authentication precedes scope lookup. |
| `test_psp_adapter.py::test_callback_ignores_payload_tenant_and_replay_converges` | R | Allowed callback field projection; fake-store replay is not database proof. |
| `test_psp_adapter.py::test_callback_disagreements_never_verify` | R | Amount/currency/status/version disagreements. |
| `test_psp_adapter.py::test_provider_lookup_failure_is_unknown_not_permission_to_send` | R | Recovery does not create a missing effect. |
| `test_psp_adapter.py::test_charge_creation_is_pending_and_never_a_settlement` | R | Non-payable creation state. |
| `test_psp_adapter.py::test_card_link_uses_an_invalid_host_and_frozen_installments` | R | Recorded card-link delivery; assertions establish link/state, not all installment persistence. |
| `test_psp_adapter.py::test_installments_reject_values_outside_exact_clinic_policy` | R | Exact installment input boundary. |
| `test_psp_adapter.py::test_pix_cannot_installment_even_when_card_policy_allows_it` | R | PIX single-installment boundary. |
| `test_psp_adapter.py::test_statement_match_requires_all_three_exact_fields` | R | Independent statement matching dimensions. |
| `test_psp_adapter.py::test_statement_preserves_gross_collection_and_separate_psp_fee` | R | Matcher success; fixture-value assertions noted above. |
| `test_psp_adapter.py::test_sigkill_after_remote_effect_recovers_without_a_second_create` | R | Real process-loss effect/key behavior. |
| `test_psp_adapter.py::test_callback_signature_is_exact_raw_bytes_not_reencoded_json` | R | Raw-byte authentication contract. |
| `test_psp_broker.py::test_required_broker_redelivers_after_remote_effect_sigkill_without_duplicate` | R | Real Celery/broker/PG lifecycle, distinct from the pure process test. |
| `test_psp_outbox.py::test_remote_timeout_keeps_in_progress_and_redelivery_uses_lookup_only` | R | Shared outbox state and attempt accounting. |
| `test_psp_outbox.py::test_sandbox_is_not_real_and_setting_alone_never_authorizes_a_provider` | R | Configuration/lifecycle security boundary. |
| `test_psp_outbox.py::test_live_mode_cannot_fall_back_to_recorded_callback_processing` | R | Live exclusion before callback scope. |
| `test_psp_patient.py::test_survivor_sees_psp_instructions_until_unmerge_without_reparenting_money` | R | Patient alias/session authority and immutable money lineage. |
| `test_psp_policy.py::test_replayed_refund_callback_retains_one_confirmed_fact` | R | Refund classification/projection; real DB keeper is the refund integration test. |
| `test_psp_policy.py::test_status_lookup_timeout_never_attempts_another_effect` | R | Lookup exception prevents mutation. |
| `test_psp_policy.py::test_exact_money_boundary_rejects_non_centavo_inputs` | R | Exact-centavo request boundary. |
| `test_psp_policy.py::test_signed_but_malformed_events_never_resolve_scope` | R | Authenticated malformed-input rejection. |
| `test_psp_posture.py::test_psp_tables_are_forced_rls_append_only_and_have_exact_policies` | R | Migration/security catalog. |
| `test_psp_posture.py::test_no_context_or_foreign_organization_sees_psp_intents` | R | Tenant read isolation; does not cover T57-B1. |
| `test_psp_reconciliation.py::test_verified_fixture_posts_gross_once_with_repasse_before_fees` | R | Journal/receipt/allocation idempotency. |
| `test_psp_reconciliation.py::test_amount_mismatch_is_retained_and_cannot_post` | R | Authoritative mismatch quarantine. |
| `test_psp_refunds.py::test_replayed_refund_callback_requires_finance_and_reverses_repasse_once` | R | Real refund authority/reservation/reversal. |
| `test_psp_statement.py::test_statement_reimport_posts_one_net_bank_transfer_and_separate_fee` | R | Actual balances and statement replay. |
| `test_psp_statement.py::test_statement_disagreement_is_quarantined_with_its_actual_amount` | R | Rejected amount retention; does not cover T57-B2's subsequent correction. |
| `browser/test_billing_psp.py::test_recorded_psp_instructions_never_pay_until_a_sealed_receipt` | R | Real patient PIX/card pending/paid surface at desktop/mobile widths. |
| `test_module_boundaries.py::test_psp_human_boundaries_derive_authority_from_current_context` | R | Required public service signature invariant. |

Modified existing audit, resolver, schema, recovery, billing catalog and live-preflight tests retain their security, storage, migration or configuration contracts. No D/C cut is proposed; there is no fabricated mutation receipt.

standardContractDisposition:
| Criterion | Source disposition |
|---|---|
| SC-1 | Human service signatures/scopes present; T57-B1 fails the evidence-write authority boundary. |
| SC-2 | Five tenant models, app posture, exact catalog pins, FORCE RLS and restricted final grants present. |
| SC-3 | No new app. |
| SC-4 | PSP and outgoing transitions use registered audit definitions; catalog updated. |
| SC-5 | Encrypted instruction fields and immutable/delete/truncate guards present; migration rehearsal is not established here. |
| SC-6 | Raw callback path and outbox order present; T57-B1 permits unauthenticated evidence through a different write path. |
| SC-7 | No new source sink carrying PHI/raw payload/credentials found; full runtime sentinel evidence remains unqualified. |
| SC-8 | Native patient projection and four translated msgids present; real browser/state/a11y results remain unqualified. |
| SC-9 | Narrow reviewer checks listed above; complete final gates not established. |
| SC-10 | Existing billing suite extended; existing CI shards cover it. |
| SC-11 | Frozen artifacts absent from the diff. |
| SC-12 | Reviewed evidence paths below; final QA and cleanup receipts incomplete. |
| SC-13 | Evidence commit identified; final green atomic delivery commit is not established. |
| SC-14 | No EG cleared and no live path enabled. |
| SC-15 | Exact posture/identity paths present; T57-B1 and T57-B2 fail relevant authorization/money behavior. |
| SC-16 | Recorded local adapter only; external legs remain BLOCKED-ON-EG-1/EG-5. |
| SC-17 | Upgrade/restore proof not established. |
| SC-18 | Modified private send signature and its two callers inspected via LSP; human API pins updated. |
| SC-19 | No dependency additions. |

checkedArtifacts:
- Original brief and SC-1..SC-19: `/home/samarone/Documents/clinic-ops-new-roadmap/clinic-os/.omo/plans/clinic-ops-premium-intelligence.md:111` and `:1268`.
- Source: all 58 changed paths in `git diff --name-only ae1b3e4c 745f51ca`, including all `apps/billing/psp_*.py`, five PSP migration files, the core outbox diff, patient templates/presentation, catalog/fixture changes, live activation exclusions, audit changes and runner registration.
- Supporting owners: `apps/billing/{security,collections,journal,allocation_posting,refunds,payouts}.py`, `_journal_{core,post,collection,legacy,refund}_sql.py`, `apps/providers/{services,lifecycle}.py`, `apps/comms/tasks.py`, `config/{celery.py,settings/contracts.py,settings/test.py}`, `apps/tenancy/{posture,migrations_support}.py`, and `tests/billing/journal_support.py`.
- Evidence under `.omo/evidence/clinic-ops-premium-intelligence/task-57/takeover/`: `commands.jsonl`, `gates.py`, `targeted.log`, `r2-targeted.log`, `r2-fresh-migrate.log`, `r2-db-posture.log`, `r4-mypy.log`, `r4-static-unit.txt`, `browsers.py`, and `manual_qa.py`.
- The earlier `targeted.log` contains failures, including truncate-guard teardown failures; it is not green evidence. `r2-targeted.log` had no completed verdict when inspected. `commands.jsonl` records earlier successful migration/drift/posture checks and r4 lint/format/mypy commands, but does not pin those commands to a source tree.
- Later `tests/billing/test_psp_review.py` and working-file corrections were inspected only to identify source drift; they are not credited as fixes to the requested snapshot.

exactEvidenceGaps:
- No completed final targeted/broker run and matching JUnit artifact for the reviewed tree was reproduced or accepted here. In particular the `CLINIC_BROKER_GATE=required` SIGKILL variant remains unqualified by this review.
- No completed three-engine billing/locale report set or rendered desktop/mobile screenshot matrix was inspected. Browser source is present; visual/axe/CSP success is not claimed.
- No completed manual QA result set (`pix.json`, timeout, adversarial/privacy, refund/payout and statement receipts) was supplied at the time of inspection; the newly present runner source is not its result.
- No migration-upgrade, restore-rehearsal, full uncommitted-source R, coverage, dependency-audit or resource-cleanup receipt was established here.
- A private runtime credential file remained present for ongoing gates. Its secret contents were not printed or copied into this report; final evidence export and cleanup still need qualification.
- No separate predecessor code review report or notepad path was supplied or found in the requested task evidence directory. Consequently another report's programming/remove-ai-slops coverage cannot be confirmed. The direct skill-perspective check in this report does not invent that missing artifact.
- No runtime reproduction of T57-B1/T57-B2 was performed against a separate immutable snapshot database: the review stayed read-only and the lane database was in active use. Both findings follow from the cited snapshot authorization/comparison predicates. Later unstaged regression tests must be run against their intended baseline and final source before they can qualify a replacement snapshot.
