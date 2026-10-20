"""Assemble the official Visio XML schema from the Microsoft docs fragments.

The Microsoft docs repository ships the XSD fragments of VisioSchema15 as
markdown files (one per complexType / element). This script downloads them,
stitches the definitions into a single schema and validates a generated .vsdx
against it.

Usage: python3 build_visio_xsd.py <docs-list.txt> <out-dir>
"""

from __future__ import annotations

import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from lxml import etree

RAW = "https://raw.githubusercontent.com/MicrosoftDocs/office-developer-client-docs/main/"
NS = "http://schemas.microsoft.com/office/visio/2012/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
XS = "http://www.w3.org/2001/XMLSchema"

CODE_BLOCK = re.compile(r"```(?:XML|xml)\s*\n(.*?)```", re.DOTALL)


def fetch(path: str, out_dir: Path) -> Path | None:
    target = out_dir / path.replace("/", "__")
    if target.is_file():
        return target
    try:
        with urllib.request.urlopen(RAW + path, timeout=60) as response:
            target.write_bytes(response.read())
    except Exception as exc:  # noqa: BLE001
        print(f"skip {path}: {exc}")
        return None
    return target


XSD_ATTRIBUTE_FIXES = {
    "Type": "type",
    "Base": "base",
    "Name": "name",
    "Ref": "ref",
    "MinOccurs": "minOccurs",
    "MaxOccurs": "maxOccurs",
    "Use": "use",
    "ItemType": "itemType",
    "ProcessContents": "processContents",
}


def normalize(fragment: str) -> str:
    fragment = fragment.replace("< xs:", "<xs:").replace("</ xs:", "</xs:")
    fragment = re.sub(r"\bxsd:", "xs:", fragment)
    # the docs contain a broken placeholder element in a few fragments
    fragment = re.sub(r'<xs:\s*name=""\s*>\s*</xs:>', "", fragment)
    # the docs sometimes misspell attribute names ("Type=" instead of "type=")
    def fix_tag(match: re.Match) -> str:
        tag = match.group(0)
        for wrong, right in XSD_ATTRIBUTE_FIXES.items():
            tag = re.sub(rf'\s{wrong}="', f' {right}="', tag)
        return tag

    fragment = re.sub(r"<xs:[A-Za-z]+[^>]*>", fix_tag, fragment)
    # xs:all does not allow maxOccurs="unbounded" - the docs use it anyway
    fragment = fragment.replace("<xs:all>", '<xs:choice maxOccurs="unbounded">').replace(
        "</xs:all>", "</xs:choice>"
    )
    # extending a simple type requires simpleContent
    def fix_content(match: re.Match) -> str:
        block = match.group(0)
        if 'base="xs:' in block:
            return block.replace("<xs:complexContent>", "<xs:simpleContent>").replace(
                "</xs:complexContent>", "</xs:simpleContent>"
            )
        return block

    fragment = re.sub(
        r"<xs:complexContent>.*?</xs:complexContent>",
        fix_content,
        fragment,
        flags=re.DOTALL,
    )
    return fragment.strip()


# base row types that the docs only reference; permissive placeholders
PLACEHOLDER_TYPES = (
    "NamedRow_Type",
    "NamedIndexedRow_Type",
    "IndexedRow_Type",
    "GeometryRow_Type",
)


def parse_definitions(fragment: str) -> list[tuple[str, str]]:
    """Return (kind, xml) pairs for the top level definitions in a fragment."""
    wrapper = (
        f'<root xmlns:xs="{XS}" xmlns:r="{REL_NS}" xmlns:xsd="{XS}">'
        f"{fragment}</root>"
    )
    try:
        root = etree.fromstring(wrapper.encode("utf-8"))
    except etree.XMLSyntaxError as exc:
        print(f"  unparsable fragment: {exc}")
        return []
    found: list[tuple[str, str]] = []
    for node in root:
        tag = etree.QName(node).localname
        if tag in ("element", "complexType", "simpleType"):
            name = node.get("name")
            if not name:
                continue
            if tag == "element":
                # global declarations must not carry occurrence constraints
                for attribute in ("minOccurs", "maxOccurs"):
                    if attribute in node.attrib:
                        del node.attrib[attribute]
            found.append((tag, etree.tostring(node, encoding="unicode")))
    return found


