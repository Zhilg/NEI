"""Safe, deterministic PDF discovery and stable-descriptor hashing."""

from __future__ import annotations

import os
import stat
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Callable, Iterator
from uuid import UUID, uuid4

from idp.domain.models import BatchItemSnapshot, BatchSnapshot
from idp.domain.states import BatchItemState


class DiscoveryError(ValueError):
    """Raised when submitted roots violate the controller's filesystem policy."""


@dataclass(frozen=True)
class DiscoveryLimits:
    """Bound untrusted filesystem traversal before it consumes batch resources."""

    stability_seconds: float
    max_file_bytes: int
    max_candidates: int
    max_depth: int
    hash_chunk_bytes: int


@dataclass(frozen=True)
class DiscoveryResult:
    """An immutable metadata snapshot plus exact descriptor-copied source files."""

    snapshot: BatchSnapshot
    staged_sources: dict[UUID, Path]


Sleep = Callable[[float], None]


def normalize_allowed_root(root: Path) -> Path:
    """Resolve a configured root and require it to exist as a real directory."""
    if not root.is_absolute():
        msg = f"allowlist root must be absolute: {root}"
        raise DiscoveryError(msg)
    try:
        resolved = root.resolve(strict=True)
    except OSError as error:
        msg = f"allowlist root cannot be resolved: {root}"
        raise DiscoveryError(msg) from error
    if not resolved.is_dir():
        msg = f"allowlist root is not a directory: {resolved}"
        raise DiscoveryError(msg)
    return resolved


def normalize_submitted_root(submitted: Path, allowed_roots: tuple[Path, ...]) -> Path:
    """Resolve a submitted root and enforce component-safe allowlist containment."""
    if not submitted.is_absolute():
        msg = f"submitted root must be absolute: {submitted}"
        raise DiscoveryError(msg)
    try:
        resolved = submitted.resolve(strict=True)
    except OSError as error:
        msg = f"submitted root cannot be resolved: {submitted}"
        raise DiscoveryError(msg) from error
    if not resolved.is_dir():
        msg = f"submitted root is not a directory: {resolved}"
        raise DiscoveryError(msg)
    for allowed_root in allowed_roots:
        try:
            resolved.relative_to(allowed_root)
        except ValueError:
            continue
        return resolved
    msg = f"submitted root is outside allowed roots: {resolved}"
    raise DiscoveryError(msg)


