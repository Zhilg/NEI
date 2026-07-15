"""Build, import, activate, and rollback immutable offline release bundles."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from idp.releases.manifest import (
    ReleaseVerificationError,
    VerificationReport,
    load_manifest,
    verify_bundle,
)
from idp.releases.models import ReleaseAssetKind, ReleaseManifest


class ReleaseLifecycleError(RuntimeError):
    """Raised when an immutable release cannot be safely built or activated."""


class ReleaseManager:
    """Manages local target releases through verify-first and atomic filesystem operations."""

    def __init__(
        self,
        release_root: Path,
        verification_key: Ed25519PublicKey,
        *,
        container_runtime: str = "docker",
    ) -> None:
        self._root = release_root.resolve()
        self._key = verification_key
        self._container_runtime = container_runtime

    @property
    def releases_directory(self) -> Path:
        """Directory containing immutable imported release directories."""
        return self._root / "releases"

    @property
    def active_link(self) -> Path:
        """Atomic symlink pointer consumed by runtime deployment tooling."""
        return self._root / "active"

    def import_bundle(self, bundle_root: Path) -> VerificationReport:
        """Copy an already verified bundle through staging, then atomically import it."""
        source = bundle_root.resolve()
        manifest = load_manifest(source / "manifest.json")
        report = verify_bundle(source, manifest, self._key)
        self.releases_directory.mkdir(parents=True, exist_ok=True)
        destination = self.releases_directory / manifest.release_id
        if destination.exists():
            existing = self._verified_manifest(destination)
            if existing.model_dump(mode="json") != manifest.model_dump(mode="json"):
                msg = f"release ID collision with different manifest: {manifest.release_id}"
                raise ReleaseLifecycleError(msg)
            return verify_bundle(destination, existing, self._key)

        staging = self.releases_directory / f".{manifest.release_id}.importing"
        if staging.exists():
            shutil.rmtree(staging)
        try:
            self._copy_bundle(source, staging)
            imported_manifest = load_manifest(staging / "manifest.json")
            verify_bundle(staging, imported_manifest, self._key)
            self._load_oci_images(staging, imported_manifest)
            os.replace(staging, destination)
        except OSError as error:
            msg = f"cannot import release {manifest.release_id}: {error}"
            raise ReleaseLifecycleError(msg) from error
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return report

    def activate(self, release_id: str) -> VerificationReport:
        """Verify an imported release again and atomically make it active."""
        release_dir = self._release_directory(release_id)
        manifest = self._verified_manifest(release_dir)
        report = verify_bundle(release_dir, manifest, self._key)
        self._root.mkdir(parents=True, exist_ok=True)
        temporary_link = self._root / f".active.{release_id}.tmp"
        if temporary_link.exists() or temporary_link.is_symlink():
            temporary_link.unlink()
        relative_target = Path("releases") / release_id
        temporary_link.symlink_to(relative_target)
        os.replace(temporary_link, self.active_link)
        return report

    def rollback(self, release_id: str) -> VerificationReport:
        """Rollback is an activation of a previously imported immutable release."""
        return self.activate(release_id)

    def verify_imported(self, release_id: str) -> VerificationReport:
        """Verify one imported release without changing the active pointer."""
        release_dir = self._release_directory(release_id)
        manifest = self._verified_manifest(release_dir)
        return verify_bundle(release_dir, manifest, self._key)

    def imported_manifest(self, release_id: str) -> ReleaseManifest:
        """Return a verified imported manifest without exposing lifecycle internals."""
        return self._verified_manifest(self._release_directory(release_id))

    def active_manifest(self) -> ReleaseManifest:
        """Load the trusted manifest for the currently activated local release."""
        if not self.active_link.is_symlink():
            msg = "no active release is configured"
            raise ReleaseLifecycleError(msg)
        active_dir = self.active_link.resolve(strict=True)
        if self.releases_directory.resolve() not in active_dir.parents:
            msg = "active release link points outside managed release directory"
            raise ReleaseLifecycleError(msg)
        return self._verified_manifest(active_dir)

    def list_releases(self) -> tuple[str, ...]:
        """List completed imported releases without treating staging directories as releases."""
        if not self.releases_directory.is_dir():
            return ()
        return tuple(
            sorted(
                directory.name
                for directory in self.releases_directory.iterdir()
                if directory.is_dir() and not directory.name.startswith(".")
            )
        )

    def _release_directory(self, release_id: str) -> Path:
        candidate = (self.releases_directory / release_id).resolve()
        if self.releases_directory.resolve() not in candidate.parents or not candidate.is_dir():
            msg = f"unknown imported release: {release_id}"
            raise ReleaseLifecycleError(msg)
        return candidate

    def _verified_manifest(self, release_dir: Path) -> ReleaseManifest:
        try:
            manifest = load_manifest(release_dir / "manifest.json")
            verify_bundle(release_dir, manifest, self._key)
            return manifest
        except ReleaseVerificationError as error:
            raise ReleaseLifecycleError(str(error)) from error

    @staticmethod
    def _copy_bundle(source: Path, destination: Path) -> None:
        """Copy regular files only, rejecting symlinks before the target trusts them."""
        for path in source.rglob("*"):
            if path.is_symlink():
                msg = f"release bundle contains forbidden symbolic link: {path}"
                raise ReleaseLifecycleError(msg)
        shutil.copytree(source, destination, copy_function=shutil.copy2)

    def _load_oci_images(self, bundle_root: Path, manifest: ReleaseManifest) -> None:
        """Load verified local image archives before a release becomes importable.

        A host-native release may have no OCI asset. The command never receives a
        network reference: only a verified archive path from the imported bundle.
        """
        for asset in manifest.assets:
            if asset.kind != ReleaseAssetKind.OCI_IMAGE:
                continue
            archive = bundle_root / asset.path
            try:
                subprocess.run(
                    [self._container_runtime, "image", "load", "--input", str(archive)],
                    check=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=1800,
                )
            except (OSError, subprocess.SubprocessError) as error:
                msg = f"cannot load verified OCI image {asset.path}: {error}"
                raise ReleaseLifecycleError(msg) from error
