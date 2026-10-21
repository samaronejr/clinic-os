# Specialty packs and deterministic calculators

All clinical activation remains BLOCKED-ON-EG. Installing a bundle is not an
external clinical, privacy, licensing or provider approval.

## Versioned installation

`install_pack(*, clinic_id, pack_key, version)` installs the immutable
`general`, `pediatrics`, `psychiatry` or `procedures` version-1 bundle. It
publishes a v2 SOAP template and an original, unscored narrative questionnaire,
then binds their exact ids and calculator versions in `SpecialtyPack`.
Only a current clinic owner or clinic administrator installs or uninstalls.
The installing administrator is recorded as the local `clinical_owner`
(content steward), not represented as an external clinical approver.

Each clinic chooses which bundles to install and their retained ordering
(the todo's A-11 ordering reference). Initial order follows installation;
`reorder_packs(*, clinic_id, pack_keys)` requires exactly the clinic's active
bundles, once each. Native move controls are administrator-only and serialize
with installation and retirement. Reordering changes no historical content.
Retirement is terminal and idempotent
for an exact version: a retired version requires a future published bundle
version, rather than silently reviving it. Pickers hide retired content;
database guards reject new notes, questionnaire assignments and specialty
entries against it. Already assigned forms, recorded facts, notes and their
corrections retain their exact bindings.

The pack's template and questionnaire are its single-entry `templates` and
`questionnaires` bundles. Future versions can extend bundle membership without
changing historical content. Every install, retirement and specialty entry
has a registered metadata-only audit event. No clinical field enters audit.

## Clinical records

Pediatrics reuses encrypted observations for growth recording and the
existing vital trend/table. There is no percentile computation, reference
curve, normal range or inferred growth interpretation. Growth percentiles
are visibly unavailable pending authorized reference data under EG-13.
Corrected age is a separate transient calculation. Immunization history
records the vaccine, administration date, dose and unconfirmed origin
exactly as reported; it neither forecasts a schedule nor recommends a dose.

Psychiatry installs an original mental status examination template without
normal defaults. A document restriction is inserted before its first note
version, with no automatic grants. The database refuses a direct initial
version without that restriction. Other physicians need the existing
revocable note grant **and** ordinary current clinical/care authority.
Retirement does not remove this restriction.

Procedures retain an encrypted plan and a real, available consent-document
attachment for the same encounter. Image references are bounded to 20
distinct available JPEG/PNG attachments from that encounter. A linked
document is evidence selected by the clinician, not an inferred patient
consent or proof that the procedure was performed. The attachment workspace
continues to own upload, quarantine, scanning and authorized retrieval.
Materials linkage belongs to todo 61 and is visibly unavailable here.

## Sources and permissions

Re-verification date: 2026-10-21. Mathematical formulas are implemented
independently in Python using `Decimal`, with owned precision 48 and
half-even rounding once to 0.01. No provider or LLM is involved.

| Key/version | Source and implementation | Release status |
| --- | --- | --- |
| `bmi/1` | CDC, [About Body Mass Index](https://www.cdc.gov/bmi/about/index.html); kilograms / metres squared. Explicit kg/g and m/cm conversions. Direct CDC retrieval was denied; its indexed primary [training example](https://www.cdc.gov/growth-chart-training/hcp/using-bmi/plotting-interpreting-bmi.html) and [CDC training PDF](https://stacks.cdc.gov/view/cdc/141887/cdc_141887_DS1.pdf) verify 16.9 kg / 105.4 cm, published as 15.2 at one decimal place. The implementation independently derives 15.21 at its declared two-decimal precision. Other BMI vectors are synthetic arithmetic/conversion checks, not claimed publisher cases. No pediatric interpretation or chart data are shipped. | Published mathematical formula; no chart data or CDC text distributed |
| `egfr/1` | National Kidney Foundation, [CKD-EPI creatinine equation (2021)](https://www.kidney.org/ckd-epi-creatinine-equation-2021), fetched 2026-10-21. `142 * min(Scr/k,1)^alpha * max(Scr/k,1)^(-1.200) * 0.9938^age * sex_coefficient`; female k=0.7, alpha=-0.241, coefficient=1.012; male k=0.9, alpha=-0.302, coefficient=1. Scr in mg/dL; umol/L divided by 88.4. Completed age >=18. Output is indexed mL/min/1.73m2, with no race term, disease classification, dosing or claim of suitability for acute kidney injury. | Published mathematical formula, no restricted reference dataset distributed |
| `corrected_age/1` | American Academy of Pediatrics, [Corrected Age For Preemies](https://www.healthychildren.org/English/ages-stages/baby/preemie/Pages/Corrected-Age-For-Preemies.aspx), last updated 2018-12-10, fetched 2026-10-21. Chronological weeks minus (40 minus gestational weeks). Published example: 16 chronological weeks and 32 gestational weeks -> 8 corrected weeks. Available only through the source's first-two-year scope (104 chronological weeks). Negative values before the due date are retained, not clamped or interpreted. | Independently implemented published arithmetic; no AAP prose distributed |
| `growth_percentile/1` | EG-13, authorized reference dataset and version required | Unavailable: license pending; recording remains usable |
| `phq9/1`, `gad7/1` | Pfizer [PHQ Screeners terms](https://www.phqscreeners.com/terms) and the publisher's [PHQ and GAD-7 instructions](https://www.phqscreeners.com/images/sites/g/files/g10016261/f/201412/instructions.pdf), fetched 2026-10-21. The terms exempt screeners from general Pfizer copyright restrictions and describe use as free; this is permission evidence, **not** a public-domain determination. Instruction pages 3 and 5-6 specify integer item scores 0-3, nine or seven items, sums 0-27 or 0-21; the PHQ-9 worked example totals 16. | Publisher-permitted score-entry arithmetic only. Enter all item scores from the official unchanged instrument. No translated questions, substitute instrument, diagnostic algorithm or threshold classification ships. The local clinical review and all live activation remain BLOCKED-ON-EG. |

Reference equality tests cover explicit unit conversion and kappa boundaries;
the adult eGFR boundary values are independently derived from the published
formula, not labeled as publisher-supplied clinical test cases.

## Input and refusal contract

Each calculator exposes `compute(inputs) -> Calculation | Refusal`. The closed
dispatcher also requires the exact key and integer version. Inputs are
canonical, nonnegative decimal **strings using a point**, never floats,
locale-coerced numbers, exponent notation, grouping or mixed comma/point
notation. The UI states that contract beside the fields. Quantities and unit
choices are explicit; zero/negative denominators, missing/extra fields,
non-finite numbers and unsupported versions refuse without a partial result.
No score, result or clinical text is stored in browser storage or URLs, and
calculations do not populate or save findings.

## Database and recovery contract

Both new tenant models have FORCE RLS, closed policy sets, no runtime hard
delete or truncate, and least-privilege column-only pack retirement.
Shared transaction advisory locks serialize new use against uninstallation
without giving the resolver UPDATE privileges just to lock a row.
Record details are purpose-enveloped from creation. Migration 0015 is
non-atomic with an atomic protected-schema publication; it is irreversible;
rollback = restore. Both durable relations are included in the live-derived
recovery manifest and its isolation guard.
