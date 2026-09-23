# Software Development Plan — Clinic OS “Wave 1” MVP (Solo Developer, Brazil)

## TL;DR
- **Build a modular monolith with Django + PostgreSQL (multi-tenant using a shared schema + `tenant_id` + Row-Level Security), hosted on AWS sa-east-1 (São Paulo), with video through Daily.co, prescriptions signed through cloud-based ICP-Brasil certificates (BirdID/VIDaaS), and PIX billing through Asaas** — this combination maximizes the productivity of a solo Python developer while already addressing LGPD/CFM/ANVISA regulatory constraints without the overengineering of microservices.
- **Realistic timeline: approximately 7–9 months full-time (40 hours/week), or approximately 14–18 months at 20 hours/week**, to reach a pilot with a partner clinic using real data. “Minimum viable compliance” — privacy policy, operator–controller DPA, hash-chained audit ledger, encryption, PITR backups, and a three-business-day incident-response plan — is mandatory *before* the first real patient is onboarded.
- **Critical signature decision:** the gov.br signature service **cannot** be integrated by a private company; it is restricted to public-sector bodies and requires hosting on a government domain. Therefore, for electronic prescriptions, the MVP should use cloud-based ICP-Brasil certificates (BirdID/VIDaaS), providing a *verifiable advanced/qualified electronic signature*. The data structures should be prepared for SNCR — now postponed by ANVISA **until September 30, 2026**, under RDC 1.028/2026 — and for the mandatory qualified signature of controlled-substance prescriptions in later waves.

## Key Findings

1. **Use a modular monolith, not microservices.** For a solo developer, microservices would be operational suicide. The recommendation is a Django modular monolith with explicit module boundaries — isolated Python packages with interfaces and adapters — in which the three high-risk services, Prescription, Consent, and Audit Ledger, are independently testable modules that can be extracted later. Django beats FastAPI here because its “batteries included” capabilities — admin, ORM, authentication, migrations, and RBAC — save weeks of work, which is decisive for a one-person team.

2. **Multi-tenancy: shared schema + `tenant_id` + PostgreSQL Row-Level Security (RLS).** This is the recommended pattern for a healthcare SaaS built by a solo developer: low operational cost, one migration path, and — crucially — RLS moves tenant isolation from application code into the database. This eliminates the most dangerous class of bug: cross-tenant leakage caused by a forgotten `WHERE tenant_id` clause. A schema-per-tenant model is justified only with fewer than five fixed customers or a contractual requirement for physical isolation.

3. **Append-only audit ledger with SHA-256 hash chaining.** Each `AUDIT_EVENT` stores a hash of its own content plus the hash of the previous event, forming a chain similar to Git or certificate-transparency logs. Immutability is reinforced in three layers: (a) a PostgreSQL trigger that raises an exception on UPDATE or DELETE; (b) an application role limited to INSERT and SELECT; and (c) cryptographic verification of the chain. This supports the SBIS NGS1.08 audit requirement and improves medico-legal defensibility.

4. **The build-versus-buy decision should strongly favor “buy” for regulated and infrastructure components.** Video through Daily.co with a healthcare/BAA arrangement, WhatsApp through a BSP, PIX through Asaas, signatures through BirdID/VIDaaS, and CRM registration verification through the CFM web service should be purchased and integrated rather than built. Build in-house: scheduling, EHR, intake, consent, audit ledger, RBAC, and multi-tenant configuration.

5. **gov.br signatures are not viable for private-sector integration** — a finding confirmed in primary sources, including the `servicos.gov.br` integration manual and SGD/MGI Ordinance No. 7,076/2024. Credentials are available only to a “Public Manager,” and production approval requires hosting on an official government domain such as gov.br or jus.br. Therefore, cloud-based ICP-Brasil certificates — BirdID from Soluti or VIDaaS from Valid — are the practical route. The good news is that **RDC No. 1,000/2025, Article 11**, establishes that “prescriptions subject to retention and issued electronically must be signed with an advanced or qualified electronic signature,” covering antimicrobials and GLP-1 receptor agonists. It requires a **qualified ICP-Brasil signature** only for Prescription Notifications and Special Control Prescriptions under Article 8. An advanced signature can include gov.br, but because private systems cannot integrate it, a cloud-based ICP-Brasil certificate is the practical choice that satisfies *both* cases.