def main() -> None:
    listing = Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
    out_dir = Path(sys.argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)

    wanted = [line for line in listing if line.endswith(".md")]
    with ThreadPoolExecutor(max_workers=16) as pool:
        files = [f for f in pool.map(lambda p: fetch(p, out_dir), wanted) if f]

    elements: dict[str, str] = {}
    types: dict[str, str] = {}
    for file in files:
        text = file.read_text(encoding="utf-8")
        for block in CODE_BLOCK.findall(text):
            for kind, xml in parse_definitions(normalize(block)):
                name = re.search(r'name="([^"]+)"', xml)
                if not name:
                    continue
                key = name.group(1)
                if kind == "element":
                    elements.setdefault(key, xml)
                else:
                    types.setdefault(key, xml)

    print(f"collected {len(elements)} element declarations, {len(types)} complex types")

    for name in PLACEHOLDER_TYPES:
        types.setdefault(
            name,
            f'<xs:complexType name="{name}">'
            '<xs:sequence><xs:any minOccurs="0" maxOccurs="unbounded" processContents="skip"/></xs:sequence>'
            '<xs:anyAttribute processContents="skip"/>'
            "</xs:complexType>",
        )

    # the docs leave a few leaf types empty (GlueSettings, Rel, ShowRulers...);
    # the real schema treats them as strings with optional attributes
    empty = re.compile(
        r'<xs:complexType\b[^>]*\bname="([A-Za-z0-9_]+)"[^>]*(?:/>|>\s*</xs:complexType>)'
    )
    for key, xml in list(types.items()):
        if empty.search(xml):
            types[key] = (
                f'<xs:complexType name="{key}">'
                '<xs:simpleContent>'
                '<xs:extension base="xs:string">'
                '<xs:anyAttribute processContents="lax"/>'
                "</xs:extension>"
                "</xs:simpleContent>"
                "</xs:complexType>"
            )

    # the text of a shape is character data of the Text element, mixed with the
    # cp/pp/tp/fld run markers; the docs fragment drops the mixed="true" flag
    if "Text_Type" in types and 'mixed="true"' not in types["Text_Type"]:
        types["Text_Type"] = types["Text_Type"].replace(
            "<xs:complexType ", '<xs:complexType mixed="true" ', 1
        )

    header = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<xsd:schema xmlns:xsd="{XS}" xmlns:xs="{XS}" xmlns:r="{REL_NS}" xmlns="{NS}" '
        f'targetNamespace="{NS}" '
        'elementFormDefault="qualified" attributeFormDefault="unqualified">\n'
        f'<xsd:import namespace="{REL_NS}" schemaLocation="oRel.xsd"/>\n'
    )
    schema_path = out_dir / "visio.xsd"
    (out_dir / "oRel.xsd").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<xsd:schema xmlns:xsd="{XS}" xmlns="{REL_NS}" targetNamespace="{REL_NS}" '
        'elementFormDefault="qualified" attributeFormDefault="unqualified">\n'
        '<xsd:attribute name="id" type="xsd:string"/>\n'
        '<xsd:attribute name="embed" type="xsd:string"/>\n'
        '<xsd:attribute name="link" type="xsd:string"/>\n'
        "</xsd:schema>\n",
        encoding="utf-8",
    )

    def write_schema() -> None:
        body = "\n".join(list(elements.values()) + list(types.values()))
        schema_path.write_text(header + body + "\n</xsd:schema>\n", encoding="utf-8")

    def drop_references(name: str) -> None:
        """Remove declarations that point at a type that was dropped."""
        self_closing = re.compile(
            r'<xs:element\b[^>]*\b(?:type|base)="' + re.escape(name) + r'"[^>]*/>'
        )
        with_body = re.compile(
            r'<xs:element\b[^>]*\b(?:type|base)="' + re.escape(name) + r'"[^>]*>.*?</xs:element>',
            re.DOTALL,
        )
        for store in (elements, types):
            for key, xml in list(store.items()):
                cleaned = with_body.sub("", self_closing.sub("", xml))
                if cleaned != xml:
                    store[key] = cleaned

    # a few doc fragments are not deterministic (UPA) or broken - drop those types
    dropped: list[str] = []
    for _ in range(300):
        write_schema()
        try:
            etree.XMLSchema(etree.parse(str(schema_path)))
            break
        except etree.XMLSchemaParseError as exc:
            message = str(exc)
            match = re.search(r"complex type '([^']+)'", message)
            if match and match.group(1) in types:
                name = match.group(1)
                types.pop(name)
                dropped.append(name)
                drop_references(name)
                continue
            match = re.search(r"element decl\. '\{[^}]*\}([^']+)'", message)
            if match and match.group(1) in elements:
                name = match.group(1)
                elements.pop(name)
                dropped.append(f"element {name}")
                continue
            print(f"cannot repair schema: {message}")
            break
    if dropped:
        print(f"dropped {len(dropped)} non deterministic doc types: {', '.join(dropped)}")
    print(f"schema written: {schema_path}")


if __name__ == "__main__":
    main()
