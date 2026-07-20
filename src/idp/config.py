"""Validated, environment-backed configuration for the local deployment."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings that do not contain model-specific pipeline policy."""

    model_config = SettingsConfigDict(
        env_prefix="IDP_",
        extra="forbid",
    )

    allowed_roots: tuple[Path, ...] = Field(default=())
    database_url: str = "postgresql+psycopg://idp:idp@postgres:5432/idp"
    minio_endpoint: str = "minio:9000"
    minio_secure: bool = False
    minio_bucket: str = "idp-artifacts"
    minio_access_key: str | None = None
    minio_secret_key: str | None = None
    metrics_port: int = Field(default=9100, ge=1, le=65535)
    controller_poll_seconds: float = Field(default=5.0, gt=0, le=300)
    scan_stability_seconds: float = Field(default=1.0, ge=0, le=60)
    scan_max_file_bytes: int = Field(default=2 * 1024 * 1024 * 1024, gt=0)
    scan_max_candidates: int = Field(default=100_000, gt=0)
    scan_max_depth: int = Field(default=64, ge=1, le=1024)
    scan_hash_chunk_bytes: int = Field(default=1024 * 1024, gt=0)
    render_dpi: int = Field(default=200, ge=72, le=600)
    render_max_pages: int = Field(default=2_000, gt=0)
    render_max_pixels_per_page: int = Field(default=80_000_000, gt=0)
    render_max_total_pixels: int = Field(default=2_000_000_000, gt=0)
    upscale_entropy_tolerance: float = Field(default=0.12, ge=0, le=1)
    upscale_clipping_tolerance: float = Field(default=0.01, ge=0, le=1)
    ocr_max_lines_per_block: int = Field(default=500, gt=0)
    ocr_min_token_confidence: float = Field(default=0.0, ge=0, le=1)
    qwen_vl_endpoint: str = "http://qwen-vl:8000/v1"
    qwen_vl_model_id: str = "Qwen2.5-VL-32B-Instruct"
    qwen_vl_model_revision: str = "pinned-in-profile"
    qwen_vl_prompt_version: str = "reconstruction-v1"
    qwen_vl_timeout_seconds: float = Field(default=600, gt=0, le=3600)
    qwen_vl_max_blocks_per_request: int = Field(default=100, gt=0)
    qwen_vl_max_images_per_request: int = Field(default=40, gt=0)
    qwen_vl_gpu0_slot_unit: str = "role"
    qwen3_endpoint: str = "http://qwen3:8000/v1"
    qwen3_model_id: str = "Qwen3-14B"
    qwen3_model_revision: str = "pinned-in-profile"
    qwen3_timeout_seconds: float = Field(default=300, gt=0, le=900)
    qwen3_gpu0_slot_unit: str = "role"
    gpu1_slot_unit: str = "role"
    pipeline_profile_version: str = Field(default="v1", min_length=1)
    mineru_command: tuple[str, ...] = ()
    ocr_detector_command: tuple[str, ...] = ()
    ocr_router_command: tuple[str, ...] = ()
    ocr_east_slavic_command: tuple[str, ...] = ()
    ocr_cyrillic_command: tuple[str, ...] = ()
    ocr_latin_cjk_command: tuple[str, ...] = ()
    data_root: Path = Path("/data")
    batch_staging_root: Path = Path("/data/staging")
    offline_mode: bool = True
    telemetry_enabled: bool = False

    @field_validator("allowed_roots")
    @classmethod
    def validate_allowed_roots(cls, roots: tuple[Path, ...]) -> tuple[Path, ...]:
        """Require absolute, normalized roots before any scanner is introduced."""
        normalized: list[Path] = []
        for root in roots:
            if not root.is_absolute():
                msg = f"allowed root must be absolute: {root}"
                raise ValueError(msg)
            normalized.append(root.resolve(strict=False))
        return tuple(normalized)

    @field_validator("data_root", "batch_staging_root")
    @classmethod
    def require_absolute_data_path(cls, value: Path) -> Path:
        """Persistent state paths must be explicit host-mounted container paths."""
        if not value.is_absolute():
            msg = f"data path must be absolute: {value}"
            raise ValueError(msg)
        return value

    @field_validator("qwen_vl_endpoint")
    @classmethod
    def require_local_qwen_endpoint(cls, value: str) -> str:
        """The VLM endpoint must be an internal service, never an external API URL."""
        endpoint = urlparse(value)
        if (
            endpoint.scheme != "http"
            or endpoint.hostname not in {
            "qwen-vl",
            "localhost",
            "127.0.0.1",
            "::1",
            }
            or endpoint.port != 8000
            or endpoint.path.rstrip("/") != "/v1"
        ):
            msg = "Qwen-VL endpoint must use an approved local/internal HTTP host"
            raise ValueError(msg)
        return value.rstrip("/")

    @field_validator("qwen3_endpoint")
    @classmethod
    def require_local_qwen3_endpoint(cls, value: str) -> str:
        """The entity endpoint is an internal Qwen3/Fenic-compatible service only."""
        endpoint = urlparse(value)
        if (
            endpoint.scheme != "http"
            or endpoint.hostname not in {
            "qwen3",
            "qwen3-fenic",
            "qwen3-vllm",
            "fenic",
            "localhost",
            "127.0.0.1",
            "::1",
            }
            or endpoint.port != 8000
            or endpoint.path.rstrip("/") != "/v1"
        ):
            msg = "Qwen3 endpoint must use an approved local/internal HTTP host"
            raise ValueError(msg)
        return value.rstrip("/")

    @field_validator(
        "mineru_command",
        "ocr_detector_command",
        "ocr_router_command",
        "ocr_east_slavic_command",
        "ocr_cyrillic_command",
        "ocr_latin_cjk_command",
        mode="before",
    )
    @classmethod
    def require_json_command_array(cls, value: Any) -> tuple[str, ...]:
        """Accept only explicit JSON command arrays from deployment environment variables."""
        if value in (None, (), ""):
            return ()
        decoded = value
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError as error:
                raise ValueError("command must be a JSON array of non-empty strings") from error
        if (
            not isinstance(decoded, (list, tuple))
            or not decoded
            or not all(isinstance(argument, str) and argument for argument in decoded)
        ):
            raise ValueError("command must be a JSON array of non-empty strings")
        return tuple(decoded)
