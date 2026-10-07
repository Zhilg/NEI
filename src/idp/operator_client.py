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

_CANDIDATE_EDITORS = ("nano", "vim", "vi", "micro", "emacs")


class OperatorClient:
    def __init__(
        self,
        input_path: Path,
        document_type: str = "other",
        limit: int = 0,
        drafts_dir: Path | None = None,
        open_image: bool = False,
        force: bool = False,
    ) -> None:
        self.input_path = input_path
        self.document_type = document_type
        self.limit = limit
        self.open_image = open_image
        self.force = force
        self.drafts_dir = drafts_dir or corrections_path().parent / "operator_drafts"
        self.drafts_dir.mkdir(parents=True, exist_ok=True)

    def _collect_images(self) -> list[Path]:
        input_path = self.input_path
        drafts_dir = self.drafts_dir
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

    @staticmethod
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

    @staticmethod
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

    @staticmethod
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

    @staticmethod
    def _write_draft(draft_path: Path, image_path: Path, markdown: str) -> None:
        draft_path.parent.mkdir(parents=True, exist_ok=True)
        normalized = markdown.replace("\r\n", "\n").replace("\r", "\n")
        normalized = normalized.replace("\\n", "\n")
        draft_path.write_text(normalized, encoding="utf-8")

    @staticmethod
    def _read_draft(draft_path: Path) -> str:
        return draft_path.read_text(encoding="utf-8")

    async def _run_vlm(self, image_path: Path) -> str:
        return await reconstruct_markdown([image_path])

    def _already_reviewed(self, image_path: Path, reviewed: set[str]) -> bool:
        if self.force:
            return False
        marker = self.drafts_dir / f"{image_path.stem}.reviewed"
        if marker.exists():
            return True
        return False

    async def _review_page(self, image_path: Path) -> bool:
        draft_path = self.drafts_dir / f"{image_path.stem}.md"
        original_markdown = await self._run_vlm(image_path)
        self._write_draft(draft_path, image_path, original_markdown)
        if self.open_image:
            self._open_image(image_path)
        self._render_console(image_path, original_markdown, draft_path)

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
                self._launch_editor(draft_path)
                try:
                    markdown = self._read_draft(draft_path)
                except (OSError, json.JSONDecodeError) as exc:
                    print(f"Cannot read draft after edit: {exc}", file=sys.stderr)
                    continue
                self._render_console(image_path, markdown, draft_path)
                continue
            if choice in {"r", "reread"}:
                try:
                    markdown = self._read_draft(draft_path)
                except (OSError, json.JSONDecodeError) as exc:
                    print(f"Cannot read draft: {exc}", file=sys.stderr)
                    continue
                self._render_console(image_path, markdown, draft_path)
                continue
            if choice in {"a", "accept"}:
                try:
                    saved_markdown = self._read_draft(draft_path)
                except (OSError, json.JSONDecodeError) as exc:
                    print(f"Cannot read draft: {exc}", file=sys.stderr)
                    continue
                append_correction(
                    page_image=str(image_path),
                    markdown=saved_markdown,
                    vlm_entities=[],
                    operator_corrected_entities=[],
                    timestamp=time.time(),
                )
                marker = self.drafts_dir / f"{image_path.stem}.reviewed"
                marker.write_text("reviewed", encoding="utf-8")
                print(f"Saved correction -> {corrections_path()}")
                return True

    async def run(self) -> None:
        images = self._collect_images()
        if self.limit > 0:
            images = images[: self.limit]
        if not images:
            print(f"No page images found under {self.input_path}", file=sys.stderr)
            return

        print(f"Pages to review: {len(images)} (document_type={self.document_type}, test_mode={settings.test_mode})")

        reviewed = {str(record.get("page_image", "")) for record in read_corrections()}
        pending = [img for img in images if not self._already_reviewed(img, set())]
        print(f"Pending pages: {len(pending)}")

        if pending:
            print("Processing all pages with VLM first...")
            for index, image_path in enumerate(pending, start=1):
                draft_path = self.drafts_dir / f"{image_path.stem}.md"
                if draft_path.exists():
                    print(f"  [{index}/{len(pending)}] draft exists: {image_path.name}")
                    continue
                print(f"  [{index}/{len(pending)}] processing: {image_path.name}")
                try:
                    markdown = await self._run_vlm(image_path)
                    self._write_draft(draft_path, image_path, markdown)
                except Exception as exc:
                    print(f"  Failed: {exc}", file=sys.stderr)
                    continue

        for index, image_path in enumerate(images, start=1):
            print(f"\n[{index}/{len(images)}] {image_path}")
            if self._already_reviewed(image_path, reviewed):
                print("Already reviewed — skipping.")
                continue
            try:
                if not await self._review_page(image_path):
                    print("Stopped by operator.")
                    return
            except KeyboardInterrupt:
                print("\nInterrupted by operator.")
                return
            except Exception as exc:  # noqa: BLE001
                print(f"Page failed: {exc}", file=sys.stderr)
                continue


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Operator review loop for VLM handwriting extraction",
    )
    parser.add_argument("--input", required=True, help="PDF, image, or directory of pages to review")
    parser.add_argument("--document-type", default="other", help="Document type hint passed to the VLM prompt")
    parser.add_argument("--limit", type=int, default=0, help="Max pages to process (0 = all)")
    parser.add_argument("--drafts-dir", default="", help="Where to write editable draft files")
    parser.add_argument("--open-image", action="store_true", help="Open each page image in the default viewer")
    parser.add_argument("--production-prompts", action="store_true", help="Use production prompts instead of test prompts")
    parser.add_argument("--test-mode", action="store_true", help="Use minimal test prompts and reduced max_tokens")
    parser.add_argument("--force", action="store_true", help="Re-review pages even if they were already reviewed")
    return parser.parse_args()


async def _main() -> None:
    args = _parse_args()
    settings.test_mode = args.test_mode

    input_path = Path(args.input)
    drafts_dir = Path(args.drafts_dir) if args.drafts_dir else corrections_path().parent / "operator_drafts"

    client = OperatorClient(
        input_path=input_path,
        document_type=args.document_type,
        limit=args.limit,
        drafts_dir=drafts_dir,
        open_image=args.open_image,
        force=args.force,
    )
    client._test_mode = args.test_mode
    await client.run()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
