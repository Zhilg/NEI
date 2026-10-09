"""VL-only pipeline: reconstruct markdown and extract entities from PDF/DOCX/PPTX/HTML files."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from tqdm import tqdm

from idp.config import settings
from idp.document_classifier import detect_document_type
from idp.docx_converter import convert_docx_to_markdown
from idp.html_converter import convert_html_folder_to_markdown, convert_html_to_markdown
from idp.metadata_extractor import extract_metadata
from idp.pptx_converter import convert_pptx_to_markdown
from idp.renderer import extract_pdf_text_and_visual_pages
from idp.entity_store import EntityStore
from idp.result_writer import ResultWriter
from idp.stats_writer import StatsWriter, FileTimer
from idp.txt_export import TxtExporter
from idp.vlm_client import (
    _clean_ocr_artifacts,
    extract_entities_from_text,
    extract_paragraphs,
    generate_document_annotation,
    reconstruct_markdown,
    update_entity_schema,
    extract_entities_from_images,
)

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".pptx", ".html"}


@dataclass
class FileResult:
    file: str
    type: str
    size_bytes: int
    pages: int | None
    duration_sec: float
    status: str
    error: str | None
    paragraphs: list[dict]
    entities: list[dict]
    annotation: str
    document_type: str
    metadata: dict
    text: str = ""

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "type": self.type,
            "size_bytes": self.size_bytes,
            "pages": self.pages,
            "duration_sec": self.duration_sec,
            "status": self.status,
            "error": self.error,
            "paragraphs": self.paragraphs,
            "entities": self.entities,
            "annotation": self.annotation,
            "document_type": self.document_type,
            "metadata": self.metadata,
        }


class Worker:
    def __init__(self, artifacts_mode: bool = False, force: bool = False) -> None:
        self.artifacts_mode = artifacts_mode
        self.force = force

    def _find_files(self, input_root: Path) -> list[Path]:
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

    def _already_processed(self, file_path: Path, processed: set[str]) -> bool:
        relative = str(file_path.relative_to(settings.input_root))
        return relative in processed

    def _get_new_entity_types(self, entities: list[dict], schema: dict) -> list[dict]:
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

    def _postprocess_markdown(self, markdown: str) -> str:
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
                normalized_table = self._normalize_table(table_block)
                result.append("\n".join(normalized_table))
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

    @staticmethod
    def _normalize_table(table_lines: list[str]) -> list[str]:
        if len(table_lines) < 2:
            return table_lines
        parsed_rows: list[list[str]] = []
        for line in table_lines:
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            parsed_rows.append(cells)
        if not parsed_rows:
            return table_lines
        max_cols = max(len(row) for row in parsed_rows)
        normalized: list[str] = []
        for row_idx, row in enumerate(parsed_rows):
            while len(row) < max_cols:
                row.append("")
            normalized_row = "| " + " | ".join(row) + " |"
            normalized.append(normalized_row)
            if row_idx == 0:
                separator = "| " + " | ".join(["---"] * max_cols) + " |"
                normalized.append(separator)
        return normalized

    @staticmethod
    def _normalize_typography(text: str) -> str:
        text = text.replace("\u00A0", " ")
        text = text.replace("\u200b", "")
        text = text.replace("\u200c", "")
        text = text.replace("\u200d", "")
        text = text.replace("«", '"').replace("»", '"')
        text = text.replace("„", '"').replace("“", '"').replace("”", '"')
        text = text.replace("‘", "'").replace("’", "'")
        text = text.replace("–", "-")
        text = text.replace("—", "-")
        text = text.replace("…", "...")
        return text

    @staticmethod
    def _normalize_entities_and_formulas(text: str) -> str:
        text = re.sub(r"от\s+(\d+(?:\.\d+)?)\s*до\s+(\d+(?:\.\d+)?)", lambda m: f" RANGE_NUM {m.group(1)}-{m.group(2)} ", text)
        text = re.sub(r"[±\+]\s*(\d+(?:\.\d+)?)", lambda m: f" TOLERANCE_NUM {m.group(1)} ", text)
        text = re.sub(r"\b\d+(?:\.\d+)?\s*(?:мм|см|м|км|кг|г|с|мс|Гц|кГц|МГц|В|А|Вт|кВт)\b", " NUM UNIT ", text)
        text = re.sub(r"\$\$[^$]+\$\$", " FORMULA ", text)
        text = re.sub(r"\$[^$]+\$", " FORMULA ", text)
        return text

    def _normalize_markdown(self, markdown: str) -> str:
        return self._normalize_typography(
            self._normalize_entities_and_formulas(
                _clean_ocr_artifacts(
                    self._postprocess_markdown(markdown)
                )
            )
        )

    def _aggregate_entities(self, entities: list[dict]) -> list[dict]:
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
        self,
        file_path: Path,
        result_writer: ResultWriter,
        entity_store: EntityStore,
        pbar: tqdm,
        txt_exporter: TxtExporter,
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

        try:
            if file_path.is_dir():
                result = await self._process_html_folder(file_path, output_md_tmp, output_md, timer)
            elif file_path.suffix.lower() == ".pdf":
                result = await self._process_pdf(file_path, output_md_tmp, output_md, timer)
            elif file_path.suffix.lower() == ".docx":
                result = await self._process_docx(file_path, output_md_tmp, output_md, timer)
            elif file_path.suffix.lower() == ".pptx":
                result = await self._process_pptx(file_path, output_md_tmp, output_md, timer)
            elif file_path.suffix.lower() == ".html":
                result = await self._process_html(file_path, output_md_tmp, output_md, timer)
            else:
                result = self._skip_result(file_path, timer, file_type)
                pbar.set_postfix(file=file_path.name, stage="skip")
                return result.to_dict()
            self._export_txt_bundle(file_path, result, txt_exporter)
            pbar.set_postfix(file=file_path.name, stage="done")
            return result.to_dict()
        except Exception as exc:  # noqa: BLE001
            if output_md_tmp.exists():
                output_md_tmp.unlink()
            pbar.set_postfix(file=file_path.name, stage="error")
            result = self._error_result(file_path, timer, file_type, exc)
            return result.to_dict()
        finally:
            self._record_stats(file_path, timer, result if 'result' in dir() else None)

    async def _process_html_folder(self, file_path: Path, output_md_tmp: Path, output_md: Path, timer: FileTimer) -> FileResult:
        markdown = convert_html_folder_to_markdown(file_path)
        if not markdown.strip():
            return self._skip_result_from_data(file_path, timer, "html_folder", markdown)
        document_type = detect_document_type(file_path, markdown)
        all_entities = await extract_entities_from_text(markdown, model=settings.vl_model, document_type=document_type)
        annotation = await self._safe_annotation(text=markdown)
        self._write_artifact(output_md_tmp, output_md, markdown)
        paragraphs = extract_paragraphs(markdown)
        return self._ok_result(file_path, timer, "html_folder", markdown, paragraphs, all_entities, annotation, document_type, text=markdown)

    async def _process_pdf(self, file_path: Path, output_md_tmp: Path, output_md: Path, timer: FileTimer) -> FileResult:
        _, pngs, _ = extract_pdf_text_and_visual_pages(file_path)
        timer.pages = len(pngs)
        rendered_dir = settings.output_root / "rendered" / file_path.stem
        rendered_dir.mkdir(parents=True, exist_ok=True)
        for png in pngs:
            shutil.copy2(png, rendered_dir / png.name)
        vlm_markdown = await reconstruct_markdown(pngs)
        document_type = detect_document_type(file_path, vlm_markdown)
        text_entities = await extract_entities_from_text(vlm_markdown, model=settings.vl_model, document_type=document_type)
        image_entities = await extract_entities_from_images(pngs, document_type=document_type)
        all_entities = self._aggregate_entities(text_entities + image_entities)
        paragraphs = extract_paragraphs(vlm_markdown)
        annotation = await self._safe_annotation(text=vlm_markdown)
        final_markdown = self._normalize_markdown(vlm_markdown)
        self._write_artifact(output_md_tmp, output_md, final_markdown)
        return self._ok_result(file_path, timer, "pdf", vlm_markdown, paragraphs, all_entities, annotation, document_type, text=final_markdown)

    async def _process_docx(self, file_path: Path, output_md_tmp: Path, output_md: Path, timer: FileTimer) -> FileResult:
        markdown = convert_docx_to_markdown(file_path)
        document_type = detect_document_type(file_path, markdown)
        paragraphs = extract_paragraphs(markdown)
        all_entities = await extract_entities_from_text(markdown, model=settings.vl_model, document_type=document_type)
        annotation = await self._safe_annotation(text=markdown)
        final_markdown = self._normalize_markdown(markdown)
        self._write_artifact(output_md_tmp, output_md, final_markdown)
        return self._ok_result(file_path, timer, "docx", markdown, paragraphs, all_entities, annotation, document_type, text=final_markdown)

    async def _process_pptx(self, file_path: Path, output_md_tmp: Path, output_md: Path, timer: FileTimer) -> FileResult:
        markdown = convert_pptx_to_markdown(file_path)
        document_type = detect_document_type(file_path, markdown)
        paragraphs = extract_paragraphs(markdown)
        all_entities = await extract_entities_from_text(markdown, model=settings.vl_model, document_type=document_type)
        annotation = await self._safe_annotation(text=markdown)
        final_markdown = self._normalize_markdown(markdown)
        self._write_artifact(output_md_tmp, output_md, final_markdown)
        return self._ok_result(file_path, timer, "pptx", markdown, paragraphs, all_entities, annotation, document_type, text=final_markdown)

    async def _process_html(self, file_path: Path, output_md_tmp: Path, output_md: Path, timer: FileTimer) -> FileResult:
        markdown = convert_html_to_markdown(file_path)
        document_type = detect_document_type(file_path, markdown)
        paragraphs = extract_paragraphs(markdown)
        all_entities = await extract_entities_from_text(markdown, model=settings.vl_model, document_type=document_type)
        annotation = await self._safe_annotation(text=markdown)
        final_markdown = self._normalize_markdown(markdown)
        self._write_artifact(output_md_tmp, output_md, final_markdown)
        return self._ok_result(file_path, timer, "html", markdown, paragraphs, all_entities, annotation, document_type, text=final_markdown)

    def _write_artifact(self, tmp: Path, final: Path, content: str) -> None:
        if self.artifacts_mode:
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, final)

    async def _safe_annotation(self, text: str | None = None, images: list[Path] | None = None) -> str:
        try:
            return await generate_document_annotation(text=text, images=images)
        except Exception as exc:  # noqa: BLE001
            print(f"Annotation generation failed: {exc}", file=sys.stderr)
            return ""

    def _ok_result(self, file_path: Path, timer: FileTimer, file_type: str, markdown: str, paragraphs: list[dict], entities: list[dict], annotation: str, document_type: str, text: str = "") -> FileResult:
        return FileResult(
            file=str(file_path.relative_to(settings.input_root)),
            type=file_type,
            size_bytes=timer.size_bytes,
            pages=timer.pages,
            duration_sec=round(time.perf_counter() - timer.start, 3),
            status="ok",
            error=None,
            paragraphs=paragraphs,
            entities=entities,
            annotation=annotation,
            document_type=document_type,
            metadata=extract_metadata(file_path) if file_path.is_file() else {},
            text=text,
        )

    def _export_txt_bundle(self, file_path: Path, result: FileResult, txt_exporter: TxtExporter) -> None:
        try:
            txt_exporter.export(
                file_path,
                file_type=result.type,
                text=result.text,
                pages=result.pages,
                document_type=result.document_type,
                annotation=result.annotation,
                metadata=result.metadata,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"Txt export failed for {file_path.name}: {exc}", file=sys.stderr)

    def _skip_result(self, file_path: Path, timer: FileTimer, file_type: str) -> FileResult:
        return self._skip_result_from_data(file_path, timer, file_type, "")

    def _skip_result_from_data(self, file_path: Path, timer: FileTimer, file_type: str, markdown: str) -> FileResult:
        return FileResult(
            file=str(file_path.relative_to(settings.input_root)),
            type=file_type,
            size_bytes=timer.size_bytes,
            pages=timer.pages,
            duration_sec=round(time.perf_counter() - timer.start, 3),
            status="skip",
            error=None,
            paragraphs=[],
            entities=[],
            annotation="",
            document_type="other",
            metadata={},
        )

    def _error_result(self, file_path: Path, timer: FileTimer, file_type: str, exc: Exception) -> FileResult:
        return FileResult(
            file=str(file_path.relative_to(settings.input_root)),
            type=file_type,
            size_bytes=timer.size_bytes,
            pages=timer.pages,
            duration_sec=round(time.perf_counter() - timer.start, 3),
            status="error",
            error=str(exc),
            paragraphs=[],
            entities=[],
            annotation="",
            document_type="other",
            metadata={},
        )

    def _record_stats(self, file_path: Path, timer: FileTimer, result: FileResult | None) -> None:
        duration = time.perf_counter() - timer.start
        stats = StatsWriter(settings.output_root / "stats.jsonl")
        stats.record(
            file=str(file_path.relative_to(settings.input_root)),
            type=timer.file_type,
            size_bytes=timer.size_bytes,
            pages=timer.pages,
            duration_sec=round(duration, 3),
            status=result.status if result else "error",
            error=result.error if result else "unknown",
        )
        if result:
            aggregated = self._aggregate_entities(result.entities)
            result.entities = aggregated
            from idp.entity_store import EntityStore
            entity_store = EntityStore(settings.output_root / "entities.json")
            entity_store.append(result.file, result.paragraphs, aggregated, result.annotation)

    def _load_entity_schema(self) -> dict:
        from idp.vlm_client import _load_entity_schema as _schema
        return _schema()

    async def run(self, input_root: Path | None = None) -> None:
        root = input_root or settings.input_root
        settings.output_root.mkdir(parents=True, exist_ok=True)
        files = self._find_files(root)
        result_writer = ResultWriter(
            settings.output_root / "results.jsonl",
            pretty_path=settings.output_root / "results_readable.json",
        )
        entity_store = EntityStore(settings.output_root / "entities.json")
        txt_exporter = TxtExporter()

        processed: set[str] = set()
        if not self.force:
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
        file_semaphore = asyncio.Semaphore(settings.vl_concurrency)

        async def _process_with_semaphore(file_path: Path) -> dict:
            async with file_semaphore:
                return await self._process_file(file_path, result_writer, entity_store, pbar, txt_exporter)

        pending = []
        for file_path in pbar:
            if self._already_processed(file_path, processed):
                pbar.set_postfix(file=file_path.name, stage="skip")
                continue
            pending.append(_process_with_semaphore(file_path))

        results = []
        if pending:
            for coro in asyncio.as_completed(pending):
                result = await coro
                results.append(result)
                all_new_entities.extend(result.get("entities", []))

        if all_new_entities:
            schema = self._load_entity_schema()
            new_types = self._get_new_entity_types(all_new_entities, schema)
            if new_types:
                update_entity_schema(new_types)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="IDP pipeline: extract entities from documents")
    parser.add_argument("--artifacts", action="store_true", help="Save markdown artifacts to output directory")
    parser.add_argument("--force", action="store_true", help="Ignore processed cache and reprocess all files")
    return parser.parse_args()


async def _main() -> None:
    args = _parse_args()
    worker = Worker(artifacts_mode=args.artifacts, force=args.force)
    await worker.run()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
