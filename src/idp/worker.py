"""Simplified worker: scan input, process PDF/DOCX, write results."""

from __future__ import annotations

import os
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


async def _process_file(file_path: Path, stats: StatsWriter, entity_store: EntityStore, pbar: tqdm) -> None:
    relative = file_path.relative_to(settings.input_root)
    stem = file_path.stem
    output_md = settings.output_root / f"{stem}.md"
    output_md_tmp = output_md.with_suffix(".md.tmp")

    if output_md.exists():
        pbar.set_postfix(file=file_path.name, stage="skip")
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
            pbar.set_postfix(file=file_path.name, stage="render")
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                pngs = render_pdf_to_pngs(file_path, tmp_path / "pages")
                timer.pages = len(pngs)
                pbar.set_postfix(file=file_path.name, stage="vlm")
                markdown = await reconstruct_markdown_from_images(pngs)
        else:
            pbar.set_postfix(file=file_path.name, stage="docx")
            markdown = convert_docx_to_markdown(file_path)

        pbar.set_postfix(file=file_path.name, stage="save")
        output_md_tmp.write_text(markdown, encoding="utf-8")

        pbar.set_postfix(file=file_path.name, stage="entities")
        entities = await extract_entities(output_md_tmp)
        entity_store.append(str(relative), entities)

        os.replace(output_md_tmp, output_md)
        timer.finish(status="ok")
        pbar.set_postfix(file=file_path.name, stage="done")
    except Exception as exc:  # noqa: BLE001
        if output_md_tmp.exists():
            output_md_tmp.unlink()
        timer.finish(status="error", error=str(exc))
        pbar.set_postfix(file=file_path.name, stage="error")


def main() -> None:
    import asyncio
    asyncio.run(_main())


async def _main() -> None:
    settings.output_root.mkdir(parents=True, exist_ok=True)
    files = _find_files(settings.input_root)
    stats = StatsWriter(settings.output_root / "stats.jsonl")
    entity_store = EntityStore(settings.output_root / "entities.jsonl")

    pbar = tqdm(files, desc="Files", unit="file")
    for file_path in pbar:
        await _process_file(file_path, stats, entity_store, pbar)


if __name__ == "__main__":
    main()
