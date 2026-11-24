"""Reflow через VLM: склейка «лесенки» в абзацы моделью.

Файлы, где строки разорваны шириной колонки (много коротких строк без конца
предложения), отправляются в VLM с инструкцией склеить строки в естественные
абзацы, НЕ меняя текст.  Эвристический джойнер в plaintext.py остаётся
первым уровнем; эта фаза — второй, для случаев, которые эвристика портит
или не берёт (стихи, сложная вёрстка).

Запуск на сервере (пока воркер остановлен, vllm-сервисы живы):

    docker compose -f infra/compose/local.yml run --rm --no-deps worker \\
        python /workspace/scripts/reflow_vlm.py

Прогон идёт по всем txt в output/txts; тронуты будут только «лесеночные».
Каждый файл логируется; файл заменяется только если ответ валиден
(длина ответа в разумных пределах относительно исходника).
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("reflow_vlm")

sys.path.insert(0, "/workspace/src")

from idp.config import settings  # noqa: E402
from idp.vlm_client import VlmClient, _strip_code_fences, _strip_thinking_blocks  # noqa: E402

PROMPT = (
    "The text below was extracted from a document where every line was "
    "hard-wrapped at a fixed column width, so sentences are broken across "
    "many short lines. Reformat it into natural paragraphs: join lines that "
    "belong to the same paragraph and keep existing paragraph breaks. "
    "Preserve the text EXACTLY - do not add, remove, translate, summarize "
    "or rewrite anything. If a fragment is verse/poetry, a list, or table "
    "data, keep its line structure. Output ONLY the reformatted plain text, "
    "no comments and no markdown markup."
)

CHUNK_CHARS = 20000
MAX_OUTPUT_TOKENS = 8192
MIN_LINES = 25
MIN_BAD_SHARE = 0.5
MIN_LEN_RATIO = 0.6
MAX_LEN_RATIO = 1.4

_SENT_END_CHARS = (".", "!", "?", ":", ";", "»", ")", "…")
_LIST_ITEM_RE = re.compile(r"^([-*•‣]|\d{1,3}[.)])\s")


def looks_broken(text: str) -> bool:
    """Похоже ли на разорванную колонкой 'лесенку'."""
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if len(lines) < MIN_LINES:
        return False
    mean_len = sum(len(ln) for ln in lines) / len(lines)
    if mean_len > 50:
        return False
    bad = 0
    for ln in lines:
        s = ln.strip()
        if (
            len(s) < 50
            and not s.endswith(_SENT_END_CHARS)
            and not _LIST_ITEM_RE.match(s)
        ):
            bad += 1
    return bad / len(lines) >= MIN_BAD_SHARE


def _chunk_text(text: str, max_chars: int) -> list[str]:
    """Разрезать текст на чанки <= max_chars по границам абзацев; одиночные
    длинные абзацы дополнительно режутся по границам предложений."""
    paragraphs = re.split(r"\n\s*\n", text)
    pieces: list[str] = []
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        while len(para) > max_chars:
            window = para[:max_chars]
            cut = window.rfind(". ")
            if cut < max_chars // 2:
                cut = window.rfind(" ")
            if cut <= 0:
                cut = max_chars
            elif para[cut] == ".":
                cut += 1
            pieces.append(para[:cut].strip())
            para = para[cut:].strip()
        if para:
            pieces.append(para)
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for piece in pieces:
        add = len(piece) + 2
        if buf and size + add > max_chars:
            chunks.append("\n\n".join(buf))
            buf, size = [], 0
        buf.append(piece)
        size += add
    if buf:
        chunks.append("\n\n".join(buf))
    return chunks


PROGRESS_INTERVAL_SEC = 30.0


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} ч {minutes:02d} мин"
    if minutes:
        return f"{minutes} мин {secs:02d} с"
    return f"{secs} с"


class Reflower:
    def __init__(self, total_files: int) -> None:
        self._client = VlmClient()
        self._sem = asyncio.Semaphore(settings.vl_concurrency)
        self._total_files = total_files
        self._finished = 0
        self._done = 0
        self._skipped = 0
        self._failed = 0
        self._chunks_total = 0
        self._chunks_done = 0
        self._active_chunks = 0
        self._active_files: set[str] = set()
        self._started = time.perf_counter()

    def log_progress(self) -> None:
        """Понятный heartbeat: работа не зависла, даже если VLM долго молчит."""
        elapsed = time.perf_counter() - self._started
        remaining = self._total_files - self._finished
        if self._finished:
            eta = remaining * elapsed / self._finished
            eta_text = _format_duration(eta)
        else:
            eta_text = "появится после первых завершений"
        active_names = sorted(self._active_files)
        active = ", ".join(active_names[:3])
        if len(active_names) > 3:
            active += f" … ещё {len(active_names) - 3}"
        active_hint = f" | сейчас: {active}" if active else ""
        logger.info(
            "Reflow прогресс: %d/%d файлов, осталось %d | перефлоучено %d, "
            "без 'лесенки' %d, ошибок %d | VLM-чанки: готово %d из %d "
            "обнаруженных, в работе %d | прошло %s, ETA %s%s",
            self._finished, self._total_files, remaining,
            self._done, self._skipped, self._failed,
            self._chunks_done, self._chunks_total, self._active_chunks,
            _format_duration(elapsed), eta_text, active_hint,
        )

    async def reflow_file(self, path: Path) -> None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            self._failed += 1
            self._finished += 1
            logger.warning("%s: файл не читается, оставляю как есть: %s", path.name, exc)
            return
        if not looks_broken(text):
            self._skipped += 1
            self._finished += 1
            return

        chunks = _chunk_text(text, CHUNK_CHARS)
        self._chunks_total += len(chunks)
        self._active_files.add(path.name)
        logger.info("Reflow VLM старт: %s — %d чанков", path.name, len(chunks))
        try:
            out = await self._reflow_chunks(chunks)
            if not out.strip():
                self._failed += 1
                logger.warning("%s: пустой ответ, оставляю как есть", path.name)
                return
            ratio = len(out) / max(1, len(text))
            if not (MIN_LEN_RATIO <= ratio <= MAX_LEN_RATIO):
                self._failed += 1
                logger.warning(
                    "%s: подозрительное соотношение длин %.2f, оставляю как есть",
                    path.name, ratio,
                )
                return
            path.write_text(out.strip() + "\n", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 — не роняем прогон из-за файла
            self._failed += 1
            logger.warning("%s: ошибка VLM/записи, оставляю как есть: %s", path.name, exc)
            return
        finally:
            self._active_files.discard(path.name)
            self._finished += 1

        self._done += 1
        logger.info(
            "Reflow файл готов: %s | завершено %d/%d",
            path.name, self._finished, self._total_files,
        )

    async def _reflow_chunks(self, chunks: list[str]) -> str:
        async with self._client._http() as http:  # noqa: SLF001 — тот же пакет
            async def one(chunk: str) -> str:
                async with self._sem:
                    self._active_chunks += 1
                    try:
                        payload = {
                            "model": settings.vl_model,
                            "messages": [
                                {"role": "system", "content": PROMPT},
                                {"role": "user", "content": chunk},
                            ],
                            "temperature": 0.1,
                            "max_tokens": MAX_OUTPUT_TOKENS,
                        }
                        response = await http.post(
                            f"{http.selector.next()}/chat/completions", payload
                        )
                        if response.status_code != 200:
                            raise RuntimeError(
                                f"vLLM {response.status_code}: {response.text[:500]}"
                            )
                        data = response.json()
                        content = data["choices"][0]["message"]["content"]
                        self._chunks_done += 1
                        return _strip_code_fences(_strip_thinking_blocks(content))
                    finally:
                        self._active_chunks -= 1

            # Ждём все чанки, даже если один упал: счётчики активных запросов
            # остаются точными, а heartbeat не показывает фантомную работу.
            results = await asyncio.gather(
                *[one(c) for c in chunks], return_exceptions=True,
            )
        errors = [r for r in results if isinstance(r, Exception)]
        if errors:
            raise errors[0]
        parts = [r for r in results if isinstance(r, str)]
        return "\n\n".join(p.strip() for p in parts if p.strip())


FILE_WORKERS = 16  # файлы параллельно: внутри файла чанки и так параллельны,
# но мелкие файлы не наполняют семафор — держим несколько файлов в полёте.


async def _progress_loop(reflower: Reflower) -> None:
    while True:
        await asyncio.sleep(PROGRESS_INTERVAL_SEC)
        reflower.log_progress()


async def amain() -> int:
    txts_root = settings.output_root / "txts"
    if not txts_root.is_dir():
        logger.error("Нет %s", txts_root)
        return 1
    files = sorted(p for p in txts_root.glob("*.txt") if p.is_file())
    logger.info(
        "Reflow: всего %d txt в %s; подробный статус каждые %.0f с",
        len(files), txts_root, PROGRESS_INTERVAL_SEC,
    )
    reflower = Reflower(total_files=len(files))
    queue: asyncio.Queue[Path | None] = asyncio.Queue()
    for path in files:
        queue.put_nowait(path)
    for _ in range(FILE_WORKERS):
        queue.put_nowait(None)

    async def worker() -> None:
        while True:
            path = await queue.get()
            if path is None:
                return
            await reflower.reflow_file(path)

    progress_task = asyncio.create_task(_progress_loop(reflower))
    try:
        await asyncio.gather(*[worker() for _ in range(FILE_WORKERS)])
    finally:
        progress_task.cancel()
        try:
            await progress_task
        except asyncio.CancelledError:
            pass
    reflower.log_progress()
    logger.info(
        "Готово за %.0f с: %d перефлоучено, %d без 'лесенки', %d отклонено",
        time.perf_counter() - reflower._started,
        reflower._done, reflower._skipped, reflower._failed,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
