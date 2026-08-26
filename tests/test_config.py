import pytest
from pydantic import ValidationError

from afterhours_lab.config import AppSettings


def test_settings_raise_when_gateway_url_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCHWAB_GATEWAY_URL", raising=False)
    monkeypatch.setenv("SCHWAB_GATEWAY_API_KEY", "test-key")
    with pytest.raises(ValidationError):
        AppSettings(_env_file=None)  # type: ignore[call-arg]


def test_settings_raise_when_api_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCHWAB_GATEWAY_URL", "https://gateway.internal")
    monkeypatch.delenv("SCHWAB_GATEWAY_API_KEY", raising=False)
    with pytest.raises(ValidationError):
        AppSettings(_env_file=None)  # type: ignore[call-arg]


def test_settings_raise_when_gateway_url_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCHWAB_GATEWAY_URL", "   ")
    monkeypatch.setenv("SCHWAB_GATEWAY_API_KEY", "test-key")
    with pytest.raises(ValidationError):
        AppSettings()


def test_settings_load_when_both_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCHWAB_GATEWAY_URL", "https://gateway.internal")
    monkeypatch.setenv("SCHWAB_GATEWAY_API_KEY", "test-key")
    settings = AppSettings()
    assert settings.gateway_url == "https://gateway.internal"
    assert settings.gateway_api_key.get_secret_value() == "test-key"
    assert settings.gateway_timeout_seconds == 5.0
    assert settings.gateway_max_concurrency == 4
    assert settings.gateway_max_attempts == 3
    assert settings.gateway_retry_backoff_seconds == 0.5


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SCHWAB_GATEWAY_TIMEOUT_SECONDS", "0"),
        ("SCHWAB_GATEWAY_MAX_CONCURRENCY", "0"),
        ("SCHWAB_GATEWAY_MAX_CONCURRENCY", "33"),
        ("SCHWAB_GATEWAY_MAX_ATTEMPTS", "0"),
        ("SCHWAB_GATEWAY_MAX_ATTEMPTS", "7"),
        ("SCHWAB_GATEWAY_RETRY_BACKOFF_SECONDS", "-1"),
    ],
)
def test_gateway_resilience_settings_are_bounded(
    name: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCHWAB_GATEWAY_URL", "https://gateway.internal")
    monkeypatch.setenv("SCHWAB_GATEWAY_API_KEY", "test-key")
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        AppSettings(_env_file=None)  # type: ignore[call-arg]
