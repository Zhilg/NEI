"""Build Visio drawings (.vsdx) from PNG images.

A ``.vsdx`` file is an OPC package: a ZIP archive with XML parts and embedded
media. This module writes the minimal set of parts a Visio drawing needs
(document, page list, page sheets, media) and places every PNG as a picture
shape, either one PNG per Visio page (``pages`` mode) or a tiled contact sheet
(``sheet`` mode).

Only the standard library is used, so the air-gapped image stays untouched.

Run standalone:  idp-visio --out drawing.vsdx page_00001.png page_00002.png
"""

from __future__ import annotations

import logging
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

from idp.config import settings

logger = logging.getLogger(__name__)

VISIO_NS = "http://schemas.microsoft.com/office/visio/2012/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

IMAGE_CONTENT_TYPES = {
    ".png": ("image/png", "PNG"),
    ".jpg": ("image/jpeg", "JPEG"),
    ".jpeg": ("image/jpeg", "JPEG"),
}

_DEFAULT_DPI = 200
_DEFAULT_MARGIN = 0.25
_REQUIRED_PARTS = (
    "[Content_Types].xml",
    "_rels/.rels",
    "visio/document.xml",
    "visio/_rels/document.xml.rels",
    "visio/pages/pages.xml",
    "visio/pages/_rels/pages.xml.rels",
)
_XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'


@dataclass
class VisioImage:
    """A PNG (or JPEG) prepared for embedding into a drawing."""

    path: Path
    width_px: int
    height_px: int

    @property
    def extension(self) -> str:
        return self.path.suffix.lower()

    @property
    def content_type(self) -> str:
        return IMAGE_CONTENT_TYPES.get(self.extension, ("image/png", "PNG"))[0]

    @property
    def compression(self) -> str:
        return IMAGE_CONTENT_TYPES.get(self.extension, ("image/png", "PNG"))[1]


@dataclass
class _Shape:
    """A picture shape placed on a page (coordinates are inches)."""

    name: str
    rel_id: str
    left: float
    bottom: float
    width: float
    height: float
    compression: str = "PNG"

    @property
    def center_x(self) -> float:
        return self.left + self.width / 2.0

    @property
    def center_y(self) -> float:
        return self.bottom + self.height / 2.0


@dataclass
class _Page:
    """One Visio page: its size in inches and the shapes placed on it."""

    name: str
    width: float
    height: float
    shapes: list[_Shape] = field(default_factory=list)


def _num(value: float) -> str:
    """Format a number for a Visio cell value (inches, up to 6 decimals)."""
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text if text else "0"


def _attr(value: str) -> str:
    return quoteattr(str(value))


def image_size(path: Path) -> tuple[int, int]:
    """Pixel size of an image; PNG/JPEG headers are parsed without PIL."""
    try:
        from PIL import Image

        with Image.open(path) as image:
            return image.size
    except Exception:  # noqa: BLE001 - fall back to the PNG header
        logger.debug("Pillow cannot read %s, parsing the header", path)
    try:
        with open(path, "rb") as handle:
            header = handle.read(32)
    except OSError:
        return (1, 1)
    if header[:8] == b"\x89PNG\r\n\x1a\n" and len(header) >= 24:
        width = int.from_bytes(header[16:20], "big")
        height = int.from_bytes(header[20:24], "big")
        return (max(1, width), max(1, height))
    return (1, 1)


def load_images(paths: list[Path]) -> list[VisioImage]:
    """Read pixel sizes of the given image files."""
    images: list[VisioImage] = []
    for path in paths:
        width, height = image_size(path)
        images.append(VisioImage(path=path, width_px=width, height_px=height))
    return images