6. **Regulatory deadlines that shape the roadmap:**
   - **SNCR:** the mandatory integration deadline for electronic controlled-prescription services was officially **postponed until September 30, 2026**, under **ANVISA RDC 1.028/2026**; the previous deadline was June 1, 2026. Integration will occur through an ANVISA API, and requests for prescription numbering require a qualified signature.
   - **Security incidents:** **ANPD Board Resolution No. 15 of April 24, 2024**, Article 6, requires notification to the ANPD “within three business days”; Article 9 establishes the same period for notifying the data subject. The deadline is **doubled for small-scale processing agents** under ANPD Board Resolution No. 2/2022. The former deadline was two business days.
   - **Medical-record retention:** an **electronic/born-digital medical record must be retained permanently** under CFM Resolution No. 1,821/2007, Article 7, which establishes permanent retention for patient records archived electronically. The minimum 20-year period in Law No. 13,787/2018, Article 6 — after which paper and digitized records may be destroyed — applies to paper and digitized records, **not** born-digital records. The product default should therefore be permanent retention with legal hold.

## Details

### 1. Recommended Technology Stack and Rationale

| Layer | Recommendation | Rationale for a Solo Python Developer in Brazil |
|---|---|---|
| **Backend** | **Django 5.x + Django REST Framework** | “Batteries included”: admin, ORM, migrations, authentication, and permissions are ready to use. FastAPI can require approximately 30–40% less code for pure APIs and may be faster, but for a full-product SaaS with admin, RBAC, and forms, Django saves weeks for a one-person team. It also has a mature Portuguese-language ecosystem. |
| **Async/real-time** | Django + **HTMX** for the server-rendered frontend, with React islands where needed, such as the video room | HTMX dramatically reduces the JavaScript surface area for a solo developer. Reserve React/Next.js for the teleconsultation experience and interactive scheduling only. |
| **Database** | Managed **PostgreSQL 16**, through RDS or equivalent, with **RLS** | Secure multi-tenancy, extensions such as `pgcrypto` and `pgaudit`, JSONB for templates and consent artifacts, and point-in-time recovery. |
| **Queues/tasks** | **Celery + Redis**, or the simpler `django-q2` | Supports email/SMS/WhatsApp reminders, PDF generation, and payment/signature webhooks. |
| **PWA frontend** | React only for interactive screens; use a PWA rather than a native app | A PWA covers mobile use for the MVP; a native app belongs on the “do not build yet” list. |
| **Authentication** | **Django’s own authentication + 2FA using `django-otp`/TOTP**; consider self-hosted Keycloak only if SSO/OIDC becomes necessary | Keeping identity in your own PostgreSQL database avoids dependence on a foreign SaaS and aligns well with RLS. Clerk and Auth0 provide excellent developer experience but introduce coupling and data-residency questions. Supabase Auth — 50,000 free monthly active users, then approximately US$0.00325 per MAU — is worthwhile only if Supabase is adopted as the broader platform. |
| **Video** | **Daily.co** for telehealth, with BAA/HIPAA support on a paid plan, GDPR support, and EU–U.S. Data Privacy Framework participation | It offers the fastest API integration. Recording and transcription can document the teleconsultation in the medical record, as required by CFM Resolution 2,314/2022. LiveKit Cloud becomes a cheaper alternative at scale above approximately 200,000 minutes/month and can also be self-hosted under Apache 2.0. |
| **WhatsApp** | **Meta Cloud API through a BSP**, such as 360dialog or Zenvia | 360dialog offers lean SaaS pricing with no markup on Meta fees and plans starting around US$59/€49 per month. Meta discontinued the On-Premises model, with a gradual sunset through 2026. Since July 1, 2025, Meta charges per template message sent rather than per 24-hour conversation window. |
| **Email/SMS** | Transactional email through Amazon SES or Resend; SMS through Zenvia or Twilio | SES is inexpensive and remains within AWS. SMS is costly, so prioritize WhatsApp and email. |
| **PIX** | **Asaas** for PIX billing and receipts | No monthly fee, charges only upon settlement, a simple API, sandbox support, and webhooks. It is the 31st payment institution authorized by the Central Bank of Brazil, code 461. |
| **Signature/prescription** | **Cloud-based ICP-Brasil: BirdID (Soluti) or VIDaaS (Valid)**; optionally embed **Memed** for prescription workflows | BirdID and VIDaaS support hash-based API signing using CMS/PAdES and formats such as RAW ASN.1 PKCS#1 and CMS-detached. Memed offers more than 80,000 medications, interaction checking, and a systems API, avoiding the need to build a medication database; however, it creates vendor dependence and covers prescriptions only, not the medical record. |
| **CRM verification** | **CFM Physician Listing Web Service** under CFM Resolution 2,129/2015 | Verifies that a physician’s registration status is “Regular.” Annual subscription is R$948.00 for a private company and free for public entities, through SEI-Medicina. It returns CRM number, state, registration type/status, and specialty. Infosimples is an alternative API. |
| **Hosting** | **AWS sa-east-1, São Paulo** | The LGPD does not require Brazilian data residency, but latency and healthcare-customer perception favor Brazil. Use managed PostgreSQL with PITR. Hetzner and Fly.io are approximately 5–10 times cheaper but lack a Brazilian region and managed PostgreSQL; reserve them for staging or CI. |
| **Observability** | **Sentry** for errors plus uptime monitoring through Better Stack or UptimeRobot | Low cost and essential when the bus factor is one. |
| **Repository/CI** | **GitHub + GitHub Actions** | Testing, linting, and deployment. |

