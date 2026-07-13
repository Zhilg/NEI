"""Immutable artifact-store interface for workers and final publication."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from idp.domain.models import ArtifactReference, StoredArtifact
from idp.domain.states import ArtifactRetention


class ArtifactStore(Protocol):
    """Object storage boundary with content-addressed artifact verification."""

    def put_file(
        self,
        *,
        object_key: str,
        source: Path,
        media_type: str,
        retention: ArtifactRetention,
    ) -> StoredArtifact:
        """Store a file under an immutable key and return its verified identity."""

    def put_bytes(
        self,
        *,
        object_key: str,
        payload: bytes,
        media_type: str,
        retention: ArtifactRetention,
    ) -> StoredArtifact:
        """Store a generated payload such as Markdown or a manifest."""

    def exists(self, reference: ArtifactReference) -> bool:
        """Verify that a referenced immutable object exists with its expected hash."""

    def delete(self, artifact: StoredArtifact) -> None:
        """Delete only a retention-eligible temporary artifact."""
