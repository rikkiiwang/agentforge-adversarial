from __future__ import annotations

import httpx
import pytest

from agentforge_adversarial.target import (
    CopilotClient,
    GenericChatClient,
    auto_pick_patient_id,
    make_client,
)


def test_make_client_copilot():
    client = make_client(
        {
            "target_type": "copilot",
            "target_url": "https://example.invalid",
            "config_json": {"patient_id": "p-1", "physician_user_id": "admin"},
        }
    )
    assert isinstance(client, CopilotClient)
    assert client.patient_id == "p-1"
    assert client.physician_user_id == "admin"


def test_make_client_copilot_env_fallback_when_config_empty(monkeypatch):
    """The seeded target row stores patient_id="" by design; the
    `make run-live` workflow exports COPILOT_PATIENT_ID. The factory
    must pick that up so CLI live runs don't require a dashboard
    round-trip to populate config_json."""
    monkeypatch.setenv("COPILOT_PATIENT_ID", "env-pid-9")
    monkeypatch.setenv("COPILOT_PHYSICIAN_USER_ID", "env-physician")
    client = make_client(
        {
            "target_type": "copilot",
            "target_url": "https://example.invalid",
            "config_json": {"patient_id": "", "physician_user_id": ""},
        }
    )
    assert isinstance(client, CopilotClient)
    assert client.patient_id == "env-pid-9"
    assert client.physician_user_id == "env-physician"


def test_make_client_copilot_config_wins_over_env(monkeypatch):
    """A non-empty config_json.patient_id (set explicitly via the
    dashboard's Add-target form) must override the env var — otherwise
    operators with multiple Co-Pilot targets get cross-contamination."""
    monkeypatch.setenv("COPILOT_PATIENT_ID", "env-pid-9")
    client = make_client(
        {
            "target_type": "copilot",
            "target_url": "https://example.invalid",
            "config_json": {"patient_id": "config-pid-1",
                            "physician_user_id": "admin"},
        }
    )
    assert client.patient_id == "config-pid-1"


def test_make_client_generic_chat_defaults():
    client = make_client(
        {
            "target_type": "generic_chat",
            "target_url": "https://example.invalid/chat",
            "config_json": {},
        }
    )
    assert isinstance(client, GenericChatClient)
    assert client.request_template == {"prompt": "{PROMPT}"}
    assert client.response_path == "response"


def test_make_client_openai_compat_authorization_header():
    client = make_client(
        {
            "target_type": "openai_compat",
            "target_url": "https://api.example.invalid/v1/chat/completions",
            "config_json": {"api_key": "sk-test-123", "model": "gpt-x"},
        }
    )
    assert isinstance(client, GenericChatClient)
    assert client.headers["Authorization"] == "Bearer sk-test-123"
    assert client.request_template["model"] == "gpt-x"
    assert client.response_path == "choices.0.message.content"


def test_make_client_rejects_unknown_type():
    with pytest.raises(ValueError, match="Unknown target_type"):
        make_client({"target_type": "telnet", "target_url": "x", "config_json": {}})


async def test_generic_chat_client_substitutes_and_extracts():
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _j
        captured.append(_j.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "the answer"}}]},
        )

    client = GenericChatClient(
        "https://example.invalid/v1/chat/completions",
        request_template={
            "model": "gpt-mini",
            "messages": [{"role": "user", "content": "{PROMPT}"}],
        },
        response_path="choices.0.message.content",
    )
    client._transport = httpx.MockTransport(handler)
    out = await client.chat("hello world")
    assert out == "the answer"
    assert captured[0]["messages"][0]["content"] == "hello world"


async def test_auto_pick_returns_first_uuid():
    """Happy path: /v1/patients returns one UUID; helper extracts the id."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={"patients": [{"id": "uuid-1"}, {"id": "uuid-2"}], "count": 2},
        )

    picked = await auto_pick_patient_id(
        "https://copilot.example.invalid/",
        transport=httpx.MockTransport(handler),
    )
    assert picked == "uuid-1"
    # Trailing slash stripped, limit=1 always requested.
    assert str(captured[0].url) == (
        "https://copilot.example.invalid/v1/patients?limit=1"
    )


async def test_auto_pick_returns_none_when_endpoint_missing():
    """A Co-Pilot deploy that predates the /v1/patients endpoint returns 404.
    Helper must return None so the caller falls back to its existing error."""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Not Found"})

    picked = await auto_pick_patient_id(
        "https://copilot.example.invalid",
        transport=httpx.MockTransport(handler),
    )
    assert picked is None


async def test_auto_pick_returns_none_when_list_empty():
    """200 OK with empty list (e.g. physician panel filters everything out)
    → None, not a crash."""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"patients": [], "count": 0})

    picked = await auto_pick_patient_id(
        "https://copilot.example.invalid",
        transport=httpx.MockTransport(handler),
    )
    assert picked is None


async def test_auto_pick_returns_none_on_network_error():
    """Network failure (DNS, timeout, TLS) must not raise — we want the
    campaign launch to continue to make_client and surface its existing
    'CopilotClient needs patient_id' error message instead."""
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated DNS failure")

    picked = await auto_pick_patient_id(
        "https://copilot.example.invalid",
        transport=httpx.MockTransport(handler),
    )
    assert picked is None


async def test_auto_pick_forwards_physician_user_id():
    """When the caller passes a physician_user_id, it must reach the endpoint
    as a query param so the server-side panel filter applies correctly."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"patients": [{"id": "uuid-x"}], "count": 1})

    picked = await auto_pick_patient_id(
        "https://copilot.example.invalid",
        physician_user_id="dr_alvarez",
        transport=httpx.MockTransport(handler),
    )
    assert picked == "uuid-x"
    assert "physician_user_id=dr_alvarez" in str(captured[0].url)


async def test_generic_chat_client_falls_back_on_missing_path():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    client = GenericChatClient(
        "https://example.invalid/chat",
        request_template={"prompt": "{PROMPT}"},
        response_path="response",
    )
    client._transport = httpx.MockTransport(handler)
    out = await client.chat("hi")
    assert "unexpected" in out
