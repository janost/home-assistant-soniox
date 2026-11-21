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
    SttCatalog,
)
from custom_components.soniox.api.client import SonioxEndpoints
from custom_components.soniox.const import Region

SSL_CONTEXT = ssl.create_default_context()
STT = SttCatalog(model="stt-rt-v6", languages={"en": "English"}, strict_language_hints=True)
CLIENT_MODULE = "custom_components.soniox.api.client"
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

    def push(self, *replies: Reply) -> None:
        for reply in replies:
            self._inbox.put_nowait(reply)

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)
        self.push(*self._respond(message))

    async def recv(self) -> str:
        reply = await self._inbox.get()
        if reply is CLOSE:
            raise ConnectionClosedOK(Close(1001, ""), Close(1001, ""), rcvd_then_sent=True)
        return json.dumps(reply)

    def sent_json(self) -> list[dict[str, Any]]:
        return [json.loads(message) for message in self.sent if isinstance(message, str) and message]


class TtsServer:
    """Fake Soniox TTS server.

    On `text_end` it answers with one audio chunk holding the stream's text,
    then terminates the stream. Streams listed in `hold` (by open order) stay
    active until `finish` is called. Opening a stream while another is active
    is counted in `overlaps`.
    """

    def __init__(self, *, hold: tuple[int, ...] = (), close_early: bool = False) -> None:
        self.websocket = FakeWebSocket(self._respond)
        self.order: list[str] = []
        self.texts: dict[str, str] = {}
        self.active: set[str] = set()
        self.overlaps = 0
        self.keepalives = 0
        self.text_end_seen = asyncio.Event()
        self._hold = hold
        self._close_early = close_early

    def finish(self, stream_id: str) -> None:
        self.active.discard(stream_id)
        audio = base64.b64encode(self.texts[stream_id].encode()).decode()
        self.websocket.push({"stream_id": stream_id, "audio": audio}, {"stream_id": stream_id, "terminated": True})

    def _respond(self, message: str | bytes) -> list[Reply]:
        payload = json.loads(message)
        if payload.get("keep_alive"):
            self.keepalives += 1
            return []
        stream_id = payload["stream_id"]
        if "voice" in payload:
            self.overlaps += bool(self.active)
            self.active.add(stream_id)
            self.order.append(stream_id)
            self.texts[stream_id] = ""
            return []
        self.texts[stream_id] += payload["text"]
        if payload["text_end"]:
            self.text_end_seen.set()
            if self._close_early:
                return [CLOSE]
            if self.order.index(stream_id) not in self._hold:
                self.finish(stream_id)
        return []


def on_audio_end(*replies: Reply) -> Responder:
    """STT server: reply once the client signals end of audio."""
    return lambda message: list(replies) if message == "" else []


@pytest.fixture
def client() -> SonioxApiClient:
    return SonioxApiClient(api_key="key", region=Region.EU, ssl_context=SSL_CONTEXT)


@pytest.fixture
def connect() -> Generator[MagicMock]:
    with patch(f"{CLIENT_MODULE}.connect") as mock_connect:
        yield mock_connect


@pytest.fixture
def server(connect: MagicMock) -> TtsServer:
    tts_server = TtsServer()
    connect.return_value = tts_server.websocket
    return tts_server


async def _chunks[T](*items: T) -> AsyncIterator[T]:
    for item in items:
        yield item


async def _forever() -> None:
    await asyncio.Event().wait()


def _synthesize_stream(client: SonioxApiClient, text: AsyncIterator[str], **kwargs: Any) -> AsyncIterator[bytes]:
    options = {"model": "tts-rt-v2", "language": "en", "voice": "Maya", "speed": None} | kwargs
    return client.async_synthesize_stream(text, **options)


async def _synthesize(client: SonioxApiClient, text: AsyncIterator[str], **kwargs: Any) -> list[bytes]:
    return [chunk async for chunk in _synthesize_stream(client, text, **kwargs)]


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


# Speech-to-text


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

    transcript = await client.async_transcribe(_chunks(b"\x01", b"", b"\x02"), catalog=STT, language="de")

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


async def test_transcribe_without_strict_hint_support(client: SonioxApiClient, connect: MagicMock) -> None:
    websocket = FakeWebSocket(on_audio_end({"finished": True}))
    connect.return_value = websocket
    catalog = SttCatalog(model="stt-rt-v5", languages={"en": "English"}, strict_language_hints=False)

    assert await client.async_transcribe(_chunks(b"\x01"), catalog=catalog, language="en") == ""
    assert "language_hints_strict" not in websocket.sent_json()[0]


@pytest.mark.parametrize(
    ("code", "expected"), [(401, SonioxAuthenticationError), (403, SonioxPermissionError), (400, SonioxRequestError)]
)
async def test_transcribe_error_event(
    client: SonioxApiClient, connect: MagicMock, code: int, expected: type[Exception]
) -> None:
    connect.return_value = FakeWebSocket(on_audio_end({"error_code": code, "error_message": "Nope"}))
    with pytest.raises(expected, match="Nope"):
        await client.async_transcribe(_chunks(b"\x01"), catalog=STT, language="en")


