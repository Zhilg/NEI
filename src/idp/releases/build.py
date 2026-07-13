"""Connected-host release bundle assembly from an explicit asset specification."""

from __future__ import annotations

import shutil
from pathlib import Path, PurePosixPath

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel, ConfigDict, Field, field_validator

from idp.releases.manifest import asset_from_file, sign_manifest, write_manifest
from idp.releases.models import MANIFEST_VERSION, ReleaseAssetKind, ReleaseManifest


class ReleaseAssetInput(BaseModel):
    """One connected-host source file and its immutable target path in the bundle."""

    model_config = ConfigDict(frozen=True)

    source: Path
    target: str = Field(min_length=1)
    kind: ReleaseAssetKind
    required: bool = True
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("target")
    @classmethod
    def require_safe_target(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or path == PurePosixPath("."):
            msg = f"release asset target must be a safe relative path: {value!r}"
            raise ValueError(msg)
        return str(path)


class ReleaseBuildSpec(BaseModel):
    """Build input intentionally contains every asset; globbing is prohibited."""

    model_config = ConfigDict(frozen=True)

    release_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,127}$")
    pipeline_profile_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_revision: str = Field(min_length=7, max_length=128)
    assets: tuple[ReleaseAssetInput, ...] = Field(min_length=1)

    @field_validator("assets")
    @classmethod
    def require_unique_targets(
        cls, assets: tuple[ReleaseAssetInput, ...]
    ) -> tuple[ReleaseAssetInput, ...]:
        targets = [asset.target for asset in assets]
        if len(targets) != len(set(targets)):
            msg = "release build specification contains duplicate targets"
            raise ValueError(msg)
        return assets


def build_bundle(
    spec: ReleaseBuildSpec, output_directory: Path, signing_key: Ed25519PrivateKey
) -> ReleaseManifest:
    """Create a self-contained, signed release directory without network access."""
    output = output_directory.resolve()
    if output.exists():
        msg = f"refusing to overwrite release output directory: {output}"
        raise FileExistsError(msg)
    output.mkdir(parents=True)
    assets = []
    try:
        for input_asset in spec.assets:
            source = input_asset.source.resolve()
            if not source.is_file() or source.is_symlink():
                msg = f"release source must be a regular non-symlink file: {source}"
                raise ValueError(msg)
            target = output / input_asset.target
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            assets.append(
                asset_from_file(
                    bundle_root=output,
                    path=target,
                    kind=input_asset.kind,
                    required=input_asset.required,
                    metadata=input_asset.metadata,
                )
            )
        unsigned = ReleaseManifest(
            manifest_version=MANIFEST_VERSION,
            release_id=spec.release_id,
            pipeline_profile_hash=spec.pipeline_profile_hash,
            source_revision=spec.source_revision,
            assets=tuple(assets),
            signature="unsigned",
        )
        manifest = sign_manifest(unsigned, signing_key)
        write_manifest(output / "manifest.json", manifest)
        return manifest
    except Exception:
        shutil.rmtree(output, ignore_errors=True)
        raise
