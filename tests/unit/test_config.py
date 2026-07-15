from pathlib import Path

import pytest
from pydantic import ValidationError

from idp.config import Settings


def test_settings_normalizes_absolute_allowed_roots() -> None:
    settings = Settings(allowed_roots=(Path("/data/incoming/../incoming"),))

    assert settings.allowed_roots == (Path("/data/incoming"),)


def test_settings_rejects_relative_allowed_roots() -> None:
    with pytest.raises(ValidationError, match="allowed root must be absolute"):
        Settings(allowed_roots=(Path("relative/path"),))


def test_settings_accepts_only_json_command_arrays() -> None:
    settings = Settings(mineru_command='["mineru", "--input", "{images}"]')

    assert settings.mineru_command == ("mineru", "--input", "{images}")

    with pytest.raises(ValidationError, match="JSON array"):
        Settings(mineru_command="mineru --input {images}")


def test_settings_requires_local_vllm_port_and_v1_path() -> None:
    with pytest.raises(ValidationError, match="approved local/internal"):
        Settings(qwen_vl_endpoint="http://qwen-vl:9000/v1")

    with pytest.raises(ValidationError, match="approved local/internal"):
        Settings(qwen3_endpoint="http://qwen3:8000/api")