async def test_transcribe_closed_before_finished(client: SonioxApiClient, connect: MagicMock) -> None:
    connect.return_value = FakeWebSocket(on_audio_end({"tokens": [{"text": "Turn", "is_final": True}]}, CLOSE))
    with pytest.raises(SonioxCommunicationError):
        await client.async_transcribe(_chunks(b"\x01"), catalog=STT, language="en")


async def test_handshake_rejected(client: SonioxApiClient, connect: MagicMock) -> None:
    connect.side_effect = InvalidStatus(Response(401, "Unauthorized", Headers()))
    with pytest.raises(SonioxAuthenticationError):
        await client.async_transcribe(_chunks(b"\x01"), catalog=STT, language="en")


@pytest.mark.parametrize("error", [RuntimeError("microphone gone"), TimeoutError("no audio")])
async def test_audio_source_error_propagates_unchanged(
    client: SonioxApiClient, connect: MagicMock, error: Exception
) -> None:
    async def failing_audio() -> AsyncIterator[bytes]:
        yield b"\x01"
        raise error

    # No reply ever arrives: the failure must not wait for the server, and a
    # TimeoutError from the source must not turn into a Soniox error.
    connect.return_value = FakeWebSocket(lambda message: [])
    with pytest.raises(type(error)) as caught:
        await client.async_transcribe(failing_audio(), catalog=STT, language="en")
    assert caught.value is error


# Text-to-speech


async def test_synthesize_stream(client: SonioxApiClient, server: TtsServer, connect: MagicMock) -> None:
    chunks = await _synthesize(client, _chunks("Hel", "", "lo", " ", "there"), speed=1.1)

    assert chunks == [b"Hello there"]
    assert connect.call_args.args == ("wss://tts-rt.eu.soniox.com/tts-websocket",)
    config, *messages = server.websocket.sent_json()
    assert config["voice"] == "Maya"
    assert config["audio_format"] == "mp3"
    assert config["speed"] == 1.1
    assert [(m["text"], m["text_end"]) for m in messages] == [
        ("Hel", False),
        ("lo", False),
        (" ", False),
        ("there", False),
        ("", True),
    ]
    assert {m["stream_id"] for m in messages} == {config["stream_id"]}