class BatchDiscovery:
    """Create an immutable scan snapshot without following symlinks or mutable paths."""

    def __init__(self, limits: DiscoveryLimits, sleep: Sleep = time.sleep) -> None:
        self._limits = limits
        self._sleep = sleep

    def scan(self, *, roots: tuple[Path, ...], profile_name: str) -> BatchSnapshot:
        """Scan ordered roots into a deterministic complete snapshot of candidate paths."""
        return self._scan(roots=roots, profile_name=profile_name, staging_directory=None).snapshot

    def scan_and_stage(
        self, *, roots: tuple[Path, ...], profile_name: str, staging_directory: Path
    ) -> DiscoveryResult:
        """Scan and copy stable descriptors once into controlled temporary staging."""
        staging_directory.mkdir(parents=True, exist_ok=True)
        return self._scan(
            roots=roots, profile_name=profile_name, staging_directory=staging_directory
        )

    def _scan(
        self, *, roots: tuple[Path, ...], profile_name: str, staging_directory: Path | None
    ) -> DiscoveryResult:
        items: list[BatchItemSnapshot] = []
        staged_sources: dict[UUID, Path] = {}
        observed_at = datetime.now(UTC)
        for root in sorted(set(roots), key=str):
            for candidate in self._walk(root):
                if len(items) >= self._limits.max_candidates:
                    items.append(
                        self._item(
                            root,
                            candidate,
                            BatchItemState.SKIPPED_UNSUPPORTED,
                            reason="scan_candidate_limit_reached",
                            observed_at=observed_at,
                        )
                    )
                    break
                item, staged_source = self._inspect(
                    root, candidate, observed_at, staging_directory
                )
                items.append(item)
                if staged_source is not None:
                    staged_sources[item.item_id] = staged_source
            if len(items) >= self._limits.max_candidates:
                break
        return DiscoveryResult(
            snapshot=BatchSnapshot(
                profile_name=profile_name,
                roots=roots,
                items=tuple(sorted(items, key=lambda item: (str(item.root), str(item.path)))),
                created_at=observed_at,
            ),
            staged_sources=staged_sources,
        )

    def _walk(self, root: Path) -> Iterator[Path]:
        """Yield every directory entry deterministically without recursing through links."""
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack:
            directory, depth = stack.pop()
            try:
                entries = sorted(directory.iterdir(), key=lambda entry: entry.name, reverse=True)
            except OSError:
                yield directory
                continue
            for entry in entries:
                try:
                    entry_stat = entry.lstat()
                except OSError:
                    continue
                if stat.S_ISDIR(entry_stat.st_mode) and not stat.S_ISLNK(entry_stat.st_mode):
                    if depth < self._limits.max_depth:
                        stack.append((entry, depth + 1))
                    continue
                yield entry

    def _inspect(
        self,
        root: Path,
        candidate: Path,
        observed_at: datetime,
        staging_directory: Path | None,
    ) -> tuple[BatchItemSnapshot, Path | None]:
        """Classify candidate paths without allowing a check-to-use symlink swap."""
        try:
            first = candidate.lstat()
        except OSError as error:
            return self._item(
                root,
                candidate,
                BatchItemState.SKIPPED_UNSTABLE,
                reason=f"lstat_failed:{error.__class__.__name__}",
                observed_at=observed_at,
            ), None
        if stat.S_ISLNK(first.st_mode):
            return self._item(
                root,
                candidate,
                BatchItemState.SKIPPED_SYMLINK,
                reason="symbolic_link_not_followed",
                observed_at=observed_at,
                source_stat=first,
            ), None
        if stat.S_ISDIR(first.st_mode):
            return self._item(
                root,
                candidate,
                BatchItemState.SKIPPED_UNSUPPORTED,
                reason="directory_entry",
                observed_at=observed_at,
                source_stat=first,
            ), None
        if not stat.S_ISREG(first.st_mode):
            return self._item(
                root,
                candidate,
                BatchItemState.SKIPPED_UNSUPPORTED,
                reason="not_regular_file",
                observed_at=observed_at,
                source_stat=first,
            ), None
        if candidate.suffix.lower() not in {".pdf", ".docx"}:
            return self._item(
                root,
                candidate,
                BatchItemState.SKIPPED_UNSUPPORTED,
                reason="unsupported_extension",
                observed_at=observed_at,
                source_stat=first,
            ), None
        if first.st_size > self._limits.max_file_bytes:
            return self._item(
                root,
                candidate,
                BatchItemState.SKIPPED_UNSUPPORTED,
                reason="file_size_limit_exceeded",
                observed_at=observed_at,
                source_stat=first,
            ), None
        if self._limits.stability_seconds:
            self._sleep(self._limits.stability_seconds)
        try:
            second = candidate.lstat()
        except OSError as error:
            return self._item(
                root,
                candidate,
                BatchItemState.SKIPPED_UNSTABLE,
                reason=f"second_lstat_failed:{error.__class__.__name__}",
                observed_at=observed_at,
                source_stat=first,
            ), None
        if not self._same_snapshot(first, second):
            return self._item(
                root,
                candidate,
                BatchItemState.SKIPPED_UNSTABLE,
                reason="file_changed_during_stability_check",
                observed_at=observed_at,
                source_stat=first,
            ), None
        return self._hash_stable_file(root, candidate, first, observed_at, staging_directory)

    def _hash_stable_file(
        self,
        root: Path,
        candidate: Path,
        expected: os.stat_result,
        observed_at: datetime,
        staging_directory: Path | None,
    ) -> tuple[BatchItemSnapshot, Path | None]:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(candidate, flags)
        except OSError as error:
            return self._item(
                root,
                candidate,
                BatchItemState.SKIPPED_UNSTABLE,
                reason=f"secure_open_failed:{error.__class__.__name__}",
                observed_at=observed_at,
                source_stat=expected,
            ), None
        item_id = uuid4()
        staged_source = None if staging_directory is None else staging_directory / f"{item_id}{candidate.suffix.lower()}"
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or not self._same_snapshot(expected, before):
                return self._item(
                    root,
                    candidate,
                    BatchItemState.SKIPPED_UNSTABLE,
                    reason="file_replaced_before_hash",
                    observed_at=observed_at,
                    source_stat=expected,
                ), None
            digest = sha256()
            size_bytes = 0
            if staged_source is None:
                while chunk := os.read(descriptor, self._limits.hash_chunk_bytes):
                    digest.update(chunk)
                    size_bytes += len(chunk)
                    if size_bytes > self._limits.max_file_bytes:
                        return self._item(
                            root,
                            candidate,
                            BatchItemState.SKIPPED_UNSUPPORTED,
                            reason="file_size_limit_exceeded_during_hash",
                            observed_at=observed_at,
                            source_stat=before,
                        ), None
            else:
                with staged_source.open("xb") as output:
                    while chunk := os.read(descriptor, self._limits.hash_chunk_bytes):
                        digest.update(chunk)
                        output.write(chunk)
                        size_bytes += len(chunk)
                        if size_bytes > self._limits.max_file_bytes:
                            staged_source.unlink(missing_ok=True)
                            return self._item(
                                root,
                                candidate,
                                BatchItemState.SKIPPED_UNSUPPORTED,
                                reason="file_size_limit_exceeded_during_hash",
                                observed_at=observed_at,
                                source_stat=before,
                            ), None
            after = os.fstat(descriptor)
            if not self._same_snapshot(before, after) or size_bytes != before.st_size:
                if staged_source is not None:
                    staged_source.unlink(missing_ok=True)
                return self._item(
                    root,
                    candidate,
                    BatchItemState.SKIPPED_UNSTABLE,
                    reason="file_changed_during_hash",
                    observed_at=observed_at,
                    source_stat=before,
                ), None
            return self._item(
                root,
                candidate,
                BatchItemState.QUEUED,
                observed_at=observed_at,
                source_sha256=digest.hexdigest(),
                source_stat=before,
                item_id=item_id,
            ), staged_source
        finally:
            os.close(descriptor)

    @staticmethod
    def _same_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
        return (
            stat.S_IFMT(left.st_mode) == stat.S_IFMT(right.st_mode)
            and left.st_dev == right.st_dev
            and left.st_ino == right.st_ino
            and left.st_size == right.st_size
            and left.st_mtime_ns == right.st_mtime_ns
            and left.st_ctime_ns == right.st_ctime_ns
        )

    @staticmethod
    def _item(
        root: Path,
        candidate: Path,
        state: BatchItemState,
        *,
        observed_at: datetime,
        reason: str | None = None,
        source_sha256: str | None = None,
        source_stat: os.stat_result | None = None,
        item_id: UUID | None = None,
    ) -> BatchItemSnapshot:
        return BatchItemSnapshot(
            item_id=uuid4() if item_id is None else item_id,
            root=root,
            path=candidate.absolute(),
            source_sha256=source_sha256,
            state=state,
            reason=reason,
            size_bytes=None if source_stat is None else source_stat.st_size,
            mtime_ns=None if source_stat is None else source_stat.st_mtime_ns,
            device=None if source_stat is None else source_stat.st_dev,
            inode=None if source_stat is None else source_stat.st_ino,
            observed_at=observed_at,
        )
