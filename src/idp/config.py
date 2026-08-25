"""Minimal runtime configuration for the VL-only pipeline."""

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
    render_dpi: int = Field(default=300, ge=72, le=600)
    vl_endpoint: str = "http://vllm-vl:8000/v1"
    vl_endpoints: list[str] = Field(default_factory=list)
    vl_model: str = "Qwen2.5-VL-32B-Instruct-AWQ"
    vl_timeout_seconds: float = Field(default=600, gt=0, le=3600)
    vl_max_tokens: int = Field(default=8192, ge=1, le=65536)
    vl_max_images: int = Field(default=3, gt=0)
    vl_concurrency: int = Field(default=8, ge=1, le=64)

    text_llm_endpoint: str = ""
    text_llm_model: str = ""
    # Recommended non-Qwen text-only models for Russian entity extraction:
    #   IDP_TEXT_LLM_MODEL=mistralai/Mistral-7B-Instruct-v0.3
    #   IDP_TEXT_LLM_MODEL=IlyaGusev/saiga_mistral_7b
    #   IDP_TEXT_LLM_MODEL=IlyaGusev/saiga_llama3_8b

    test_mode: bool = Field(default=False)
    artifacts_mode: bool = Field(default=False)
    entity_schema_path: Path = Field(default=Path(__file__).parent / "entity_schema.json")
    trash_path: str = Field(default="")

    @field_validator("input_root", "output_root", "models_root")
    @classmethod
    def require_absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            msg = f"path must be absolute: {value}"
            raise ValueError(msg)
        return value

    @field_validator("vl_endpoint", "text_llm_endpoint")
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
            return [v.rstrip("/") for v in value if v]
        if not value:
            return []
        return [v.strip().rstrip("/") for v in str(value).split(",") if v.strip()]


settings = Settings()
