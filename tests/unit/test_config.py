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
