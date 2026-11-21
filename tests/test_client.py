"""Tests for the Soniox API client protocol handling."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator, Callable, Generator
import json
import ssl
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from soniox.errors import (
    SonioxAuthenticationError as SDKAuthenticationError,
    SonioxPermissionDeniedError as SDKPermissionDeniedError,
)
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedOK, InvalidStatus
from websockets.frames import Close
from websockets.http11 import Response

from custom_components.soniox.api import (
    SonioxApiClient,
    SonioxAuthenticationError,
    SonioxCommunicationError,
    SonioxPermissionError,
    SonioxRequestError,
)
from custom_components.soniox.api.client import SonioxEndpoints
from custom_components.soniox.const import Region

SSL_CONTEXT = ssl.create_default_context()
CLOSE = object()

type Reply = dict[str, Any] | object
type Responder = Callable[[str | bytes], list[Reply]]


class FakeWebSocket:
    """Websocket double. `respond` maps each sent message to the replies it triggers."""

    def __init__(self, respond: Responder) -> None:
        self.sent: list[str | bytes] = []
        self._respond = respond
        self._inbox: asyncio.Queue[Reply] = asyncio.Queue()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)
        for reply in self._respond(message):
            self._inbox.put_nowait(reply)

    async def recv(self) -> str:
        reply = await self._inbox.get()
        if reply is CLOSE:
            raise ConnectionClosedOK(Close(1001, ""), Close(1001, ""), rcvd_then_sent=True)
        return json.dumps(reply)

    def sent_json(self) -> list[dict[str, Any]]:
        return [json.loads(message) for message in self.sent if isinstance(message, str) and message]


def on_audio_end(*replies: Reply) -> Responder:
    """STT server: reply once the client signals end of audio."""
    return lambda message: list(replies) if message == "" else []


def tts_server(*, close_early: bool = False) -> Responder:
    """TTS server: on `text_end`, send one audio chunk per stream (its text) and terminate it."""
    texts: dict[str, str] = {}

    def respond(message: str | bytes) -> list[Reply]:
        payload = json.loads(message)
        stream_id = payload.get("stream_id")
        if "text" in payload:
            texts[stream_id] = texts.get(stream_id, "") + payload["text"]
        if payload.get("text_end"):
            if close_early:
                return [CLOSE]
            audio = base64.b64encode(texts[stream_id].encode()).decode()
            return [{"stream_id": stream_id, "audio": audio}, {"stream_id": stream_id, "terminated": True}]
        if payload.get("cancel"):
            return [{"stream_id": stream_id, "terminated": True}]
        return []

    return respond


@pytest.fixture
def client() -> SonioxApiClient:
    return SonioxApiClient(api_key="key", region=Region.EU, ssl_context=SSL_CONTEXT)


@pytest.fixture
def connect() -> Generator[MagicMock]:
    with patch("custom_components.soniox.api.client.connect") as mock_connect:
        yield mock_connect


async def _chunks[T](*items: T) -> AsyncIterator[T]:
    for item in items:
        yield item


async def _synthesize(client: SonioxApiClient, text: AsyncIterator[str], **kwargs: Any) -> list[bytes]:
    options = {"model": "tts-rt-v2", "language": "en", "voice": "Maya", "speed": None} | kwargs
    return [chunk async for chunk in client.async_synthesize_stream(text, **options)]


def test_endpoints_per_region() -> None:
    assert SonioxEndpoints.for_region(Region.US) == SonioxEndpoints(
        api="https://api.soniox.com/v1",
        stt_websocket="wss://stt-rt.soniox.com/transcribe-websocket",
        tts_websocket="wss://tts-rt.soniox.com/tts-websocket",
    )
    assert SonioxEndpoints.for_region(Region.EU) == SonioxEndpoints(
        api="https://api.eu.soniox.com/v1",
        stt_websocket="wss://stt-rt.eu.soniox.com/transcribe-websocket",
        tts_websocket="wss://tts-rt.eu.soniox.com/tts-websocket",
    )


async def test_transcribe(client: SonioxApiClient, connect: MagicMock) -> None:
    websocket = FakeWebSocket(
        on_audio_end(
            {"tokens": [{"text": "Turn", "is_final": True}, {"text": " of", "is_final": False}]},
            {
                "tokens": [{"text": " on", "is_final": True}, {"text": " the lights ", "is_final": True}],
                "finished": True,
            },
        )
    )
    connect.return_value = websocket

    transcript = await client.async_transcribe(_chunks(b"\x01", b"", b"\x02"), model="stt-rt-v6", language="de")

    assert transcript == "Turn on the lights"
    assert connect.call_args.args == ("wss://stt-rt.eu.soniox.com/transcribe-websocket",)
    assert connect.call_args.kwargs["additional_headers"] == {"Authorization": "Bearer key"}
    assert connect.call_args.kwargs["ssl"] is SSL_CONTEXT
    # The key travels in the handshake header only.
    assert websocket.sent_json()[0] == {
        "model": "stt-rt-v6",
        "audio_format": "pcm_s16le",
        "sample_rate": 16000,
        "num_channels": 1,
        "language_hints": ["de"],
        "language_hints_strict": True,
    }
    assert websocket.sent[1:] == [b"\x01", b"\x02", ""]


@pytest.mark.parametrize(
    ("code", "expected"), [(401, SonioxAuthenticationError), (403, SonioxPermissionError), (400, SonioxRequestError)]
)
async def test_transcribe_error_event(
    client: SonioxApiClient, connect: MagicMock, code: int, expected: type[Exception]
) -> None:
    connect.return_value = FakeWebSocket(on_audio_end({"error_code": code, "error_message": "Nope"}))
    with pytest.raises(expected, match="Nope"):
        await client.async_transcribe(_chunks(b"\x01"), model="stt-rt-v6", language="en")


async def test_transcribe_closed_before_finished(client: SonioxApiClient, connect: MagicMock) -> None:
    connect.return_value = FakeWebSocket(on_audio_end({"tokens": [{"text": "Turn", "is_final": True}]}, CLOSE))
    with pytest.raises(SonioxCommunicationError):
        await client.async_transcribe(_chunks(b"\x01"), model="stt-rt-v6", language="en")


async def test_handshake_rejected(client: SonioxApiClient, connect: MagicMock) -> None:
    connect.side_effect = InvalidStatus(Response(401, "Unauthorized", Headers()))
    with pytest.raises(SonioxAuthenticationError):
        await client.async_transcribe(_chunks(b"\x01"), model="stt-rt-v6", language="en")


async def test_audio_source_failure_stops_transcription(client: SonioxApiClient, connect: MagicMock) -> None:
    async def failing_audio() -> AsyncIterator[bytes]:
        yield b"\x01"
        raise RuntimeError("microphone gone")

    # No reply ever arrives: the failure must not wait for the server.
    connect.return_value = FakeWebSocket(lambda message: [])
    with pytest.raises(RuntimeError, match="microphone gone"):
        await client.async_transcribe(failing_audio(), model="stt-rt-v6", language="en")


async def test_synthesize_stream(client: SonioxApiClient, connect: MagicMock) -> None:
    websocket = FakeWebSocket(tts_server())
    connect.return_value = websocket

    chunks = await _synthesize(client, _chunks("Hel", "", "lo"), speed=1.1)

    assert chunks == [b"Hello"]
    assert connect.call_args.args == ("wss://tts-rt.eu.soniox.com/tts-websocket",)
    config, *messages = websocket.sent_json()
    assert config["voice"] == "Maya"
    assert config["audio_format"] == "mp3"
    assert config["speed"] == 1.1
    assert [(m["text"], m["text_end"]) for m in messages] == [("Hel", False), ("lo", False), ("", True)]
    assert {m["stream_id"] for m in messages} == {config["stream_id"]}


async def test_synthesize_rotates_streams_on_pause(
    client: SonioxApiClient, connect: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("custom_components.soniox.api.client.TTS_IDLE_SECONDS", 0.01)

    async def text_with_tool_call() -> AsyncIterator[str]:
        yield "Let me check. "
        await asyncio.sleep(0.05)  # LLM runs a tool
        yield "It is sunny."

    websocket = FakeWebSocket(tts_server())
    connect.return_value = websocket

    chunks = await _synthesize(client, text_with_tool_call())

    assert chunks == [b"Let me check. ", b"It is sunny."]
    configs = [m for m in websocket.sent_json() if "voice" in m]
    assert len(configs) == 2
    assert configs[0]["stream_id"] != configs[1]["stream_id"]


async def test_synthesize_empty_text(client: SonioxApiClient, connect: MagicMock) -> None:
    websocket = FakeWebSocket(tts_server())
    connect.return_value = websocket

    assert await _synthesize(client, _chunks("")) == []
    assert websocket.sent == []


async def test_synthesize_splits_long_text(client: SonioxApiClient, connect: MagicMock) -> None:
    websocket = FakeWebSocket(tts_server())
    connect.return_value = websocket

    await _synthesize(client, _chunks("x" * 12000))

    texts = [m["text"] for m in websocket.sent_json()[1:]]
    assert [len(text) for text in texts] == [5000, 5000, 2000, 0]


async def test_synthesize_closed_before_terminated(client: SonioxApiClient, connect: MagicMock) -> None:
    connect.return_value = FakeWebSocket(tts_server(close_early=True))
    with pytest.raises(SonioxCommunicationError):
        await _synthesize(client, _chunks("Hello"))


async def test_text_source_failure_cancels_stream(client: SonioxApiClient, connect: MagicMock) -> None:
    async def failing_text() -> AsyncIterator[str]:
        yield "Hello"
        raise RuntimeError("llm failed")

    websocket = FakeWebSocket(tts_server())
    connect.return_value = websocket

    with pytest.raises(RuntimeError, match="llm failed"):
        await _synthesize(client, failing_text())

    assert websocket.sent_json()[-1]["cancel"] is True


async def test_tts_error_while_text_is_still_arriving(client: SonioxApiClient, connect: MagicMock) -> None:
    source_done = asyncio.Event()

    async def endless_text() -> AsyncIterator[str]:
        yield "Hello"
        await source_done.wait()

    # The server fails the stream as soon as the first text arrives.
    connect.return_value = FakeWebSocket(
        lambda message: (
            [{"error_code": 400, "error_message": "Unknown voice"}] if '"text":"Hello"' in str(message) else []
        )
    )
    with pytest.raises(SonioxRequestError, match="Unknown voice"):
        await _synthesize(client, endless_text())


async def test_tts_malformed_message(client: SonioxApiClient, connect: MagicMock) -> None:
    connect.return_value = FakeWebSocket(
        lambda message: [{"stream_id": "s", "audio": "not base64!"}] if '"text_end":true' in str(message) else []
    )
    with pytest.raises(SonioxRequestError):
        await _synthesize(client, _chunks("Hi"))


def _model(model_id: str, *, alias: str | None = None, mode: str = "real_time", **extra: Any) -> SimpleNamespace:
    languages = [SimpleNamespace(code="en", name="English")]
    return SimpleNamespace(id=model_id, aliased_model_id=alias, transcription_mode=mode, languages=languages, **extra)


def _mock_rest(
    client: SonioxApiClient, stt_models: list[SimpleNamespace], tts_models: list[SimpleNamespace]
) -> MagicMock:
    rest = MagicMock()
    rest.models.list = AsyncMock(return_value=SimpleNamespace(models=stt_models))
    rest.tts_models.list = AsyncMock(return_value=SimpleNamespace(models=tts_models))
    client._client = rest  # noqa: SLF001
    return rest


async def test_catalog_prefers_default_models_and_ready_clones(client: SonioxApiClient) -> None:
    tts_model = _model("tts-rt-v2", voices=[SimpleNamespace(id="Maya")], supports_voice_cloning=True)
    rest = _mock_rest(
        client, [_model("stt-async-v6", mode="async"), _model("stt-rt-v5"), _model("stt-rt-v6")], [tts_model]
    )

    async def cloned_voices() -> AsyncIterator[SimpleNamespace]:
        yield SimpleNamespace(id="v1", name="Me", models=[SimpleNamespace(model="tts-rt-v2", status="ready")])
        yield SimpleNamespace(id="v2", name="WIP", models=[SimpleNamespace(model="tts-rt-v2", status="processing")])

    rest.voices.list_all = cloned_voices

    catalog = await client.async_get_catalog()

    assert catalog.stt.model == "stt-rt-v6"
    assert catalog.stt.languages == {"en": "English"}
    assert catalog.tts is not None
    assert catalog.tts.model == "tts-rt-v2"
    assert catalog.tts.voices == {"Maya": "Maya", "v1": "Me (cloned)"}


async def test_catalog_without_cloned_voice_permission(client: SonioxApiClient) -> None:
    tts_model = _model("tts-rt-v2", voices=[SimpleNamespace(id="Maya")], supports_voice_cloning=True)
    rest = _mock_rest(client, [_model("stt-rt-v6")], [tts_model])

    async def forbidden() -> AsyncIterator[SimpleNamespace]:
        raise SDKPermissionDeniedError("no access")
        yield  # pragma: no cover

    rest.voices.list_all = forbidden

    catalog = await client.async_get_catalog()

    assert catalog.tts is not None
    assert catalog.tts.voices == {"Maya": "Maya"}


async def test_catalog_falls_back_to_newest_model(client: SonioxApiClient) -> None:
    _mock_rest(client, [_model("stt-rt-v9"), _model("stt-rt-v10"), _model("stt-rt-latest", alias="stt-rt-v10")], [])

    catalog = await client.async_get_catalog()

    assert catalog.stt.model == "stt-rt-v10"
    assert catalog.tts is None


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (SDKAuthenticationError("bad key"), SonioxAuthenticationError),
        (SDKPermissionDeniedError("no model listing"), SonioxPermissionError),
        (httpx.ConnectError("no route"), SonioxCommunicationError),
    ],
)
async def test_catalog_error_mapping(client: SonioxApiClient, error: Exception, expected: type[Exception]) -> None:
    rest = _mock_rest(client, [], [])
    rest.models.list = AsyncMock(side_effect=error)
    with pytest.raises(expected):
        await client.async_get_catalog()