### 2. Architecture — Modular Monolith and Module Map

Module boundaries should align with the report’s domain model. Each module should be a Python package with an explicit internal API based on services and adapters, allowing future extraction:

- **`identity`** — ORGANIZATION > CLINIC > USER, USER_CLINIC_ROLE for RBAC, 2FA, short-lived tokens, and step-up authentication before document issuance.
- **`tenancy`** — tenant resolution. Middleware sets `app.tenant_id` in the PostgreSQL session through `SET LOCAL` inside a transaction, activating RLS policies. Also supports clinic-specific overlays such as branding, templates, and consent text.
- **`scheduling`** — multi-professional calendars, online self-scheduling, waitlists, and email/SMS/WhatsApp reminders.
- **`intake`** — patient registration and pre-consultation forms.
- **`ehr`** — PATIENT > APPOINTMENT > ENCOUNTER > CLINICAL_DOCUMENT; SOAP notes and specialty templates, problem lists, allergies, attachments, document versioning, and a controlled amendment model. Never hard-delete; supersede with a signed amendment.
- **`teleconsult`** — video room using a Daily.co adapter, waiting room, consent capture, encounter documentation in the same workflow, and recordings linked to the ENCOUNTER.
- **`prescription`** *(high risk, isolated)* — prescription generation, signature adapter for BirdID/VIDaaS/Memed, and data fields prepared for SNCR and qualified signatures.
- **`consent`** *(high risk, isolated)* — CONSENT_ARTIFACT.
- **`audit`** *(high risk, isolated)* — append-only AUDIT_EVENT ledger with hash chaining.
- **`billing`** — PIX through an Asaas adapter and receipts.
- **`comms`** — patient communications through email, SMS, and WhatsApp adapters.
- **`retention`** — policy-based retention engine plus legal hold.
- **`interop`** *(stub in Wave 1)* — external FHIR model using RNDS profiles. HL7 FHIR R4 is mandatory for exchanges with RNDS. Internally, use a simple relational model.

