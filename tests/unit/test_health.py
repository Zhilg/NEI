import pytest

from idp.config import Settings
from idp.health import RuntimeHealthError, _check_model_endpoint, check_runtime_health


class _Response:
    status = 200

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def test_healthcheck_skips_model_services_when_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("idp.health._check_postgres", lambda _: None)
    monkeypatch.setattr("idp.health._check_minio", lambda _: None)
    monkeypatch.setattr(
        "idp.health._check_model_endpoint",
        lambda *_: (_ for _ in ()).throw(AssertionError("models must be skipped")),
    )

    report = check_runtime_health(Settings(pipeline_profile_version="test"), include_models=False)

    assert report.postgres_ok is True
    assert report.qwen_vl_ok is False
    assert report.qwen3_ok is False


def test_model_healthcheck_requires_nonempty_model_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *_args, **_kwargs: _Response(b'{"data":[]}')
    )

    with pytest.raises(RuntimeHealthError, match="no loaded models"):
        _check_model_endpoint("http://qwen-vl:8000/v1", "Qwen-VL")


def test_model_healthcheck_accepts_loaded_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *_args, **_kwargs: _Response(b'{"data":[{"id":"local"}]}')
    )

    _check_model_endpoint("http://qwen-vl:8000/v1", "Qwen-VL")
