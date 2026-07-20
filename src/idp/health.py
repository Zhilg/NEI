"""Small local readiness checks for the Compose deployment."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from minio import Minio
from sqlalchemy import create_engine, text

from idp.config import Settings


class RuntimeHealthError(RuntimeError):
    """Raised when a local Compose dependency is unavailable or misconfigured."""


@dataclass(frozen=True)
class RuntimeHealthReport:
    """Machine-readable result for `idp healthcheck`."""

    postgres_ok: bool
    minio_ok: bool
    qwen_vl_ok: bool
    qwen3_ok: bool


def check_runtime_health(settings: Settings, *, include_models: bool = True) -> RuntimeHealthReport:
    """Check only services required by the mounted Compose stack; no signatures or releases."""
    _check_postgres(settings)
    _check_minio(settings)
    if include_models:
        _check_model_endpoint(settings.qwen_vl_endpoint, "Qwen-VL")
        _check_model_endpoint(settings.qwen3_endpoint, "Qwen3")
    return RuntimeHealthReport(
        postgres_ok=True,
        minio_ok=True,
        qwen_vl_ok=include_models,
        qwen3_ok=include_models,
    )


def _check_postgres(settings: Settings) -> None:
    try:
        engine = create_engine(settings.database_url, pool_pre_ping=True)
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        engine.dispose()
    except Exception as error:
        raise RuntimeHealthError(f"PostgreSQL is unavailable: {error}") from error


def _check_minio(settings: Settings) -> None:
    if not settings.minio_access_key or not settings.minio_secret_key:
        raise RuntimeHealthError("MinIO credentials are required")
    try:
        client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        if not client.bucket_exists(settings.minio_bucket):
            raise RuntimeHealthError(f"MinIO bucket does not exist: {settings.minio_bucket}")
    except RuntimeHealthError:
        raise
    except Exception as error:
        raise RuntimeHealthError(f"MinIO is unavailable: {error}") from error


def _check_model_endpoint(endpoint: str, label: str) -> None:
    request = urllib.request.Request(f"{endpoint}/models", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 200:
                raise RuntimeHealthError(f"{label} returned HTTP {response.status}")
            payload = json.loads(response.read())
    except RuntimeHealthError:
        raise
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeHealthError(f"{label} is unavailable: {error}") from error
    if not isinstance(payload, dict) or not payload.get("data"):
        raise RuntimeHealthError(f"{label} returned no loaded models")
