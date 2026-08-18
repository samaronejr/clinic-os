"""Shared executable browser contract re-exported for host-side tests.

The runner image only ships `ops/testing/`, so the executable assertions live in
`ops.testing.browser_visual_contract` and this module re-exports them unchanged
for tests that run on the host.
"""

from ops.testing.browser_visual_contract import (
    AUDIT_SCRIPT,
    SERIOUS_RULES,
    VIEWPORTS,
    VisualContractError,
    blocking_violations,
    require_clean_console,
    require_no_blocking_violations,
    require_no_state_in_url,
    require_post_only_forms,
)

__all__ = (
    "AUDIT_SCRIPT",
    "SERIOUS_RULES",
    "VIEWPORTS",
    "VisualContractError",
    "blocking_violations",
    "require_clean_console",
    "require_no_blocking_violations",
    "require_no_state_in_url",
    "require_post_only_forms",
)
