"""Grounded, schema-driven entity extraction using a local Qwen3/Fenic endpoint."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal, Mapping, Protocol, Sequence
from urllib.parse import urlparse
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from idp.domain.models import ArtifactReference, Entity, ResourceRequest, StoredArtifact
from idp.domain.states import ArtifactRetention, JobState, ReservationKind
from idp.persistence.repository import ResourceCapacityError, SqlAlchemyBatchRepository
from idp.ports.artifact_store import ArtifactStore
from idp.services.qwen_vl import ReconstructionManifest


class EntityExtractionError(RuntimeError):
    """Raised when the configured local extraction service cannot return JSON."""


EntityKind = Literal["person", "organization", "date", "address", "identifier", "amount"]
SUPPORTED_ENTITY_TYPES: tuple[EntityKind, ...] = (
    "person",
    "organization",
    "date",
    "address",
    "identifier",
    "amount",
)


class EntityCandidateV1(BaseModel):
    """The only entity record shape accepted from the extraction model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: EntityKind
    value: str = Field(min_length=1)
    normalized_value: str | None = None
    page: int = Field(ge=1)
    block_id: str = Field(min_length=1)
    bbox: tuple[float, float, float, float]
    evidence: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)


class EntityResponseV1(BaseModel):
    """Pydantic response contract supplied to structured extraction backends."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entities: tuple[EntityCandidateV1, ...]


@dataclass(frozen=True)
class EntitySchemaPack:
    """A versioned, closed entity schema passed verbatim to the local model."""

    version: str
    entity_types: tuple[EntityKind, ...]

    def response_schema(self) -> dict[str, Any]:
        """Return the Pydantic JSON schema required by OpenAI-compatible endpoints."""
        return EntityResponseV1.model_json_schema()


ENTITY_SCHEMA_V1 = EntitySchemaPack(
    version="entity-v1",
    entity_types=SUPPORTED_ENTITY_TYPES,
)


@dataclass(frozen=True)
class EntityFinding:
    """A rejected extraction candidate with stable, machine-readable provenance."""

    code: str
    detail: str
    candidate_index: int | None


@dataclass(frozen=True)
class EntityExtractionResult:
    """Validated entity output; rejected candidates are retained as findings."""

    schema_version: str
    entities: tuple[Entity, ...]
    findings: tuple[EntityFinding, ...]


@dataclass(frozen=True)
class EntityManifest:
    """Persisted entity result tied to the exact reconstruction used as evidence."""

    schema_version: str
    reconstruction: ArtifactReference
    entities: tuple[Entity, ...]
    findings: tuple[EntityFinding, ...]


class EntityExtractionClient(Protocol):
    """Fenic-compatible boundary for schema-driven, local structured extraction."""

    def extract(
        self, context: Mapping[str, Any], schema: EntitySchemaPack
    ) -> Mapping[str, Any]:
        """Return a JSON object generated from the supplied grounded Markdown blocks."""


class LocalOpenAIQwen3Client:
    """OpenAI-compatible Qwen3 client limited to approved local/internal HTTP services."""

    _ALLOWED_HOSTS = frozenset(
        {
            "qwen3",
            "qwen3-fenic",
            "qwen3-vllm",
            "fenic",
            "localhost",
            "127.0.0.1",
            "::1",
        }
    )

    def __init__(self, *, endpoint: str, model_id: str, timeout_seconds: float) -> None:
        parsed = urlparse(endpoint)
        if parsed.scheme != "http" or parsed.hostname not in self._ALLOWED_HOSTS:
            raise ValueError("Qwen3 client endpoint must be a local/internal HTTP service")
        if not model_id:
            raise ValueError("Qwen3 model ID must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("Qwen3 timeout must be positive")
        self._endpoint = endpoint.rstrip("/")
        self._model_id = model_id
        self._timeout = timeout_seconds

    def extract(self, context: Mapping[str, Any], schema: EntitySchemaPack) -> Mapping[str, Any]:
        """Call only the internal chat endpoint with deterministic JSON-schema decoding."""
        payload = {
            "model": self._model_id,
            "temperature": 0,
            "top_p": 1,
            "seed": 0,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.version.replace("-", "_"),
                    "strict": True,
                    "schema": schema.response_schema(),
                },
            },
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a grounded entity extraction engine. Return only a JSON object "
                        "that conforms to the supplied schema. Extract only entities supported by "
                        "the schema. Every entity must cite exact evidence from its supplied block."
                    ),
                },
                {"role": "user", "content": _prompt(context, schema)},
            ],
        }
        request = urllib.request.Request(
            f"{self._endpoint}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                response_payload = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise EntityExtractionError(f"local Qwen3 extraction request failed: {error}") from error
        try:
            content = response_payload["choices"][0]["message"]["content"]
            if isinstance(content, Mapping):
                return content
            if not isinstance(content, str):
                raise TypeError("content is not a JSON string or object")
            return _decode_json_object(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
            raise EntityExtractionError("local Qwen3 response has no valid JSON content") from error


class EntityExtractor:
    """Extract closed-schema entities and independently validate every candidate."""

    def __init__(
        self,
        *,
        client: EntityExtractionClient,
        schema: EntitySchemaPack = ENTITY_SCHEMA_V1,
    ) -> None:
        self._client = client
        self._schema = schema

    def extract(self, reconstruction: ReconstructionManifest) -> EntityExtractionResult:
        """Return every valid entity even when neighboring candidates are malformed."""
        context = _context(reconstruction, self._schema)
        response = self._client.extract(context, self._schema)
        raw_entities = response.get("entities") if isinstance(response, Mapping) else None
        if not isinstance(raw_entities, list):
            return EntityExtractionResult(
                schema_version=self._schema.version,
                entities=(),
                findings=(
                    EntityFinding(
                        code="entity_response_invalid",
                        detail="local extraction response must contain an entities array",
                        candidate_index=None,
                    ),
                ),
            )

        blocks = {block.block_id: block for block in reconstruction.blocks}
        entities: list[Entity] = []
        findings: list[EntityFinding] = []
        for candidate_index, raw_candidate in enumerate(raw_entities):
            candidate, finding = _validated_candidate(raw_candidate, candidate_index)
            if finding is not None:
                findings.append(finding)
                continue
            assert candidate is not None
            block = blocks.get(candidate.block_id)
            if block is None:
                findings.append(
                    EntityFinding(
                        code="entity_block_unknown",
                        detail=f"block_id does not exist in reconstruction manifest: {candidate.block_id}",
                        candidate_index=candidate_index,
                    )
                )
                continue
            if candidate.page != block.page_number:
                findings.append(
                    EntityFinding(
                        code="entity_page_mismatch",
                        detail=(
                            f"page {candidate.page} does not match block {candidate.block_id} "
                            f"page {block.page_number}"
                        ),
                        candidate_index=candidate_index,
                    )
                )
                continue
            if candidate.bbox != block.bbox:
                findings.append(
                    EntityFinding(
                        code="entity_bbox_mismatch",
                        detail=f"bbox does not match block {candidate.block_id} geometry",
                        candidate_index=candidate_index,
                    )
                )
                continue
            if candidate.evidence not in block.markdown:
                findings.append(
                    EntityFinding(
                        code="entity_evidence_not_in_block",
                        detail=f"evidence is not an exact substring of block {candidate.block_id} Markdown",
                        candidate_index=candidate_index,
                    )
                )
                continue
            entities.append(
                Entity(
                    type=candidate.type,
                    value=candidate.value,
                    normalized_value=candidate.normalized_value,
                    page=candidate.page,
                    block_id=candidate.block_id,
                    bbox=candidate.bbox,
                    evidence=candidate.evidence,
                    confidence=candidate.confidence,
                )
            )
        return EntityExtractionResult(
            schema_version=self._schema.version,
            entities=tuple(entities),
            findings=tuple(findings),
        )


class EntityStageHandler:
    """Reserve GPU0 for local Qwen3 extraction and persist a resumable entity manifest."""

    def __init__(
        self,
        *,
        extractor: EntityExtractor,
        artifacts: ArtifactStore,
        repository: SqlAlchemyBatchRepository,
        gpu0_slot_unit: str,
    ) -> None:
        self._extractor = extractor
        self._artifacts = artifacts
        self._repository = repository
        self._gpu0_slot_unit = gpu0_slot_unit

    def handle(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        reconstruction: ReconstructionManifest,
        reconstruction_reference: ArtifactReference,
        artifact_prefix: str,
    ) -> EntityManifest:
        """Extract and validate entities with the exclusive GPU0 admission slot."""
        lease_duration = timedelta(minutes=20)
        try:
            self._repository.renew_lease(
                job_id=job_id,
                worker_id=worker_id,
                lease_duration=lease_duration,
            )
            self._repository.reserve_resources(
                job_id=job_id,
                owner=worker_id,
                requests=(ResourceRequest(ReservationKind.GPU0, 1, self._gpu0_slot_unit),),
                lease_duration=lease_duration,
            )
        except ResourceCapacityError:
            self._repository.defer_job_for_capacity(
                job_id=job_id,
                worker_id=worker_id,
                error_detail="GPU0 Qwen3/Fenic role slot is unavailable",
            )
            raise
        result = self._extractor.extract(reconstruction)
        manifest = EntityManifest(
            schema_version=result.schema_version,
            reconstruction=reconstruction_reference,
            entities=result.entities,
            findings=result.findings,
        )
        artifact = self._artifacts.put_bytes(
            object_key=f"{artifact_prefix.rstrip('/')}/entity_manifest.json",
            payload=json.dumps(_manifest_payload(manifest), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            media_type="application/json",
            retention=ArtifactRetention.TEMPORARY,
        )
        self._repository.record_entity_output(
            job_id=job_id,
            worker_id=worker_id,
            manifest=artifact,
        )
        self._repository.complete_job(job_id=job_id, worker_id=worker_id, state=JobState.SUCCEEDED)
        return manifest


def _validated_candidate(
    value: Any, candidate_index: int
) -> tuple[EntityCandidateV1 | None, EntityFinding | None]:
    """Validate one item so a malformed model entry cannot discard valid neighbors."""
    if not isinstance(value, Mapping):
        return None, EntityFinding(
            code="entity_candidate_invalid",
            detail="entity candidate must be an object",
            candidate_index=candidate_index,
        )
    try:
        return EntityCandidateV1.model_validate(value), None
    except ValidationError as error:
        details = "; ".join(
            f"{_error_location(issue.get('loc', ()))}: {issue['msg']}" for issue in error.errors()
        )
        return None, EntityFinding(
            code="entity_candidate_invalid",
            detail=details,
            candidate_index=candidate_index,
        )


def _context(reconstruction: ReconstructionManifest, schema: EntitySchemaPack) -> dict[str, Any]:
    """Expose only Markdown blocks paired with the geometry used for later validation."""
    return {
        "schema_version": schema.version,
        "source_sha256": reconstruction.source_sha256,
        "blocks": [
            {
                "page": block.page_number,
                "block_id": block.block_id,
                "bbox": block.bbox,
                "markdown": block.markdown,
            }
            for block in reconstruction.blocks
        ],
    }


def _prompt(context: Mapping[str, Any], schema: EntitySchemaPack) -> str:
    """Construct a stable, explicit prompt for local structured extraction."""
    return (
        f"Extract only these entity types: {', '.join(schema.entity_types)}. "
        "For every entity, copy evidence exactly from its block Markdown and repeat that block's "
        "page, block_id, and bbox exactly. "
        "GROUND_TRUTH_CONTEXT="
        f"{json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"
    )


def _decode_json_object(value: str) -> Mapping[str, Any]:
    """Accept a JSON object, tolerating a Markdown code fence from imperfect backends."""
    text = value.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    decoded = json.loads(text)
    if not isinstance(decoded, Mapping):
        raise EntityExtractionError("local Qwen3 JSON response root must be an object")
    return decoded


def _error_location(location: Sequence[Any]) -> str:
    """Make Pydantic error locations stable and compact for persisted findings."""
    return ".".join(str(part) for part in location) or "candidate"


def _manifest_payload(manifest: EntityManifest) -> dict[str, Any]:
    """Serialize every accepted entity and rejected-candidate finding for publication."""
    return {
        "schema_version": manifest.schema_version,
        "reconstruction": manifest.reconstruction.model_dump(mode="json"),
        "entities": [entity.model_dump(mode="json") for entity in manifest.entities],
        "findings": [
            {
                "code": finding.code,
                "detail": finding.detail,
                "candidate_index": finding.candidate_index,
            }
            for finding in manifest.findings
        ],
    }


def entity_manifest_from_payload(payload: Mapping[str, Any]) -> EntityManifest:
    """Load the persisted entity contract before final publication."""
    try:
        findings = tuple(
            EntityFinding(
                code=str(value["code"]),
                detail=str(value["detail"]),
                candidate_index=(
                    None if value.get("candidate_index") is None else int(value["candidate_index"])
                ),
            )
            for value in payload.get("findings", [])
        )
        return EntityManifest(
            schema_version=str(payload["schema_version"]),
            reconstruction=ArtifactReference.model_validate(payload["reconstruction"]),
            entities=tuple(Entity.model_validate(value) for value in payload["entities"]),
            findings=findings,
        )
    except (KeyError, TypeError, ValueError, ValidationError) as error:
        raise EntityExtractionError(f"persisted entity manifest is invalid: {error}") from error
