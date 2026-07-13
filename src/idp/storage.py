"""Local and MinIO artifact stores with immutable object-key semantics."""

from __future__ import annotations

from io import BufferedReader, BytesIO
from pathlib import Path, PurePosixPath
from shutil import copyfileobj
from typing import Mapping

from minio import Minio
from minio.error import S3Error

from idp.domain.models import ArtifactReference, StoredArtifact
from idp.domain.states import ArtifactRetention
from idp.ports.artifact_store import ArtifactStore
from idp.services.hashing import sha256_bytes, sha256_file


class ArtifactStoreError(RuntimeError):
    """Raised when an artifact operation violates integrity or retention rules."""


def validate_object_key(object_key: str) -> PurePosixPath:
    """Reject absolute and traversal keys before filesystem or S3 access."""
    key = PurePosixPath(object_key)
    if not object_key or key.is_absolute() or ".." in key.parts or key == PurePosixPath("."):
        msg = f"invalid artifact object key: {object_key!r}"
        raise ArtifactStoreError(msg)
    return key


class LocalArtifactStore(ArtifactStore):
    """Filesystem store used by lightweight tests and offline maintenance tools."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def put_file(
        self,
        *,
        object_key: str,
        source: Path,
        media_type: str,
        retention: ArtifactRetention,
    ) -> StoredArtifact:
        digest, size_bytes = sha256_file(source)
        target = self._target(object_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            self._assert_hash(target, digest)
        else:
            try:
                with source.open("rb") as input_file, target.open("xb") as output_file:
                    copyfileobj(input_file, output_file)
            except FileExistsError:
                self._assert_hash(target, digest)
            else:
                self._assert_hash(target, digest)
        return self._artifact(object_key, digest, media_type, size_bytes, retention)

    def put_bytes(
        self,
        *,
        object_key: str,
        payload: bytes,
        media_type: str,
        retention: ArtifactRetention,
    ) -> StoredArtifact:
        digest = sha256_bytes(payload)
        target = self._target(object_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            self._assert_hash(target, digest)
        else:
            try:
                with target.open("xb") as output_file:
                    output_file.write(payload)
            except FileExistsError:
                self._assert_hash(target, digest)
        return self._artifact(object_key, digest, media_type, len(payload), retention)

    def exists(self, reference: ArtifactReference) -> bool:
        target = self._target(reference.object_key)
        if not target.is_file():
            return False
        actual, _ = sha256_file(target)
        return actual == reference.sha256

    def get_file(self, reference: ArtifactReference, target: Path) -> None:
        """Copy a verified artifact into a new local file without path traversal."""
        source = self._target(reference.object_key)
        self._assert_hash(source, reference.sha256)
        target.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as input_file, target.open("xb") as output_file:
            copyfileobj(input_file, output_file)

    def delete(self, artifact: StoredArtifact) -> None:
        if artifact.retention != ArtifactRetention.TEMPORARY:
            msg = "refusing to delete a final artifact"
            raise ArtifactStoreError(msg)
        target = self._target(artifact.reference.object_key)
        if target.exists():
            self._assert_hash(target, artifact.reference.sha256)
            target.unlink()

    def _target(self, object_key: str) -> Path:
        target = (self._root / validate_object_key(object_key)).resolve()
        if target != self._root and self._root not in target.parents:
            msg = f"artifact path escapes storage root: {object_key!r}"
            raise ArtifactStoreError(msg)
        return target

    @staticmethod
    def _assert_hash(target: Path, expected: str) -> None:
        actual, _ = sha256_file(target)
        if actual != expected:
            msg = f"immutable artifact key collision: {target}"
            raise ArtifactStoreError(msg)

    @staticmethod
    def _artifact(
        object_key: str,
        digest: str,
        media_type: str,
        size_bytes: int,
        retention: ArtifactRetention,
    ) -> StoredArtifact:
        return StoredArtifact(
            reference=ArtifactReference(
                object_key=object_key,
                sha256=digest,
                media_type=media_type,
            ),
            size_bytes=size_bytes,
            retention=retention,
        )


class MinioArtifactStore(ArtifactStore):
    """MinIO adapter; the target bucket must enforce versioning/object lock at provisioning."""

    def __init__(self, client: Minio, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    def ensure_bucket(self) -> None:
        """Create the storage bucket during controlled setup, never during worker execution."""
        if not self._client.bucket_exists(self._bucket):
            self._client.make_bucket(self._bucket)

    def put_file(
        self,
        *,
        object_key: str,
        source: Path,
        media_type: str,
        retention: ArtifactRetention,
    ) -> StoredArtifact:
        digest, size_bytes = sha256_file(source)
        with source.open("rb") as source_file:
            self._put(object_key, source_file, size_bytes, digest, media_type)
        return LocalArtifactStore._artifact(object_key, digest, media_type, size_bytes, retention)

    def put_bytes(
        self,
        *,
        object_key: str,
        payload: bytes,
        media_type: str,
        retention: ArtifactRetention,
    ) -> StoredArtifact:
        digest = sha256_bytes(payload)
        self._put(object_key, BytesIO(payload), len(payload), digest, media_type)
        return LocalArtifactStore._artifact(object_key, digest, media_type, len(payload), retention)

    def exists(self, reference: ArtifactReference) -> bool:
        validate_object_key(reference.object_key)
        try:
            stat = self._client.stat_object(self._bucket, reference.object_key)
        except S3Error as error:
            if error.code in {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}:
                return False
            raise ArtifactStoreError(str(error)) from error
        return self._metadata_value(stat.metadata, "sha256") == reference.sha256

    def get_file(self, reference: ArtifactReference, target: Path) -> None:
        """Download a local MinIO object and verify its content hash before use."""
        validate_object_key(reference.object_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            response = self._client.get_object(self._bucket, reference.object_key)
            with target.open("xb") as output_file:
                for chunk in response.stream(amt=1024 * 1024):
                    output_file.write(chunk)
            response.close()
            response.release_conn()
        except (OSError, S3Error) as error:
            target.unlink(missing_ok=True)
            raise ArtifactStoreError(str(error)) from error
        actual, _ = sha256_file(target)
        if actual != reference.sha256:
            target.unlink(missing_ok=True)
            msg = f"downloaded artifact SHA-256 mismatch: {reference.object_key}"
            raise ArtifactStoreError(msg)

    def delete(self, artifact: StoredArtifact) -> None:
        if artifact.retention != ArtifactRetention.TEMPORARY:
            msg = "refusing to delete a final artifact"
            raise ArtifactStoreError(msg)
        if self.exists(artifact.reference):
            self._client.remove_object(self._bucket, artifact.reference.object_key)

    def _put(
        self,
        object_key: str,
        stream: BufferedReader | BytesIO,
        size_bytes: int,
        digest: str,
        media_type: str,
    ) -> None:
        validate_object_key(object_key)
        try:
            existing = self._client.stat_object(self._bucket, object_key)
        except S3Error as error:
            if error.code not in {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}:
                raise ArtifactStoreError(str(error)) from error
            existing = None
        if existing is not None:
            if self._metadata_value(existing.metadata, "sha256") == digest:
                return
            msg = f"immutable artifact key collision: {object_key}"
            raise ArtifactStoreError(msg)
        try:
            self._client.put_object(
                self._bucket,
                object_key,
                stream,
                size_bytes,
                content_type=media_type,
                metadata={"sha256": digest},
            )
        except S3Error as error:
            raise ArtifactStoreError(str(error)) from error

    @staticmethod
    def _metadata_value(metadata: Mapping[str, str], key: str) -> str | None:
        expected = {key.lower(), f"x-amz-meta-{key}".lower()}
        for candidate, value in metadata.items():
            if candidate.lower() in expected:
                return value
        return None
