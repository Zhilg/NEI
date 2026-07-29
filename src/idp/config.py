"""Minimal runtime configuration for the simplified pipeline."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="IDP_",
        extra="ignore",
    )

    input_root: Path = Field(default=Path("/input"))
    output_root: Path = Field(default=Path("/output"))
    models_root: Path = Field(default=Path("/models"))
    render_dpi: int = Field(default=200, ge=72, le=600)
    vl_endpoint: str = "http://vllm-vl:8000/v1"
    llm_endpoint: str = "http://vllm-llm:8000/v1"
    vl_model: str = "Qwen2.5-VL-32B-Instruct"
    llm_model: str = "Qwen3-14B-Instruct"
    vl_timeout_seconds: float = Field(default=600, gt=0, le=3600)
    llm_timeout_seconds: float = Field(default=300, gt=0, le=3600)
    max_images_per_request: int = Field(default=10, gt=0)

    @field_validator("input_root", "output_root", "models_root")
    @classmethod
    def require_absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            msg = f"path must be absolute: {value}"
            raise ValueError(msg)
        return value

    @field_validator("vl_endpoint", "llm_endpoint")
    @classmethod
    def require_local_endpoint(cls, value: str) -> str:
        endpoint = urlparse(value)
        if endpoint.scheme != "http":
            msg = "endpoint must use http"
            raise ValueError(msg)
        return value.rstrip("/")


settings = Settings()
