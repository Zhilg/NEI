import json
from typing import Any

import pytest

from idp.domain.models import ArtifactReference
from idp.services.entities import (
    ENTITY_SCHEMA_V1,
    SUPPORTED_ENTITY_TYPES,
    EntityExtractor,
    LocalOpenAIQwen3Client,
)
from idp.services.qwen_vl import GroundedBlock, ReconstructionManifest


class RecordingClient:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.contexts: list[dict[str, Any]] = []
        self.schemas = []

    def extract(self, context, schema):
        self.contexts.append(dict(context))
        self.schemas.append(schema)
        return self.response


def _reconstruction() -> ReconstructionManifest:
    reference = ArtifactReference(
        object_key="intermediate/layout.json",
        sha256="a" * 64,
        media_type="application/json",
    )
    return ReconstructionManifest(
        schema_version="reconstruction-manifest-v1",
        source_sha256="b" * 64,
        layout_manifest=reference,
        ocr_manifest=ArtifactReference(
            object_key="intermediate/ocr.json",
            sha256="c" * 64,
            media_type="application/json",
        ),
        model_id="Qwen2.5-VL-32B-Instruct",
        model_revision="pinned",
        prompt_version="reconstruction-v1",
        markdown="Alice Example\n\nInvoice INV-42 dated 2026-07-15",
        blocks=(
            GroundedBlock(
                block_id="b1",
                page_number=1,
                bbox=(0.0, 0.0, 100.0, 20.0),
                markdown="Alice Example signed the agreement.",
                evidence="b1 image",
                corrections=(),
            ),
            GroundedBlock(
                block_id="b2",
                page_number=2,
                bbox=(0.0, 20.0, 100.0, 40.0),
                markdown="Invoice INV-42 dated 2026-07-15.",
                evidence="b2 image",
                corrections=(),
            ),
        ),
        findings=(),
    )


def _candidate(**overrides: Any) -> dict[str, Any]:
    candidate: dict[str, Any] = {
        "type": "person",
        "value": "Alice Example",
        "normalized_value": "Alice Example",
        "page": 1,
        "block_id": "b1",
        "bbox": [0.0, 0.0, 100.0, 20.0],
        "evidence": "Alice Example",
        "confidence": 0.98,
    }
    candidate.update(overrides)
    return candidate


def test_extractor_keeps_valid_entities_when_candidates_fail_grounding() -> None:
    client = RecordingClient(
        {
            "entities": [
                _candidate(),
                _candidate(type="unsupported"),
                _candidate(block_id="missing"),
                _candidate(page=2),
                _candidate(bbox=[0.0, 0.0, 1.0, 1.0]),
                _candidate(
                    type="date",
                    value="2026-07-15",
                    page=1,
                    block_id="b1",
                    evidence="2026-07-15",
                ),
                "not an object",
            ]
        }
    )

    result = EntityExtractor(client=client).extract(_reconstruction())

    assert result.schema_version == "entity-v1"
    assert [entity.value for entity in result.entities] == ["Alice Example"]
    assert [finding.code for finding in result.findings] == [
        "entity_candidate_invalid",
        "entity_block_unknown",
        "entity_page_mismatch",
        "entity_bbox_mismatch",
        "entity_evidence_not_in_block",
        "entity_candidate_invalid",
    ]
    assert [finding.candidate_index for finding in result.findings] == [1, 2, 3, 4, 5, 6]
    assert client.contexts[0]["blocks"][1]["markdown"] == "Invoice INV-42 dated 2026-07-15."
    assert client.schemas == [ENTITY_SCHEMA_V1]


def test_local_qwen3_client_uses_closed_schema_and_local_http_only(monkeypatch) -> None:
    requests = []

    class Response:
        def read(self) -> bytes:
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps({"entities": [_candidate()]}),
                            }
                        }
                    ]
                }
            ).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            return None

    def fake_urlopen(request, *, timeout):
        requests.append((request, timeout))
        return Response()

    monkeypatch.setattr("idp.services.entities.urllib.request.urlopen", fake_urlopen)
    client = LocalOpenAIQwen3Client(
        endpoint="http://qwen3:8000/v1", model_id="Qwen3-14B", timeout_seconds=12
    )

    response = client.extract({"blocks": []}, ENTITY_SCHEMA_V1)

    request, timeout = requests[0]
    payload = json.loads(request.data)
    schema = payload["response_format"]["json_schema"]["schema"]
    assert request.full_url == "http://qwen3:8000/v1/chat/completions"
    assert timeout == 12
    assert response == {"entities": [_candidate()]}
    assert payload["temperature"] == 0
    assert payload["top_p"] == 1
    assert payload["seed"] == 0
    assert schema["$defs"]["EntityCandidateV1"]["properties"]["type"]["enum"] == list(
        SUPPORTED_ENTITY_TYPES
    )

    with pytest.raises(ValueError, match="local/internal HTTP"):
        LocalOpenAIQwen3Client(
            endpoint="http://example.test/v1", model_id="Qwen3-14B", timeout_seconds=12
        )
