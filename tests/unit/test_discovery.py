from pathlib import Path

import pytest

from idp.domain.states import BatchItemState
from idp.services.discovery import (
    BatchDiscovery,
    DiscoveryError,
    DiscoveryLimits,
    normalize_allowed_root,
    normalize_submitted_root,
)


def _limits() -> DiscoveryLimits:
    return DiscoveryLimits(
        stability_seconds=0,
        max_file_bytes=1024 * 1024,
        max_candidates=100,
        max_depth=10,
        hash_chunk_bytes=4,
    )


def test_submit_root_rejects_component_prefix_bypass(tmp_path: Path) -> None:
    allowed = tmp_path / "data"
    bypass = tmp_path / "data-private"
    allowed.mkdir()
    bypass.mkdir()

    with pytest.raises(DiscoveryError, match="outside allowed roots"):
        normalize_submitted_root(bypass, (normalize_allowed_root(allowed),))


def test_discovery_does_not_follow_symlinks_and_reports_them(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "real.pdf").write_bytes(b"pdf-bytes")
    (root / "linked.pdf").symlink_to(root / "real.pdf")
    (root / "nested").symlink_to(root, target_is_directory=True)

    snapshot = BatchDiscovery(_limits()).scan(roots=(root,), profile_name="profile")

    states = {item.path.name: item.state for item in snapshot.items}
    assert states["real.pdf"] == BatchItemState.QUEUED
    assert states["linked.pdf"] == BatchItemState.SKIPPED_SYMLINK
    assert states["nested"] == BatchItemState.SKIPPED_SYMLINK
    assert len(snapshot.items) == 3


def test_discovery_marks_non_pdf_without_losing_neighbor_pdf(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "keep.pdf").write_bytes(b"pdf")
    (root / "skip.txt").write_text("no", encoding="utf-8")

    snapshot = BatchDiscovery(_limits()).scan(roots=(root,), profile_name="profile")

    states = {item.path.name: item.state for item in snapshot.items}
    assert states == {
        "keep.pdf": BatchItemState.QUEUED,
        "skip.txt": BatchItemState.SKIPPED_UNSUPPORTED,
    }


def test_descriptor_staging_preserves_stable_source_bytes(tmp_path: Path) -> None:
    root = tmp_path / "root"
    staging = tmp_path / "staging"
    root.mkdir()
    source = root / "document.PDF"
    source.write_bytes(b"stable-pdf-content")

    result = BatchDiscovery(_limits()).scan_and_stage(
        roots=(root,), profile_name="profile", staging_directory=staging
    )

    item = result.snapshot.items[0]
    staged = result.staged_sources[item.item_id]
    assert item.state == BatchItemState.QUEUED
    assert staged.read_bytes() == b"stable-pdf-content"
    assert len(item.source_sha256 or "") == 64


def test_candidate_limit_is_reported_explicitly(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "one.pdf").write_bytes(b"one")
    (root / "two.pdf").write_bytes(b"two")
    limited = DiscoveryLimits(
        stability_seconds=0,
        max_file_bytes=1024,
        max_candidates=1,
        max_depth=1,
        hash_chunk_bytes=32,
    )

    snapshot = BatchDiscovery(limited).scan(roots=(root,), profile_name="profile")

    assert any(item.reason == "scan_candidate_limit_reached" for item in snapshot.items)


def test_file_changed_between_stability_snapshots_is_not_hashed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "changing.pdf"
    source.write_bytes(b"before")

    def mutate(_: float) -> None:
        source.write_bytes(b"after-with-a-different-size")

    unstable = DiscoveryLimits(
        stability_seconds=1,
        max_file_bytes=1024,
        max_candidates=10,
        max_depth=1,
        hash_chunk_bytes=32,
    )
    snapshot = BatchDiscovery(unstable, sleep=mutate).scan(roots=(root,), profile_name="profile")

    item = snapshot.items[0]
    assert item.state == BatchItemState.SKIPPED_UNSTABLE
    assert item.reason == "file_changed_during_stability_check"
    assert item.source_sha256 is None
