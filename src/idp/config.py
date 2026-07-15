"""Validated, environment-backed configuration for the local deployment."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings that do not contain model-specific pipeline policy."""

    model_config = SettingsConfigDict(
        env_prefix="IDP_",
        env_file=".env",
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
    batch_staging_root: Path = Path("/var/lib/idp/staging")
    offline_mode: bool = True
    telemetry_enabled: bool = False
    release_manifest_path: Path | None = None
    release_root: Path = Path("/var/lib/idp")
    release_public_key_path: Path = Path("/etc/idp/release-public.pem")
    container_runtime: str = "docker"

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

    @field_validator("release_manifest_path")
    @classmethod
    def require_local_release_manifest(cls, value: Path | None) -> Path | None:
        """Release manifests are local files; remote URLs violate air-gapped runtime."""
        if value is not None and not value.is_absolute():
            msg = "release manifest path must be absolute"
            raise ValueError(msg)
        return value

    @field_validator("release_root", "release_public_key_path", "batch_staging_root")
    @classmethod
    def require_absolute_release_path(cls, value: Path) -> Path:
        """Target release state and trust material must never resolve relatively."""
        if not value.is_absolute():
            msg = f"release path must be absolute: {value}"
            raise ValueError(msg)
        return value

    @field_validator("qwen_vl_endpoint")
    @classmethod
    def require_local_qwen_endpoint(cls, value: str) -> str:
        """The VLM endpoint must be an internal service, never an external API URL."""
        endpoint = urlparse(value)
        if endpoint.scheme != "http" or endpoint.hostname not in {
            "qwen-vl",
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            msg = "Qwen-VL endpoint must use an approved local/internal HTTP host"
            raise ValueError(msg)
        return value.rstrip("/")
