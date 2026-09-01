"""VL-only pipeline: reconstruct markdown and extract entities from PDF/DOCX/PPTX/HTML files."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from tqdm import tqdm

from idp.config import settings
from idp.docx_converter import convert_docx_to_markdown
from idp.html_converter import convert_html_folder_to_markdown, convert_html_to_markdown
from idp.pptx_converter import convert_pptx_to_markdown
from idp.renderer import extract_pdf_text_and_visual_pages
from idp.result_writer import ResultWriter
from idp.stats_writer import StatsWriter, FileTimer
from idp.vlm_client import (
    extract_entities_from_text,
    extract_paragraphs,
    reconstruct_markdown,
    update_entity_schema,
)


SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".pptx", ".html"}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="IDP pipeline: extract entities from documents")
    parser.add_argument("--artifacts", action="store_true", help="Save markdown artifacts to output directory")
    return parser.parse_args()


def _find_files(input_root: Path) -> list[Path]:
    files: list[Path] = []
    dirs: set[Path] = set()
    for path in sorted(input_root.rglob("*")):
        if path.is_dir():
            html_files = list(path.glob("*.html")) + list(path.glob("*.htm"))
            if html_files:
                dirs.add(path)
        elif path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
            files.append(path)
    filtered_files = [f for f in files if not any(f.is_relative_to(d) for d in dirs)]
    return sorted(dirs) + sorted(filtered_files)


def _already_processed(file_path: Path, processed: set[str]) -> bool:
    relative = str(file_path.relative_to(settings.input_root))
    return relative in processed


def _get_new_entity_types(entities: list[dict], schema: dict) -> list[dict]:
    existing_names = {t["name"] for t in schema.get("entity_types", [])}
    found_types: set[str] = set()
    for entity in entities:
        if isinstance(entity, dict):
            etype = entity.get("type")
            if etype and etype not in existing_names:
                found_types.add(etype)
    new_types = []
    for name in sorted(found_types):
        new_types.append({"name": name, "description": f"Auto-detected entity type: {name}"})
    return new_types


def _postprocess_markdown(markdown: str) -> str:
    lines = markdown.splitlines()
    result: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if "|" in stripped and (stripped.startswith("|") or re.match(r"^\s*\|", stripped)):
            table_block = [stripped]
            j = i + 1
            while j < len(lines):
                next_line = lines[j].strip()
                if "|" in next_line and (next_line.startswith("|") or re.match(r"^\s*\|", next_line)):
                    table_block.append(next_line)
                    j += 1
                else:
                    break
            result.append("\n".join(table_block))
            i = j
            continue
        if not stripped:
            result.append("")
            i += 1
            continue
        cleaned = re.sub(r"^\s*(?:\d+[\.\)]\s+|[-•]\s+)", "", stripped)
        result.append(cleaned)
        i += 1
    text = "\n".join(result)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _aggregate_entities(entities: list[dict]) -> list[dict]:
    merged: dict[tuple, dict] = {}
    for entity in entities:
        key = (
            str(entity.get("type", "")),
            str(entity.get("value", "")).strip().lower(),
        )
        if key in merged:
            existing = merged[key]
            evidence = str(entity.get("evidence", "") or existing.get("evidence", ""))
            if evidence and evidence not in existing.get("evidence", ""):
                if existing.get("evidence"):
                    existing["evidence"] = existing["evidence"] + " | " + evidence
                else:
                    existing["evidence"] = evidence
        else:
            merged[key] = dict(entity)
    return list(merged.values())


async def _process_file(
    file_path: Path,
    result_writer: ResultWriter,
    pbar: tqdm,
    artifacts_mode: bool,
) -> dict:
    relative = file_path.relative_to(settings.input_root)
    stem = file_path.stem if file_path.is_file() else file_path.name
    output_md = settings.output_root / f"{stem}.md"
    output_md_tmp = output_md.with_suffix(".md.tmp")
    file_type = file_path.suffix.lower().lstrip(".") if file_path.is_file() else "html_folder"

    timer = FileTimer(
        stats_writer=None,
        file_name=str(relative),
        file_type=file_type,
        size_bytes=file_path.stat().st_size if file_path.is_file() else 0,
        pages=None,
    )

    markdown = ""
    paragraphs: list[dict] = []
    all_entities: list[dict] = []
    status = "error"
    error = None

    try:
        if file_path.is_dir():
            pbar.set_postfix(file=file_path.name, stage="html_folder")
            markdown = convert_html_folder_to_markdown(file_path)
            if not markdown.strip():
                status = "skip"
                pbar.set_postfix(file=file_path.name, stage="skip")
                return {
                    "file": str(relative),
                    "type": file_type,
                    "size_bytes": timer.size_bytes,
                    "pages": None,
                    "duration_sec": round(time.perf_counter() - timer.start, 3),
                    "status": status,
                    "error": error,
                    "paragraphs": [],
                    "entities": [],
                }
            pbar.set_postfix(file=file_path.name, stage="vlm_entities")
            paragraphs = extract_paragraphs(markdown)
            llm_model = settings.vl_model
            all_entities = await extract_entities_from_text(markdown, model=llm_model)
            if artifacts_mode:
                output_md_tmp.write_text(markdown, encoding="utf-8")
                os.replace(output_md_tmp, output_md)
            status = "ok"
        elif file_path.suffix.lower() == ".pdf":
            pbar.set_postfix(file=file_path.name, stage="render")
            _, pngs, _ = extract_pdf_text_and_visual_pages(file_path)
            timer.pages = len(pngs)
            rendered_dir = settings.output_root / "rendered" / file_path.stem
            rendered_dir.mkdir(parents=True, exist_ok=True)
            for png in pngs:
                shutil.copy2(png, rendered_dir / png.name)
            pbar.set_postfix(file=file_path.name, stage="vlm_markdown")
            vlm_markdown = await reconstruct_markdown(pngs)
            pbar.set_postfix(file=file_path.name, stage="text_entities")
            llm_model = settings.vl_model
            all_entities = await extract_entities_from_text(vlm_markdown, model=llm_model)
            paragraphs = extract_paragraphs(vlm_markdown)
            if artifacts_mode:
                output_md_tmp.write_text(_postprocess_markdown(vlm_markdown), encoding="utf-8")
                os.replace(output_md_tmp, output_md)
            status = "ok"
        elif file_path.suffix.lower() == ".docx":
            pbar.set_postfix(file=file_path.name, stage="docx")
            markdown = convert_docx_to_markdown(file_path)
            pbar.set_postfix(file=file_path.name, stage="vlm_entities")
            paragraphs = extract_paragraphs(markdown)
            llm_model = settings.vl_model
            all_entities = await extract_entities_from_text(markdown, model=llm_model)
            if artifacts_mode:
                output_md_tmp.write_text(_postprocess_markdown(markdown), encoding="utf-8")
                os.replace(output_md_tmp, output_md)
            status = "ok"
        elif file_path.suffix.lower() == ".pptx":
            pbar.set_postfix(file=file_path.name, stage="pptx")
            markdown = convert_pptx_to_markdown(file_path)
            pbar.set_postfix(file=file_path.name, stage="vlm_entities")
            paragraphs = extract_paragraphs(markdown)
            llm_model = settings.vl_model
            all_entities = await extract_entities_from_text(markdown, model=llm_model)
            if artifacts_mode:
                output_md_tmp.write_text(_postprocess_markdown(markdown), encoding="utf-8")
                os.replace(output_md_tmp, output_md)
            status = "ok"
        elif file_path.suffix.lower() == ".html":
            pbar.set_postfix(file=file_path.name, stage="html")
            markdown = convert_html_to_markdown(file_path)
            pbar.set_postfix(file=file_path.name, stage="vlm_entities")
            paragraphs = extract_paragraphs(markdown)
            llm_model = settings.vl_model
            all_entities = await extract_entities_from_text(markdown, model=llm_model)
            if artifacts_mode:
                output_md_tmp.write_text(_postprocess_markdown(markdown), encoding="utf-8")
                os.replace(output_md_tmp, output_md)
            status = "ok"
        else:
            status = "skip"
            pbar.set_postfix(file=file_path.name, stage="skip")
            return {
                "file": str(relative),
                "type": file_type,
                "size_bytes": timer.size_bytes,
                "pages": None,
                "duration_sec": round(time.perf_counter() - timer.start, 3),
                "status": status,
                "error": error,
                "paragraphs": [],
                "entities": [],
            }
        pbar.set_postfix(file=file_path.name, stage="done")
    except Exception as exc:  # noqa: BLE001
        if output_md_tmp.exists():
            output_md_tmp.unlink()
        status = "error"
        error = str(exc)
        pbar.set_postfix(file=file_path.name, stage="error")

    duration = time.perf_counter() - timer.start
    stats = StatsWriter(settings.output_root / "stats.jsonl")
    stats.record(
        file=str(relative),
        type=file_type,
        size_bytes=timer.size_bytes,
        pages=timer.pages,
        duration_sec=round(duration, 3),
        status=status,
        error=error,
    )

    aggregated = _aggregate_entities(all_entities)
    result = {
        "file": str(relative),
        "type": file_type,
        "size_bytes": timer.size_bytes,
        "pages": timer.pages,
        "duration_sec": round(duration, 3),
        "status": status,
        "error": error,
        "paragraphs": paragraphs,
        "entities": aggregated,
    }
    result_writer.write(result)
    return result


async def _main() -> None:
    args = _parse_args()
    if args.artifacts:
        settings.artifacts_mode = True

    settings.output_root.mkdir(parents=True, exist_ok=True)
    files = _find_files(settings.input_root)
    result_writer = ResultWriter(
        settings.output_root / "results.jsonl",
        pretty_path=settings.output_root / "results_readable.json",
    )

    processed: set[str] = set()
    results_path = settings.output_root / "results.jsonl"
    if results_path.exists():
        with open(results_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    if record.get("status") in ("ok", "skip"):
                        processed.add(record["file"])
                except json.JSONDecodeError:
                    continue

    pbar = tqdm(files, desc="Files", unit="file")
    all_new_entities: list[dict] = []
    for file_path in pbar:
        if _already_processed(file_path, processed):
            pbar.set_postfix(file=file_path.name, stage="skip")
            continue
        result = await _process_file(file_path, result_writer, pbar, settings.artifacts_mode)
        all_new_entities.extend(result.get("entities", []))

    if all_new_entities:
        schema = _load_entity_schema()
        new_types = _get_new_entity_types(all_new_entities, schema)
        if new_types:
            update_entity_schema(new_types)


def _load_entity_schema() -> dict:
    from idp.vlm_client import _load_entity_schema as _schema

    return _schema()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
