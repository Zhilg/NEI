"""Versioned manifest contract for a verified offline release bundle."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator


MANIFEST_VERSION = 1


class ReleaseAssetKind(StrEnum):
    """Classes of assets that are delivered without runtime network access."""

    OCI_IMAGE = "oci_image"
    PYTHON_WHEEL = "python_wheel"
    OS_PACKAGE = "os_package"
    MODEL = "model"
    TOKENIZER = "tokenizer"
    OCR_DICTIONARY = "ocr_dictionary"
    CONFIGURATION = "configuration"
    TEST_CORPUS = "test_corpus"
    SBOM = "sbom"
    LICENSES = "licenses"


class ReleaseAsset(BaseModel):
    """One immutable file in a release bundle."""

    model_config = ConfigDict(frozen=True)

    path: str = Field(min_length=1)
    kind: ReleaseAssetKind
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0)
    required: bool = True
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("path")
    @classmethod
    def require_safe_relative_path(cls, value: str) -> str:
        """Forbid traversal and absolute paths before a bundle is imported."""
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or path == PurePosixPath("."):
            msg = f"release asset path must be a safe relative path: {value!r}"
            raise ValueError(msg)
        return str(path)


class ReleaseManifest(BaseModel):
    """Signed-style release inventory consumed on an offline target server."""

    model_config = ConfigDict(frozen=True)

    manifest_version: int = MANIFEST_VERSION
    release_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,127}$")
    pipeline_profile_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source_revision: str = Field(min_length=7, max_length=128)
    assets: tuple[ReleaseAsset, ...] = Field(min_length=1)
    signature_algorithm: str = "ed25519"
    signature: str = Field(min_length=1)

    @field_validator("assets")
    @classmethod
    def require_unique_asset_paths(cls, assets: tuple[ReleaseAsset, ...]) -> tuple[ReleaseAsset, ...]:
        """Prevent shadowed paths inside an otherwise valid manifest."""
        paths = [asset.path for asset in assets]
        if len(paths) != len(set(paths)):
            msg = "release manifest contains duplicate asset paths"
            raise ValueError(msg)
        return assets
