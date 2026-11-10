# Website and help center

**Binding contract.** This app serves the public Clinic Ops website (home,
product, security posture, contact, brand board) and the pt-BR help center. It
has no models, migrations, services or permissions, and it reads no tenant data.
The surface decision is the todo 70 note in
[ADR-001](../../docs/adr/ADR-001-frontend-htmx-first-islands.md).

## Routes

Every view is GET/HEAD only (`require_safe`) and renders a template from
`templates/website/`. Each path is listed in the tenancy bypass
(`apps/tenancy/middleware.py`): `/product/`, `/security/`, `/contact/`,
`/brand/` and `/pricing/` are exact paths, and `/help/` is a prefix. `/` stays
named `index`. It forwards a signed-in staff session to `/workspace/` and
greets everyone else with the website. No page sets a cookie, links a script,
renders a form or puts a selector in a query string. The privacy middleware
keeps the default private, no-store cache headers.

`/pricing/` is a reserved slot. It returns 404 and no page links to it. Todo 74
replaces it after the owner approves the packaging, and records that approval
under `pricing` in `docs/web/claims.yaml`.

`/contact/` shows `CLINIC_WEBSITE_CONTACT_EMAIL`
(`config.settings.contracts.resolve_website_contact_email`), which the owner
sets at deployment. When it is empty, the page says no address is published;
a malformed value fails at startup. The renewal runner's `website` suite uses
the reserved `equipe@clinic-ops.invalid`.

## Claims

`docs/web/claims.yaml` classifies every text in the templates under
`templates/website/` and in the help guides. Each
statement of fact, including every module status badge, is a claim with a
tracked `evidence_ref` and the `evidence_quote` phrases that file must
contain. On pages the element marked `data-claim="<id>"` directly wraps the
claim's msgid; help-guide claims carry `surface: guides`. Every other text
(headings, navigation, labels, instructions, advice, fictional illustrations,
brand rules, metadata) is a `non_claims` entry with a reason from a closed
vocabulary; guide steps are declared instructions, and any fact they imply
lives in the step's result as a claim. `tests/website/test_claims.py` reads every template under
`templates/website/`, subfolders included, and every help-guide string, step,
screen title and control label. In the templates it parses every msgid
(`trans` or `translate`, either quote style), literal text node, visible
attribute (every text-valued ARIA attribute included) and blocktrans body,
and fails on a template form it cannot classify (its docstring lists them).
It fails on a text in neither list, a claim without evidence, an unmarked
page claim or drifted wording. A route test renders every route of the website urlconf, every root route a
website view serves and every route under `WEBSITE_PUBLIC` (the one constant
the tenancy bypass is built from). It fails if one renders a template outside
`templates/website/` that is not on its allowlist: the text-free icon include
and the app-wide 404 page with its layouts, which are outside this scope. The
few view outputs it trusts are pinned by path, and the `website` browser suite
checks the rendered pt-BR claims against the registry. Declared limits: the
tests do not check values the views compute at request time or the pt-BR
translations, and the route test renders each route once, anonymously, with
no query string.
Testimonials, certifications and metrics are claims too, and none exists today.

## Help guides

`guides.py` is the registry. A `Screen` names a real URL, template and visible
title. A `Step` names the control the user acts on: its exact `{% trans %}`
msgid, or the literal label for templates that print pt-BR directly (the
encounter screen). The step's text holds `{control}` exactly once, and the
page renders the control's own pt-BR label in its place.
`tests/website/test_guides.py` reverses each route and finds each title and
control in its template, so a changed screen breaks its guide rather than
leaving a stale instruction. `FIRST_STEPS` is the onboarding path for a new
clinic team.

## Styles and verification

`static/css/clinic-os-website.css` uses only the semantic tokens of
`clinic-os.css`. Its display scale is scoped to `.site` and documented in
DESIGN.md ("Website"). `tests/renewal/test_design_tokens.py` keeps raw colors,
shadows, gradients and animation out of it. The `website` browser suite covers
desktop and two phone profiles (axe 0 serious/critical, 44px targets, no
overflow), forced colors, 200% zoom, JavaScript off and, on Chromium, the
throttled LCP budget of `ops/testing/website_perf.py`.
