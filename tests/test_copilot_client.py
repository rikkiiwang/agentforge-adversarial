from __future__ import annotations

import httpx
import pytest

from agentforge_adversarial.target import CopilotClient


def test_copilot_client_requires_patient_id():
    with pytest.raises(RuntimeError, match="patient_id"):
        CopilotClient("https://example.invalid", patient_id="")


async def test_copilot_client_creates_session_on_first_chat():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v1/sessions":
            return httpx.Response(
                200, json={"session_id": "sess-1", "patient_pseudonym": "P-A1B2"}
            )
        return httpx.Response(200, json={"response": "ok"})

    client = CopilotClient("https://example.invalid", patient_id="patient-1")
    client._transport = httpx.MockTransport(handler)
    out = await client.chat("hello")
    assert out == "ok"
    assert paths == ["/v1/sessions", "/v1/chat"]


async def test_copilot_client_caches_session_across_chats():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"session_id": "sess-1"})
        return httpx.Response(200, json={"response": "ok"})

    client = CopilotClient("https://example.invalid", patient_id="patient-1")
    client._transport = httpx.MockTransport(handler)
    for _ in range(5):
        await client.chat("hello")
    assert paths.count("/v1/sessions") == 1
    assert paths.count("/v1/chat") == 5


async def test_copilot_client_retries_once_on_404():
    paths: list[str] = []
    chat_index = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chat_index
        paths.append(request.url.path)
        if request.url.path == "/v1/sessions":
            return httpx.Response(
                200, json={"session_id": f"sess-{paths.count('/v1/sessions')}"}
            )
        chat_index += 1
        if chat_index == 1:
            return httpx.Response(404, json={"detail": "session expired"})
        return httpx.Response(200, json={"response": "after-retry"})

    client = CopilotClient("https://example.invalid", patient_id="patient-1")
    client._transport = httpx.MockTransport(handler)
    out = await client.chat("hello")
    assert out == "after-retry"
    assert paths == ["/v1/sessions", "/v1/chat", "/v1/sessions", "/v1/chat"]


async def test_copilot_client_default_physician_is_admin():
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        if request.url.path == "/v1/sessions":
            captured.append(_json.loads(request.content))
            return httpx.Response(200, json={"session_id": "sess-1"})
        return httpx.Response(200, json={"response": "ok"})

    client = CopilotClient("https://example.invalid", patient_id="patient-1")
    client._transport = httpx.MockTransport(handler)
    await client.chat("hello")
    assert captured[0] == {"patient_id": "patient-1", "physician_user_id": "admin"}
