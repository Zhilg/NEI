"""VL-only pipeline: reconstruct markdown and extract entities from PDF/DOCX/PPTX/HTML files."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from tqdm import tqdm

from idp.config import settings
from idp.document_classifier import detect_document_type
from idp.entity_schema import load_entity_schema, update_entity_schema
from idp.docx_converter import convert_docx_to_markdown
from idp.html_converter import convert_html_folder_to_markdown, convert_html_to_markdown
from idp.metadata_extractor import extract_metadata
from idp.pptx_converter import convert_pptx_to_markdown
from idp.renderer import extract_pdf_text_and_visual_pages
from idp.entity_store import EntityStore
from idp.reconstructor import reconstruct_bundle
from idp.result_writer import ResultWriter
from idp.stats_writer import FileTimer, StatsWriter
from idp.txt_export import TxtExporter
from idp.vlm_client import (
    clean_ocr_artifacts,
    strip_bbox_annotations,
    extract_entities_from_text,
    extract_paragraphs,
    generate_document_annotation,
    reconstruct_layout,
    reconstruct_markdown,
    extract_entities_from_images,
    close_vlm_client,
)

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".pptx", ".html", ".png", ".jpg", ".jpeg"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}


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
    def __init__(
        self,
        artifacts_mode: bool = False,
        force: bool = False,
        visio_mode: bool = False,
        visio_content: str = "",
        visio_layout: bool = False,
    ) -> None:
        self.artifacts_mode = artifacts_mode or settings.artifacts_mode
        self.force = force
        self.visio_mode = visio_mode or settings.visio_mode
        self.visio_content = (visio_content or "").strip().lower()
        self.visio_layout = visio_layout
        self._input_root: Path = settings.input_root
        self._processed: set[str] = set()
        self._stats_writer: StatsWriter | None = None

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

    def _load_processed(self) -> set[str]:
        processed: set[str] = set()
        if self.force:
            return processed
        results_path = settings.output_root / "results.jsonl"
        if not results_path.exists():
            return processed
        with open(results_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("status") in ("ok", "skip"):
                    processed.add(record["file"])
        return processed

    def _already_processed(self, file_path: Path) -> bool:
        relative = str(file_path.relative_to(self._input_root))
        return relative in self._processed

    def _artifact_paths(self, relative: Path) -> tuple[Path, Path]:
        output_md = settings.output_root / relative.parent / f"{relative.stem}.md"
        return output_md, output_md.with_suffix(".md.tmp")

    def _rendered_dir(self, relative: Path) -> Path:
        return settings.output_root / "rendered" / relative.parent / relative.stem

    def _get_new_entity_types(self, entity_types: set[str], schema: dict) -> list[dict]:
        existing_names = {t["name"] for t in schema.get("entity_types", []) if t.get("name")}
        return [
            {"name": name, "description": f"Auto-detected entity type: {name}"}
            for name in sorted(entity_types - existing_names)
            if name
        ]

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
            result.append(stripped)
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
            if all(re.fullmatch(r":?-+:?", cell) for cell in cells if cell):
                continue
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
    def _layout_to_markdown(layout: dict) -> str:
        lines: list[str] = []
        for block in layout.get("blocks", []) or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "heading":
                level = min(max(int(block.get("level", 1) or 1), 1), 6)
                lines.append(f"{chr(35) * level} {block.get('text', '')}")
            elif kind == "paragraph":
                lines.append(str(block.get("text", "")))
            elif kind == "list":
                for item in block.get("items", []) or []:
                    lines.append(f"- {item}")
            elif kind == "table":
                rows = block.get("rows", []) or []
                if rows:
                    lines.append(" | ".join(str(c) for c in rows[0]))
                    lines.append(" | ".join("---" for _ in rows[0]))
                    for row in rows[1:]:
                        lines.append(" | ".join(str(c) for c in row))
            elif kind == "image":
                caption = block.get("caption", "")
                if caption:
                    lines.append(f"[Image: {caption}]")
            lines.append("")
        return "\n".join(lines).strip()

    def _normalize_markdown(self, markdown: str) -> str:
        return self._normalize_typography(
            clean_ocr_artifacts(
                self._postprocess_markdown(markdown)
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
        relative = file_path.relative_to(self._input_root)
        output_md, output_md_tmp = self._artifact_paths(relative)
        file_type = file_path.suffix.lower().lstrip(".") if file_path.is_file() else "html_folder"

        timer = FileTimer(
            file_name=str(relative),
            file_type=file_type,
            size_bytes=file_path.stat().st_size if file_path.is_file() else 0,
            pages=None,
        )

        result: FileResult | None = None
        try:
            if file_path.is_dir():
                result = await self._process_html_folder(file_path, relative, output_md_tmp, output_md, timer)
            elif file_path.suffix.lower() == ".pdf":
                result = await self._process_pdf(file_path, relative, output_md_tmp, output_md, timer)
            elif file_path.suffix.lower() in (".docx", ".pptx", ".html"):
                result = await self._process_native(
                    file_path, relative, output_md_tmp, output_md, timer,
                    file_type=file_path.suffix.lower().lstrip("."),
                )
            elif file_path.suffix.lower() in IMAGE_EXTENSIONS:
                result = await self._process_image(
                    file_path, relative, output_md_tmp, output_md, timer
                )
            else:
                result = self._skip_result(file_path, relative, timer, file_type)
                pbar.set_postfix(file=file_path.name, stage="skip")
                return result.to_dict()
            await self._export_txt_bundle(file_path, relative, result, txt_exporter)
            pbar.set_postfix(file=file_path.name, stage="done")
            return result.to_dict()
        except Exception as exc:  # noqa: BLE001
            if output_md_tmp.exists():
                output_md_tmp.unlink()
            pbar.set_postfix(file=file_path.name, stage="error")
            logger.exception("Failed to process %s", relative)
            result = self._error_result(file_path, relative, timer, file_type, exc)
            return result.to_dict()
        finally:
            self._record_stats(timer, result, entity_store)

    async def _process_native(
        self,
        file_path: Path,
        relative: Path,
        output_md_tmp: Path,
        output_md: Path,
        timer: FileTimer,
        *,
        file_type: str,
    ) -> FileResult:
        converters = {
            "docx": convert_docx_to_markdown,
            "pptx": convert_pptx_to_markdown,
            "html": convert_html_to_markdown,
        }
        converter = converters[file_type]
        markdown = await asyncio.to_thread(converter, file_path)
        document_type = detect_document_type(file_path, markdown)
        paragraphs = extract_paragraphs(markdown)
        all_entities = await extract_entities_from_text(markdown, model=settings.vl_model, document_type=document_type)
        annotation = await self._safe_annotation(text=markdown)
        final_markdown = self._normalize_markdown(markdown)
        self._write_artifact(output_md_tmp, output_md, final_markdown)
        return self._ok_result(
            file_path, relative, timer, file_type, markdown, paragraphs,
            all_entities, annotation, document_type, text=final_markdown,
        )

    async def _process_html_folder(
        self,
        file_path: Path,
        relative: Path,
        output_md_tmp: Path,
        output_md: Path,
        timer: FileTimer,
    ) -> FileResult:
        markdown = await asyncio.to_thread(convert_html_folder_to_markdown, file_path)
        if not markdown.strip():
            return self._skip_result_from_data(file_path, relative, timer, "html_folder", markdown)
        document_type = detect_document_type(file_path, markdown)
        all_entities = await extract_entities_from_text(markdown, model=settings.vl_model, document_type=document_type)
        annotation = await self._safe_annotation(text=markdown)
        self._write_artifact(output_md_tmp, output_md, markdown)
        paragraphs = extract_paragraphs(markdown)
        return self._ok_result(
            file_path, relative, timer, "html_folder", markdown, paragraphs,
            all_entities, annotation, document_type, text=markdown,
        )

    async def _process_pdf(
        self,
        file_path: Path,
        relative: Path,
        output_md_tmp: Path,
        output_md: Path,
        timer: FileTimer,
    ) -> FileResult:
        _, pngs, _ = await asyncio.to_thread(
            extract_pdf_text_and_visual_pages, file_path, self._rendered_dir(relative)
        )
        timer.pages = len(pngs)
        vlm_markdown = await reconstruct_markdown(pngs)
        clean_markdown = strip_bbox_annotations(vlm_markdown)
        document_type = detect_document_type(file_path, clean_markdown)
        text_entities = await extract_entities_from_text(clean_markdown, model=settings.vl_model, document_type=document_type)
        image_entities = await extract_entities_from_images(pngs, document_type=document_type)
        all_entities = self._aggregate_entities(text_entities + image_entities)
        paragraphs = extract_paragraphs(clean_markdown)
        annotation = await self._safe_annotation(text=clean_markdown)
        final_markdown = self._normalize_markdown(clean_markdown)
        annotated_markdown = self._normalize_markdown(vlm_markdown)
        self._write_artifact(output_md_tmp, output_md, final_markdown)
        return self._ok_result(
            file_path, relative, timer, "pdf", annotated_markdown, paragraphs,
            all_entities, annotation, document_type, text=annotated_markdown,
        )

    async def _process_image(
        self,
        file_path: Path,
        relative: Path,
        output_md_tmp: Path,
        output_md: Path,
        timer: FileTimer,
    ) -> FileResult:
        """Reconstruct a scan (PNG/JPEG) with the VLM into markdown.

        The image is the only page of the document, so the reconstruction is
        the same as for a one page PDF: the VLM returns the text, the tables
        and the structure, and the Visio export turns them into editable
        shapes instead of a picture of the scan.
        """
        png = await asyncio.to_thread(self._prepare_image, file_path, relative)
        timer.pages = 1
        layout = None
        if self.visio_layout:
            layout = await reconstruct_layout([png])
            clean_markdown = self._layout_to_markdown(layout)
            layout_path = self._rendered_dir(relative).parent / "layout.json"
            try:
                layout_path.write_text(json.dumps(layout, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
        else:
            vlm_markdown = await reconstruct_markdown([png])
            clean_markdown = strip_bbox_annotations(vlm_markdown)
        document_type = detect_document_type(file_path, clean_markdown)
        text_entities = await extract_entities_from_text(
            clean_markdown, model=settings.vl_model, document_type=document_type
        )
        image_entities = await extract_entities_from_images([png], document_type=document_type)
        all_entities = self._aggregate_entities(text_entities + image_entities)
        paragraphs = extract_paragraphs(clean_markdown)
        annotation = await self._safe_annotation(text=clean_markdown)
        final_markdown = self._normalize_markdown(clean_markdown)
        self._write_artifact(output_md_tmp, output_md, final_markdown)
        result = self._ok_result(
            file_path, relative, timer, file_path.suffix.lower().lstrip("."),
            final_markdown, paragraphs, all_entities, annotation, document_type,
            text=final_markdown,
        )
        if layout is not None:
            result._layout = layout
        return result

    def _prepare_image(self, file_path: Path, relative: Path) -> Path:
        """Put the scan into the rendered pages as a PNG the VLM can read."""
        from idp.renderer import convert_image_to_png

        target_dir = self._rendered_dir(relative)
        target_dir.mkdir(parents=True, exist_ok=True)
        return convert_image_to_png(file_path, target_dir / "page_00001.png")

    def _write_artifact(self, tmp: Path, final: Path, content: str) -> None:
        if self.artifacts_mode:
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, final)

    async def _safe_annotation(self, text: str | None = None, images: list[Path] | None = None) -> str:
        try:
            return await generate_document_annotation(text=text, images=images)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Annotation generation failed: %s", exc)
            return ""

    def _ok_result(
        self,
        file_path: Path,
        relative: Path,
        timer: FileTimer,
        file_type: str,
        markdown: str,
        paragraphs: list[dict],
        entities: list[dict],
        annotation: str,
        document_type: str,
        text: str = "",
    ) -> FileResult:
        return FileResult(
            file=str(relative),
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

    async def _export_txt_bundle(
        self,
        file_path: Path,
        relative: Path,
        result: FileResult,
        txt_exporter: TxtExporter,
    ) -> None:
        if result.status != "ok":
            return
        try:
            layout = getattr(result, "_layout", None)
            bundle_dir = await asyncio.to_thread(
                txt_exporter.export,
                file_path,
                result.type,
                result.text,
                relative=relative,
                pages=result.pages,
                document_type=result.document_type,
                annotation=result.annotation,
                metadata=result.metadata,
                layout=layout,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Txt export failed for %s: %s", file_path.name, exc)
            return
        formats = self._reconstruct_formats()
        if formats:
            try:
                await asyncio.to_thread(
                    reconstruct_bundle, bundle_dir, formats, self._visio_content(file_path)
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Document reconstruction failed for %s: %s", file_path.name, exc)

    def _reconstruct_formats(self) -> list[str]:
        """Formats to rebuild from the bundle; ``--visio`` adds the Visio drawing."""
        formats = list(settings.reconstruct_formats)
        if self.visio_mode and "vsdx" not in formats:
            formats.append("vsdx")
        return formats

    def _visio_content(self, file_path: Path) -> str:
        """Content of the Visio drawing of a file.

        The flag wins, then ``IDP_VISIO_CONTENT``. A scan (PNG/JPEG) is
        rebuilt as an editable document by default: the picture of the scan
        would be just the picture again.
        """
        if self.visio_content:
            return self.visio_content
        configured = (settings.visio_content or "pages").lower()
        if configured == "pages" and file_path.suffix.lower() in IMAGE_EXTENSIONS:
            return "document"
        return configured

    def _skip_result(self, file_path: Path, relative: Path, timer: FileTimer, file_type: str) -> FileResult:
        return self._skip_result_from_data(file_path, relative, timer, file_type, "")

    def _skip_result_from_data(self, file_path: Path, relative: Path, timer: FileTimer, file_type: str, markdown: str) -> FileResult:
        return FileResult(
            file=str(relative),
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

    def _error_result(self, file_path: Path, relative: Path, timer: FileTimer, file_type: str, exc: Exception) -> FileResult:
        return FileResult(
            file=str(relative),
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

    def _record_stats(self, timer: FileTimer, result: FileResult | None, entity_store: EntityStore | None = None) -> None:
        duration = time.perf_counter() - timer.start
        stats = self._stats_writer or StatsWriter(settings.output_root / "stats.jsonl")
        stats.record(
            file=timer.file_name,
            type=timer.file_type,
            size_bytes=timer.size_bytes,
            pages=timer.pages,
            duration_sec=round(duration, 3),
            status=result.status if result else "error",
            error=result.error if result else "unknown",
        )
        if result is not None and result.status == "ok" and entity_store is not None:
            aggregated = self._aggregate_entities(result.entities)
            result.entities = aggregated
            entity_store.append(result.file, result.paragraphs, aggregated, result.annotation)

    async def run(self, input_root: Path | None = None) -> None:
        root = input_root or settings.input_root
        self._input_root = root
        settings.output_root.mkdir(parents=True, exist_ok=True)
        files = self._find_files(root)
        result_writer = ResultWriter(
            settings.output_root / "results.jsonl",
            pretty_path=settings.output_root / "results_readable.json",
        )
        entity_store = EntityStore(settings.output_root / "entities.json")
        txt_exporter = TxtExporter()
        self._stats_writer = StatsWriter(settings.output_root / "stats.jsonl")
        self._processed = self._load_processed()

        pending: list[Path] = []
        for file_path in files:
            if self._already_processed(file_path):
                continue
            pending.append(file_path)

        pbar = tqdm(total=len(pending), desc="Files", unit="file")
        file_semaphore = asyncio.Semaphore(settings.vl_concurrency)
        new_entity_types: set[str] = set()

        async def _process_with_semaphore(file_path: Path) -> dict:
            async with file_semaphore:
                return await self._process_file(file_path, result_writer, entity_store, pbar, txt_exporter)

        tasks = [_process_with_semaphore(file_path) for file_path in pending]
        try:
            for coro in asyncio.as_completed(tasks):
                result = await coro
                pbar.update(1)
                result_writer.write(result)
                for entity in result.get("entities", []):
                    if isinstance(entity, dict) and entity.get("type"):
                        new_entity_types.add(str(entity["type"]))
        finally:
            pbar.close()
            result_writer.finalize()
            entity_store.flush()

        if new_entity_types:
            new_types = self._get_new_entity_types(new_entity_types, load_entity_schema())
            if new_types:
                update_entity_schema(new_types)
                logger.info("Added %d new entity type(s) to %s", len(new_types), settings.entity_schema_path or settings.output_root / "entity_schema.json")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="IDP pipeline: extract entities from documents")
    parser.add_argument("--artifacts", action="store_true", help="Save markdown artifacts to output directory")
    parser.add_argument("--force", action="store_true", help="Ignore processed cache and reprocess all files")
    parser.add_argument("--visio", action="store_true",
                        help="Also build a Visio drawing (.vsdx) from the rendered pages")
    parser.add_argument("--visio-content", choices=("pages", "sheet", "document"), default="",
                        help="Content of the Visio drawing: page renders (default), "
                             "all renders on one sheet or the editable document")
    parser.add_argument("--visio-layout", action="store_true",
                        help="Have the VLM return a structured JSON layout instead of Markdown")
    return parser.parse_args()


async def _main() -> None:
    args = _parse_args()
    level = getattr(logging, os.getenv("IDP_LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    worker = Worker(
        artifacts_mode=args.artifacts,
        force=args.force,
        visio_mode=args.visio,
        visio_content=args.visio_content,
    )
    await worker.run()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
