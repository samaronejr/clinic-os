# SCHEDULING WORKFLOWS

## OVERVIEW
Availability, staff booking, agendas and appointment transitions; score 14 for the largest workflow and concurrency domain.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Public facade | `services.py` | Explicit re-exports and typed domain errors |
| Availability lifecycle | `availability_creation.py`, `availability_retirement.py`, `availability_view.py` | Create, retire, scoped listing |
| Booking preparation | `booking_queries.py`, `booking_views.py` | Authorized enrollment/practitioner/availability choices |
| Appointment creation | `appointment_creation.py`, `appointment_values.py`, `appointment_persistence.py` | Validate, fingerprint, replay, persist |
| Write serialization | `appointment_locking.py`, `locks.py` | Advisory gates followed by row locks |
| Cancel/reschedule | `appointment_cancellation.py`, `appointment_rescheduling.py`, `appointment_transition_state.py` | Terminal state and transition rechecks |
| Agenda reads | `agenda_queries.py`, `agenda_presenter.py`, `agenda_views.py` | Role-scoped query, presentation, HTTP |
| Transition/availability HTTP | `transition_views.py`, `views.py`, `forms.py`, `appointment_forms.py` | Safe continuations and submitted forms |
| Local civil time | `timezones.py` | IANA validation, unambiguous local minutes, day/week boundaries |
| Database protections | `models.py`, `rls.py`, `migrations/` | Exclusions, lifecycle constraints and ACLs |
| Regression coverage | `../../tests/test_appointment_*.py`, `test_availability_*.py`, `test_agenda_*.py`, `test_booking_queries.py` | API, HTTP, RLS and races |

## CONVENTIONS
- Keep the service facade stable while implementations remain split by operation.
- Lock domains are ordered identity, clinic, organization-patient, then user.
- `acquire_advisory_locks` rejects duplicate or globally misordered keys.
- Appointment creation rechecks authorization, replay, practitioner and availability after locking.
- Patient gates span clinics within an organization; practitioner gates use user IDs.
- Appointment persistence distinguishes a new insert from an idempotent replay.
- Local input is exactly `YYYY-MM-DDTHH:MM`; ambiguous and nonexistent local minutes are rejected.
- Stored/formatted instants must be aware UTC minutes with zero seconds/microseconds.
- Agenda day/week bounds are civil boundaries, not fixed-duration UTC arithmetic.
- Weeks run Monday to Monday; intervals are half-open.
- Clinic timezone dependency checks include enrollment, availability and appointment tables.
- Patient self-booking, waitlists and reminders are not implemented workflows.

## ANTI-PATTERNS
- Do not hard-delete appointments; database enforcement requires lifecycle transitions.
- Do not revive a cancelled appointment or bypass terminal-state checks.
- Do not replace overlap exclusions and post-lock validation with an optimistic preflight alone.
- Do not bypass shared lock ordering in identity or intake callers.
- Do not silently choose a daylight-saving fold or normalize a nonexistent local time.
- Do not change a populated clinic's timezone as a display-only preference.
