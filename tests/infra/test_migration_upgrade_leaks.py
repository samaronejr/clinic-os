"""The upgrade gate refuses to publish any live secret value."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

import pytest
from ops.testing import migration_upgrade_leaks as leaks

if TYPE_CHECKING:
    from pathlib import Path

SECRET = bytes(range(20))


@pytest.mark.parametrize(
    "form",
    [
        SECRET.hex(),
        SECRET.hex().upper(),
        base64.b64encode(SECRET).decode(),
        base64.urlsafe_b64encode(SECRET).decode().rstrip("="),
        base64.b32encode(SECRET).decode().rstrip("="),
    ],
)
def test_any_encoding_of_a_live_secret_in_an_output_fails(
    tmp_path: Path, form: str
) -> None:
    published = tmp_path / "domain-seed.json"
    published.write_text('{"history": [{"key": "' + form + '"}]}', encoding="utf-8")
    assert leaks.leak_failures([published], [("totp seed", SECRET)]) == [
        "domain-seed.json holds a totp seed"
    ]


def test_digest_of_a_secret_is_not_a_leak(tmp_path: Path) -> None:
    published = tmp_path / "domain-seed.json"
    published.write_text('{"key": "sha256:' + "0" * 64 + '"}', encoding="utf-8")
    assert leaks.leak_failures([published], [("totp seed", SECRET)]) == []


def test_base_checkout_is_not_a_published_file(tmp_path: Path) -> None:
    work_root = tmp_path / "migration-upgrade"
    (work_root / "base").mkdir(parents=True)
    (work_root / "base" / "x.py").write_text("x", encoding="utf-8")
    (work_root / "seed.json").write_text("{}", encoding="utf-8")
    assert [path.name for path in leaks.published_files(work_root)] == ["seed.json"]
