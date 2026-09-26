# AI risk register

**Status: UNAPPROVED — DO NOT USE LIVE DATA**

One record per AI capability AI-01 to AI-08. Each risk level is a
**proposal** from the product team for the EG-3 clinical safety owner, who
sets the level recorded for use. Each SaMD question is an input for the EG-4
Anvisa assessment, not an answer to it. Nothing here clears a gate, and no
capability runs on real data until its gates in
[applicability-matrix.md](applicability-matrix.md) are cleared. See
[LIVE-DATA-GATE.md](LIVE-DATA-GATE.md).

## Frame

- **CFM Res. 2.454/2026.** Art. 12 asks institutions that develop or use AI
  to run a preliminary risk assessment. Art. 13 sorts solutions into low,
  medium, high or unacceptable risk and says the level must be shown to the
  user. Anexo II, as retrieved on 2026-09-24, describes low, medium and high
  only; the reviewer has to settle how "unacceptable" is judged. Clinic Ops
  acts as developer (Anexo I V) and, for clinics, as distributor (Anexo I
  IX); the clinic is the contracting institution that runs its own
  preliminary assessment (Anexo I XII). These records are inputs for that
  assessment and for a clinic's AI and Telemedicine Commission where art. 14
  requires one.
- **Anvisa.** RDC 657/2022 governs software as a medical device and excludes
  software used only for administrative and financial management (art. 1 §2
  III). RDC 751/2022 Regra 11 places software that informs diagnostic or
  therapeutic decisions in class II, III or IV by the severity of the
  possible impact.
- **Autonomy tiers** follow the plan's AI table: A0 explain, A1 draft, A2
  bounded execution, A3 specific approval.
- **Shared disable controls.** Every capability has a per-capability kill
  switch in the AI gateway (todo 38), a per-clinic entitlement, and its
  provider path revocable in the provider lifecycle registry (todo 4). A
  clinician may switch AI off for an encounter (CFM 2.454 art. 19) and a
  patient may refuse it (art. 5 §3, recorded through todo 20). Turning a
  capability off always leaves the manual path complete.

Sources, retrieval dates and digests are in
[reverification-log.md](reverification-log.md).

## AI-01 ambient scribe

- **Intended use:** turn consented consultation audio into a transcript and
  suggested note sections, each linked to transcript spans, for the clinician
  to accept, edit or reject. Tier A1.
- **CFM 2.454/2026 risk level proposal:** medium. It supports clinical
  documentation without acting on its own, and every suggestion passes
  clinician review before finalization, which matches the Anexo II
  description of medium risk. Reclassification trigger: any path that
  finalizes, signs or sends a note without a clinician action.
- **SaMD question for EG-4:** does drafting clinical notes from audio count as
  providing information for diagnostic or therapeutic decisions (RDC 751/2022
  Regra 11), or does it stay outside the SaMD definition as documentation
  support? What intended-use wording keeps the scribe out of diagnostic
  claims?
- **Limitations:** may mishear drugs, doses, negations or decimals; speaker
  attribution fails with overlapping voices, children and interpreters; no
  inference of exams or findings not spoken; quality on real encounters is
  unknown until the EG-11 corpus is evaluated (todo 47).
- **Disable path:** scribe kill switch and entitlement off; the ASR or LLM
  provider revoked in the lifecycle registry; a clinician stops capture at
  any time; a patient refusal blocks capture for that encounter. Manual
  documentation remains available.

## AI-02 pre-visit brief and chart assistant

- **Intended use:** summarize one patient's permitted records before or during
  a visit and answer questions about them, citing every statement and marking
  what wasn't found. Read only. Tier A0.
- **CFM 2.454/2026 risk level proposal:** medium. It doesn't act, but an
  omission or wrong citation could steer a decision; the clinician checks the
  cited source.
- **SaMD question for EG-4:** is a cited summary of the patient's own record
  information for a diagnostic or therapeutic decision under Regra 11, or
  record navigation outside the SaMD definition?
- **Limitations:** can omit relevant history; can't see records the requester
  isn't authorized for, including restricted notes; never compares patients;
  stale data stays stale.
- **Disable path:** capability kill switch and entitlement off; LLM provider
  revoked. The chart stays fully usable by hand.

## AI-03 document and result extraction

- **Intended use:** read permitted PDFs and images and propose field values
  with page and region anchors for a clinician or operator to confirm before
  anything enters the chart. Tier A1.
- **CFM 2.454/2026 risk level proposal:** medium. A wrong value, unit or
  decimal could affect care if confirmed carelessly; confirmation per field is
  the control.
