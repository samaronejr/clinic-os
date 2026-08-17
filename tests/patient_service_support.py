from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from django.db import connection

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextmanager
def runtime_role() -> Iterator[None]:
    with connection.cursor() as cursor:
        cursor.execute("SET ROLE clinic_app")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")
