"""Batch submission and reporting service built on safe discovery and durable storage."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from idp.config import Settings
from idp.domain.models import ArtifactReference, BatchSnapshot
from idp.domain.states import ArtifactRetention, BatchItemState
from idp.persistence.repository import SqlAlchemyBatchRepository
from idp.services.discovery import (
    BatchDiscovery,
    DiscoveryLimits,
    normalize_allowed_root,
    normalize_submitted_root,
)
from idp.ports.artifact_store import ArtifactStore


@dataclass(frozen=True)
class BatchSubmission:
    """The accepted immutable batch identity and its snapshot counters."""

    batch_id: UUID
    total_items: int
    queued_items: int
    skipped_items: int


class BatchService:
    """Coordinates safe source discovery, immutable source upload, and submit persistence."""

    def __init__(
        self,
        settings: Settings,
        repository: SqlAlchemyBatchRepository,
        artifact_store: ArtifactStore,
        staging_root: Path,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._artifact_store = artifact_store
        self._staging_root = staging_root

    def submit(self, roots: tuple[Path, ...], profile: str) -> BatchSubmission:
        """Create a durable batch only after stable sources are copied to artifact storage."""
        allowed_roots = tuple(normalize_allowed_root(root) for root in self._settings.allowed_roots)
        if not allowed_roots:
            raise ValueError("no IDP_ALLOWED_ROOTS are configured")
        normalized_roots = tuple(
            normalize_submitted_root(root, allowed_roots) for root in roots
        )
        profile_hash = self._repository.resolve_profile_hash(profile)
        limits = DiscoveryLimits(
            stability_seconds=self._settings.scan_stability_seconds,
            max_file_bytes=self._settings.scan_max_file_bytes,
            max_candidates=self._settings.scan_max_candidates,
            max_depth=self._settings.scan_max_depth,
            hash_chunk_bytes=self._settings.scan_hash_chunk_bytes,
        )
        staging = self._staging_root / "submissions"
        result = BatchDiscovery(limits).scan_and_stage(
            roots=normalized_roots,
            profile_name=profile,
            staging_directory=staging,
        )
        try:
            sources: dict[UUID, ArtifactReference] = {}
            for item_id, staged_file in result.staged_sources.items():
                item = next(item for item in result.snapshot.items if item.item_id == item_id)
                if item.source_sha256 is None:
                    raise RuntimeError(f"stable staged source has no SHA-256: {item_id}")
                extension = staged_file.suffix.lower()
                media_type = (
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    if extension == ".docx"
                    else "application/pdf"
                )
                artifact = self._artifact_store.put_file(
                    object_key=f"sources/{result.snapshot.batch_id}/{item_id}{extension}",
                    source=staged_file,
                    media_type=media_type,
                    retention=ArtifactRetention.TEMPORARY,
                )
                if artifact.reference.sha256 != item.source_sha256:
                    raise RuntimeError(f"source object hash mismatch: {item_id}")
                sources[item_id] = artifact.reference
            self._repository.create_batch(result.snapshot, profile_hash, sources)
        finally:
            for staged_file in result.staged_sources.values():
                staged_file.unlink(missing_ok=True)
            if staging.exists() and not any(staging.iterdir()):
                staging.rmdir()
        return self._submission(result.snapshot)

    def status(self, batch_id: UUID) -> dict[str, object]:
        """Read durable batch state only from PostgreSQL."""
        return self._repository.get_batch_status(batch_id)

    def report(self, batch_id: UUID) -> list[dict[str, object]]:
        """Return all paths and dispositions in deterministic database order."""
        return self._repository.get_batch_report(batch_id)

    def cancel(self, batch_id: UUID) -> None:
        """Cancel future work and request safe checkpoint cancellation for active jobs."""
        self._repository.cancel_batch(batch_id)

    def retry(self, item_id: UUID) -> UUID:
        """Retry one quarantined item from its immutable source artifact."""
        return self._repository.retry_quarantined_item(item_id)

    @staticmethod
    def _submission(snapshot: BatchSnapshot) -> BatchSubmission:
        queued = sum(item.state == BatchItemState.QUEUED for item in snapshot.items)
        return BatchSubmission(
            batch_id=snapshot.batch_id,
            total_items=len(snapshot.items),
            queued_items=queued,
            skipped_items=len(snapshot.items) - queued,
        )
