from __future__ import annotations

from pathlib import Path
from typing import cast
from xml.etree.ElementTree import tostring

import xmlschema
from apps.insurance.tiss import NS, protocol_hash


def return_xml(*, guide: str = "101", duplicate: bool = False) -> bytes:
    root = xmlschema.XMLResource(
        (Path(__file__).parent / "fixtures" / "return.xml").read_bytes(),
        allow="none",
        defuse="always",
    ).root
    number = root.find(f".//{{{NS}}}relacaoGuias/{{{NS}}}numeroGuiaPrestador")
    assert number is not None
    number.text = guide
    if duplicate:
        group = root.find(f".//{{{NS}}}relacaoGuias")
        assert group is not None
        line = group.find(f"{{{NS}}}detalhesGuia")
        assert line is not None
        # Same line number, not a timing-dependent duplicate submission.
        group.insert(list(group).index(line), line)
    checksum = root.find(f"{{{NS}}}epilogo/{{{NS}}}hash")
    assert checksum is not None
    checksum.text = protocol_hash(root)
    return cast("bytes", tostring(root, encoding="utf-8", xml_declaration=True))
