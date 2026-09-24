# PATIENT INTAKE

## OVERVIEW
Organization patients with clinic-specific enrollments and private registry screens; score 11 for the distinct patient-data boundary.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Public API | `services.py` | Patient creation/search and typed results/errors |
| Persistent identity/enrollment | `models.py` | `Patient`, `PatientClinicEnrollment` |
| Registration and replay | `patient_creation.py` | Patient/enrollment pair under one idempotency key |
| Registry queries | `patient_search.py` | Selected-clinic filtering and deterministic pages |
| Manager authorization | `access.py` | Trusted actor role gate |
| Database policies | `rls.py`, `migrations/0001_patient_and_enrollment.py` | Patient and enrollment boundaries |
| HTTP forms and responses | `forms.py`, `views.py`, `urls.py` | Blank GET, submitted POST, HTMX partials |
| Templates | `../../templates/intake/` | Full screens and result partial |
| Regression coverage | `../../tests/test_patient_*.py` | Services, privacy, context, concurrency and timezone races |

## CONVENTIONS
- `create_patient` returns both patient and selected-clinic enrollment.
- Replay lookup uses organization plus idempotency key and compares canonical fingerprints.
- The shared clinic advisory lock serializes creation with timezone changes.
- Authorization and replay are checked again after acquiring that lock.
- Birth date is validated against the locked clinic's local date, not server date.
- Patient/enrollment insertions use a savepoint; integrity conflicts can resolve to an existing replay.
- Search requires a normalized 2-100-character query and a positive integer page.
- Pages contain 25 rows ordered by lowercased name, birth date, then patient ID.
- Search results expose enrollment IDs for booking, not a free-form patient selector.
- GET renders a blank search; actual search and pagination submit POST bodies.
- Successful registration redirects with 303, or 204 plus `HX-Redirect` for HTMX.
- Invalid registration submissions retain their submitted idempotency key.

## ANTI-PATTERNS
- Do not put patient queries, names, birth dates or search-result state in browser URLs.
- Do not book against a raw patient identifier where a clinic enrollment is required.
- Do not calculate birth-date validity before the clinic timezone gate is held.
- Do not append a second create audit event when returning an idempotent replay.
- Do not use an unrestricted patient query in place of the clinic enrollment search.
