"""Minimal runtime configuration for the VL-only pipeline."""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Union

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
    vl_endpoints: Union[str, list[str]] = Field(default="")
    vl_model: str = "Qwen3.8-27B"
    vl_timeout_seconds: float = Field(default=600, gt=0, le=3600)
    vl_max_tokens: int = Field(default=16384, ge=1, le=65536)
    vl_max_images: int = Field(default=2, gt=0)
    vl_concurrency: int = Field(default=12, ge=1, le=64)

    test_mode: bool = Field(default=False)
    artifacts_mode: bool = Field(default=False)
    visio_mode: bool = Field(default=False)
    visio_content: str = Field(default="pages")
    entity_schema_path: Union[str, Path, None] = Field(default=None)
    trash_path: str = Field(default="")
    min_entity_confidence: float = Field(default=0.3, ge=0.0, le=1.0)
    reconstruct_formats: Union[str, list[str]] = Field(default="docx,pdf")

    vllm_quantization: str = Field(default="awq")
    vllm_dtype: str = Field(default="half")
    vllm_gpu_memory_utilization: float = Field(default=0.9, ge=0.1, le=1.0)
    vllm_max_model_len: int = Field(default=32768, ge=1024)
    vllm_kv_cache_memory: str = Field(default="15000000000")

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

    @field_validator("entity_schema_path", mode="before")
    @classmethod
    def parse_entity_schema_path(cls, value: str | Path | None) -> Path | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return Path(value)

    @field_validator("vl_endpoint")
    @classmethod
    def require_local_endpoint(cls, value: str) -> str:
        if not value:
            return value
        endpoint = urlparse(value)
        if endpoint.scheme != "http":
            msg = "endpoint must use http"
            raise ValueError(msg)
        return value.rstrip("/")

    @field_validator("vl_endpoints", mode="before")
    @classmethod
    def parse_vl_endpoints(cls, value: str | list[str]) -> list[str]:
        if isinstance(value, list):
            return [v.strip().rstrip("/") for v in value if v and v.strip()]
        if not value:
            return []
        if isinstance(value, str):
            value = value.strip('"').strip("'")
            if not value:
                return []
            return [v.strip().rstrip("/") for v in value.split(",") if v.strip()]
        return []

    @field_validator("reconstruct_formats", mode="before")
    @classmethod
    def parse_reconstruct_formats(cls, value: str | list[str]) -> list[str]:
        if isinstance(value, list):
            return [v.strip().lower().lstrip(".") for v in value if v and v.strip()]
        if not value:
            return []
        if isinstance(value, str):
            value = value.strip('"').strip("'")
            if not value:
                return []
            return [v.strip().lower().lstrip(".") for v in value.split(",") if v.strip()]
        return []

    @field_validator("visio_content")
    @classmethod
    def check_visio_content(cls, value: str) -> str:
        content = (value or "").strip().lower()
        if content not in ("pages", "sheet", "document"):
            msg = f"visio_content must be pages, sheet or document (got {value!r})"
            raise ValueError(msg)
        return content


settings = Settings()
