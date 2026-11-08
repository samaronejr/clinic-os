from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from billing.journal_support import finance_setup
from insurance.cycle_support import build_cycle

__all__ = ("finance_setup",)

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

    from billing.journal_support import FinanceSetup
    from insurance.cycle_support import Cycle


@pytest.fixture
def cycle(finance_setup: FinanceSetup, settings: SettingsWrapper) -> Cycle:
    settings.INSURANCE_SYNTHETIC_ENABLED = True
    return build_cycle(finance_setup)
