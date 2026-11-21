"""Tests for the Soniox API client protocol handling."""

from __future__ import annotations

import asyncio
import base64
from collections import deque
from collections.abc import AsyncIterator, Generator
import json
import ssl
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from soniox.errors import SonioxAuthenticationError as SDKAuthenticationError
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedOK, InvalidStatus
from websockets.frames import Close
from websockets.http11 import Response

from custom_components.soniox.api import (
    SonioxApiClient,
    SonioxAuthenticationError,
    SonioxCommunicationError,
    SonioxRequestError,
)
from custom_components.soniox.api.client import SonioxEndpoints
from custom_components.soniox.const import Region

SSL_CONTEXT = ssl.create_default_context()


class FakeWebSocket:
    """Websocket double that replies once the client has ended its input."""

    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.sent: list[str | bytes] = []
        self._replies = deque(json.dumps(reply) for reply in replies)
        self._input_done = asyncio.Event()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)
        if message == "" or (isinstance(message, str) and ('"text_end":true' in message or '"cancel":true' in message)):
            self._input_done.set()

    async def recv(self) -> str:
        await self._input_done.wait()
        if self._replies:
            return self._replies.popleft()
        raise ConnectionClosedOK(Close(1000, ""), Close(1000, ""), rcvd_then_sent=True)

    def sent_json(self) -> list[dict[str, Any]]:
        return [json.loads(message) for message in self.sent if isinstance(message, str) and message]


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


def _audio_event(data: bytes, **extra: Any) -> dict[str, Any]:
    return {"stream_id": "s", "audio": base64.b64encode(data).decode(), **extra}


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
        [
            {"tokens": [{"text": "Turn", "is_final": True}, {"text": " of", "is_final": False}]},
            {
                "tokens": [{"text": " on", "is_final": True}, {"text": " the lights ", "is_final": True}],
                "finished": True,
            },
        ]
    )
    connect.return_value = websocket

    transcript = await client.async_transcribe(_chunks(b"\x01", b"", b"\x02"), model="stt-rt-v6", language="de")

    assert transcript == "Turn on the lights"
    assert connect.call_args.args == ("wss://stt-rt.eu.soniox.com/transcribe-websocket",)
    assert connect.call_args.kwargs["additional_headers"] == {"Authorization": "Bearer key"}
    assert connect.call_args.kwargs["ssl"] is SSL_CONTEXT
    # The key travels in the handshake header only; Soniox rejects it in the config too.
    assert websocket.sent_json()[0] == {
        "model": "stt-rt-v6",
        "audio_format": "pcm_s16le",
        "sample_rate": 16000,
        "num_channels": 1,
        "language_hints": ["de"],
        "language_hints_strict": True,
    }
    assert websocket.sent[1:] == [b"\x01", b"\x02", ""]


async def test_transcribe_error_event(client: SonioxApiClient, connect: MagicMock) -> None:
    connect.return_value = FakeWebSocket([{"error_code": 401, "error_message": "Invalid API key"}])
    with pytest.raises(SonioxAuthenticationError, match="Invalid API key"):
        await client.async_transcribe(_chunks(b"\x01"), model="stt-rt-v6", language=None)


async def test_handshake_rejected(client: SonioxApiClient, connect: MagicMock) -> None:
    connect.side_effect = InvalidStatus(Response(401, "Unauthorized", Headers()))
    with pytest.raises(SonioxAuthenticationError):
        await client.async_transcribe(_chunks(b"\x01"), model="stt-rt-v6", language=None)


async def test_audio_source_failure_stops_transcription(client: SonioxApiClient, connect: MagicMock) -> None:
    async def failing_audio() -> AsyncIterator[bytes]:
        yield b"\x01"
        raise RuntimeError("microphone gone")

    # No reply ever arrives: the failure must not wait for the server.
    connect.return_value = FakeWebSocket([])
    with pytest.raises(RuntimeError, match="microphone gone"):
        await client.async_transcribe(failing_audio(), model="stt-rt-v6", language=None)


