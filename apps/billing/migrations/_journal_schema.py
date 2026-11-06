"""Additive model state, kept in original dependency order across small modules."""

from typing import Final

from apps.billing.migrations._journal_collection_schema import (
    OPERATIONS as COLLECTION_OPERATIONS,
)
from apps.billing.migrations._journal_ledger_schema import (
    OPERATIONS as LEDGER_OPERATIONS,
)
from apps.billing.migrations._journal_money_schema import OPERATIONS as MONEY_OPERATIONS

OPERATIONS: Final = [
    *LEDGER_OPERATIONS,
    *COLLECTION_OPERATIONS,
    *MONEY_OPERATIONS,
]
