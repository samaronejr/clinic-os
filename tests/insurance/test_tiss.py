from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from xml.etree.ElementTree import Element, SubElement

import pytest
from apps.insurance.errors import InsuranceInputError
from apps.insurance.tiss import (
    NS,
    PINNED_VERSIONS,
    TissValidationError,
    generate_batch,
    parse_minor,
    parse_return,
    protocol_hash,
    validate_xml,
)
from apps.insurance.tiss_types import (
    BatchFacts,
    GuideFacts,
    Procedure,
    Professional,
)

from insurance.fixture_xml import return_xml


def facts(kind: str = "spsadt") -> BatchFacts:
    return BatchFacts(
        "1",
        datetime(2031, 1, 1, 12, tzinfo=UTC),
        (
            GuideFacts(
                number="101",
                kind=kind,
                registry="000001",
                card="SYNTHETIC-001",
                provider_code="SYNTHETIC001",
                provider_name="Synthetic Clinic",
                cnes="0000001",
                performed_on=date(2031, 1, 1),
                professional=Professional(
                    "Synthetic Physician", "06", "123456", "35", "225125"
                ),
                procedures=(
                    Procedure("22", "10101012", "Synthetic consultation", 1, 12345),
                ),
                principal_number="1",
            ),
        ),
    )


@pytest.mark.parametrize("kind", ["consulta", "spsadt", "honorarios"])
def test_official_xsd_accepts_each_supported_guide(kind: str) -> None:
    xml = generate_batch(facts=facts(kind), versions=PINNED_VERSIONS)
    validate_xml(xml=xml, versions=PINNED_VERSIONS)
    assert b"123.45" in xml
    assert xml == generate_batch(facts=facts(kind), versions=PINNED_VERSIONS)


def test_xsd_failure_exposes_only_bounded_field_errors() -> None:
    batch = facts()
    malformed = replace(batch, guides=(replace(batch.guides[0], cnes="x" * 20),))
    with pytest.raises(TissValidationError) as caught:
        generate_batch(facts=malformed, versions=PINNED_VERSIONS)
    assert caught.value.fields
    assert all(error.code == "tiss_xsd" for error in caught.value.fields)
    assert "xxxxxxxx" not in str(caught.value)
    assert all("xxxxxxxx" not in error.field for error in caught.value.fields)


@pytest.mark.parametrize(
    "xml",
    [
        b'<!DOCTYPE x [<!ENTITY a SYSTEM "file:///etc/passwd">]><x>&a;</x>',
        b'<!DOCTYPE x SYSTEM "https://synthetic.invalid/a"><x/>',
        b'<x xmlns:xi="http://www.w3.org/2001/XInclude"><xi:include href="file:///etc/passwd"/></x>',
        b"\x00",
    ],
)
def test_untrusted_xml_cannot_resolve_entities_files_or_network(xml: bytes) -> None:
    with pytest.raises(TissValidationError):
        validate_xml(xml=xml, versions=PINNED_VERSIONS)


def test_component_version_drift_fails_closed() -> None:
    with pytest.raises(InsuranceInputError):
        generate_batch(facts=facts(), versions={**PINNED_VERSIONS, "content": "209901"})


@pytest.mark.parametrize("value", ["1.001", "-1.00", "NaN", "1e2", "1,00", ""])
def test_money_cannot_be_rounded_or_inferred_from_invalid_text(value: str) -> None:
    with pytest.raises(InsuranceInputError):
        parse_minor(value)


def test_integer_centavo_boundary() -> None:
    assert parse_minor("123.45") == 12345
    assert parse_minor("0.00") == 0
    assert parse_minor("1") == 100


def test_official_return_fixture_keeps_adjudication_separate_from_cash() -> None:
    result = parse_return(xml=return_xml(), versions=PINNED_VERSIONS)
    assert len(result) == 1
    line = result[0]
    assert (line.guide_number, line.sequence) == ("101", 1)
    assert (line.informed_minor, line.accepted_minor, line.denied_minor) == (
        12345,
        8345,
        4000,
    )
    assert line.glosas == (("1701", 4000),)
    assert (line.registry, line.batch_number, line.card) == (
        "000001",
        "1",
        "SYNTHETIC-001",
    )


def test_duplicate_adjudication_line_is_not_double_counted() -> None:
    with pytest.raises(InsuranceInputError):
        parse_return(xml=return_xml(duplicate=True), versions=PINNED_VERSIONS)


def test_protocol_checksum_preserves_literal_values_not_framing_or_signature() -> None:
    root = Element(f"{{{NS}}}mensagemTISS")
    header = SubElement(root, f"{{{NS}}}cabecalho")
    header.text = "\n  "
    field = SubElement(header, f"{{{NS}}}sequencialTransacao")
    field.text = " \t"
    field.tail = "\n"
    body = SubElement(root, f"{{{NS}}}prestadorParaOperadora")
    SubElement(body, f"{{{NS}}}descricaoProcedimento").text = "A\u00e7\u00e3o\t"
    SubElement(root, f"{{{NS}}}epilogo").text = "not checksum input"
    SubElement(root, "{http://www.w3.org/2000/09/xmldsig#}Signature").text = "signature"
    assert protocol_hash(root) == "8e5d1cc5b38f10636ca7cd8c32f9a4be"
