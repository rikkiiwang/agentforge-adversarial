from __future__ import annotations

import httpx
import pytest

from agentforge_adversarial.target import (
    CopilotClient,
    GenericChatClient,
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