def _content_types(page_count: int, media_types: set[str]) -> str:
    defaults = [
        (
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        ),
        '<Default Extension="xml" ContentType="application/xml"/>',
    ]
    for extension in sorted(media_types):
        content_type = IMAGE_CONTENT_TYPES.get(extension, ("image/png", "PNG"))[0]
        defaults.append(
            f'<Default Extension={_attr(extension.lstrip("."))} ContentType={_attr(content_type)}/>'
        )
    overrides = [
        (
            '<Override PartName="/docProps/app.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        ),
        (
            '<Override PartName="/docProps/core.xml" '
            'ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        ),
        (
            '<Override PartName="/visio/document.xml" '
            'ContentType="application/vnd.ms-visio.drawing.main+xml"/>'
        ),
        (
            '<Override PartName="/visio/pages/pages.xml" '
            'ContentType="application/vnd.ms-visio.pages+xml"/>'
        ),
        (
            '<Override PartName="/visio/windows.xml" '
            'ContentType="application/vnd.ms-visio.windows+xml"/>'
        ),
    ]
    for index in range(1, page_count + 1):
        overrides.append(
            f'<Override PartName="/visio/pages/page{index}.xml" '
            'ContentType="application/vnd.ms-visio.page+xml"/>'
        )
    return (
        _XML_DECL
        + f'<Types xmlns="{CT_NS}">'
        + "".join(defaults)
        + "".join(overrides)
        + "</Types>"
    )


def _root_rels() -> str:
    return (
        _XML_DECL
        + f'<Relationships xmlns="{PKG_REL_NS}">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.microsoft.com/visio/2010/relationships/document" '
        'Target="visio/document.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" '
        'Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" '
        'Target="docProps/app.xml"/>'
        "</Relationships>"
    )


def _document_xml() -> str:
    return (
        _XML_DECL
        + f'<VisioDocument xmlns="{VISIO_NS}" xmlns:r="{REL_NS}">'
        '<DocumentSettings TopPage="0" DefaultTextStyle="0" DefaultLineStyle="0" '
        'DefaultFillStyle="0" DefaultGuideStyle="0">'
        "<GlueSettings>9</GlueSettings>"
        "<SnapSettings>65847</SnapSettings>"
        "<SnapExtensions>34</SnapExtensions>"
        "<SnapAngles/>"
        "<DynamicGridEnabled>1</DynamicGridEnabled>"
        "<ProtectStyles>0</ProtectStyles>"
        "<ProtectShapes>0</ProtectShapes>"
        "<ProtectMasters>0</ProtectMasters>"
        "<ProtectBkgnds>0</ProtectBkgnds>"
        "</DocumentSettings>"
        "<Colors>"
        '<ColorEntry IX="24" RGB="#7F7F7F"/>'
        '<ColorEntry IX="25" RGB="#FFFFFF"/>'
        "</Colors>"
        "<FaceNames>"
        '<FaceName NameU="Calibri" UnicodeRanges="-1 -369098753 63 0" CharSets="-1" '
        'Panose="2 11 5 2 2 2 4 2 2 4" Flags="325"/>'
        '<FaceName NameU="Times New Roman" UnicodeRanges="-1 -369098753 63 0" CharSets="-1" '
        'Panose="2 2 6 3 3 2 4 2 2 4" Flags="325"/>'
        "</FaceNames>"
        "<StyleSheets>"
        '<StyleSheet ID="0" NameU="No Style" IsCustomName="0" IsCustomNameU="0">'
        '<Cell N="EnableLineProps" V="1"/>'
        '<Cell N="EnableFillProps" V="1"/>'
        '<Cell N="EnableTextProps" V="1"/>'
        '<Cell N="LineWeight" V="0.01"/>'
        '<Cell N="LineColor" V="#000000"/>'
        '<Cell N="LinePattern" V="1"/>'
        '<Cell N="Rounding" V="0.0625"/>'
        '<Cell N="EndArrow" V="0"/>'
        '<Cell N="BeginArrow" V="0"/>'
        '<Cell N="LineCap" V="0"/>'
        '<Cell N="FillForegnd" V="#ffffff"/>'
        '<Cell N="FillBkgnd" V="#ffffff"/>'
        '<Cell N="FillPattern" V="1"/>'
        '<Cell N="ShdwPattern" V="0"/>'
        '<Cell N="LeftMargin" V="0.125"/>'
        '<Cell N="RightMargin" V="0.125"/>'
        '<Cell N="TopMargin" V="0.125"/>'
        '<Cell N="BottomMargin" V="0.125"/>'
        '<Cell N="VerticalAlign" V="0"/>'
        '<Cell N="TextBkgnd" V="#ffffff"/>'
        '<Cell N="DefaultTabStop" V="0.5"/>'
        '<Cell N="TextDirection" V="0"/>'
        "</StyleSheet>"
        "</StyleSheets>"
        '<DocumentSheet NameU="TheDoc" Name="TheDoc" LineStyle="0" FillStyle="0" TextStyle="0"/>'
        "</VisioDocument>"
    )


def _document_rels() -> str:
    return (
        _XML_DECL
        + f'<Relationships xmlns="{PKG_REL_NS}">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.microsoft.com/visio/2010/relationships/pages" '
        'Target="pages/pages.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.microsoft.com/visio/2010/relationships/windows" '
        'Target="windows.xml"/>'
        "</Relationships>"
    )


def _pages_xml(pages: list[_Page]) -> str:
    chunks = [
        _XML_DECL,
        f'<Pages xmlns="{VISIO_NS}" xmlns:r="{REL_NS}">',
    ]
    for index, page in enumerate(pages):
        chunks.append(
            f'<Page ID="{index}" NameU="Page-{index + 1}" Name="Page-{index + 1}" '
            f'ViewScale="-1" ViewCenterX="{_num(page.width / 2)}" '
            f'ViewCenterY="{_num(page.height / 2)}">'
            '<PageSheet LineStyle="0" FillStyle="0" TextStyle="0">'
            f'<Cell N="PageWidth" V="{_num(page.width)}"/>'
            f'<Cell N="PageHeight" V="{_num(page.height)}"/>'
            '<Cell N="ShdwOffsetX" V="0.1181102362204724"/>'
            '<Cell N="ShdwOffsetY" V="-0.1181102362204724"/>'
            '<Cell N="PageScale" V="0.03937007874015748" U="MM"/>'
            '<Cell N="DrawingScale" V="0.03937007874015748" U="MM"/>'
            '<Cell N="DrawingSizeType" V="0"/>'
            '<Cell N="DrawingScaleType" V="0"/>'
            '<Cell N="InhibitSnap" V="0"/>'
            '<Cell N="PageLockReplace" V="0" U="BOOL"/>'
            '<Cell N="PageLockDuplicate" V="0" U="BOOL"/>'
            '<Cell N="UIVisibility" V="0"/>'
            '<Cell N="ShdwType" V="0"/>'
            '<Cell N="ShdwObliqueAngle" V="0"/>'
            '<Cell N="ShdwScaleFactor" V="1"/>'
            '<Cell N="DrawingResizeType" V="1"/>'
            '<Cell N="PageShapeSplit" V="1"/>'
            "</PageSheet>"
            f'<Rel r:id="rId{index + 1}"/>'
            "</Page>"
        )
    chunks.append("</Pages>")
    return "".join(chunks)


def _pages_rels(page_count: int) -> str:
    chunks = [_XML_DECL, f'<Relationships xmlns="{PKG_REL_NS}">']
    for index in range(1, page_count + 1):
        chunks.append(
            f'<Relationship Id="rId{index}" '
            'Type="http://schemas.microsoft.com/visio/2010/relationships/page" '
            f'Target="page{index}.xml"/>'
        )
    chunks.append("</Relationships>")
    return "".join(chunks)


def _page_xml(page: _Page) -> str:
    chunks = [
        _XML_DECL,
        f'<PageContents xmlns="{VISIO_NS}" xmlns:r="{REL_NS}">',
        "<Shapes>",
    ]
    for index, shape in enumerate(page.shapes, start=1):
        chunks.append(
            f'<Shape ID="{index}" NameU={_attr(shape.name)} Name={_attr(shape.name)} '
            'Type="Foreign" LineStyle="0" FillStyle="0" TextStyle="0">'
            f'<Cell N="PinX" V="{_num(shape.center_x)}"/>'
            f'<Cell N="PinY" V="{_num(shape.center_y)}"/>'
            f'<Cell N="Width" V="{_num(shape.width)}"/>'
            f'<Cell N="Height" V="{_num(shape.height)}"/>'
            f'<Cell N="LocPinX" V="{_num(shape.width / 2)}" F="Width*0.5"/>'
            f'<Cell N="LocPinY" V="{_num(shape.height / 2)}" F="Height*0.5"/>'
            '<Cell N="Angle" V="0"/>'
            '<Cell N="FlipX" V="0"/>'
            '<Cell N="FlipY" V="0"/>'
            '<Cell N="ResizeMode" V="0"/>'
            '<Cell N="ObjType" V="1"/>'
            '<Cell N="ImgOffsetX" V="0" F="ImgWidth*0"/>'
            '<Cell N="ImgOffsetY" V="0" F="ImgHeight*0"/>'
            f'<Cell N="ImgWidth" V="{_num(shape.width)}" F="Width*1"/>'
            f'<Cell N="ImgHeight" V="{_num(shape.height)}" F="Height*1"/>'
            '<Cell N="LineWeight" V="0.01"/>'
            '<Cell N="LineColor" V="#000000"/>'
            '<Cell N="LinePattern" V="0"/>'
            '<Cell N="Rounding" V="0.0625"/>'
            '<Cell N="FillForegnd" V="#ffffff"/>'
            '<Cell N="FillBkgnd" V="#ffffff"/>'
            '<Cell N="FillPattern" V="0"/>'
            '<Cell N="ShdwPattern" V="0"/>'
            '<Cell N="LeftMargin" V="0.0555555555555556"/>'
            '<Cell N="VerticalAlign" V="0"/>'
            '<Cell N="TextDirection" V="0"/>'
            '<Section N="Geometry" IX="0">'
            '<Cell N="NoFill" V="1"/>'
            '<Cell N="NoLine" V="1"/>'
            '<Cell N="NoShow" V="0"/>'
            '<Cell N="NoSnap" V="0"/>'
            '<Cell N="NoQuickDrag" V="0"/>'
            '<Row T="RelMoveTo" IX="1"><Cell N="X" V="0"/><Cell N="Y" V="0"/></Row>'
            '<Row T="RelLineTo" IX="2"><Cell N="X" V="1"/><Cell N="Y" V="0"/></Row>'
            '<Row T="RelLineTo" IX="3"><Cell N="X" V="1"/><Cell N="Y" V="1"/></Row>'
            '<Row T="RelLineTo" IX="4"><Cell N="X" V="0"/><Cell N="Y" V="1"/></Row>'
            '<Row T="RelLineTo" IX="5"><Cell N="X" V="0"/><Cell N="Y" V="0"/></Row>'
            "</Section>"
            f'<ForeignData ForeignType="Bitmap" CompressionType="{shape.compression}">'
            f'<Rel r:id={_attr(shape.rel_id)}/>'
            "</ForeignData>"
            "</Shape>"
        )
    chunks.append("</Shapes></PageContents>")
    return "".join(chunks)


def _page_rels(images: list[VisioImage]) -> str:
    chunks = [_XML_DECL, f'<Relationships xmlns="{PKG_REL_NS}">']
    for index, image in enumerate(images, start=1):
        name = f"image{index}{image.extension}"
        chunks.append(
            f'<Relationship Id="rId{index}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
            f'Target="../media/{name}"/>'
        )
    chunks.append("</Relationships>")
    return "".join(chunks)


def _windows_xml(pages: list[_Page]) -> str:
    first = pages[0]
    return (
        _XML_DECL
        + f'<Windows xmlns="{VISIO_NS}" xmlns:r="{REL_NS}" '
        'ClientWidth="1920" ClientHeight="807">'
        '<Window ID="0" WindowType="Drawing" WindowState="1073741824" WindowLeft="-9" '
        'WindowTop="-38" WindowWidth="1938" WindowHeight="854" ContainerType="Page" '
        f'Page="0" ViewScale="-1" ViewCenterX="{_num(first.width / 2)}" '
        f'ViewCenterY="{_num(first.height / 2)}">'
        "<ShowRulers>1</ShowRulers>"
        "<ShowGrid>1</ShowGrid>"
        "<ShowPageBreaks>0</ShowPageBreaks>"
        "<ShowGuides>1</ShowGuides>"
        "<ShowConnectionPoints>1</ShowConnectionPoints>"
        "<GlueSettings>9</GlueSettings>"
        "<SnapSettings>65847</SnapSettings>"
        "<SnapExtensions>34</SnapExtensions>"
        "<SnapAngles/>"
        "<DynamicGridEnabled>1</DynamicGridEnabled>"
        "<TabSplitterPos>0.5</TabSplitterPos>"
        "</Window></Windows>"
    )


def _doc_props(title: str, page_count: int) -> tuple[str, str]:
    core = (
        _XML_DECL
        + '<cp:coreProperties '
        'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:dcmitype="http://purl.org/dc/dcmitype/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{escape(title)}</dc:title>"
        "<dc:creator>IDP</dc:creator>"
        "<cp:lastModifiedBy>IDP</cp:lastModifiedBy>"
        '<dcterms:created xsi:type="dcterms:W3CDTF">2024-01-01T00:00:00Z</dcterms:created>'
        '<dcterms:modified xsi:type="dcterms:W3CDTF">2024-01-01T00:00:00Z</dcterms:modified>'
        "</cp:coreProperties>"
    )
    app = (
        _XML_DECL
        + '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        "<Template/>"
        "<TotalTime>0</TotalTime>"
        f"<Pages>{page_count}</Pages>"
        "<Words>0</Words>"
        "<Characters>0</Characters>"
        "<Application>Microsoft Visio</Application>"
        "<DocSecurity>0</DocSecurity>"
        "<Lines>0</Lines>"
        "<Paragraphs>0</Paragraphs>"
        "<ScaleCrop>false</ScaleCrop>"
        "<Company/>"
        "<LinksUpToDate>false</LinksUpToDate>"
        "<CharactersWithSpaces>0</CharactersWithSpaces>"
        "<SharedDoc>false</SharedDoc>"
        "<HyperlinksChanged>false</HyperlinksChanged>"
        "<AppVersion>16.0000</AppVersion>"
        "</Properties>"
    )
    return core, app


def _fit(width: float, height: float, max_width: float, max_height: float) -> tuple[float, float]:
    """Scale (width, height) to fit into the box, keeping the aspect ratio."""
    if width <= 0 or height <= 0:
        return (max_width, max_height)
    scale = min(max_width / width, max_height / height)
    return (width * scale, height * scale)


def _plan_pages(
    images: list[VisioImage],
    *,
    dpi: int,
    mode: str,
    page_size: tuple[float, float] | None,
    columns: int | None,
    margin: float,
) -> list[_Page]:
    """Compute page sizes and shape placement for every image."""
    natural = [
        (image, image.width_px / float(dpi), image.height_px / float(dpi)) for image in images
    ]

    if mode == "sheet":
        return _plan_sheet(natural, page_size=page_size, columns=columns, margin=margin)

    pages: list[_Page] = []
    for index, (image, width, height) in enumerate(natural, start=1):
        if page_size is not None:
            page_width, page_height = page_size
            fitted = _fit(width, height, page_width - 2 * margin, page_height - 2 * margin)
            left = (page_width - fitted[0]) / 2.0
            bottom = (page_height - fitted[1]) / 2.0
        else:
            page_width, page_height = width, height
            fitted = (width, height)
            left, bottom = 0.0, 0.0
        pages.append(
            _Page(
                name=f"Page-{index}",
                width=page_width,
                height=page_height,
                shapes=[
                    _Shape(
                        name=f"Picture {index}",
                        rel_id="rId1",
                        left=left,
                        bottom=bottom,
                        width=fitted[0],
                        height=fitted[1],
                        compression=image.compression,
                    )
                ],
            )
        )
    return pages


def _plan_sheet(
    natural: list[tuple[VisioImage, float, float]],
    *,
    page_size: tuple[float, float] | None,
    columns: int | None,
    margin: float,
) -> list[_Page]:
    """Tile every image on a single page."""
    count = len(natural)
    if columns is None or columns < 1:
        columns = max(1, int(count ** 0.5 + 0.999))
    rows = max(1, int(count / columns + 0.999))

    if page_size is not None:
        available_width = (page_size[0] - 2 * margin) / columns
        available_height = (page_size[1] - 2 * margin) / rows
        cell_width = max(
            _fit(width, height, available_width, available_height)[0]
            for _, width, height in natural
        )
        cell_height = max(
            _fit(width, height, available_width, available_height)[1]
            for _, width, height in natural
        )
        page_width, page_height = page_size
    else:
        cell_width = max(width for _, width, _ in natural)
        cell_height = max(height for _, _, height in natural)
        page_width = columns * cell_width
        page_height = rows * cell_height

    shapes: list[_Shape] = []
    for index, (image, width, height) in enumerate(natural):
        column = index % columns
        row = index // columns
        fitted = _fit(width, height, cell_width, cell_height)
        left = column * cell_width + (cell_width - fitted[0]) / 2.0
        bottom = page_height - (row + 1) * cell_height + (cell_height - fitted[1]) / 2.0
        shapes.append(
            _Shape(
                name=f"Picture {index + 1}",
                rel_id=f"rId{index + 1}",
                left=left,
                bottom=bottom,
                width=fitted[0],
                height=fitted[1],
                compression=image.compression,
            )
        )
    return [
        _Page(
            name="Page-1",
            width=page_width,
            height=page_height,
            shapes=shapes,
        )
    ]


def build_vsdx(
    images: list[VisioImage] | list[Path],
    out_path: Path,
    *,
    dpi: int | None = None,
    mode: str = "pages",
    page_size: tuple[float, float] | None = None,
    columns: int | None = None,
    title: str = "",
    margin: float = _DEFAULT_MARGIN,
) -> Path:
    """Write a Visio drawing with the given images.

    ``mode='pages'`` puts every image on its own page, ``mode='sheet'`` tiles
    them on a single page. ``page_size`` (inches) forces a fixed page format.
    """
    if not images:
        raise ValueError("no images to convert")
    if dpi is None or dpi <= 0:
        dpi = settings.render_dpi or _DEFAULT_DPI
    if mode not in ("pages", "sheet"):
        raise ValueError(f"unknown mode: {mode}")

    prepared: list[VisioImage] = []
    for image in images:
        if isinstance(image, VisioImage):
            prepared.append(image)
        else:
            path = Path(image)
            width, height = image_size(path)
            prepared.append(VisioImage(path=path, width_px=width, height_px=height))
    pages = _plan_pages(
        prepared,
        dpi=dpi,
        mode=mode,
        page_size=page_size,
        columns=columns,
        margin=margin,
    )
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    media_types = {image.extension for image in prepared}
    core, app = _doc_props(title or out_path.stem, len(pages))

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _content_types(len(pages), media_types))
        archive.writestr("_rels/.rels", _root_rels())
        archive.writestr("docProps/core.xml", core)
        archive.writestr("docProps/app.xml", app)
        archive.writestr("visio/document.xml", _document_xml())
        archive.writestr("visio/_rels/document.xml.rels", _document_rels())
        archive.writestr("visio/pages/pages.xml", _pages_xml(pages))
        archive.writestr("visio/pages/_rels/pages.xml.rels", _pages_rels(len(pages)))
        archive.writestr("visio/windows.xml", _windows_xml(pages))

        media_index = 0
        for page_index, page in enumerate(pages, start=1):
            page_media: list[VisioImage] = []
            for position, shape in enumerate(page.shapes, start=1):
                shape.rel_id = f"rId{position}"
                image = prepared[media_index]
                media_index += 1
                page_media.append(image)
                archive.writestr(
                    f"visio/media/image{media_index}{image.extension}",
                    image.path.read_bytes(),
                )
            archive.writestr(f"visio/pages/page{page_index}.xml", _page_xml(page))
            archive.writestr(
                f"visio/pages/_rels/page{page_index}.xml.rels",
                _page_rels(page_media),
            )
    logger.info("Visio drawing written: %s (%d page(s))", out_path, len(pages))
    return out_path


def bundle_images(bundle_dir: Path) -> list[Path]:
    """Page renders of a txts bundle.

    Page renders (``page_*.png``) are the document pages; when a bundle has
    none (DOCX/PPTX/HTML), its embedded pictures are used instead.
    """
    images_dir = bundle_dir / "images"
    if not images_dir.is_dir():
        return []
    pages = sorted(images_dir.glob("page_*.png"))
    if pages:
        return pages
    return sorted(
        path
        for path in images_dir.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_CONTENT_TYPES
    )


def build_bundle_vsdx(
    bundle_dir: Path,
    out_path: Path | None = None,
    *,
    dpi: int | None = None,
    mode: str = "pages",
    page_size: tuple[float, float] | None = None,
    columns: int | None = None,
    title: str = "",
) -> Path:
    """Build a Visio drawing from the images of a txts bundle."""
    bundle_dir = Path(bundle_dir)
    images = bundle_images(bundle_dir)
    if not images:
        raise ValueError(f"bundle has no images: {bundle_dir}")
    target = Path(out_path) if out_path is not None else bundle_dir / "reconstruct.vsdx"
    return build_vsdx(
        images,
        target,
        dpi=dpi,
        mode=mode,
        page_size=page_size,
        columns=columns,
        title=title or bundle_dir.name,
    )


def _resolve(base_part: str, target: str) -> str:
    """Resolve a relationship target against the part that declares it."""
    base = Path(base_part).parent
    resolved = (base / target).as_posix()
    parts: list[str] = []
    for piece in resolved.split("/"):
        if piece in ("", "."):
            continue
        if piece == "..":
            if parts:
                parts.pop()
            continue
        parts.append(piece)
    return "/".join(parts)


def validate_vsdx(path: Path) -> list[str]:
    """Check the package structure of a .vsdx file.

    Returns a list of problems; an empty list means the file looks sound.
    """
    problems: list[str] = []
    path = Path(path)
    if not path.is_file():
        return [f"file not found: {path}"]
    try:
        with zipfile.ZipFile(path) as archive:
            broken = archive.testzip()
            if broken is not None:
                problems.append(f"corrupted zip entry: {broken}")
            names = set(archive.namelist())
            for required in _REQUIRED_PARTS:
                if required not in names:
                    problems.append(f"missing part: {required}")
            for name in sorted(names):
                if not name.endswith((".xml", ".rels")):
                    continue
                try:
                    ET.fromstring(archive.read(name))
                except ET.ParseError as exc:
                    problems.append(f"malformed xml in {name}: {exc}")
            problems.extend(_validate_content_types(archive, names))
            problems.extend(_validate_relationships(archive, names))
            problems.extend(_validate_pages(archive, names))
            problems.extend(_validate_schema_rules(archive, names))
    except zipfile.BadZipFile as exc:
        problems.append(f"not a zip archive: {exc}")
    return problems


def _validate_content_types(archive: zipfile.ZipFile, names: set[str]) -> list[str]:
    problems: list[str] = []
    if "[Content_Types].xml" not in names:
        return problems
    root = ET.fromstring(archive.read("[Content_Types].xml"))
    defaults = {
        element.get("Extension", "").lower()
        for element in root.findall(f"{{{CT_NS}}}Default")
    }
    overrides = {
        element.get("PartName", "").lstrip("/")
        for element in root.findall(f"{{{CT_NS}}}Override")
    }
    for name in sorted(names):
        if name.endswith("/"):
            continue
        extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if name in overrides or extension in defaults:
            continue
        problems.append(f"content type not declared for {name}")
    return problems


def _validate_relationships(archive: zipfile.ZipFile, names: set[str]) -> list[str]:
    problems: list[str] = []
    for name in sorted(names):
        if not name.endswith(".rels"):
            continue
        root = ET.fromstring(archive.read(name))
        base = name.replace("_rels/", "").removesuffix(".rels")
        for relationship in root.findall(f"{{{PKG_REL_NS}}}Relationship"):
            if relationship.get("TargetMode") == "External":
                continue
            target = relationship.get("Target", "")
            resolved = _resolve(base, target)
            if resolved not in names:
                problems.append(f"{name}: relationship {relationship.get('Id')} -> missing {resolved}")
    return problems


def _validate_pages(archive: zipfile.ZipFile, names: set[str]) -> list[str]:
    problems: list[str] = []
    if "visio/pages/pages.xml" not in names:
        return problems
    pages_root = ET.fromstring(archive.read("visio/pages/pages.xml"))
    page_elements = pages_root.findall(f"{{{VISIO_NS}}}Page")
    if not page_elements:
        problems.append("visio/pages/pages.xml declares no pages")
    for index, page in enumerate(page_elements, start=1):
        part = f"visio/pages/page{index}.xml"
        if part not in names:
            problems.append(f"missing page part: {part}")
            continue
        page_root = ET.fromstring(archive.read(part))
        shapes = page_root.findall(f".//{{{VISIO_NS}}}Shape")
        if not shapes:
            problems.append(f"{part} contains no shapes")
        rels_part = f"visio/pages/_rels/page{index}.xml.rels"
        rel_ids: set[str] = set()
        if rels_part in names:
            rels_root = ET.fromstring(archive.read(rels_part))
            rel_ids = {
                element.get("Id", "")
                for element in rels_root.findall(f"{{{PKG_REL_NS}}}Relationship")
            }
        for shape in shapes:
            for reference in shape.findall(f"{{{VISIO_NS}}}ForeignData/{{{VISIO_NS}}}Rel"):
                rel_id = reference.get(f"{{{REL_NS}}}id", "")
                if rel_id not in rel_ids:
                    problems.append(f"{part}: picture refers to unknown {rel_id}")
    return problems


def _validate_schema_rules(archive: zipfile.ZipFile, names: set[str]) -> list[str]:
    """Check the parts against the rules of the official Visio schema.

    The rules are the ones VisioSchema15 enforces for the elements this module
    writes (see tests/schemas/visio.xsd): the order of the children of the
    document, the mandatory ``ColorEntry`` inside ``Colors``, the mandatory
    ``PageSheet``/``Rel`` of a page and the picture shape layout.
    """
    problems: list[str] = []
    if "visio/document.xml" in names:
        root = ET.fromstring(archive.read("visio/document.xml"))
        order = [child.tag for child in root]
        expected = [
            f"{{{VISIO_NS}}}DocumentSettings",
            f"{{{VISIO_NS}}}Colors",
            f"{{{VISIO_NS}}}FaceNames",
            f"{{{VISIO_NS}}}StyleSheets",
            f"{{{VISIO_NS}}}DocumentSheet",
        ]
        if [tag for tag in order if tag in expected] != expected:
            problems.append("visio/document.xml: children are out of the schema order")
        colors = root.find(f"{{{VISIO_NS}}}Colors")
        if colors is None:
            problems.append("visio/document.xml: no Colors table")
        elif not colors.findall(f"{{{VISIO_NS}}}ColorEntry"):
            problems.append("visio/document.xml: Colors needs at least one ColorEntry")
        styles = root.find(f"{{{VISIO_NS}}}StyleSheets")
        if styles is None or not styles.findall(f"{{{VISIO_NS}}}StyleSheet"):
            problems.append("visio/document.xml: StyleSheets needs at least one StyleSheet")
    if "visio/pages/pages.xml" in names:
        pages_root = ET.fromstring(archive.read("visio/pages/pages.xml"))
        for index, page in enumerate(pages_root.findall(f"{{{VISIO_NS}}}Page"), start=1):
            if page.get("ID") is None:
                problems.append(f"visio/pages/pages.xml: page {index} has no ID")
            if page.find(f"{{{VISIO_NS}}}PageSheet") is None:
                problems.append(f"visio/pages/pages.xml: page {index} has no PageSheet")
            if page.find(f"{{{VISIO_NS}}}Rel") is None:
                problems.append(f"visio/pages/pages.xml: page {index} has no Rel")
    for name in sorted(names):
        if not re.fullmatch(r"visio/pages/page\d+\.xml", name):
            continue
        page_root = ET.fromstring(archive.read(name))
        shapes_element = page_root.find(f"{{{VISIO_NS}}}Shapes")
        if shapes_element is None:
            problems.append(f"{name}: no Shapes element")
            continue
        for shape in shapes_element.findall(f"{{{VISIO_NS}}}Shape"):
            if shape.get("Type") != "Foreign":
                problems.append(f"{name}: picture shape must have Type='Foreign'")
            if shape.get("ID") is None:
                problems.append(f"{name}: shape without ID")
            foreign = shape.find(f"{{{VISIO_NS}}}ForeignData")
            if foreign is None:
                problems.append(f"{name}: shape without ForeignData")
                continue
            if foreign.get("ForeignType") != "Bitmap":
                problems.append(f"{name}: ForeignData must be a Bitmap")
            if foreign.get("CompressionType") not in ("PNG", "JPEG"):
                problems.append(f"{name}: unsupported CompressionType {foreign.get('CompressionType')!r}")
            if foreign.find(f"{{{VISIO_NS}}}Rel") is None:
                problems.append(f"{name}: ForeignData without Rel")
            cells = {cell.get("N") for cell in shape.findall(f"{{{VISIO_NS}}}Cell")}
            for required in ("PinX", "PinY", "Width", "Height"):
                if required not in cells:
                    problems.append(f"{name}: shape without {required} cell")
    return problems


def inspect_vsdx(path: Path) -> dict:
    """Summary of a .vsdx file: page count, page sizes and embedded images."""
    summary = {"pages": 0, "images": 0, "sizes": []}
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        summary["images"] = len([name for name in names if name.startswith("visio/media/")])
        if "visio/pages/pages.xml" not in names:
            return summary
        pages_root = ET.fromstring(archive.read("visio/pages/pages.xml"))
        for page in pages_root.findall(f"{{{VISIO_NS}}}Page"):
            summary["pages"] += 1
            sheet = page.find(f"{{{VISIO_NS}}}PageSheet")
            width = height = None
            if sheet is not None:
                for cell in sheet.findall(f"{{{VISIO_NS}}}Cell"):
                    if cell.get("N") == "PageWidth":
                        width = float(cell.get("V", "0"))
                    elif cell.get("N") == "PageHeight":
                        height = float(cell.get("V", "0"))
            summary["sizes"].append((width, height))
    return summary
