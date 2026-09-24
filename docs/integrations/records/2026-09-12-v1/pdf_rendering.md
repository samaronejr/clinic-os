# PDF rendering

| Record field | Value |
| --- | --- |
| Capability | `pdf_rendering` |
| Record version / reviewed date | 2026-09-12-v1 / 2026-09-12 UTC |
| Status | unavailable; documentation only |
| Selected provider | None |
| Candidate / reference | Unselected; WeasyPrint is a reference example. |
| Accountable owner / decision | Unassigned; technical package owner and clinical document reviewer required. |
| Approval / contract | None supplied; provider API/profile/package version unverified unless stated below |
| Sandbox / executed provider evidence | Unavailable; none executed |
| Residency / subprocessors / retention | Unverified; owner-approved region, processing agreement, retention and legal-hold handling required |
| Costs / budget | Unverified; historical prices are not approval or current quotes |
| Required packages | Unapproved: WeasyPrint or another renderer, pinned Python/system dependencies, fonts and licenses. |
| Dependent tasks | 33, 34, 35, 36, 44 |
| Review trigger | Provider selection, protocol/package change or release; no approval expiry exists |

## Source-backed observations

[WeasyPrint first steps](https://doc.courtbouillon.org/weasyprint/stable/first_steps.html) documents a Python/system-dependent HTML/CSS-to-PDF renderer. No renderer is declared in the current pyproject.toml. Renderer output does not establish a qualified signature.

## Contract required before use

Approve renderer/version, system libraries, font files/licenses, pt-BR glyphs, pagination and required PDF profile. Restrict untrusted HTML/CSS, URL/file fetching, network access and resource size/time. Pin exact document version and output bytes before review/signing; sign the reviewed immutable bytes, not a later rerender. Decide determinism requirements and normalization before issuance.

## Required future fixtures

Authorized local synthetic fixtures covering long/overflowing text, accents, page breaks, missing fonts/assets, external/file URL denial and resource limits; inspect rendered pages plus output digest.

These are acceptance requirements, not executed sandbox results. Fixtures may
contain only synthetic or explicitly provider-authorized data.

## Unavailable branch

Task 33 real rendering waits for explicit package/system/font approval. A mock or browser printout stays synthetic and cannot unlock task 34 signing or task 35 verified distribution.

Follow the [register's versioning and approval rules](../../capabilities.md).
