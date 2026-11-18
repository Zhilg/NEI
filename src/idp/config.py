"""Minimal runtime configuration for the VL-only pipeline."""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="IDP_",
        extra="ignore",
    )

    input_root: Path = Field(default=Path("/input"))
    output_root: Path = Field(default=Path("/output"))
    render_dpi: int = Field(default=200, ge=72, le=600)
    upscale_factor: int = Field(default=1, ge=1, le=4)
    max_image_dimension: int = Field(default=2048, ge=512, le=4096)
    vl_endpoint: str = "http://vllm-vl:8000/v1"
    vl_endpoints: str | list[str] = Field(default="")
    vl_model: str = "Qwen3.8-27B"
    vl_timeout_seconds: float = Field(default=1800, gt=0, le=3600)
    vl_max_tokens: int = Field(default=512, ge=1, le=65536)
    vl_max_images: int = Field(default=2, gt=0)
    vl_max_context_chars: int = Field(default=500, ge=100, le=100000)
    vl_concurrency: int = Field(default=12, ge=1, le=64)

    # Сколько файлов обрабатывается параллельно (очередь воркеров и пул
    # потоков для CPU-части: текстовый слой, конвертеры, запись txt).
    worker_threads: int = Field(default=32, ge=1, le=256)
    # Как часто (сек) архив сверяется полностью (CRC журналов + недостающие
    # файлы).  Между сверками результаты дозаписываются точечно после
    # каждого файла — на больших пачках это в разы быстрее полной перепаковки.
    archive_sync_interval: float = Field(default=60.0, ge=0)

    test_mode: bool = Field(default=False)
    artifacts_mode: bool = Field(default=False)
    visio_mode: bool = Field(default=False)
    visio_content: str = Field(default="pages")
    reconstruct_formats: str | list[str] = Field(default="")

    # Пути-строки (пустая строка = не задано): compose передаёт их как env,
    # при отсутствии полей pydantic молча игнорировал бы эти переменные.
    entity_schema_path: str = Field(default="")
    trash_path: str = Field(default="")
    min_entity_confidence: float = Field(default=0.3, ge=0, le=1)

    vllm_quantization: str = Field(default="awq")
    vllm_dtype: str = Field(default="half")
    vllm_gpu_memory_utilization: float = Field(default=0.9, ge=0.1, le=1.0)
    vllm_max_model_len: int = Field(default=32768, ge=1024)
    vllm_kv_cache_memory: str = Field(default="15000000000")

    # Image preprocessing for poor-quality scans (Xiaomi camera docs, low res,
    # rotated pages, shadows). Disabled by default so clean born-digital PDFs
    # are rendered untouched.
    preprocess_enabled: bool = Field(default=False)
    preprocess_steps: str = Field(default="upscale,denoise,shadows,clahe,deskew")
    preprocess_upscale: int = Field(default=2, ge=1, le=4)
    preprocess_binarize: bool = Field(default=False)

    finetune_feedback_enabled: bool = Field(default=True)
    finetune_feedback_dir: Path = Field(default=Path("/output/finetune"))
    finetune_max_feedback_examples: int = Field(default=10, ge=0, le=100)
    operator_corrections_path: Path = Field(default=Path("/output/operator_corrections.jsonl"))
    finetune_dataset_path: Path = Field(default=Path("/output/finetune_data.jsonl"))

    @field_validator("input_root", "output_root", "operator_corrections_path", "finetune_dataset_path")
    @classmethod
    def require_absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            msg = f"path must be absolute: {value}"
            raise ValueError(msg)
        return value

    @field_validator("visio_content")
    @classmethod
    def check_visio_content(cls, value: str) -> str:
        content = (value or "").strip().lower()
        if content not in ("pages", "sheet", "document"):
            msg = f"visio_content must be pages, sheet or document (got {value!r})"
            raise ValueError(msg)
        return content


settings = Settings()
