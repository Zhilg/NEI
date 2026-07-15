"""Target-side release/profile validation with no network dependency."""

from __future__ import annotations

import os
import json
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass

from minio import Minio
from sqlalchemy import create_engine, text

from idp.config import Settings
from idp.releases.lifecycle import ReleaseManager
from idp.releases.manifest import load_public_verification_key, verify_bundle


class ProfileValidationError(RuntimeError):
    """Raised when an activated offline release or local service prerequisite is invalid."""


@dataclass(frozen=True)
class ProfileValidationReport:
    """Machine-readable result returned by `idp profile validate`."""

    release_id: str
    pipeline_profile_hash: str
    verified_assets: int
    verified_bytes: int
    postgres_ok: bool
    minio_ok: bool
    qwen_vl_ok: bool
    qwen3_ok: bool
    gpu_vram_bytes: int


def validate_profile(settings: Settings, release_id: str | None = None) -> ProfileValidationReport:
    """Verify active/imported release, offline policy, PostgreSQL, and local MinIO."""
    _require_offline_policy(settings)
    verification_key = load_public_verification_key(settings.release_public_key_path)
    manager = ReleaseManager(settings.release_root, verification_key)
    if release_id is None:
        manifest = manager.active_manifest()
        bundle_root = manager.active_link.resolve(strict=True)
    else:
        bundle_root = manager.releases_directory / release_id
        manifest = manager.imported_manifest(release_id)
    report = verify_bundle(bundle_root, manifest, verification_key)
    _check_postgres(settings)
    _check_minio(settings)
    _check_model_endpoint(settings.qwen_vl_endpoint, "Qwen-VL")
    _check_model_endpoint(settings.qwen3_endpoint, "Qwen3/Fenic")
    gpu_vram_bytes = _check_gpu_vram()
    return ProfileValidationReport(
        release_id=manifest.release_id,
        pipeline_profile_hash=manifest.pipeline_profile_hash,
        verified_assets=report.verified_assets,
        verified_bytes=report.verified_bytes,
        postgres_ok=True,
        minio_ok=True,
        qwen_vl_ok=True,
        qwen3_ok=True,
        gpu_vram_bytes=gpu_vram_bytes,
    )


def _require_offline_policy(settings: Settings) -> None:
    if not settings.offline_mode:
        msg = "IDP_OFFLINE_MODE must be true on target"
        raise ProfileValidationError(msg)
    if settings.telemetry_enabled:
        msg = "IDP_TELEMETRY_ENABLED must be false on target"
        raise ProfileValidationError(msg)
    required_variables = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
    for name, expected in required_variables.items():
        if os.environ.get(name) != expected:
            msg = f"target offline policy requires {name}={expected}"
            raise ProfileValidationError(msg)


def _check_postgres(settings: Settings) -> None:
    try:
        engine = create_engine(settings.database_url, pool_pre_ping=True)
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        engine.dispose()
    except Exception as error:
        msg = f"PostgreSQL health check failed: {error}"
        raise ProfileValidationError(msg) from error


def _check_minio(settings: Settings) -> None:
    if settings.minio_access_key is None or settings.minio_secret_key is None:
        msg = "MinIO access credentials are required for profile validation"
        raise ProfileValidationError(msg)
    try:
        client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        client.bucket_exists(settings.minio_bucket)
    except Exception as error:
        msg = f"MinIO health check failed: {error}"
        raise ProfileValidationError(msg) from error


def _check_model_endpoint(endpoint: str, label: str) -> None:
    """Verify a local vLLM-compatible endpoint without permitting external egress."""
    request = urllib.request.Request(f"{endpoint.rstrip('/')}/models", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise ProfileValidationError(f"{label} endpoint health check failed: {error}") from error
    if not isinstance(payload, dict) or not payload.get("data"):
        raise ProfileValidationError(f"{label} endpoint returned no loaded models")


def _check_gpu_vram() -> int:
    """Require a target GPU and expose its configured device-memory envelope."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        values = [int(line.strip()) for line in result.stdout.splitlines() if line.strip()]
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise ProfileValidationError(f"GPU/VRAM health check failed: {error}") from error
    if not values or any(value <= 0 for value in values):
        raise ProfileValidationError("GPU/VRAM health check found no usable NVIDIA device")
    return sum(values) * 1024 * 1024