async def test_synthesize_stream(client: SonioxApiClient, connect: MagicMock) -> None:
    websocket = FakeWebSocket(
        [_audio_event(b"ab"), _audio_event(b"cd", audio_end=True), {"stream_id": "s", "terminated": True}]
    )
    connect.return_value = websocket

    chunks = [
        chunk
        async for chunk in client.async_synthesize_stream(
            _chunks("Hel", "lo"), model="tts-rt-v2", language="en", voice="Maya", speed=1.1
        )
    ]

    assert chunks == [b"ab", b"cd"]
    assert connect.call_args.args == ("wss://tts-rt.eu.soniox.com/tts-websocket",)
    config, *messages = websocket.sent_json()
    assert config["voice"] == "Maya"
    assert config["audio_format"] == "mp3"
    assert config["speed"] == 1.1
    assert [(m["text"], m["text_end"]) for m in messages] == [("Hel", False), ("lo", False), ("", True)]
    assert {m["stream_id"] for m in messages} == {config["stream_id"]}


async def test_synthesize_splits_long_text(client: SonioxApiClient, connect: MagicMock) -> None:
    websocket = FakeWebSocket([{"terminated": True}])
    connect.return_value = websocket

    await client.async_synthesize("x" * 12000, model="tts-rt-v2", language="en", voice="Maya", speed=None)

    texts = [m["text"] for m in websocket.sent_json()[1:]]
    assert [len(text) for text in texts] == [5000, 5000, 2000, 0]


async def test_text_source_failure_cancels_stream(client: SonioxApiClient, connect: MagicMock) -> None:
    async def failing_text() -> AsyncIterator[str]:
        yield "Hello"
        raise RuntimeError("llm failed")

    websocket = FakeWebSocket([{"stream_id": "s", "terminated": True}])
    connect.return_value = websocket

    with pytest.raises(RuntimeError, match="llm failed"):
        async for _ in client.async_synthesize_stream(
            failing_text(), model="tts-rt-v2", language="en", voice="Maya", speed=None
        ):
            pass

    assert websocket.sent_json()[-1]["cancel"] is True


async def test_tts_error_event(client: SonioxApiClient, connect: MagicMock) -> None:
    connect.return_value = FakeWebSocket([{"stream_id": "s", "error_code": 400, "error_message": "Unknown voice"}])
    with pytest.raises(SonioxRequestError, match="Unknown voice"):
        await client.async_synthesize("Hi", model="tts-rt-v2", language="en", voice="Nobody", speed=None)


def _model(model_id: str, *, alias: str | None = None, mode: str = "real_time", **extra: Any) -> SimpleNamespace:
    languages = [SimpleNamespace(code="en", name="English")]
    return SimpleNamespace(id=model_id, aliased_model_id=alias, transcription_mode=mode, languages=languages, **extra)


def _mock_rest(client: SonioxApiClient) -> MagicMock:
    rest = MagicMock()
    client._client = rest  # noqa: SLF001
    return rest


async def test_catalog_prefers_default_models_and_ready_clones(client: SonioxApiClient) -> None:
    rest = _mock_rest(client)
    rest.models.list = AsyncMock(
        return_value=SimpleNamespace(
            models=[_model("stt-async-v6", mode="async"), _model("stt-rt-v5"), _model("stt-rt-v6")]
        )
    )
    tts_model = _model("tts-rt-v2", voices=[SimpleNamespace(id="Maya")], supports_voice_cloning=True)
    rest.tts_models.list = AsyncMock(return_value=SimpleNamespace(models=[tts_model]))

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


async def test_catalog_falls_back_to_newest_model(client: SonioxApiClient) -> None:
    rest = _mock_rest(client)
    rest.models.list = AsyncMock(
        return_value=SimpleNamespace(models=[_model("stt-rt-v7"), _model("stt-rt-latest", alias="stt-rt-v7")])
    )
    rest.tts_models.list = AsyncMock(return_value=SimpleNamespace(models=[]))

    catalog = await client.async_get_catalog()

    assert catalog.stt.model == "stt-rt-v7"
    assert catalog.tts is None


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (SDKAuthenticationError("bad key"), SonioxAuthenticationError),
        (httpx.ConnectError("no route"), SonioxCommunicationError),
    ],
)
async def test_catalog_error_mapping(client: SonioxApiClient, error: Exception, expected: type[Exception]) -> None:
    rest = _mock_rest(client)
    rest.models.list = AsyncMock(side_effect=error)
    with pytest.raises(expected):
        await client.async_get_catalog()
