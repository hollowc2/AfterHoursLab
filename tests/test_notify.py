import urllib.error

import pytest

from afterhours_lab import notify


def test_send_noops_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    assert notify.send("hello") is None


def test_send_returns_true_on_200(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(notify.urllib.request, "urlopen", lambda *_a, **_k: FakeResponse())

    assert notify.send("hello") is True


def test_send_returns_false_on_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")

    def raise_error(*_a, **_k):
        raise urllib.error.URLError("unreachable")

    monkeypatch.setattr(notify.urllib.request, "urlopen", raise_error)

    assert notify.send("hello") is False
