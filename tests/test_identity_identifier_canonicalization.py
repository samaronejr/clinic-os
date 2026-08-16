from __future__ import annotations

import importlib
import importlib.util


def test_shared_identity_canonicalizers_match_fixed_unicode_vectors() -> None:
    module_name = "apps.identity.identifiers"
    assert importlib.util.find_spec(module_name) is not None
    identifiers = importlib.import_module(module_name)
    canonicalize_username = getattr(identifiers, "canonicalize_username", None)
    canonicalize_email = getattr(identifiers, "canonicalize_email", None)
    assert callable(canonicalize_username)
    assert callable(canonicalize_email)

    assert (
        canonicalize_username("  \N{FULLWIDTH LATIN CAPITAL LETTER A}lice  ") == "alice"
    )
    assert canonicalize_username("  \N{KELVIN SIGN}ELVIN  ") == "kelvin"
    assert canonicalize_email("  A\N{COMBINING RING ABOVE}@EXAMPLE.COM  ") == (
        "å@example.com"
    )
