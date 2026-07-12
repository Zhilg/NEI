"""Validated, environment-backed configuration for the local deployment."""

from __future__ import annotations

from pathlib import Path

from pydantic import AnyUrl, Field, field_validator
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
    metrics_port: int = Field(default=9100, ge=1, le=65535)
    offline_mode: bool = True
    telemetry_enabled: bool = False
    release_manifest_url: AnyUrl | None = None

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

    @field_validator("release_manifest_url")
    @classmethod
    def reject_remote_manifest_in_offline_mode(cls, value: AnyUrl | None) -> AnyUrl | None:
        """A remote release manifest would violate target runtime assumptions."""
        if value is not None and value.scheme not in {"file"}:
            msg = "release manifest must be a local file URL"
            raise ValueError(msg)
        return value