- **SaMD question for EG-4:** does structured capture of result values count
  as processing for a clinical purpose, or as data entry assistance outside
  RDC 657/2022?
- **Limitations:** poor scans, handwriting and unusual layouts reduce
  accuracy; units are never guessed; a document attached to the wrong patient
  is not detected by extraction alone.
- **Disable path:** capability kill switch and entitlement off; provider
  revoked. Manual entry remains available.

## AI-04 front-desk and patient-access assistant

- **Intended use:** answer patients' administrative questions from the
  clinic's knowledge base, hold slots and book only through approved actions,
  and hand off to a person. No clinical advice. Tier A2, administrative only.
- **CFM 2.454/2026 risk level proposal:** low. Anexo II lists scheduling and
  chatbots giving general health information without personalized clinical
  advice as low-risk examples. It talks to external users (Anexo I VIII), so
  the AI disclosure applies. Reclassification trigger: any triage, symptom
  handling or clinical content in replies.
- **SaMD question for EG-4:** confirm that scheduling and administrative
  replies with a hard ban on clinical advice fall outside RDC 657/2022, and
  name the reply patterns that would pull it in.
- **Limitations:** may misread free text; can't verify identity beyond the
  channel's checks; availability and prices come only from system data, never
  invented; urgent messages go to a human queue.
- **Disable path:** capability kill switch and entitlement off; messaging or
  LLM provider revoked; conversations route to the staff inbox.

## AI-05 care-coordination proposals

- **Intended use:** turn an approved care plan version into typed task and
  follow-up proposals that staff or clinicians approve per action type. Tier
  A1, with A3 bundle approval.
- **CFM 2.454/2026 risk level proposal:** medium. Proposals shape follow-up
  care, and a missed or wrong action could delay care; nothing executes
  without approval.
- **SaMD question for EG-4:** is converting an approved plan into tasks
  workflow management, or a therapeutic-decision aid under Regra 11?
- **Limitations:** works only from the approved plan text; never infers
  changes that weren't written; can mis-time recurring actions.
- **Disable path:** capability kill switch and entitlement off. Staff create
  tasks by hand.

## AI-06 revenue-cycle assistant

- **Intended use:** list discrepancies between financial and claim facts and
  draft appeal text for finance staff. No refunds or submissions without
  approval. Tier A1, with A3 for material actions.
- **CFM 2.454/2026 risk level proposal:** low. It works on administrative and
  financial facts with no direct influence on diagnosis or treatment.
- **SaMD question for EG-4:** confirm it falls under the RDC 657/2022 art. 1
  §2 III exclusion for software used only for administrative and financial
  management.
- **Limitations:** depends on correct claim facts; can't judge payer rules
  not in the data; never invents facts or changes codes to raise billing.
- **Disable path:** capability kill switch and entitlement off. Finance works
  the claim queue by hand.

## AI-07 operations analyst

- **Intended use:** answer operational questions by building metric query
  descriptors from the governed metric catalog and explaining the result. No
  model-written SQL. Tier A0, with A2 reminders under policy.
- **CFM 2.454/2026 risk level proposal:** low. Administrative and operational
  use only.
- **SaMD question for EG-4:** confirm it falls under the same administrative
  exclusion as AI-06.
- **Limitations:** limited to catalog metrics; can't answer outside them;
  protected proxies are blocked, so some breakdowns are refused.
- **Disable path:** capability kill switch and entitlement off. Reports stay
  available.

## AI-08 clinician decision support

- **Intended use:** show a clinician labeled hypotheses about the patient in
  front of them, each with citations to the patient's facts and licensed
  references. It never diagnoses, prescribes or talks to the patient. Tier
  A0/A1. Off until EG-3, EG-4 and EG-13 records exist.
- **CFM 2.454/2026 risk level proposal:** high. It speaks directly to
  diagnostic and therapeutic reasoning, the area Anexo II describes as high
  risk when solutions influence critical medical decisions. The proposal is
  deliberately conservative; EG-3 may lower it only with evaluation evidence.
- **SaMD question for EG-4:** under Regra 11 this looks like software that
  informs diagnostic or therapeutic decisions (class II at least, III or IV
  by impact). What intended-use statement, classification and regularization
  route (notification or registration) apply, and is the capability
  marketable before that decision? The dossier inputs come from todo 46.
- **Limitations:** quality depends on licensed references and on the facts in
  the record; hypotheses can be wrong or incomplete; no validated performance
  exists until the EG-11 corpus evaluation with clinician adjudication.
- **Disable path:** off by default; capability kill switch and entitlement;
  licensed-content access revoked; the panel shows as unavailable, never as
  an empty answer.
