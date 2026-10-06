"""Terminal operator tool: review VLM output for handwritten pages and correct Markdown."""

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
from idp.vlm_client import reconstruct_markdown

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
PDF_EXTENSIONS = {".pdf"}

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


_CANDIDATE_EDITORS = ("nano", "vim", "vi", "micro", "emacs")


def _launch_editor(path: Path) -> None:
    configured = os.environ.get("IDP_EDITOR") or os.environ.get("EDITOR") or os.environ.get("VISUAL")
    candidates = [configured] if configured else []
    candidates.extend(candidate for candidate in _CANDIDATE_EDITORS if candidate not in candidates)

    for editor in candidates:
        if not editor:
            continue
        try:
            subprocess.run([editor, str(path)], check=False)
            return
        except FileNotFoundError:
            continue

    print(f"No terminal editor found. Edit file manually: {path}", file=sys.stderr)



def _render_console(image_path: Path, markdown: str, draft_path: Path) -> None:
    line = "=" * 78
    print(f"\n{line}")
    print(f"IMAGE : {image_path}")
    print(f"DRAFT : {draft_path}")
    print(line)
    print("\n--- Markdown ---")
    md = markdown.strip() if markdown else ""
    if not md:
        print("(empty)")
    else:
        lines = md.splitlines()
        for idx, ln in enumerate(lines[:50], start=1):
            print(f"  {idx:>2}: {ln}")
        if len(lines) > 50:
            print(f"  ... ({len(lines) - 50} more lines)")


def _write_draft(draft_path: Path, image_path: Path, markdown: str) -> None:
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    normalized = markdown.replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace("\\n", "\n")
    draft_path.write_text(normalized, encoding="utf-8")


def _read_draft(draft_path: Path) -> str:
    return draft_path.read_text(encoding="utf-8")



async def _run_vlm(image_path: Path, document_type: str) -> str:
    return await reconstruct_markdown([image_path])


def _already_reviewed(image_path: Path, reviewed: set[str], drafts_dir: Path | None = None) -> bool:
    if str(image_path) in reviewed:
        return True
    if drafts_dir:
        draft_path = drafts_dir / f"{image_path.stem}.md"
        return draft_path.exists()
    return False


_CANDIDATE_EDITORS = ("nano", "vim", "vi", "micro", "emacs")


def _launch_editor(path: Path) -> None:
    configured = os.environ.get("IDP_EDITOR") or os.environ.get("EDITOR") or os.environ.get("VISUAL")
    candidates = [configured] if configured else []
    candidates.extend(candidate for candidate in _CANDIDATE_EDITORS if candidate not in candidates)

    last_error = None
    for editor in candidates:
        if not editor:
            continue
        try:
            subprocess.run([editor, str(path)], check=False)
            return
        except FileNotFoundError as exc:
            last_error = exc
            continue

    print(
        f"No terminal editor found. Edit file manually, then press Enter: {path}",
        file=sys.stderr,
    )
    input("Press Enter after editing...")


async def _review_page(
    image_path: Path,
    document_type: str,
    drafts_dir: Path,
    open_image: bool,
) -> bool:
    draft_path = drafts_dir / f"{image_path.stem}.md"
    original_markdown = await _run_vlm(image_path, document_type)
    _write_draft(draft_path, image_path, original_markdown)
    if open_image:
        _open_image(image_path)
    _render_console(image_path, original_markdown, draft_path)

    markdown = original_markdown


    while True:
        print("\n[a] accept   [e] edit draft   [r] re-read draft   [s] skip   [q] quit")
        choice = input("> ").strip().lower() or "a"
        if choice in {"q", "quit", "exit"}:
            return False
        if choice in {"s", "skip"}:
            print("Skipped (no correction saved).")
            return True
        if choice in {"e", "edit"}:
            _launch_editor(draft_path)
            try:
                markdown = _read_draft(draft_path)
            except (OSError, json.JSONDecodeError) as exc:
                print(f"Cannot read draft after edit: {exc}", file=sys.stderr)
                continue
            _render_console(image_path, markdown, draft_path)
            continue

        if choice in {"r", "reread"}:
            try:
                markdown = _read_draft(draft_path)
            except (OSError, json.JSONDecodeError) as exc:
                print(f"Cannot read draft: {exc}", file=sys.stderr)
                continue
            _render_console(image_path, markdown, draft_path)
            continue
        if choice in {"a", "accept"}:
            try:
                saved_markdown = _read_draft(draft_path)
            except (OSError, json.JSONDecodeError) as exc:
                print(f"Cannot read draft: {exc}", file=sys.stderr)
                continue
            md_changed = bool(saved_markdown.strip()) and saved_markdown.strip() != original_markdown.strip()
            ent_changed = bool(corrected) and corrected != original_entities
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
                vlm_entities=[],
                operator_corrected_entities=[],

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
            draft_path = drafts_dir / f"{image_path.stem}.md"
            if draft_path.exists():
                print(f"  [{index}/{len(pending)}] draft exists: {image_path.name}")
                continue
            print(f"  [{index}/{len(pending)}] processing: {image_path.name}", file=sys.stderr, flush=True)
            try:
                markdown = await _run_vlm(image_path, document_type)
                _write_draft(draft_path, image_path, markdown)

            except Exception as exc:
                print(f"  Failed: {exc}", file=sys.stderr, flush=True)
                continue

    for index, image_path in enumerate(images, start=1):
        print(f"\n[{index}/{len(images)}] {image_path}")
        if _already_reviewed(image_path, reviewed, drafts_dir):
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