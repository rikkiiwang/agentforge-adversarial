from __future__ import annotations

import pytest

from agentforge_adversarial.target import CopilotClient


def test_copilot_client_requires_session_id(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("COPILOT_SESSION_ID", raising=False)
    with pytest.raises(RuntimeError, match="session_id"):
        CopilotClient("https://example.invalid")


def test_copilot_client_reads_session_id_from_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COPILOT_SESSION_ID", "abc-123-def")
    client = CopilotClient("https://example.invalid")
    assert client.session_id == "abc-123-def"


def test_copilot_client_explicit_session_id_wins(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COPILOT_SESSION_ID", "env-value")
    client = CopilotClient("https://example.invalid", session_id="explicit-value")
    assert client.session_id == "explicit-value"
