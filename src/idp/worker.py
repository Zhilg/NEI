"""Simplified worker: scan input, process PDF/DOCX, write results."""

from __future__ import annotations

import tempfile
from pathlib import Path

from tqdm import tqdm

from idp.config import settings
from idp.docx_converter import convert_docx_to_markdown
from idp.entity_store import EntityStore
from idp.entity_client import extract_entities
from idp.renderer import render_pdf_to_pngs
from idp.stats_writer import StatsWriter, FileTimer
from idp.vlm_client import reconstruct_markdown_from_images


SUPPORTED_EXTENSIONS = {".pdf", ".docx"}


def _find_files(input_root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(input_root.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
            files.append(path)
    return files


def _process_file(file_path: Path, stats: StatsWriter, entity_store: EntityStore) -> None:
    relative = file_path.relative_to(settings.input_root)
    stem = file_path.stem
    output_md = settings.output_root / f"{stem}.md"

    if output_md.exists():
        return

    timer = FileTimer(
        stats_writer=stats,
        file_name=str(relative),
        file_type=file_path.suffix.lower().lstrip("."),
        size_bytes=file_path.stat().st_size,
        pages=None,
    )

    try:
        if file_path.suffix.lower() == ".pdf":
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                pngs = render_pdf_to_pngs(file_path, tmp_path / "pages")
                timer.pages = len(pngs)
                markdown = await reconstruct_markdown_from_images(pngs)
        else:
            markdown = convert_docx_to_markdown(file_path)

        output_md.write_text(markdown, encoding="utf-8")
        entities = await extract_entities(output_md)
        entity_store.append(str(relative), entities)
        timer.finish(status="ok")
    except Exception as exc:  # noqa: BLE001
        timer.finish(status="error", error=str(exc))


async def main() -> None:
    settings.output_root.mkdir(parents=True, exist_ok=True)
    files = _find_files(settings.input_root)
    stats = StatsWriter(settings.output_root / "stats.jsonl")
    entity_store = EntityStore(settings.output_root / "entities.jsonl")

    for file_path in tqdm(files, desc="Files", unit="file"):
        _process_file(file_path, stats, entity_store)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