async def test_pause_rotates_to_next_stream_after_previous_terminated(
    client: SonioxApiClient, connect: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(f"{CLIENT_MODULE}.TTS_IDLE_SECONDS", 0.01)
    server = TtsServer(hold=(0,))
    connect.return_value = server.websocket

    async def text_with_tool_call() -> AsyncIterator[str]:
        yield "Let me check. "
        await server.text_end_seen.wait()  # the pause ended the first stream
        yield "It is sunny."

    consumer = asyncio.create_task(_synthesize(client, text_with_tool_call()))
    await server.text_end_seen.wait()
    for _ in range(20):
        await asyncio.sleep(0)
    # The second text is waiting: the first stream is still producing audio.
    assert len(server.order) == 1

    server.finish(server.order[0])

    assert await consumer == [b"Let me check. ", b"It is sunny."]
    assert len(server.order) == 2
    assert server.overlaps == 0


async def test_long_text_rotates_on_length_budget(
    client: SonioxApiClient, server: TtsServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(f"{CLIENT_MODULE}.TTS_MAX_STREAM_CHARS", 20)
    text = "One two three. Four five six seven eight. " + "x" * 45

    chunks = await _synthesize(client, _chunks(text))

    assert b"".join(chunks).decode() == text
    stream_texts = [server.texts[stream_id] for stream_id in server.order]
    assert stream_texts == ["One two three. ", "Four five six seven ", "eight. " + "x" * 13, "x" * 20, "x" * 12]
    assert server.overlaps == 0


async def test_whitespace_alone_opens_no_stream(client: SonioxApiClient, server: TtsServer) -> None:
    assert await _synthesize(client, _chunks(" ", "")) == []
    assert server.websocket.sent == []


async def test_closed_before_terminated(client: SonioxApiClient, connect: MagicMock) -> None:
    connect.return_value = TtsServer(close_early=True).websocket
    with pytest.raises(SonioxCommunicationError):
        await _synthesize(client, _chunks("Hello"))


async def test_silent_text_source_is_abandoned(
    client: SonioxApiClient, server: TtsServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(f"{CLIENT_MODULE}.TTS_IDLE_SECONDS", 0.01)
    monkeypatch.setattr(f"{CLIENT_MODULE}.TTS_KEEPALIVE_SECONDS", 0.01)
    monkeypatch.setattr(f"{CLIENT_MODULE}.TTS_MAX_IDLE_SECONDS", 0.05)

    async def stalled_text() -> AsyncIterator[str]:
        yield "Hi"
        await _forever()  # a cancelled pipeline never ends its text stream
        yield ""  # pragma: no cover

    with pytest.raises(SonioxCommunicationError, match="No text"):
        await _synthesize(client, stalled_text())
    assert server.keepalives >= 1


async def test_server_silence_times_out_while_text_is_open(
    client: SonioxApiClient, connect: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(f"{CLIENT_MODULE}.TTS_IDLE_SECONDS", 0.01)
    monkeypatch.setattr(f"{CLIENT_MODULE}.RECEIVE_TIMEOUT_SECONDS", 0.05)
    connect.return_value = TtsServer(hold=(0,)).websocket

    async def open_text() -> AsyncIterator[str]:
        yield "Hi"
        await _forever()
        yield ""  # pragma: no cover

    with pytest.raises(SonioxCommunicationError, match="Timed out"):
        await _synthesize(client, open_text())


@pytest.mark.parametrize("error", [RuntimeError("llm failed"), OSError("llm connection reset")])
async def test_text_source_error_propagates_unchanged(
    client: SonioxApiClient, server: TtsServer, error: Exception
) -> None:
    async def failing_text() -> AsyncIterator[str]:
        yield "Hello"
        raise error

    with pytest.raises(type(error)) as caught:
        await _synthesize(client, failing_text())
    assert caught.value is error


async def test_error_after_audio(client: SonioxApiClient, connect: MagicMock) -> None:
    audio = base64.b64encode(b"ab").decode()
    connect.return_value = FakeWebSocket(
        lambda message: (
            [{"stream_id": "s", "audio": audio}, {"stream_id": "s", "error_code": 400, "error_message": "Too long"}]
            if '"text_end":true' in str(message)
            else []
        )
    )
    received: list[bytes] = []

    async def collect() -> None:
        async for chunk in _synthesize_stream(client, _chunks("Hi")):
            received.append(chunk)  # noqa: PERF401 - keeps chunks seen before the error

    with pytest.raises(SonioxRequestError, match="Too long"):
        await collect()
    assert received == [b"ab"]


async def test_error_while_text_is_still_arriving(client: SonioxApiClient, connect: MagicMock) -> None:
    async def endless_text() -> AsyncIterator[str]:
        yield "Hello"
        await _forever()
        yield ""  # pragma: no cover

    connect.return_value = FakeWebSocket(
        lambda message: (
            [{"error_code": 400, "error_message": "Unknown voice"}] if '"text":"Hello"' in str(message) else []
        )
    )
    with pytest.raises(SonioxRequestError, match="Unknown voice"):
        await _synthesize(client, endless_text())


async def test_malformed_audio(client: SonioxApiClient, connect: MagicMock) -> None:
    connect.return_value = FakeWebSocket(
        lambda message: [{"stream_id": "s", "audio": "not base64!"}] if '"text_end":true' in str(message) else []
    )
    with pytest.raises(SonioxRequestError):
        await _synthesize(client, _chunks("Hi"))


async def test_consumer_stopping_early_cleans_up(client: SonioxApiClient, server: TtsServer) -> None:
    stream = _synthesize_stream(client, _chunks("Hi"))
    assert await anext(stream) == b"Hi"

    await stream.aclose()  # type: ignore[attr-defined]

    leftover = [task for task in asyncio.all_tasks() if "_TtsSession" in repr(task) or "_read_source" in repr(task)]
    assert leftover == []


# Catalog


def _model(model_id: str, *, alias: str | None = None, mode: str = "real_time", **extra: Any) -> SimpleNamespace:
    languages = [SimpleNamespace(code="en", name="English")]
    fields = {"supports_language_hints_strict": True} | extra
    return SimpleNamespace(id=model_id, aliased_model_id=alias, transcription_mode=mode, languages=languages, **fields)


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
    stt_models = [_model("stt-async-v6", mode="async"), _model("stt-rt-v5"), _model("stt-rt-v6")]
    rest = _mock_rest(client, stt_models, [tts_model])

    async def cloned_voices() -> AsyncIterator[SimpleNamespace]:
        yield SimpleNamespace(id="v1", name="Me", models=[SimpleNamespace(model="tts-rt-v2", status="ready")])
        yield SimpleNamespace(id="v2", name="WIP", models=[SimpleNamespace(model="tts-rt-v2", status="processing")])

    rest.voices.list_all = cloned_voices

    catalog = await client.async_get_catalog()

    assert catalog.stt == SttCatalog(model="stt-rt-v6", languages={"en": "English"}, strict_language_hints=True)
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
    stt_models = [_model("stt-rt-v9"), _model("stt-rt-v10", supports_language_hints_strict=False)]
    _mock_rest(client, [*stt_models, _model("stt-rt-latest", alias="stt-rt-v10")], [])

    catalog = await client.async_get_catalog()

    assert catalog.stt.model == "stt-rt-v10"
    assert catalog.stt.strict_language_hints is False
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
