"""RNDS eligibility gate: the recorded fixture rehearsal is the only open leg.

EG-7 (DATASUS credentialing: Portal de Servicos request, ICP-Brasil certificate,
homologation, then production) is unmet, so no lifecycle record here can reach
the real RNDS. A submission needs synthetic data mode, the explicit
``INTEROP_RNDS_HOMOLOGATION`` opt-in (off by default) and the clinic's current
``rnds`` capability version in ``sandbox`` with an owner approval, on the
fixture provider, in the homologation environment. The request resolver checks
the same version in the database.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from config.settings.contracts import SYNTHETIC_DATA_MODE, resolved_data_mode
from django.conf import settings

from apps.interop.errors import RndsUnavailableError
from apps.providers.models import CapabilityVersion
from apps.providers.services import current_version

if TYPE_CHECKING:
    from uuid import UUID

CAPABILITY_KEY: Final = "rnds"
FIXTURE_PROVIDER: Final = "rnds-fixture"
FIXTURE_ACCOUNT: Final = "fixture"
ENVIRONMENT: Final = "homologation"


def require_rnds_rehearsal() -> None:
    """Refuse live, invalid or not explicitly enabled runtimes before any read."""
    if (
        resolved_data_mode() != SYNTHETIC_DATA_MODE
        or getattr(settings, "INTEROP_RNDS_HOMOLOGATION", False) is not True
    ):
        raise RndsUnavailableError


def require_rnds_homologation(*, clinic_id: UUID) -> CapabilityVersion:
    """Return the clinic's approved fixture homologation version, or refuse."""
    require_rnds_rehearsal()
    version = current_version(CAPABILITY_KEY, clinic_id=clinic_id)
    if (
        version is None
        or version.state != CapabilityVersion.State.SANDBOX
        or version.approval_id is None
        or version.provider != FIXTURE_PROVIDER
        or version.environment != ENVIRONMENT
        or version.account != FIXTURE_ACCOUNT
        or version.capability.key != CAPABILITY_KEY
        or version.capability.clinic_id not in {None, clinic_id}
    ):
        raise RndsUnavailableError
    return version
