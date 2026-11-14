+++
template_id = "research-results-v1"
required_row_fields = ["protocol_id", "prereg_sha256", "target_id", "stratum", "numerator", "denominator", "excluded", "exclusion_reason_code", "session_ids"]
+++

# Research results template

Copy this file per study run. Fill it only from real, owner-arranged
sessions (EG-10). Leave it unfilled until then; an empty copy is not a
result and doesn't satisfy any gate.

Rules:

- Every row carries a numerator and a denominator. A rate with no
  denominator isn't a result and must not be reported.
- `prereg_sha256` must equal the hash in the protocol front matter that was
  in force at the first session. A mismatch means the protocol changed
  after pre-registration and the row is flagged.
- `session_ids` are opaque ids from the capture forms. No names, no patient
  data, no free text.
- Cells with no attempts are written as 0 and 0, never left out.

| protocol_id | prereg_sha256 | target_id | stratum | numerator | denominator | excluded | exclusion_reason_code | session_ids |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
