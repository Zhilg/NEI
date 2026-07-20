from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from idp.config import Settings
from idp.runtime import register_default_profile


def test_settings_normalizes_absolute_allowed_roots() -> None:
    settings = Settings(
        allowed_roots=(Path("/data/incoming/../incoming"),), pipeline_profile_version="test"
    )

    assert settings.allowed_roots == (Path("/data/incoming"),)


def test_settings_rejects_relative_allowed_roots() -> None:
    with pytest.raises(ValidationError, match="allowed root must be absolute"):
        Settings(allowed_roots=(Path("relative/path"),), pipeline_profile_version="test")


def test_settings_accepts_only_json_command_arrays() -> None:
    settings = Settings(mineru_command='["mineru", "--input", "{images}"]', pipeline_profile_version="test")

    assert settings.mineru_command == ("mineru", "--input", "{images}")

    with pytest.raises(ValidationError, match="JSON array"):
        Settings(mineru_command="mineru --input {images}", pipeline_profile_version="test")


def test_settings_requires_local_vllm_port_and_v1_path() -> None:
    with pytest.raises(ValidationError, match="approved local/internal"):
        Settings(qwen_vl_endpoint="http://qwen-vl:9000/v1", pipeline_profile_version="test")

    with pytest.raises(ValidationError, match="approved local/internal"):
        Settings(qwen3_endpoint="http://qwen3:8000/api", pipeline_profile_version="test")


def test_default_profile_is_stable_for_same_mounted_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    repository = Mock()
    monkeypatch.setattr("idp.runtime.SqlAlchemyBatchRepository", lambda _: repository)
    monkeypatch.setattr("idp.runtime.create_session_factory", lambda _: Mock())
    settings = Settings(pipeline_profile_version="test")

    first = register_default_profile(settings)
    second = register_default_profile(settings)

    assert first == second
    assert len(first) == 64
    assert repository.register_profile.call_count == 2
    assert repository.register_profile.call_args.kwargs["name"] == "default"
