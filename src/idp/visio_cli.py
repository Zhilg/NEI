"""Convert PNG images into Visio drawings (.vsdx).

Usage:
    idp-visio --out drawing.vsdx page_00001.png page_00002.png
    idp-visio --out drawing.vsdx --mode sheet ./rendered/report/
    idp-visio --bundle data/output/txts/report.pdf
"""

from __future__ import annotations

import argparse
import logging
import os
import re
from pathlib import Path

from idp.config import settings
from idp.visio_exporter import (
    IMAGE_CONTENT_TYPES,
    build_bundle_vsdx,
    build_vsdx,
    inspect_vsdx,
    validate_vsdx,
)

logger = logging.getLogger(__name__)

_SIZE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*[xX×]\s*([0-9]*\.?[0-9]+)\s*$")


def parse_page_size(value: str) -> tuple[float, float] | None:
    """Parse ``--page-size 8.27x11.69`` (inches)."""
    if not value:
        return None
    match = _SIZE_RE.match(value)
    if match is None:
        raise argparse.ArgumentTypeError(
            f"invalid page size: {value!r} (expected WxH in inches, e.g. 8.27x11.69)"
        )
    width, height = float(match.group(1)), float(match.group(2))
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError(f"invalid page size: {value!r}")
    return (width, height)


def collect_inputs(inputs: list[str], pattern: str) -> list[Path]:
    """Expand files and directories into a sorted list of images."""
    found: list[Path] = []
    for raw in inputs:
        path = Path(raw)
        if path.is_dir():
            found.extend(sorted(p for p in path.rglob(pattern) if p.is_file()))
        elif path.is_file():
            found.append(path)
        else:
            logger.warning("not found, skipped: %s", path)
    unique: list[Path] = []
    seen: set[str] = set()
    for path in found:
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert PNG images into a Visio drawing (.vsdx)",
    )
    parser.add_argument("inputs", nargs="*", help="PNG files or directories with images")
    parser.add_argument("--out", type=Path, help="output .vsdx file")
    parser.add_argument("--bundle", type=Path, help="txts bundle to convert (uses its images/)")
    parser.add_argument("--mode", choices=("pages", "sheet"), default="pages",
                        help="one Visio page per image (default) or a single tiled sheet")
    parser.add_argument("--dpi", type=int, default=0,
                        help=f"pixels per inch used for page size (default {settings.render_dpi})")
    parser.add_argument("--page-size", type=parse_page_size, default=None,
                        help="fixed page size in inches, e.g. 8.27x11.69")
    parser.add_argument("--columns", type=int, default=0,
                        help="number of columns in sheet mode (default: square grid)")
    parser.add_argument("--pattern", default="*.png", help="image glob for directories")
    parser.add_argument("--title", default="", help="document title")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, os.getenv("IDP_LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args()

    if args.bundle is not None:
        bundle_dir = args.bundle
        if not bundle_dir.is_dir():
            raise SystemExit(f"bundle not found: {bundle_dir}")
        out_path = args.out or bundle_dir / "reconstruct.vsdx"
        try:
            created = build_bundle_vsdx(
                bundle_dir,
                out_path,
                dpi=args.dpi or None,
                mode=args.mode,
                page_size=args.page_size,
                columns=args.columns or None,
                title=args.title,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    else:
        images = collect_inputs(args.inputs, args.pattern)
        if not images:
            raise SystemExit("no images found (pass PNG files, a directory or --bundle)")
        unsupported = [
            path for path in images if path.suffix.lower() not in IMAGE_CONTENT_TYPES
        ]
        for path in unsupported:
            logger.warning("unsupported image type, skipped: %s", path)
        images = [path for path in images if path.suffix.lower() in IMAGE_CONTENT_TYPES]
        if not images:
            raise SystemExit("no supported images found (png/jpg only)")
        if args.out is not None:
            out_path = args.out
        else:
            first = images[0]
            out_path = first.with_suffix(".vsdx")
        created = build_vsdx(
            images,
            out_path,
            dpi=args.dpi or None,
            mode=args.mode,
            page_size=args.page_size,
            columns=args.columns or None,
            title=args.title,
        )

    problems = validate_vsdx(created)
    summary = inspect_vsdx(created)
    print(f"created {created}")
    print(
        f"pages: {summary['pages']}, images: {summary['images']}, "
        f"size: {created.stat().st_size} bytes"
    )
    if problems:
        for problem in problems:
            print(f"invalid: {problem}")
        raise SystemExit(1)
    print("structure check: ok")


if __name__ == "__main__":
    main()
