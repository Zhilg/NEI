"""Terminal operator tool: review VLM output for handwritten pages and record corrections.

Workflow per page image:
  1. VLM reconstructs Markdown and extracts entities (test-mode prompt: confidence + comments).
  2. The draft is written to a JSON file next to the corrections store.
  3. The operator reviews it in the terminal and/or edits the JSON file.
  4. The reviewed result is appended to operator_corrections.jsonl.

Corrections are later (a) injected as few-shot feedback into VLM prompts and
(b) converted into a fine-tuning dataset by idp.finetune_dataset.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from idp.config import settings
from idp.feedback_store import append_correction, corrections_path, read_corrections
from idp.renderer import render_pdf_to_pngs
from idp.vlm_client import extract_entities_from_images, extract_entities_from_text, reconstruct_markdown
from idp.worker import _aggregate_entities

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
PDF_EXTENSIONS = {".pdf"}

_LOW_CONFIDENCE_LIMIT = 0.5


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Operator review loop for VLM handwriting extraction",
    )
    parser.add_argument("--input", required=True, help="PDF, image, or directory of pages to review")
    parser.add_argument("--document-type", default="other", help="Document type hint passed to the VLM prompt")
    parser.add_argument("--limit", type=int, default=0, help="Max pages to process (0 = all)")
    parser.add_argument("--drafts-dir", default="", help="Where to write editable draft JSON files")
    parser.add_argument("--open-image", action="store_true", help="Open each page image in the default viewer")
    parser.add_argument("--production-prompts", action="store_true", help="Use production prompts instead of test prompts")
    return parser.parse_args()


def _collect_images(input_path: Path, drafts_dir: Path) -> list[Path]:
    if input_path.is_file():
        if input_path.suffix.lower() in PDF_EXTENSIONS:
            return render_pdf_to_pngs(input_path, drafts_dir / "rendered" / input_path.stem)
        return [input_path]
    if not input_path.is_dir():
        raise FileNotFoundError(f"input not found: {input_path}")
    images = sorted(p for p in input_path.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS)
    if not images:
        pdfs = sorted(p for p in input_path.rglob("*") if p.is_file() and p.suffix.lower() in PDF_EXTENSIONS)
        for pdf in pdfs:
            images.extend(render_pdf_to_pngs(pdf, drafts_dir / "rendered" / pdf.stem))
    return images


def _open_image(path: Path) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except Exception as exc:  # noqa: BLE001
        print(f"Could not open image viewer: {exc}", file=sys.stderr)


def _confidence_marker(confidence: float) -> str:
    if confidence < 0.3:
        return "!!"
    if confidence < 0.5:
        return "! "
    if confidence < 0.7:
        return "~ "
    return "  "


def _render_console(image_path: Path, markdown: str, entities: list[dict], draft_path: Path) -> None:
    line = "=" * 78
    print(f"\n{line}")
    print(f"IMAGE : {image_path}")
    print(f"DRAFT : {draft_path}")
    print(line)
    print("\n--- Markdown ---")
    print(markdown if markdown.strip() else "(empty)")
    print("\n--- Entities (marker: !! <0.3, ! <0.5, ~ <0.7) ---")
    if not entities:
        print("(no entities)")
        return
    for i, entity in enumerate(entities, start=1):
        try:
            confidence = float(entity.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        marker = _confidence_marker(confidence)
        handwritten = "H" if entity.get("handwritten") else " "
        etype = str(entity.get("type", "other"))
        value = str(entity.get("value", ""))
        evidence = str(entity.get("evidence", ""))
        comment = str(entity.get("comment", "") or "")
        flag = " <-- REVIEW" if confidence < _LOW_CONFIDENCE_LIMIT else ""
        print(f"{marker}[{confidence:>5.2f}] {handwritten} {i:>2}. {etype}: {value}{flag}")
        if evidence:
            print(f"          evidence: {evidence}")
        if comment:
            print(f"          comment : {comment}")


def _write_draft(draft_path: Path, image_path: Path, markdown: str, entities: list[dict]) -> None:
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "page_image": str(image_path),
        "markdown": markdown,
        "vlm_entities": entities,
        "operator_corrected_entities": entities,
        "timestamp": time.time(),
    }
    draft_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_draft(draft_path: Path) -> tuple[str, list[dict]]:
    data = json.loads(draft_path.read_text(encoding="utf-8"))
    markdown = str(data.get("markdown", ""))
    entities = data.get("operator_corrected_entities")
    if not isinstance(entities, list):
        entities = data.get("vlm_entities") or []
    return markdown, [e for e in entities if isinstance(e, dict)]


async def _run_vlm(image_path: Path, document_type: str) -> tuple[str, list[dict]]:
    return await extract_markdown_and_entities(
        [image_path],
        document_type=document_type,
        max_tokens=settings.vl_max_tokens,
    )


def _already_reviewed(image_path: Path, reviewed: set[str]) -> bool:
    return str(image_path) in reviewed


async def _review_page(
    image_path: Path,
    document_type: str,
    drafts_dir: Path,
    open_image: bool,
) -> bool:
    draft_path = drafts_dir / f"{image_path.stem}.draft.json"
    markdown, entities = await _run_vlm(image_path, document_type)
    _write_draft(draft_path, image_path, markdown, entities)
    if open_image:
        _open_image(image_path)
    _render_console(image_path, markdown, entities, draft_path)

    while True:
        print("\n[a] accept   [e] edit draft JSON   [r] re-read draft   [s] skip   [q] quit")
        choice = input("> ").strip().lower() or "a"
        if choice in {"q", "quit", "exit"}:
            return False
        if choice in {"s", "skip"}:
            print("Skipped (no correction saved).")
            return True
        if choice in {"e", "edit"}:
            print(f"Edit '{draft_path}' (fields: markdown, operator_corrected_entities), then press Enter.")
            input()
            choice = "r"
        if choice in {"r", "reread"}:
            try:
                markdown, entities = _read_draft(draft_path)
            except (OSError, json.JSONDecodeError) as exc:
                print(f"Cannot read draft: {exc}", file=sys.stderr)
                continue
            _render_console(image_path, markdown, entities, draft_path)
            continue
        if choice in {"a", "accept"}:
            try:
                saved_markdown, corrected = _read_draft(draft_path)
            except (OSError, json.JSONDecodeError) as exc:
                print(f"Cannot read draft: {exc}", file=sys.stderr)
                continue
            md_changed = bool(saved_markdown.strip()) and saved_markdown != markdown
            ent_changed = corrected != entities
            print(f"ACCEPT: md_changed={md_changed}, ent_changed={ent_changed}", file=sys.stderr)
            if md_changed and not ent_changed:
                try:
                    text_entities = await extract_entities_from_text(saved_markdown, document_type=document_type)
                    image_entities = await extract_entities_from_images([image_path], document_type=document_type)
                    corrected = _aggregate_entities(text_entities + image_entities)
                    print(f"Re-extracted {len(corrected)} entities from corrected markdown", file=sys.stderr)
                except Exception as exc:
                    print(f"Entity re-extraction failed: {exc}", file=sys.stderr)
            append_correction(
                page_image=str(image_path),
                markdown=saved_markdown,
                vlm_entities=entities,
                operator_corrected_entities=corrected,
                timestamp=time.time(),
            )
            print(f"Saved correction -> {corrections_path()}", file=sys.stderr)
            return True


async def _main() -> None:
    args = _parse_args()
    settings.test_mode = not args.production_prompts

    input_path = Path(args.input)
    drafts_dir = Path(args.drafts_dir) if args.drafts_dir else corrections_path().parent / "operator_drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)

    images = _collect_images(input_path, drafts_dir)
    if args.limit > 0:
        images = images[: args.limit]
    if not images:
        print(f"No page images found under {input_path}", file=sys.stderr)
        return

    document_type = args.document_type

    print(f"Pages to review: {len(images)} (document_type={document_type}, test_mode={settings.test_mode})")

    reviewed = {str(record.get("page_image", "")) for record in read_corrections()}
    pending = [img for img in images if not _already_reviewed(img, reviewed)]
    print(f"Pending pages: {len(pending)}")

    if pending:
        print("Processing all pages with VLM first...")
        for index, image_path in enumerate(pending, start=1):
            draft_path = drafts_dir / f"{image_path.stem}.draft.json"
            if draft_path.exists():
                print(f"  [{index}/{len(pending)}] draft exists: {image_path.name}")
                continue
            print(f"  [{index}/{len(pending)}] processing: {image_path.name}")
            try:
                markdown, entities = await _run_vlm(image_path, document_type)
                _write_draft(draft_path, image_path, markdown, entities)
            except Exception as exc:
                print(f"  Failed: {exc}", file=sys.stderr)
                continue

    for index, image_path in enumerate(images, start=1):
        print(f"\n[{index}/{len(images)}] {image_path}")
        if _already_reviewed(image_path, reviewed):
            print("Already reviewed — skipping.")
            continue
        try:
            if not await _review_page(image_path, document_type, drafts_dir, args.open_image):
                print("Stopped by operator.")
                return
        except KeyboardInterrupt:
            print("\nInterrupted by operator.")
            return
        except Exception as exc:  # noqa: BLE001
            print(f"Page failed: {exc}", file=sys.stderr)
            continue


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()