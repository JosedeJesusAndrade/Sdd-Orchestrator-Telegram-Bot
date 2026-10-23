"""Tests for the OpenCode v1 HTTP client (MockTransport, no live server)."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest

import services.opencode_server.client as client_module
from services.opencode_server.client import OpenCodeServerClient
from services.opencode_server.errors import (
    OpenCodeAuthError,
    OpenCodeRequestError,
    OpenCodeUnavailableError,
)

Handler = Callable[[httpx.Request], httpx.Response]

EXPECTED_AUTH = "Basic " + base64.b64encode(b"opencode:secret").decode()
EXPECTED_DIRECTORY = Path(r"C:\project\demo").as_posix()

SSE_BODY = (
    ": heartbeat\n"
    "\n"
    'data: {"id":"evt_1","type":"plugin.added","properties":{}}\n'
    "\n"
    'data: {"id":"evt_2","type":"server.connected","properties":{}}\n'
    "\n"
    'data: {"id":"evt_3","type":"message.part.delta","properties":{"delta":"hola"}}\n'
    "\n"
    'data: {"id":"evt_4","type":"permission.asked","properties":{"id":"per_1"}}\n'
    "\n"
    'data: {"id":"evt_5","type":"catalog.updated","properties":{}}\n'
    "\n"
    'data: {"id":"evt_6","type":"reference.updated","properties":{}}\n'
    "\n"
    'data: {"id":"evt_7","type":"integration.updated","properties":{}}\n'
    "\n"
    'data: {"id":"evt_8","type":"session.idle","properties":{"sessionID":"ses_1"}}\n'
    "\n"
    'data: {"id":"evt_9",\n'
    'data:  "type":"message.part.updated",\n'
    'data:  "properties":{"part":{}}}\n'
    "\n"
)


@pytest.fixture
async def make_client() -> AsyncIterator[Callable[..., OpenCodeServerClient]]:
    created: list[OpenCodeServerClient] = []

    def factory(
        handler: Handler,
        *,
        password: str = "secret",
        directory: str = r"C:\project\demo",
    ) -> OpenCodeServerClient:
        client = OpenCodeServerClient(
            "http://127.0.0.1:4096",
            password,
            directory,
            transport=httpx.MockTransport(handler),
        )
        created.append(client)
        return client

    yield factory

    for client in created:
        await client.close()


def _assert_auth_and_directory(request: httpx.Request) -> None:
    assert request.headers["Authorization"] == EXPECTED_AUTH
    assert request.url.params.get("directory") == EXPECTED_DIRECTORY


async def test_create_session_posts_title_with_auth_and_directory(
    make_client: Callable[..., OpenCodeServerClient],
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        _assert_auth_and_directory(request)
        assert request.method == "POST"
        assert request.url.path == "/session"
        captured["json"] = json.loads(request.content) if request.content else None
        return httpx.Response(200, json={"id": "ses_abc"})

    client = make_client(handler)

    session_id = await client.create_session("mi título")

    assert session_id == "ses_abc"
    assert captured["json"] == {"title": "mi título"}


async def test_send_prompt_splits_model_at_first_slash_and_omits_empty_agent(
    make_client: Callable[..., OpenCodeServerClient],
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        _assert_auth_and_directory(request)
        assert request.url.path == "/session/ses_1/prompt_async"
        captured["json"] = json.loads(request.content)
        return httpx.Response(204)

    client = make_client(handler)

    await client.send_prompt("ses_1", "hola", model="deepseek/deepseek-flash", agent="build")

    first = captured["json"]
    assert isinstance(first, dict)
    assert first["parts"] == [{"type": "text", "text": "hola"}]
    assert first["model"] == {"providerID": "deepseek", "modelID": "deepseek-flash"}
    assert first["agent"] == "build"


async def test_send_prompt_model_with_extra_slashes_keeps_first_split(
    make_client: Callable[..., OpenCodeServerClient],
) -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert isinstance(body, dict)
        bodies.append(body)
        return httpx.Response(204)

    client = make_client(handler)

    await client.send_prompt("ses_1", "x", model="openrouter/vendor/model")

    assert bodies[0]["model"] == {"providerID": "openrouter", "modelID": "vendor/model"}
    assert "agent" not in bodies[0]


async def test_send_prompt_rejects_invalid_model_without_a_request(
    make_client: Callable[..., OpenCodeServerClient],
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("transport must not be called")

    client = make_client(handler)

    with pytest.raises(ValueError):
        await client.send_prompt("ses_1", "x", model="sin-slash")


async def test_get_messages_parses_json(
    make_client: Callable[..., OpenCodeServerClient],
) -> None:
    messages = [{"info": {"id": "msg_1"}, "parts": [{"type": "text", "text": "hi"}]}]

    def handler(request: httpx.Request) -> httpx.Response:
        _assert_auth_and_directory(request)
        assert request.url.path == "/session/ses_1/message"
        assert request.url.params.get("limit") == "5"
        return httpx.Response(200, json=messages)

    client = make_client(handler)

    assert await client.get_messages("ses_1", limit=5) == messages


async def test_list_permissions_maps_the_bare_array(
    make_client: Callable[..., OpenCodeServerClient],
) -> None:
    raw = [
        {
            "id": "per_1",
            "sessionID": "ses_1",
            "permission": "bash",
            "patterns": ["git commit -m x"],
            "metadata": {"command": "git commit -m x"},
            "always": ["git commit *"],
            "tool": {"messageID": "msg_1", "callID": "call_1"},
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/permission"
        return httpx.Response(200, json=raw)

    client = make_client(handler)

    requests = await client.list_permissions()

    assert len(requests) == 1
    request = requests[0]
    assert request.id == "per_1"
    assert request.session_id == "ses_1"
    assert request.permission == "bash"
    assert request.patterns == ("git commit -m x",)
    assert request.always == ("git commit *",)
    assert request.metadata == {"command": "git commit -m x"}
    assert request.message_id == "msg_1"
    assert request.call_id == "call_1"


async def test_reply_permission_posts_reply_and_message(
    make_client: Callable[..., OpenCodeServerClient],
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/permission/per_1/reply"
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json=True)

    client = make_client(handler)

    assert await client.reply_permission("per_1", "reject", message="no gracias") is True
    assert captured["json"] == {"reply": "reject", "message": "no gracias"}


async def test_401_raises_auth_error_without_retry(
    make_client: Callable[..., OpenCodeServerClient],
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(401)

    client = make_client(handler)

    with pytest.raises(OpenCodeAuthError):
        await client.get_messages("ses_1")

    assert calls == ["/session/ses_1/message"]


async def test_4xx_raises_request_error_with_status_and_body(
    make_client: Callable[..., OpenCodeServerClient],
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad request detail")

    client = make_client(handler)

    with pytest.raises(OpenCodeRequestError) as exc_info:
        await client.create_session("x")

    assert exc_info.value.status_code == 400
    assert "bad request detail" in exc_info.value.body


async def test_5xx_on_get_retries_twice_then_raises_unavailable(
    make_client: Callable[..., OpenCodeServerClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_module, "_RETRY_DELAYS", (0.0, 0.0))
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(500, text="boom")

    client = make_client(handler)

    with pytest.raises(OpenCodeUnavailableError) as exc_info:
        await client.get_messages("ses_1")

    assert len(calls) == 3
    assert exc_info.value.status_code == 500


async def test_5xx_then_success_recovers_on_retry(
    make_client: Callable[..., OpenCodeServerClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_module, "_RETRY_DELAYS", (0.0, 0.0))
    responses = [httpx.Response(500), httpx.Response(200, json=[])]

    def handler(_request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    client = make_client(handler)

    assert await client.get_messages("ses_1") == []


async def test_transport_error_retries_then_raises_unavailable(
    make_client: Callable[..., OpenCodeServerClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_module, "_RETRY_DELAYS", (0.0, 0.0))
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        raise httpx.ConnectError("connection refused", request=request)

    client = make_client(handler)

    with pytest.raises(OpenCodeUnavailableError) as exc_info:
        await client.get_messages("ses_1")

    assert len(calls) == 3
    assert exc_info.value.status_code is None


async def test_prompt_does_not_retry_on_5xx(
    make_client: Callable[..., OpenCodeServerClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_module, "_RETRY_DELAYS", (0.0, 0.0))
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(500)

    client = make_client(handler)

    with pytest.raises(OpenCodeUnavailableError):
        await client.send_prompt("ses_1", "x", model="deepseek/flash")

    assert len(calls) == 1


async def test_connect_error_is_marked_retryable(
    make_client: Callable[..., OpenCodeServerClient],
) -> None:
    """#12: the request never left, so a permission reply may be resent."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = make_client(handler)

    with pytest.raises(OpenCodeUnavailableError) as exc_info:
        await client.reply_permission("per_1", "once")

    assert exc_info.value.retryable is True