Encryption: TLS 1.3 in transit; AES-256 at rest with tenant-scoped envelope encryption through `pgcrypto` and KMS. Require 2FA for physicians and administrators.

### 3. Phased Roadmap and Milestones

Effort assumptions: **Scenario A = full-time, approximately 40 hours/week**; **Scenario B = approximately 20 hours/week**. Use an AI coding assistant, such as Claude Code, extensively to increase productivity, while requiring human review for every compliance-sensitive path.

| Phase | Scope | Scenario A | Scenario B |
|---|---|---|---|
| **0. Foundation** | Repository, CI/CD, lightweight IaC, managed PostgreSQL with PITR, Sentry, Django skeleton and modules, RLS multi-tenancy, `identity` + RBAC + 2FA | 3–4 weeks | 6–8 weeks |
| **1. Scheduling** | Multi-professional calendars, self-scheduling, waitlist, and reminders; the first feature a pilot clinic can use early | 4–6 weeks | 8–12 weeks |
| **2. EHR/Encounters** | Intake, ENCOUNTER, SOAP notes/templates, problem lists, allergies, attachments, versioning, and amendment model | 6–8 weeks | 12–16 weeks |
| **3. Teleconsultation** | Daily.co video, waiting room, consent, recording, and documentation within the workflow | 3–4 weeks | 6–8 weeks |
| **4. Prescriptions/Documents** | Non-controlled prescriptions, cloud-based ICP-Brasil signatures, PDF + QR code, and structures prepared for SNCR and qualified signatures | 4–5 weeks | 8–10 weeks |
| **5. Billing** | PIX through Asaas and receipts | 2–3 weeks | 4–6 weeks |
| **6. Beta + Pilot** | Hardening, minimum viable compliance, and onboarding a partner clinic with real data | 3–4 weeks | 6–8 weeks |
| **Total** | | **Approximately 7–9 months** | **Approximately 14–18 months** |

**Cadence:** two-week sprints with a demonstrable increment in every sprint. **Design-partner strategy:** recruit one partner clinic — ideally a small practice and a personal contact in Rio de Janeiro — during Phase 1, initially using only scheduling, then expand its use in each phase.

**DO NOT build in the MVP:** TISS/insurance billing; SNCR integration beyond preparing the data structures; stock or inventory control; physician-discovery marketplace or portal; native app, since the PWA is sufficient; full RNDS/FHIR integration beyond a stub; controlled-substance prescriptions or Prescription Notifications; multilingual medical records; or advanced BI/analytics.

### 4. Wave 1 Compliance-by-Design Checklist

**Must be included in the MVP:**
- **Consent artifact (`CONSENT_ARTIFACT`)** capturing the legal basis under LGPD Articles 7 and 11 — consent is the most conservative basis for sensitive health data — text version, language, timestamp, IP/device, data-subject identification, hash of the accepted text, and a revocation mechanism. Revocation should be recorded rather than deleting the consent. Teleconsultation consent is mandatory under Law 14,510/2022 and CFM Resolution 2,314/2022 and must be stored in the medical record.
- **`AUDIT_EVENT` schema:** event type; originating component, including IP or identifier; originating user; unique and permanent identifier of the affected record; synchronized UTC timestamp; and `prev_hash` + `curr_hash` using SHA-256. *Do not include clinical data or patient-identifying information in the audit trail*, as required by SBIS. Synchronize the system clock to UTC.
- **Practical “NGS2-ready” design:** digital-certificate authentication at document-issuance points; verifiable PAdES/CAdES digital signatures; an audit trail aligned with NGS1.08; and full backups with integrity verification during restoration. SBIS certification is not required at launch — it is a design target — but the structures should support NGS2 later. NGS2 extends NGS1 through the use of ICP-Brasil A3/A1 certificates and advanced digital signatures.
- **Retention/legal hold:** default to permanent retention for born-digital clinical records under CFM Resolution 1,821/2007; provide a legal-hold flag that blocks any purge; and maintain a retention/timing marker.
- **Electronic documents under CFM Resolution 2,299/2021:** mandatory fields are physician identification and CRM, patient identification, date/time, and digital signature. Verify that the physician’s registration is regular through the CFM web service. The issuance date is the electronic-signature date and must not be changed afterward.
- **Minimum viable compliance BEFORE the first real patient:** privacy policy; DPA defining the operator–controller relationship, in which the SaaS is the *operator/processor* and the clinic is the *controller*; security baseline with TLS 1.3, AES-256, 2FA, RLS, and secrets management; incident-response plan supporting notification to the ANPD and affected data subjects within three business days under Resolution 15/2024; tested PITR backups; record of processing activities, or ROPA; and formal designation and publication of a data-protection officer. The developer may initially hold that role, but it must be formally assigned. Maintain a record of every incident for at least five years under Resolution 15/2024, Article 10.

