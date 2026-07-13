from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from idp.releases.build import ReleaseAssetInput, ReleaseBuildSpec, build_bundle
from idp.releases.lifecycle import ReleaseLifecycleError, ReleaseManager
from idp.releases.manifest import (
    ReleaseVerificationError,
    load_manifest,
    load_public_verification_key,
    verify_bundle,
)
from idp.releases.models import ReleaseAssetKind


def _keys(tmp_path: Path) -> tuple[Path, Ed25519PrivateKey]:
    private = Ed25519PrivateKey.generate()
    private_path = tmp_path / "release-private.pem"
    private_path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return private_path, private


def test_target_public_key_can_verify_without_build_private_key(tmp_path: Path) -> None:
    _, private = _keys(tmp_path)
    public_path = tmp_path / "release-public.pem"
    public_path.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    bundle = _bundle(tmp_path, private)

    report = verify_bundle(
        bundle,
        load_manifest(bundle / "manifest.json"),
        load_public_verification_key(public_path),
    )

    assert report.release_id == "release-001"


def _bundle(tmp_path: Path, private: Ed25519PrivateKey, release_id: str = "release-001") -> Path:
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    asset = source / "controller.whl"
    asset.write_bytes(b"offline-wheelhouse-asset")
    output = tmp_path / release_id
    build_bundle(
        ReleaseBuildSpec(
            release_id=release_id,
            pipeline_profile_hash="a" * 64,
            source_revision="abcdef1",
            assets=(
                ReleaseAssetInput(
                    source=asset,
                    target="wheels/controller.whl",
                    kind=ReleaseAssetKind.PYTHON_WHEEL,
                ),
            ),
        ),
        output,
        private,
    )
    return output


def test_bundle_signature_and_assets_verify_offline(tmp_path: Path) -> None:
    _, private = _keys(tmp_path)
    bundle = _bundle(tmp_path, private)
    manifest = load_manifest(bundle / "manifest.json")

    report = verify_bundle(bundle, manifest, private.public_key())

    assert report.release_id == "release-001"
    assert report.verified_assets == 1


def test_modified_asset_is_rejected_before_import(tmp_path: Path) -> None:
    _, private = _keys(tmp_path)
    bundle = _bundle(tmp_path, private)
    (bundle / "wheels" / "controller.whl").write_bytes(b"tampered")

    with pytest.raises(ReleaseVerificationError, match="SHA-256 mismatch"):
        verify_bundle(bundle, load_manifest(bundle / "manifest.json"), private.public_key())


def test_import_activate_and_rollback_switch_only_verified_releases(tmp_path: Path) -> None:
    _, private = _keys(tmp_path)
    first = _bundle(tmp_path, private, "release-001")
    second = _bundle(tmp_path, private, "release-002")
    manager = ReleaseManager(tmp_path / "target", private.public_key())

    manager.import_bundle(first)
    manager.import_bundle(second)
    manager.activate("release-002")

    assert manager.active_manifest().release_id == "release-002"
    manager.rollback("release-001")
    assert manager.active_manifest().release_id == "release-001"


def test_import_rejects_bundle_with_symlink_even_when_manifest_is_valid(tmp_path: Path) -> None:
    _, private = _keys(tmp_path)
    bundle = _bundle(tmp_path, private)
    (bundle / "unexpected-link").symlink_to(bundle / "wheels" / "controller.whl")
    manager = ReleaseManager(tmp_path / "target", private.public_key())

    with pytest.raises(ReleaseLifecycleError, match="forbidden symbolic link"):
        manager.import_bundle(bundle)
