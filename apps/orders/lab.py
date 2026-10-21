"""Authenticate a synthetic lab's signed bytes before resolving stored scope."""

from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import contextmanager
from typing import TYPE_CHECKING
from uuid import UUID

from django.conf import settings
from django.db import connection, transaction

from apps.core.secrets import SecretUnavailableError, secret_store
from apps.orders.values import OrdersAccessDeniedError

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from apps.orders.values import IngestReceipt

MAX_CALLBACK_BYTES = 15 * 1024 * 1024


class LabCallbackRejectedError(Exception):
    """One non-identifying refusal for unknown, revoked or forged lab credentials."""


def authenticate(*, headers: Mapping[str, str], body: bytes) -> tuple[UUID, str]:
    """Only a configured deterministic synthetic adapter is runnable."""
    if (
        settings.CLINIC_DATA_MODE != "synthetic"
        or not settings.ORDERS_SYNTHETIC_LAB_ENABLED
        or len(body) > MAX_CALLBACK_BYTES
    ):
        raise LabCallbackRejectedError
    try:
        account = UUID(headers.get("X-Lab-Account", ""))
        key = secret_store().get_secret(f"orders-lab-{account.hex}")
    except (ValueError, SecretUnavailableError) as error:
        raise LabCallbackRejectedError from error
    expected = hmac.digest(bytes.fromhex(key), body, hashlib.sha256).hex()
    if not hmac.compare_digest(headers.get("X-Lab-Signature", ""), expected):
        raise LabCallbackRejectedError
    return account, key


@contextmanager
def lab_context(*, account_id: UUID, proof: str) -> Iterator[UUID]:
    """Resolve stored account scope; never install or borrow a staff actor."""
    if connection.in_atomic_block:
        raise LabCallbackRejectedError
    with transaction.atomic(durable=True), connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.orders_resolve_lab(%s,%s)", [account_id, proof]
        )
        scope = cursor.fetchone()
        if scope is None:
            raise LabCallbackRejectedError
        cursor.execute(
            "SELECT set_config('app.current_tenant',%s,true),"
            "set_config('app.current_principal',%s,true),"
            "set_config('app.orders_lab_proof',%s,true)",
            [str(scope[0]), str(scope[2]), proof],
        )
        yield scope[1]


def receive_lab_result(*, headers: Mapping[str, str], body: bytes) -> IngestReceipt:
    """No JSON, correlation, account or tenant query precedes HMAC authentication."""
    from apps.orders.ingestion import ingest_result  # noqa: PLC0415
    from apps.orders.lab_payload import parse_payload  # noqa: PLC0415

    account, proof = authenticate(headers=headers, body=body)
    try:
        value = json.loads(body)
    except (ValueError, UnicodeDecodeError) as error:
        raise LabCallbackRejectedError from error
    payload = parse_payload(value)
    with lab_context(account_id=account, proof=proof) as clinic_id:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.orders_correlated_order(%s,%s)",
                [account, payload.correlation],
            )
            row = cursor.fetchone()
            if row is None:
                raise OrdersAccessDeniedError
        return ingest_result(
            clinic_id=clinic_id, submission=payload.submission(order_id=row[0])
        )
