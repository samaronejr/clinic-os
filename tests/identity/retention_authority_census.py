"""Execute retained purge authority under a separate real machine login."""

from __future__ import annotations

import os
import secrets
from contextlib import contextmanager
from typing import TYPE_CHECKING, Protocol, cast
from unittest.mock import patch
from uuid import uuid4

import psycopg
from apps.retention import tasks
from django.conf import settings
from django.db import connections
from django.test import override_settings
from psycopg import sql

from retention import test_execution

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    import pytest
    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph


class ProofRecorder(Protocol):
    def run(self, label: str, oracle: Callable[[], None]) -> None: ...


@contextmanager
def retention_login() -> Iterator[None]:
    """Keep existing v1 identities immutable while exercising the v3 login."""
    name = "clinic_agent_retention_" + uuid4().hex[:12]
    password = secrets.token_urlsafe(32)
    agent = connections["agent"]
    original = agent.settings_dict["USER"], agent.settings_dict["PASSWORD"]
    with psycopg.connect(
        os.environ["TEST_SUPERUSER_DATABASE_URL"], autocommit=True
    ) as owner:
        owner.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN INHERIT PASSWORD {} IN ROLE clinic_agent"
            ).format(sql.Identifier(name), sql.Literal(password))
        )
        agent.close()
        agent.settings_dict.update(USER=name, PASSWORD=password)
        try:
            yield
        finally:
            agent.close()
            agent.settings_dict.update(USER=original[0], PASSWORD=original[1])
            owner.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))


def exercise_retention_authority(
    proofs: ProofRecorder,
    graph: Callable[[], RbacGraph],
    monkeypatch: pytest.MonkeyPatch,
    secret_root: Path,
) -> None:
    """Issue certificates after real holds, shredding, failure and receipts."""
    for failure in (False, True):
        subject = graph()

        def run(subject: RbacGraph = subject, failure: bool = failure) -> None:
            with (
                retention_login(),
                override_settings(
                    SCRIBE_SYNTHETIC_CAPTURE=True,
                    SCRIBE_MEDIA_ROOT=secret_root.parent / ("retention-" + uuid4().hex),
                ),
                monkeypatch.context() as local,
            ):
                test_execution.test_legal_hold_receipt_and_crypto_shred_execute_with_the_real_principal(
                    subject,
                    cast("SettingsWrapper", settings),
                    secret_root.parent,
                    local,
                    failure,
                )
                with patch("apps.retention.tasks.apply_policy.apply_async") as sent:
                    assert tasks.scan_due() == 3
                assert sent.call_count == 3

        proofs.run("Retention holds, machine authority and receipt ordering", run)