**May be deferred:** SNCR integration; mandatory qualified signatures for controlled prescriptions; SBIS/CFM S-RES certification; RNDS integration; and a complete formal DPIA/RIPD, although it is recommended before scaling.

### 5. Testing and DevOps Plan

- **Proportionate but rigorous testing on critical paths:** use `pytest` for unit and integration tests, with high mandatory coverage for `prescription`, `consent`, `audit`, RBAC, and RLS isolation. Include a test proving that tenant A cannot read tenant B’s data. Use **contract tests** for all external adapters — Daily, Asaas, BirdID, WhatsApp, and CFM — with mocks and sandboxes. Use `Playwright` for end-to-end testing of scheduling, teleconsultation, and prescription issuance. Include a ledger hash-chain verification test using `verify_chain()`.
- **CI/CD:** GitHub Actions for `ruff`, `mypy`, tests, builds, and deployments. Use lightweight IaC through Terraform or Docker Compose plus scripts. Do not use Kubernetes.
- **Backups:** managed PostgreSQL with PITR and periodically tested restoration.
- **Security maintainable by one person:** dependency scanning with Dependabot and `pip-audit`; secrets in the cloud secret manager rather than the repository; an OWASP ASVS-lite checklist. Broken Access Control is the number-one OWASP Top 10 risk and exactly the class of problem RLS helps mitigate. Maintain a ROPA/processing-record template and review AI-generated pull requests especially carefully when they affect compliance code.
- **AI coding assistants:** use Claude Code for boilerplate, tests, migrations, and documentation. **Guardrail:** all code in high-risk modules — prescription, consent, audit, and RBAC — must undergo line-by-line manual review and targeted testing before merge. Never blindly merge AI-generated regulated code, with particular attention to generated queries that could bypass tenant scope.

### 6. Approximate Tooling and Infrastructure Costs for a Solo-Developer MVP/Pilot

- **AWS sa-east-1:** approximately US$50–150/month during the pilot for an RDS db.t-class Single-AZ instance, a small EC2 instance or container, and SES. RDS is expensive, so start small and scale. Reference figures: a db.t4g.micro Single-AZ instance at approximately US$14/month and AWS egress at approximately US$0.09–0.12/GB.
- **Daily.co:** free tier of approximately 10,000 minutes/month, followed by pay-as-you-go pricing of roughly US$4 per 1,000 minutes as a market reference, plus a HIPAA/BAA package on a paid plan.
- **Asaas:** no monthly fee; PIX at R$0.99 per transaction during the first three months and then R$1.99. The first 100 monthly transactions using a static key or QR code are free; this does not apply to dynamic invoice QR codes.
- **BirdID (Soluti):** “The Bird ID Cloud Digital Certificate costs R$149.90 and is valid for five years, with 5,000 transactions valid for one year”; top-ups cost R$49.90 for five transactions and R$99.90 for 50 transactions. **Important:** each physician needs an individual e-CPF certificate.
- **CFM web service:** R$948.00/year for a private company through SEI-Medicina; free for public entities.
- **360dialog for WhatsApp:** monthly SaaS fee starting around US$59/€49 per month, plus Meta conversation/template fees passed through without markup.
- **Sentry/uptime:** free or low cost.
- **Test certificate + targeted legal advice:** reserve budget for both; see Recommendations.

