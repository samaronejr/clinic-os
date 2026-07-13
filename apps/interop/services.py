"""Phase >=1 interop service entrypoints."""

from typing import NoReturn


def exchange_clinical_record() -> NoReturn:
    """Exchange a clinical record when the interop domain is implemented."""
    message = "Phase >=1"
    raise NotImplementedError(message)
