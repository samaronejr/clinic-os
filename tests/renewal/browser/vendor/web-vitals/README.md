# web-vitals 6.2.2 (test-only)

`web-vitals.iife.js` is `package/dist/web-vitals.iife.js` from the npm
tarball https://registry.npmjs.org/web-vitals/-/web-vitals-6.2.2.tgz
(integrity `sha512-oto5x6dLEgrRqfcWed+pZEUb2q6ikbFmqF54CRDhI/QGbn+qxC49b4C4OkbH+kb9C3a8shpFD3SgR9kRcs80ZQ==`),
Apache-2.0 (`LICENSE`). It is vendored, not installed: the product tree has no
npm (SD-9, ADR-001).

Only `tests/renewal/browser/test_agenda_slice.py` uses it. The suite injects it
as a Playwright init script to read INP (`onINP`, `reportAllChanges`,
`durationThreshold: 16`); no template or static path serves it. The suite pins
the file's sha256, so replacing it means updating that pin and this README.
