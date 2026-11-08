"""Offline ANS XSD validation and deterministic exact-centavo TISS messages."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import md5, sha256
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, cast
from xml.etree.ElementTree import Element, SubElement, tostring

import xmlschema
from django.utils.translation import gettext as _

from apps.insurance.errors import InsuranceInputError
from apps.insurance.limits import (
    MAX_BATCH_GUIDES,
    MAX_FIELD_ERRORS,
    MAX_GUIDE_LINES,
    MAX_PROCEDURE_QUANTITY,
)
from apps.insurance.tiss_types import Adjudication, BatchFacts, GuideFacts, Professional

if TYPE_CHECKING:
    from collections.abc import Mapping

NS: Final = "http://www.ans.gov.br/padroes/tiss/schemas"
MAX_XML_BYTES: Final = 4 * 1024 * 1024
MAX_RETURN_LINES: Final = 10000
MAX_MINOR: Final = 2**63 - 1
PINNED_VERSIONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "organizational": "202601",
        "content": "202511",
        "tuss": "202601",
        "security": "202511",
        "communication": "04.03.00",
    }
)
_MONEY: Final = re.compile(r"[0-9]{1,17}(?:\.[0-9]{1,2})?", re.ASCII)
_FIELDS: Final = frozenset(
    {
        "CNES",
        "numeroCarteira",
        "registroANS",
        "numeroGuiaPrestador",
        "codigoProcedimento",
        "codigoTabela",
        "numeroConselhoProfissional",
        "UF",
        "CBOS",
        "CBO",
        "valorTotal",
        "valorUnitario",
        "quantidadeExecutada",
        "dataExecucao",
    }
)
_GUIDE_TAGS: Final = {
    "consulta": "guiaConsulta",
    "spsadt": "guiaSP-SADT",
    "honorarios": "guiaHonorarios",
}


@dataclass(frozen=True, slots=True)
class TissFieldError:
    """Only schema-owned field names escape, never raw validator messages."""

    field: str
    code: str = "tiss_xsd"

    @property
    def message(self) -> str:
        """Translate a fixed msgid without interpolating XML values."""
        return _("Invalid TISS field.")


class TissValidationError(ValueError):
    """Bounded field-level errors with no source values or parser text."""

    def __init__(self, fields: tuple[TissFieldError, ...]) -> None:
        """Keep the exception itself safe for telemetry boundaries."""
        self.fields = fields
        super().__init__("invalid TISS XML")


def parse_minor(text: str) -> int:
    """Parse exact decimal reais; never round, accept exponents or use floats."""
    if not _MONEY.fullmatch(text):
        raise InsuranceInputError
    whole, _, fraction = text.partition(".")
    value = int(whole) * 100 + int((fraction + "00")[:2])
    if value > MAX_MINOR:
        raise InsuranceInputError
    return value


def _money(value: int) -> str:
    if type(value) is not int or not 0 <= value <= MAX_MINOR:
        raise InsuranceInputError
    return f"{value // 100}.{value % 100:02d}"


def _field(parent: Element, name: str, value: str = "") -> Element:
    child = SubElement(parent, f"{{{NS}}}{name}")
    child.text = value or None
    return child


def _schema(versions: Mapping[str, str]) -> xmlschema.XMLSchema:
    if dict(versions) != dict(PINNED_VERSIONS):
        raise InsuranceInputError
    directory = Path(__file__).parent / "schemas" / versions["communication"]
    manifest = json.loads(
        (directory.parent / "manifest.json").read_text(encoding="utf-8")
    )
    for name, record in manifest["files"].items():
        if (
            Path(name).name != name
            or not name.endswith(".xsd")
            or sha256((directory / name).read_bytes()).hexdigest()
            != record["vendored_sha256"]
        ):
            raise InsuranceInputError
    return xmlschema.XMLSchema(
        directory / "tissV4_03_00.xsd",
        base_url=directory.as_uri() + "/",
        allow="sandbox",
        defuse="always",
    )


def _resource(xml: bytes) -> xmlschema.XMLResource:
    if (
        not xml
        or len(xml) > MAX_XML_BYTES
        or b"<!DOCTYPE" in xml.upper()
        or b"<!ENTITY" in xml.upper()
        or b"\x00" in xml
    ):
        raise TissValidationError((TissFieldError("document"),))
    try:
        return xmlschema.XMLResource(xml, allow="none", defuse="always")
    except (xmlschema.XMLSchemaException, ValueError) as error:
        raise TissValidationError((TissFieldError("document"),)) from error


def validate_xml(*, xml: bytes, versions: Mapping[str, str]) -> None:
    """Validate entirely offline; unknown components and unsafe XML fail closed."""
    resource = _resource(xml)
    schema = _schema(versions)
    fields: list[TissFieldError] = []
    for error in schema.iter_errors(resource):
        element = error.elem
        tag = element.tag.rsplit("}", 1)[-1] if element is not None else ""
        fields.append(TissFieldError(tag if tag in _FIELDS else "document"))
        if len(fields) == MAX_FIELD_ERRORS:
            break
    if fields:
        raise TissValidationError(tuple(fields))


def _professional(parent: Element, name: str, professional: Professional) -> None:
    node = _field(parent, name)
    _field(node, "nomeProfissional", professional.name)
    _field(node, "conselhoProfissional", professional.council)
    _field(node, "numeroConselhoProfissional", professional.number)
    _field(node, "UF", professional.uf)
    _field(node, "CBOS", professional.cbo)


def _beneficiary(parent: Element, name: str, facts: GuideFacts) -> None:
    node = _field(parent, name)
    _field(node, "numeroCarteira", facts.card)
    _field(node, "atendimentoRN", facts.newborn)


def _provider(parent: Element, name: str, code: str) -> Element:
    node = _field(parent, name)
    _field(node, "codigoPrestadorNaOperadora", code)
    return node


def _procedures(parent: Element, facts: GuideFacts) -> int:
    honorarios = facts.kind == "honorarios"
    group = _field(
        parent, "procedimentosRealizados" if honorarios else "procedimentosExecutados"
    )
    total = 0
    for index, procedure in enumerate(facts.procedures, 1):
        if (
            type(procedure.quantity) is not int
            or not 1 <= procedure.quantity <= MAX_PROCEDURE_QUANTITY
            or type(procedure.unit_minor) is not int
            or procedure.unit_minor <= 0
        ):
            raise InsuranceInputError
        amount = procedure.quantity * procedure.unit_minor
        total += amount
        node = _field(
            group, "procedimentoRealizado" if honorarios else "procedimentoExecutado"
        )
        _field(node, "sequencialItem", str(index))
        _field(node, "dataExecucao", facts.performed_on.isoformat())
        term = _field(node, "procedimento")
        _field(term, "codigoTabela", procedure.table)
        _field(term, "codigoProcedimento", procedure.code)
        _field(term, "descricaoProcedimento", procedure.description)
        _field(node, "quantidadeExecutada", str(procedure.quantity))
        _field(node, "reducaoAcrescimo", "1.00")
        _field(node, "valorUnitario", _money(procedure.unit_minor))
        _field(node, "valorTotal", _money(amount))
        if honorarios:
            professional = _field(node, "profissionais")
            _field(professional, "grauParticipacao", "00")
            code = _field(professional, "codProfissional")
            _field(code, "codigoPrestadorNaOperadora", facts.provider_code)
            _field(professional, "nomeProfissional", facts.professional.name)
            _field(professional, "conselhoProfissional", facts.professional.council)
            _field(
                professional, "numeroConselhoProfissional", facts.professional.number
            )
            _field(professional, "UF", facts.professional.uf)
            _field(professional, "CBO", facts.professional.cbo)
    _money(total)
    return total


def _honorarios(node: Element, facts: GuideFacts) -> None:
    _field(node, "guiaSolicInternacao", facts.principal_number)
    _beneficiary(node, "beneficiario", facts)
    local = _field(node, "localContratado")
    _field(_field(local, "codigoContratado"), "codigoNaOperadora", facts.provider_code)
    _field(local, "nomeContratado", facts.provider_name)
    _field(local, "cnes", facts.cnes)
    executor = _field(node, "dadosContratadoExecutante")
    _field(executor, "codigonaOperadora", facts.provider_code)
    _field(executor, "cnesContratadoExecutante", facts.cnes)
    period = _field(node, "dadosInternacao")
    _field(period, "dataInicioFaturamento", facts.performed_on.isoformat())
    _field(period, "dataFimFaturamento", facts.performed_on.isoformat())
    _field(period, "caraterAtendimento", facts.character)
    _field(node, "valorTotalHonorarios", _money(_procedures(node, facts)))
    _field(node, "dataEmissaoGuia", facts.performed_on.isoformat())


def _guide(parent: Element, facts: GuideFacts) -> None:
    if (
        facts.kind not in _GUIDE_TAGS
        or not 1 <= len(facts.procedures) <= MAX_GUIDE_LINES
    ):
        raise InsuranceInputError
    node = _field(parent, _GUIDE_TAGS[facts.kind])
    header = _field(
        node, "cabecalhoConsulta" if facts.kind == "consulta" else "cabecalhoGuia"
    )
    _field(header, "registroANS", facts.registry)
    _field(header, "numeroGuiaPrestador", facts.number)
    if facts.kind == "honorarios":
        _honorarios(node, facts)
        return
    _beneficiary(node, "dadosBeneficiario", facts)
    if facts.kind == "consulta":
        if len(facts.procedures) != 1 or facts.procedures[0].quantity != 1:
            raise InsuranceInputError
        provider = _provider(node, "contratadoExecutante", facts.provider_code)
        _field(provider, "CNES", facts.cnes)
        _professional(node, "profissionalExecutante", facts.professional)
        _field(node, "indicacaoAcidente", facts.accident)
        attendance = _field(node, "dadosAtendimento")
        _field(attendance, "regimeAtendimento", facts.regime)
        _field(attendance, "dataAtendimento", facts.performed_on.isoformat())
        _field(attendance, "tipoConsulta", facts.consultation)
        procedure = facts.procedures[0]
        term = _field(attendance, "procedimento")
        _field(term, "codigoTabela", procedure.table)
        _field(term, "codigoProcedimento", procedure.code)
        _field(term, "valorProcedimento", _money(procedure.unit_minor))
        return
    requester = _field(node, "dadosSolicitante")
    _provider(requester, "contratadoSolicitante", facts.provider_code)
    _field(requester, "nomeContratadoSolicitante", facts.provider_name)
    _professional(requester, "profissionalSolicitante", facts.professional)
    request = _field(node, "dadosSolicitacao")
    _field(request, "dataSolicitacao", facts.performed_on.isoformat())
    _field(request, "caraterAtendimento", facts.character)
    executor = _field(node, "dadosExecutante")
    _provider(executor, "contratadoExecutante", facts.provider_code)
    _field(executor, "CNES", facts.cnes)
    attendance = _field(node, "dadosAtendimento")
    _field(attendance, "tipoAtendimento", facts.attendance)
    _field(attendance, "indicacaoAcidente", facts.accident)
    _field(attendance, "regimeAtendimento", facts.regime)
    total = _procedures(node, facts)
    _field(_field(node, "valorTotal"), "valorTotalGeral", _money(total))


def protocol_hash(root: Element) -> str:
    """ANS legacy integrity checksum is not an authentication or sealing primitive."""
    content = "".join(
        node.text or ""
        for group in root
        if group.tag
        in {
            f"{{{NS}}}cabecalho",
            f"{{{NS}}}prestadorParaOperadora",
            f"{{{NS}}}operadoraParaPrestador",
        }
        for node in group.iter()
        if len(node) == 0 and isinstance(node.tag, str)
    )
    try:
        encoded = content.encode("iso-8859-1")
    except UnicodeEncodeError as error:
        raise InsuranceInputError from error
    return md5(encoded, usedforsecurity=False).hexdigest()


def generate_batch(*, facts: BatchFacts, versions: Mapping[str, str]) -> bytes:
    """Generate one homogeneous batch and refuse sealing if the official XSD fails."""
    if (
        not 1 <= len(facts.guides) <= MAX_BATCH_GUIDES
        or len({guide.kind for guide in facts.guides}) != 1
        or len({guide.number for guide in facts.guides}) != len(facts.guides)
        or len({(guide.registry, guide.provider_code) for guide in facts.guides}) != 1
    ):
        raise InsuranceInputError
    first = facts.guides[0]
    root = Element(f"{{{NS}}}mensagemTISS")
    header = _field(root, "cabecalho")
    transaction = _field(header, "identificacaoTransacao")
    _field(transaction, "tipoTransacao", "ENVIO_LOTE_GUIAS")
    _field(transaction, "sequencialTransacao", facts.number)
    _field(transaction, "dataRegistroTransacao", facts.registered_at.date().isoformat())
    _field(
        transaction, "horaRegistroTransacao", facts.registered_at.strftime("%H:%M:%S")
    )
    origin = _field(header, "origem")
    _field(
        _field(origin, "identificacaoPrestador"),
        "codigoPrestadorNaOperadora",
        first.provider_code,
    )
    _field(_field(header, "destino"), "registroANS", first.registry)
    _field(header, "Padrao", versions.get("communication", "").removeprefix("0"))
    batch = _field(_field(root, "prestadorParaOperadora"), "loteGuias")
    _field(batch, "numeroLote", facts.number)
    guides = _field(batch, "guiasTISS")
    for guide in facts.guides:
        _guide(guides, guide)
    checksum = protocol_hash(root)
    _field(_field(root, "epilogo"), "hash", checksum)
    xml = cast("bytes", tostring(root, encoding="utf-8", xml_declaration=True))
    validate_xml(xml=xml, versions=versions)
    return xml


def parse_return(
    *, xml: bytes, versions: Mapping[str, str]
) -> tuple[Adjudication, ...]:
    """Parse demonstrativo de analise; acknowledgment and cash remain distinct facts."""
    validate_xml(xml=xml, versions=versions)
    root = _resource(xml).root
    transaction = root.findtext(
        f"{{{NS}}}cabecalho/{{{NS}}}identificacaoTransacao/{{{NS}}}tipoTransacao", ""
    )
    if transaction != "DEMONSTRATIVO_ANALISE_CONTA":
        raise InsuranceInputError
    if root.findtext(f"{{{NS}}}epilogo/{{{NS}}}hash") != protocol_hash(root):
        raise InsuranceInputError
    registry = root.findtext(
        f"{{{NS}}}cabecalho/{{{NS}}}origem/{{{NS}}}registroANS", ""
    )
    if any(
        node.text != registry
        for node in root.findall(
            f".//{{{NS}}}cabecalhoDemonstrativo/{{{NS}}}registroANS"
        )
    ):
        raise InsuranceInputError
    protocols = root.findall(
        ".//{"
        + NS
        + "}demonstrativoAnaliseConta/{"
        + NS
        + "}dadosConta/{"
        + NS
        + "}dadosProtocolo"
    )
    result: list[Adjudication] = []
    identities: set[tuple[str, int]] = set()
    for protocol in protocols:
        batch_number = protocol.findtext(f"{{{NS}}}numeroLotePrestador", "")
        for guide in protocol.findall(f"{{{NS}}}relacaoGuias"):
            number = guide.findtext(f"{{{NS}}}numeroGuiaPrestador", "")
            card = guide.findtext(f"{{{NS}}}numeroCarteira", "")
            for line in guide.findall(f"{{{NS}}}detalhesGuia"):
                result.append(
                    _adjudication(
                        line=line,
                        guide_number=number,
                        registry=registry,
                        batch_number=batch_number,
                        card=card,
                    )
                )
                identity = (number, result[-1].sequence)
                if identity in identities or len(result) > MAX_RETURN_LINES:
                    raise InsuranceInputError
                identities.add(identity)
    if not result:
        raise InsuranceInputError
    return tuple(result)


def _adjudication(
    *, line: Element, guide_number: str, registry: str, batch_number: str, card: str
) -> Adjudication:
    """Validate one exact source line without interpreting free-text payer remarks."""
    sequence = int(line.findtext(f"{{{NS}}}sequencialItem", "0"))
    if sequence <= 0:
        raise InsuranceInputError
    informed = parse_minor(line.findtext(f"{{{NS}}}valorInformado", ""))
    accepted = parse_minor(line.findtext(f"{{{NS}}}valorLiberado", ""))
    glosas = tuple(
        (
            item.findtext(f"{{{NS}}}tipoGlosa", ""),
            parse_minor(item.findtext(f"{{{NS}}}valorGlosa", "")),
        )
        for item in line.findall(f"{{{NS}}}relacaoGlosa")
    )
    denied = sum(amount for _, amount in glosas)
    if informed != accepted + denied:
        raise InsuranceInputError
    quantity = line.findtext(f"{{{NS}}}qtdExecutada", "")
    whole, _, fraction = quantity.partition(".")
    if not whole.isascii() or not whole.isdecimal() or fraction.strip("0"):
        raise InsuranceInputError
    return Adjudication(
        guide_number,
        sequence,
        informed,
        accepted,
        denied,
        glosas,
        registry,
        batch_number,
        card,
        line.findtext(f"{{{NS}}}procedimento/{{{NS}}}codigoTabela", ""),
        line.findtext(f"{{{NS}}}procedimento/{{{NS}}}codigoProcedimento", ""),
        int(whole),
    )
