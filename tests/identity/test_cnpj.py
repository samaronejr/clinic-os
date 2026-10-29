"""Exercise registration's numeric identifier boundary without registry claims."""

from __future__ import annotations

import pytest
from apps.identity.cnpj import validate_cnpj
from django.core.exceptions import ValidationError
from django.utils.translation import gettext


@pytest.mark.parametrize(
    "value",
    [
        "12345678000195",
        "12.345.678/0001-95",
        " 12.345.678/0001-95 ",
    ],
)
def test_cnpj_normalizes_a_synthetic_checksum_example(value: str) -> None:
    assert validate_cnpj(value) == "12345678000195"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "123456780001",
        "12345678000196",
        "12345678000185",
        "00000000000000",
        "11111111111111",
        "\uff11\uff12\uff13\uff14\uff15\uff16\uff17\uff18"
        "\uff10\uff10\uff10\uff11\uff19\uff15",
        "1234567800019A",
        "12 345 678 0001 95",
    ],
)
def test_cnpj_rejects_malformed_or_invalid_check_digits(value: str) -> None:
    with pytest.raises(ValidationError) as caught:
        validate_cnpj(value)
    assert caught.value.code == "invalid_cnpj"
    assert caught.value.messages == [gettext("Enter a valid CNPJ.")]
