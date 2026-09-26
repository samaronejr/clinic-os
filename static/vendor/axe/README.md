# axe-core 4.13.0 (vendored, test-only)

- Source: https://registry.npmjs.org/axe-core/-/axe-core-4.13.0.tgz
- Tarball integrity (npm `dist.integrity`):
  `sha512-UzGt8zg7Ny8djbYMhxl2zuEevVa7r2gJjYY5Lwr1xM7+XU2nd6CkIWFTVcCIbAP63vSz71NaVyyuSk9lHKcy0A==`
- Files copied unmodified from the package: `axe.min.js`, `LICENSE` (MPL-2.0),
  `LICENSE-3RD-PARTY.txt`.
- Use: injected only by browser test suites (`tests/renewal/browser/`) to run
  accessibility rules against the component showcase. No template loads it,
  and it is not part of the service-worker precache
  (`apps/core/views.py` `SHELL_STATIC_ASSETS`).
- Upgrade: replace all three files from a new tarball, verify its integrity,
  update this file and the pinned sha256 in `tests/renewal/test_design_tokens.py`.
