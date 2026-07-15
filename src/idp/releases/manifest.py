"""Deterministic manifest generation, signing, and offline verification."""

from __future__ import annotations

import json
from base64 import b64decode, b64encode
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from idp.releases.models import ReleaseAsset, ReleaseManifest
from idp.services.hashing import sha256_file


class ReleaseVerificationError(RuntimeError):
    """Raised when bundle content, manifest, or activation trust checks fail."""


@dataclass(frozen=True)
class VerificationReport:
    """A complete offline verification outcome for operations and audit logs."""

    release_id: str
    verified_assets: int
    verified_bytes: int


def canonical_manifest_payload(manifest: ReleaseManifest) -> bytes:
    """Serialize unsigned fields deterministically for release signing."""
    payload = manifest.model_dump(mode="json", exclude={"signature"})
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_manifest(manifest: ReleaseManifest, signing_key: Ed25519PrivateKey) -> ReleaseManifest:
    """Sign a deterministic manifest payload using a build-only Ed25519 private key."""
    signature = b64encode(signing_key.sign(canonical_manifest_payload(manifest))).decode("ascii")
    return manifest.model_copy(update={"signature": signature, "signature_algorithm": "ed25519"})


def verify_manifest_signature(manifest: ReleaseManifest, verification_key: Ed25519PublicKey) -> None:
    """Verify a release with the target's public key before trusting assets."""
    if manifest.signature_algorithm != "ed25519":
        msg = f"unsupported release signature algorithm: {manifest.signature_algorithm}"
        raise ReleaseVerificationError(msg)
    try:
        signature = b64decode(manifest.signature.encode("ascii"), validate=True)
        verification_key.verify(signature, canonical_manifest_payload(manifest))
    except (InvalidSignature, ValueError) as error:
        msg = f"release manifest signature mismatch: {manifest.release_id}"
        raise ReleaseVerificationError(msg) from error


def load_private_signing_key(path: Path) -> Ed25519PrivateKey:
    """Load an unencrypted build-host Ed25519 key from a controlled local path."""
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as error:
        msg = f"cannot load release private signing key {path}"
        raise ReleaseVerificationError(msg) from error
    if not isinstance(key, Ed25519PrivateKey):
        msg = "release private signing key must be Ed25519"
        raise ReleaseVerificationError(msg)
    return key


def load_public_verification_key(path: Path) -> Ed25519PublicKey:
    """Load the target-safe Ed25519 public verification key from a local path."""
    try:
        key = serialization.load_pem_public_key(path.read_bytes())
    except (OSError, ValueError, TypeError) as error:
        msg = f"cannot load release public verification key {path}"
        raise ReleaseVerificationError(msg) from error
    if not isinstance(key, Ed25519PublicKey):
        msg = "release public verification key must be Ed25519"
        raise ReleaseVerificationError(msg)
    return key


def write_manifest(path: Path, manifest: ReleaseManifest) -> None:
    """Write the release manifest in deterministic JSON form."""
    path.write_text(
        json.dumps(manifest.model_dump(mode="json"), ensure_ascii=True, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def load_manifest(path: Path) -> ReleaseManifest:
    """Load and validate a manifest without following any asset paths yet."""
    try:
        return ReleaseManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except OSError as error:
        msg = f"cannot read release manifest {path}: {error}"
        raise ReleaseVerificationError(msg) from error


def verify_bundle(
    bundle_root: Path, manifest: ReleaseManifest, verification_key: Ed25519PublicKey
) -> VerificationReport:
    """Verify signature, every required file, size, and SHA-256 offline."""
    verify_manifest_signature(manifest, verification_key)
    root = bundle_root.resolve()
    verified_bytes = 0
    for asset in manifest.assets:
        candidate = (root / asset.path).resolve()
        if root not in candidate.parents:
            msg = f"release asset escapes bundle root: {asset.path}"
            raise ReleaseVerificationError(msg)
        if not candidate.is_file():
            if asset.required:
                msg = f"required release asset is missing: {asset.path}"
                raise ReleaseVerificationError(msg)
            continue
        digest, size_bytes = sha256_file(candidate)
        if size_bytes != asset.size_bytes:
            msg = f"release asset size mismatch: {asset.path}"
            raise ReleaseVerificationError(msg)
        if digest != asset.sha256:
            msg = f"release asset SHA-256 mismatch: {asset.path}"
            raise ReleaseVerificationError(msg)
        verified_bytes += size_bytes
    return VerificationReport(
        release_id=manifest.release_id,
        verified_assets=len(manifest.assets),
        verified_bytes=verified_bytes,
    )


def asset_from_file(
    *,
    bundle_root: Path,
    path: Path,
    kind: str,
    required: bool = True,
    metadata: dict[str, str] | None = None,
) -> ReleaseAsset:
    """Build an asset record from a file that already lives inside a bundle root."""
    root = bundle_root.resolve()
    candidate = path.resolve()
    if root not in candidate.parents:
        msg = f"release asset must be inside bundle root: {path}"
        raise ValueError(msg)
    digest, size_bytes = sha256_file(candidate)
    return ReleaseAsset(
        path=candidate.relative_to(root).as_posix(),
        kind=kind,
        sha256=digest,
        size_bytes=size_bytes,
        required=required,
        metadata=metadata or {},
    )
