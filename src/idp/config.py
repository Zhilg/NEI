"""Validated, environment-backed configuration for the local deployment."""

from __future__ import annotations

from pathlib import Path

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
    offline_mode: bool = True
    telemetry_enabled: bool = False
    release_manifest_path: Path | None = None

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
