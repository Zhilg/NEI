"""Atomic final bundle publication across object storage and PostgreSQL."""

from __future__ import annotations

import json
import tempfile
from datetime import datetime
from pathlib import Path
from uuid import UUID

from idp.domain.models import ArtifactReference, Entity, FinalManifest, StoredArtifact
from idp.domain.states import ArtifactRetention, QualityState
from idp.ports.artifact_store import ArtifactStore
from idp.ports.batch_repository import BatchRepository
from idp.storage import validate_object_key


class FinalBundlePublisher:
    """Write immutable objects first, then expose one transactionally guarded pointer."""

    def __init__(self, artifacts: ArtifactStore, repository: BatchRepository) -> None:
        self._artifacts = artifacts
        self._repository = repository

    def publish(
        self,
        *,
        item_id: UUID,
        bundle_prefix: str,
        source_sha256: str,
        pipeline_profile_hash: str,
        quality: QualityState,
        markdown: str,
        entities: tuple[Entity, ...],
        schema_version: str,
        reconstruction: ArtifactReference | None = None,
        model_versions: dict[str, str] | None = None,
        findings: tuple[dict[str, object], ...] = (),
        created_at: datetime | None = None,
        job_id: UUID | None = None,
        worker_id: str | None = None,
    ) -> FinalManifest:
        """Publish final Markdown, entities and manifest through immutable object keys."""
        if (job_id is None) != (worker_id is None):
            raise ValueError("publication job_id and worker_id must be supplied together")
        prefix = str(validate_object_key(bundle_prefix)).rstrip("/")
        markdown_artifact = self._artifacts.put_bytes(
            object_key=f"{prefix}/final.md",
            payload=markdown.encode("utf-8"),
            media_type="text/markdown; charset=utf-8",
            retention=ArtifactRetention.FINAL,
        )
        entities_artifact = self._artifacts.put_bytes(
            object_key=f"{prefix}/entities.json",
            payload=json.dumps(
                {
                    "schema_version": schema_version,
                    "entities": [entity.model_dump(mode="json") for entity in entities],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            media_type="application/json",
            retention=ArtifactRetention.FINAL,
        )
        final_reconstruction = self._copy_final_provenance(prefix, reconstruction)
        manifest = FinalManifest(
            source_sha256=source_sha256,
            pipeline_profile_hash=pipeline_profile_hash,
            quality=quality,
            final_markdown=markdown_artifact.reference,
            entities=entities_artifact.reference,
            schema_version=schema_version,
            reconstruction=None if final_reconstruction is None else final_reconstruction.reference,
            model_versions=model_versions or {},
            findings=findings,
            evidence_coverage=_evidence_coverage(markdown, entities),
            **({"created_at": created_at} if created_at is not None else {}),
        )
        manifest_artifact = self._artifacts.put_bytes(
            object_key=f"{prefix}/manifest.json",
            payload=json.dumps(
                manifest.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            media_type="application/json",
            retention=ArtifactRetention.FINAL,
        )
        for artifact in (markdown_artifact, entities_artifact, manifest_artifact, final_reconstruction):
            if artifact is None:
                continue
            if not self._artifacts.exists(artifact.reference):
                raise RuntimeError(f"final artifact failed integrity verification: {artifact.reference.object_key}")
        commit_arguments: dict[str, object] = {
            "item_id": item_id,
            "bundle_prefix": prefix,
            "manifest": manifest,
            "artifacts": tuple(
                artifact
                for artifact in (markdown_artifact, entities_artifact, manifest_artifact, final_reconstruction)
                if artifact is not None
            ),
            "entities": entities,
            "schema_version": schema_version,
        }
        if job_id is not None and worker_id is not None:
            commit_arguments["job_id"] = job_id
            commit_arguments["worker_id"] = worker_id
        self._repository.commit_publication(
            **commit_arguments,  # type: ignore[arg-type]
        )
        return manifest

    def _copy_final_provenance(
        self, prefix: str, reconstruction: ArtifactReference | None
    ) -> StoredArtifact | None:
        """Keep the reconstruction contract immutable after temporary retention cleanup."""
        if reconstruction is None:
            return None
        with tempfile.TemporaryDirectory(prefix="idp-final-provenance-") as temporary:
            source = Path(temporary) / "reconstruction_manifest.json"
            self._artifacts.get_file(reconstruction, source)
            artifact = self._artifacts.put_file(
                object_key=f"{prefix}/reconstruction_manifest.json",
                source=source,
                media_type="application/json",
                retention=ArtifactRetention.FINAL,
            )
        return artifact


def _evidence_coverage(markdown: str, entities: tuple[Entity, ...]) -> float:
    """Expose a conservative evidence signal without changing technical publication success."""
    if not entities:
        return 1.0
    verified = sum(entity.evidence in markdown for entity in entities)
    return verified / len(entities)