async def test_read_timeout_is_not_marked_retryable(
    make_client: Callable[..., OpenCodeServerClient],
) -> None:
    """A read timeout is ambiguous: the server may already have applied it."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client = make_client(handler)

    with pytest.raises(OpenCodeUnavailableError) as exc_info:
        await client.reply_permission("per_1", "once")

    assert exc_info.value.retryable is False


async def test_5xx_is_not_marked_retryable(
    make_client: Callable[..., OpenCodeServerClient],
) -> None:
    client = make_client(lambda _request: httpx.Response(500))

    with pytest.raises(OpenCodeUnavailableError) as exc_info:
        await client.reply_permission("per_1", "once")

    assert exc_info.value.retryable is False


async def test_health_true_on_200_and_false_on_401(
    make_client: Callable[..., OpenCodeServerClient],
) -> None:
    ok = make_client(lambda _request: httpx.Response(200, json={"healthy": True}))
    unauthorized = make_client(lambda _request: httpx.Response(401))

    assert await ok.health() is True
    assert await unauthorized.health() is False


async def test_abort_returns_false_on_404(
    make_client: Callable[..., OpenCodeServerClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/session/ses_1/abort"
        return httpx.Response(404)

    client = make_client(handler)

    assert await client.abort("ses_1") is False


async def test_abort_returns_true_on_204(
    make_client: Callable[..., OpenCodeServerClient],
) -> None:
    client = make_client(lambda _request: httpx.Response(204))

    assert await client.abort("ses_1") is True


async def test_stream_events_parses_and_filters_sse(
    make_client: Callable[..., OpenCodeServerClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        _assert_auth_and_directory(request)
        assert request.url.path == "/event"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=SSE_BODY,
        )

    client = make_client(handler)

    events = [event async for event in client.stream_events()]

    assert [event["id"] for event in events] == ["evt_3", "evt_4", "evt_8", "evt_9"]
    assert events[0] == {
        "id": "evt_3",
        "type": "message.part.delta",
        "properties": {"delta": "hola"},
    }
    assert events[1]["type"] == "permission.asked"
    assert events[3]["properties"] == {"part": {}}


async def test_stream_events_signals_connection_before_events(
    make_client: Callable[..., OpenCodeServerClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        _assert_auth_and_directory(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=SSE_BODY,
        )

    client = make_client(handler)
    order: list[str] = []

    stream = client.stream_events(on_connect=lambda: order.append("connected"))
    async for event in stream:
        order.append(f"event:{event['id']}")

    # The backend relies on this to open the stream BEFORE sending a prompt.
    assert order[0] == "connected"
    assert order[1:] == ["event:evt_3", "event:evt_4", "event:evt_8", "event:evt_9"]


# -- Per-call directory override -------------------------------------------

OTHER_DIRECTORY = Path(r"C:\conversation\project").as_posix()


async def test_create_session_directory_override(
    make_client: Callable[..., OpenCodeServerClient],
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("directory", ""))
        return httpx.Response(200, json={"id": "ses_1"})

    client = make_client(handler)

    await client.create_session("x", directory=r"C:\conversation\project")

    assert seen == [OTHER_DIRECTORY]


async def test_send_prompt_directory_override(
    make_client: Callable[..., OpenCodeServerClient],
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("directory", ""))
        return httpx.Response(204)

    client = make_client(handler)

    await client.send_prompt(
        "ses_1",
        "x",
        model="deepseek/flash",
        directory=r"C:\conversation\project",
    )

    assert seen == [OTHER_DIRECTORY]


async def test_get_messages_directory_override(
    make_client: Callable[..., OpenCodeServerClient],
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("directory", ""))
        return httpx.Response(200, json=[])

    client = make_client(handler)

    assert await client.get_messages("ses_1", directory=r"C:\conversation\project") == []
    assert seen == [OTHER_DIRECTORY]


async def test_stream_events_directory_override(
    make_client: Callable[..., OpenCodeServerClient],
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("directory", ""))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text='data: {"id":"evt_1","type":"session.idle","properties":{}}\n\n',
        )

    client = make_client(handler)

    events = [event async for event in client.stream_events(directory=r"C:\conversation\project")]

    assert [event["id"] for event in events] == ["evt_1"]
    assert seen == [OTHER_DIRECTORY]
