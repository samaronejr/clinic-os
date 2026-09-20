# LIVE BROWSER JOURNEYS

## OVERVIEW
Live clinic journeys return named byte artifacts; the runtime-HTTPS suite borrows already-authenticated contexts.
Scope score: 8; distinct browser domain with a package boundary, dense symbols, and more than ten public definitions.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Patient search/create/pagination | `patient.py` | Receptionist journey and URL-state checks |
| Availability promise/retirement | `availability.py` | Manager workflow and physician challenge |
| Booking, agendas, move/cancel/rebook | `scheduling.py` | Manager and read-only physician journeys |
| Scheduling interaction steps | `scheduling_steps.py` | `Journey`, synthetic dates/persona, no-JavaScript path |
| Retained-context HTTPS capture | `runtime_https.py` | Fixed origin, four personas, PNG artifacts |
| Shared sign-in/audit/capture | `../browser_suite_driver.py` | Common page-driving primitives |
| Visual assertions | `../browser_visual_contract.py` | Viewports, console, forms, blocking findings |
| Suite registry contract | `../browser_runner_contract.py` | Closed known-suite set |

## CONVENTIONS
- `build_*_suite` factories return zero-argument callables producing `dict[str, bytes]`.
- Patient config requires exactly `base_url`, `clinic_id`, `password`, and `username`, all nonempty.
- Availability and scheduling additionally require physician credentials and an injected code provider.
- The runtime-HTTPS factory instead accepts retained `ClinicBrowserContexts`, not the ordinary config dictionary.
- Artifacts use `browser/<suite-id>/`; each journey includes a `summary.json`.
- Returned artifact mappings are sorted by path; screenshots and summaries are bytes, not filesystem paths.
- Ordinary journeys register console/page-error handlers before driving screens.
- Patient, availability, and scheduling reuse the shared viewport audit rather than defining private audits.
- Scheduling keeps its synthetic calendar and interaction details in `scheduling_steps.py`.
- Scheduling covers a JavaScript-disabled booking and a physician own-scope read-only agenda.
- Runtime HTTPS targets only `https://phase1a.qa.clinic-os.dev:8443/readyz`.
- Its persona tuple is `clinic-admin`, `owner`, `physician`, `receptionist`, in that order.
- Runtime HTTPS validates the PNG signature and closes each borrowed-context page after capture.

## ANTI-PATTERNS
- Do not replace the fixed HTTPS origin or substitute fresh contexts for retained authenticated ones.
- Do not introduce extra configuration keys; factory validation is exact-set validation.
- Do not put patient search state in URLs or turn POST-only patient forms into GET forms.
- Do not allow physician journeys to pass without the challenge and read-only scheduling checks.
- Do not treat console warnings/errors or blocking accessibility findings as successful visual evidence.
- Do not duplicate shared sign-in, form, viewport, or capture logic in individual suites.

## RELATED CHECKS
- `tests/test_browser_visual_evidence.py`: patient suite configuration and visual evidence contracts.
- `tests/test_browser_availability_session.py`, `tests/test_browser_scheduling_session.py`: session wiring.
- `tests/browser/test_runtime_https.py`: production-HTTPS context and capture contract.
