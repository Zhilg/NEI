"""Atomic final bundle publication across object storage and PostgreSQL."""

from __future__ import annotations

import json
from datetime import datetime
from uuid import UUID

from idp.domain.models import ArtifactReference, Entity, FinalManifest
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
    ) -> FinalManifest:
        """Publish final Markdown, entities and manifest through immutable object keys."""
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
        manifest = FinalManifest(
            source_sha256=source_sha256,
            pipeline_profile_hash=pipeline_profile_hash,
            quality=quality,
            final_markdown=markdown_artifact.reference,
            entities=entities_artifact.reference,
            schema_version=schema_version,
            reconstruction=reconstruction,
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
        for artifact in (markdown_artifact, entities_artifact, manifest_artifact):
            if not self._artifacts.exists(artifact.reference):
                raise RuntimeError(f"final artifact failed integrity verification: {artifact.reference.object_key}")
        self._repository.commit_publication(
            item_id=item_id,
            bundle_prefix=prefix,
            manifest=manifest,
            artifacts=(markdown_artifact, entities_artifact, manifest_artifact),
            entities=entities,
            schema_version=schema_version,
        )
        return manifest


def _evidence_coverage(markdown: str, entities: tuple[Entity, ...]) -> float:
    """Expose a conservative evidence signal without changing technical publication success."""
    if not entities:
        return 1.0
    verified = sum(entity.evidence in markdown for entity in entities)
    return verified / len(entities)