## Recommendations

**Step 1 — immediately, Weeks 0–4:** establish the technical foundation: Django, RLS multi-tenancy, identity/RBAC/2FA, CI/CD, PostgreSQL with PITR, and Sentry. Implement the **hash-chained audit ledger** early because it is cross-cutting and expensive to retrofit. Make the cross-tenant isolation test one of the first tests. Enable `FORCE ROW LEVEL SECURITY` and run the application under a database role without `BYPASSRLS`.

**Step 2 — Weeks 4–12:** deliver **scheduling** and place one partner clinic on the scheduling and reminders module. Progress benchmark: the clinic schedules at least one full week of real appointments through the product.

**Step 3:** implement EHR/encounters → teleconsultation → prescriptions → billing, in that order. Before activating real patient data, **complete minimum viable compliance**: privacy policy, DPA, three-business-day incident plan, ROPA, and tested backups.

**Legal advice, even with a limited budget — checkpoints:** (a) before the pilot uses real data, have a privacy/health-law attorney review the operator–controller DPA and privacy policy; (b) before issuing the first electronic prescription, validate the ICP-Brasil signature flow against RDC 1,000/2025; and (c) before handling controlled prescriptions or SNCR in a future wave, obtain a dedicated regulatory opinion.

**Triggers that change the recommendations:**
- If video volume exceeds approximately 200,000 minutes/month, migrate from Daily.co to LiveKit Cloud, which is cheaper between 200,000 and 2 million minutes/month, or self-host above 2 million minutes/month.
- When ANVISA publishes the SNCR technical specifications — deadline September 30, 2026, under RDC 1.028/2026 — plan an integration wave using qualified ICP-Brasil signatures.
- If enterprise SSO or larger clinics become necessary, introduce Keycloak/OIDC.
- If there are more than 20–30 tenants requiring physical isolation or imposing contractual isolation requirements, reconsider schema-per-tenant for premium tenants.
- After hiring a second or third developer — and only then — consider extracting high-risk modules into separate services.

## Caveats

- **Regulatory deadlines can move:** ANVISA postponed SNCR availability from June 1 to **September 30, 2026**, under RDC 1.028/2026. The technical integration specifications still depend on publication in the ANVISA portal and must be confirmed before integration planning.
- **Third-party pricing changes:** the AWS, Daily.co, Asaas, BirdID at R$149.90 for five years, WhatsApp/Meta, 360dialog, and CFM web-service prices at R$948/year are 2025–2026 reference figures and should be confirmed directly with each provider before budgeting decisions.
- **gov.br signatures:** the conclusion that a private company cannot use the integration follows the official integration manual and SGD/MGI Ordinance No. 7,076/2024, which restrict credentials to a Public Manager and requires hosting on a government domain. A regulatory change opening the API to private systems would alter the recommendation. Law 14,063/2020, Article 2, sole paragraph, also limits the scope of gov.br signatures in interactions conducted purely between private parties.
- **Bus factor = 1** is the largest non-technical risk. Document everything, automate deployment and backups, and maintain a contingency plan with version-controlled code and credentials in a vault accessible to a trusted successor. These are mandatory for a one-person healthcare SaaS. Also account for the solo sales and support burden by automating onboarding and maintaining an FAQ and documentation from an early stage.
- **SBIS/CFM certification** is not a launch blocker. It is an optional, paid “qualified technical opinion,” and software can comply with NGS1/NGS2 without the seal. However, the underlying NGS1/NGS2 requirements — ICP-Brasil, auditing, and backup — should be designed from the beginning to avoid a rewrite.
- **Born-digital retention:** the product should treat electronic medical records as subject to permanent retention rather than purging them after 20 years. The 20-year period in Law 13,787/2018 applies to paper and digitized records.
- This is a software-development plan. Corporate, tax, and business-model decisions are outside its scope and require separate professional advice.
