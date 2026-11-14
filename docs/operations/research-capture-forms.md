+++
forms_id = "research-capture-v1"

[[forms]]
id = "usability-observation"
protocol_id = "usability-v1"
fields = ["session_id", "consent_flag", "role_stratum", "device_stratum", "task_id", "outcome_code", "elapsed_seconds", "hint_count", "severity_codes", "withdrawn_flag"]

[[forms]]
id = "scribe-encounter-timing"
protocol_id = "scribe-effort-v1"
fields = ["session_id", "consent_flag", "sequence_code", "period", "arm", "encounter_index", "documentation_seconds", "correction_count", "significant_error_flag", "void_flag"]
+++

# Research data capture forms (Operations)

These are the paper or spreadsheet forms an owner-appointed observer fills
during EG-10 sessions. They hold no PHI and no personal data. Each row is
keyed by an opaque session id the owner issues; the mapping from session id
to a person, if any, stays with the owner and outside this repository.

Don't add name, email, phone, CPF, date of birth, free-text quotes or any
clinical content to these forms. Values are codes, counts and seconds only.

## usability-observation

| Field | Allowed values |
| --- | --- |
| session_id | opaque owner-issued id |
| consent_flag | yes or no |
| role_stratum | clinician, reception, clinic_admin, finance |
| device_stratum | desktop, tablet, phone |
| task_id | UC-01 to UC-08 |
| outcome_code | U, A, F, T |
| elapsed_seconds | integer |
| hint_count | integer |
| severity_codes | list of 1 to 4 |
| withdrawn_flag | yes or no |

## scribe-encounter-timing

| Field | Allowed values |
| --- | --- |
| session_id | opaque owner-issued id |
| consent_flag | yes or no |
| sequence_code | MS (manual first) or SM (scribe first) |
| period | 1 or 2 |
| arm | manual or scribe |
| encounter_index | integer within the period |
| documentation_seconds | integer |
| correction_count | integer |
| significant_error_flag | yes or no |
| void_flag | yes or no |
