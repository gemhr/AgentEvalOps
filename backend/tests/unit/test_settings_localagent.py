"""Focused Settings tests for LOCALAGENT_HTTP_BASE_URL."""

import pytest
from pydantic import ValidationError

from app.registry.settings import Settings


def test_default_base_url_is_empty() -> None:
    settings = Settings(_env_file=None)
    assert settings.LOCALAGENT_HTTP_BASE_URL == ""


def test_valid_http_base_url_is_accepted() -> None:
    settings = Settings(LOCALAGENT_HTTP_BASE_URL="http://localagent.test:8000", _env_file=None)
    assert settings.LOCALAGENT_HTTP_BASE_URL == "http://localagent.test:8000"


def test_valid_https_base_url_is_accepted() -> None:
    settings = Settings(LOCALAGENT_HTTP_BASE_URL="https://localagent.test", _env_file=None)
    assert settings.LOCALAGENT_HTTP_BASE_URL == "https://localagent.test"


@pytest.mark.parametrize(
    "value",
    [
        "ftp://localagent.test",
        "http://localagent.test/path?q=1",
        "http://localagent.test/#frag",
        "not-a-url",
        "http://",
        "localhost:8000",
    ],
    ids=["ftp", "query", "fragment", "garbage", "scheme-only", "no-scheme"],
)
def test_invalid_base_url_fails_closed(value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(LOCALAGENT_HTTP_BASE_URL=value, _env_file=None)
