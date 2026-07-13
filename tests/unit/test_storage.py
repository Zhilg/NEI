from pathlib import Path

import pytest

from idp.domain.states import ArtifactRetention
from idp.storage import ArtifactStoreError, LocalArtifactStore, validate_object_key


def test_local_store_is_idempotent_for_matching_content(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")

    first = store.put_bytes(
        object_key="batches/one/final.md",
        payload=b"# Result\n",
        media_type="text/markdown",
        retention=ArtifactRetention.FINAL,
    )
    second = store.put_bytes(
        object_key="batches/one/final.md",
        payload=b"# Result\n",
        media_type="text/markdown",
        retention=ArtifactRetention.FINAL,
    )

    assert first == second
    assert store.exists(first.reference)


def test_local_store_rejects_immutable_key_collision(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    store.put_bytes(
        object_key="batches/one/final.md",
        payload=b"first",
        media_type="text/markdown",
        retention=ArtifactRetention.FINAL,
    )

    with pytest.raises(ArtifactStoreError, match="immutable artifact key collision"):
        store.put_bytes(
            object_key="batches/one/final.md",
            payload=b"second",
            media_type="text/markdown",
            retention=ArtifactRetention.FINAL,
        )


def test_store_prevents_path_escape_and_final_deletion(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    artifact = store.put_bytes(
        object_key="batches/one/manifest.json",
        payload=b"{}",
        media_type="application/json",
        retention=ArtifactRetention.FINAL,
    )

    with pytest.raises(ArtifactStoreError, match="invalid artifact object key"):
        validate_object_key("../escape")
    with pytest.raises(ArtifactStoreError, match="refusing to delete a final artifact"):
        store.delete(artifact)


def test_store_deletes_temporary_artifact_only_after_hash_check(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    artifact = store.put_bytes(
        object_key="temporary/one/page.png",
        payload=b"page",
        media_type="image/png",
        retention=ArtifactRetention.TEMPORARY,
    )

    store.delete(artifact)

    assert not store.exists(artifact.reference)
