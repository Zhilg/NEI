"""Validate a .vsdx drawing against the official Visio XML schema.

Checks every XML part of the package:
  * well-formed XML (strict parser);
  * XSD validation against VisioSchema15 (tests/schemas/visio.xsd);
  * OPC structure: content types, relationship targets.

The schema was assembled from the official Microsoft docs fragments by
scripts/build_visio_xsd.py, so the check works offline.

Usage: python scripts/validate_visio_vsdx.py drawing.vsdx [more.vsdx ...]
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

try:
    from lxml import etree
except ImportError:  # pragma: no cover - optional dependency
    print("lxml is required for the XSD check: pip install lxml")
    raise SystemExit(2) from None

NS = "http://schemas.microsoft.com/office/visio/2012/main"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "tests" / "schemas"


def _resolve(base_part: str, target: str) -> str:
    parts: list[str] = []
    for piece in (Path(base_part).parent / target).as_posix().split("/"):
        if piece in ("", "."):
            continue
        if piece == "..":
            if parts:
                parts.pop()
            continue
        parts.append(piece)
    return "/".join(parts)


def check(vsdx_path: Path, schema: etree.XMLSchema) -> int:
    failures = 0
    checked = 0
    with zipfile.ZipFile(vsdx_path) as archive:
        names = archive.namelist()
        for name in sorted(names):
            if not name.endswith(".xml"):
                continue
            try:
                tree = etree.fromstring(archive.read(name), parser=etree.XMLParser(resolve_entities=False))
            except etree.XMLSyntaxError as exc:
                print(f"FAIL {name}: not well-formed: {exc}")
                failures += 1
                continue
            root = etree.QName(tree)
            if root.namespace != NS:
                print(f"skip {name}: namespace {root.namespace}")
                continue
            if schema.validate(tree):
                checked += 1
                print(f"OK   {name} (root {root.localname})")
            else:
                failures += 1
                print(f"FAIL {name} (root {root.localname}):")
                for error in schema.error_log:
                    print(f"    line {error.line}: {error.message}")

        types = etree.fromstring(archive.read("[Content_Types].xml"))
        defaults = {
            element.get("Extension", "").lower()
            for element in types.findall(f"{{{CT_NS}}}Default")
        }
        overrides = {
            element.get("PartName", "").lstrip("/")
            for element in types.findall(f"{{{CT_NS}}}Override")
        }
        for name in names:
            if name.endswith("/"):
                continue
            extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if name not in overrides and extension not in defaults:
                print(f"FAIL content type not declared for {name}")
                failures += 1

        for name in names:
            if not name.endswith(".rels"):
                continue
            root = etree.fromstring(archive.read(name))
            base = name.replace("_rels/", "").removesuffix(".rels")
            for relationship in root.findall(f"{{{PKG_REL_NS}}}Relationship"):
                if relationship.get("TargetMode") == "External":
                    continue
                target = relationship.get("Target", "")
                resolved = _resolve(base, target)
                if resolved not in names:
                    print(f"FAIL {name}: {relationship.get('Id')} -> missing {resolved}")
                    failures += 1

    print(f"{vsdx_path}: validated parts {checked}, failures {failures}")
    return failures


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    schema = etree.XMLSchema(etree.parse(str(SCHEMA_DIR / "visio.xsd")))
    total = 0
    for argument in sys.argv[1:]:
        total += check(Path(argument), schema)
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
