from pathlib import Path

from idp.services.hashing import sha256_bytes, sha256_file


def test_sha256_file_streams_and_counts_bytes(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    payload = b"offline-pdf-pipeline"
    source.write_bytes(payload)

    digest, size_bytes = sha256_file(source, chunk_size=3)

    assert digest == sha256_bytes(payload)
    assert size_bytes == len(payload)
