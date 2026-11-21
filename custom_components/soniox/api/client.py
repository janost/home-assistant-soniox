"""Async client for the Soniox speech-to-text and text-to-speech APIs.

REST calls (model and voice catalogs) go through the official SDK. The realtime
websockets are opened here directly because the SDK cannot be handed an SSL
context, and letting `websockets` build its own would load the CA bundle inside
the Home Assistant event loop. Messages are still built and parsed with the SDK
types.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
import re
import ssl
from typing import Any, Final
from uuid import uuid4

import httpx
from pydantic import ValidationError
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosedOK, InvalidStatus, WebSocketException

from custom_components.soniox.const import DEFAULT_STT_MODEL, DEFAULT_TTS_MODEL, LOGGER, Region
from soniox import AsyncSonioxClient
from soniox.errors import (
    SonioxAPIError as SDKSonioxAPIError,
    SonioxAuthenticationError as SDKSonioxAuthenticationError,
    SonioxError as SDKSonioxError,
    SonioxPermissionDeniedError as SDKSonioxPermissionDeniedError,
)
from soniox.types import Model, TtsModel
from soniox.types.realtime import (
    RealtimeEvent,
    RealtimeSTTConfig,
    RealtimeTTSCancelMessage,
    RealtimeTTSConfig,
    RealtimeTTSEvent,
    RealtimeTTSKeepAliveMessage,
    RealtimeTTSTextMessage,
)

CONNECT_TIMEOUT_SECONDS: Final = 10.0
# Longest wait for the next message while Soniox owes us a result.
RECEIVE_TIMEOUT_SECONDS: Final = 60.0
# Soniox terminates a TTS stream that gets no text for "a few seconds" without
# `text_end`. When the text source pauses this long (an LLM running a tool
# call), the current stream is ended and the next text opens a new one.
TTS_IDLE_SECONDS: Final = 1.5
# Keepalive interval for the TTS connection while no stream is open.
TTS_KEEPALIVE_SECONDS: Final = 20.0
# Soniox rejects realtime TTS text messages longer than this.
MAX_TTS_TEXT_CHUNK: Final = 5000
TTS_AUDIO_FORMAT: Final = "mp3"


class SonioxError(Exception):
    """Base exception for Soniox client errors."""


class SonioxCommunicationError(SonioxError):
    """Soniox could not be reached or the connection failed."""


class SonioxAuthenticationError(SonioxError):
    """Soniox rejected the API key."""


class SonioxPermissionError(SonioxAuthenticationError):
    """The API key is valid but lacks a permission this request needs."""


class SonioxRequestError(SonioxError):
    """Soniox rejected or failed a request."""


@dataclass(frozen=True, slots=True)
class SonioxEndpoints:
    """Base URLs for one Soniox region."""

    api: str
    stt_websocket: str
    tts_websocket: str

    @classmethod
    def for_region(cls, region: Region) -> SonioxEndpoints:
        """Return the endpoints of a region. US is the unprefixed default."""
        host = "soniox.com" if region is Region.US else f"{region}.soniox.com"
        return cls(
            api=f"https://api.{host}/v1",
            stt_websocket=f"wss://stt-rt.{host}/transcribe-websocket",
            tts_websocket=f"wss://tts-rt.{host}/tts-websocket",
        )


@dataclass(frozen=True, slots=True)
class SttCatalog:
    """Realtime STT model chosen for this API key and its languages (code -> name)."""

    model: str
    languages: dict[str, str]


@dataclass(frozen=True, slots=True)
class TtsCatalog:
    """TTS model chosen for this API key, its languages and voices (id -> label)."""

    model: str
    languages: dict[str, str]
    voices: dict[str, str]


@dataclass(frozen=True, slots=True)
class SonioxCatalog:
    """Models available to an API key. `tts` is None when no TTS model is offered."""

    stt: SttCatalog
    tts: TtsCatalog | None


class SonioxApiClient:
    """Async Soniox client bound to one API key and region."""

    def __init__(self, api_key: str, region: Region, ssl_context: ssl.SSLContext) -> None:
        """Initialize the client. `ssl_context` is shared by REST and websocket calls."""
        self._api_key = api_key
        self._endpoints = SonioxEndpoints.for_region(region)
        self._ssl_context = ssl_context
        # Only the REST namespaces of the SDK client are used.
        self._client = AsyncSonioxClient(api_key=api_key, api_base_url=self._endpoints.api, verify=ssl_context)

    async def async_close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def async_get_catalog(self) -> SonioxCatalog:
        """Fetch the STT and TTS models available to this API key.

        Also used to validate the API key during setup.
        """
        with _translate_errors():
            stt_models = (await self._client.models.list()).models
            tts_models = (await self._client.tts_models.list()).models
            tts = await self._async_build_tts_catalog(tts_models)
        return SonioxCatalog(stt=_build_stt_catalog(stt_models), tts=tts)

    async def async_transcribe(self, audio_stream: AsyncIterable[bytes], *, model: str, language: str) -> str:
        """Stream 16 kHz mono 16-bit PCM audio and return the final transcript.

        Recognition is biased strongly towards `language`.
        """
        config = RealtimeSTTConfig(
            model=model,
            audio_format="pcm_s16le",
            sample_rate=16000,
            num_channels=1,
            language_hints=[language],
            language_hints_strict=True,
        )
        LOGGER.debug("Starting Soniox transcription model=%s language=%s", model, language)

        with _translate_errors():
            async with self._connect(self._endpoints.stt_websocket) as websocket:
                await websocket.send(config.model_dump_json(exclude_none=True))
                sender = asyncio.create_task(_send_audio(websocket, audio_stream))
                receiver = asyncio.create_task(_receive_transcript(websocket))
                try:
                    # Stop early if either side fails; otherwise wait for both.
                    await asyncio.wait((sender, receiver), return_when=asyncio.FIRST_EXCEPTION)
                    # A server error explains a failed send, so it takes precedence.
                    for task in (receiver, sender):
                        if task.done() and (err := task.exception()) is not None:
                            raise err
                    transcript = await receiver
                finally:
                    await _cancel(sender, receiver)

        LOGGER.debug("Soniox transcription finished chars=%s", len(transcript))
        return transcript

    async def async_synthesize_stream(
        self,
        text_stream: AsyncIterable[str],
        *,
        model: str,
        language: str,
        voice: str,
        speed: float | None,
    ) -> AsyncGenerator[bytes]:
        """Synthesize text as it arrives and yield MP3 audio chunks as they are produced.

        Pauses in `text_stream` longer than `TTS_IDLE_SECONDS` split the text
        into consecutive Soniox streams on one connection, so a slow source does
        not hit Soniox's per-stream idle timeout.
        """
        LOGGER.debug("Starting Soniox TTS model=%s language=%s voice=%s", model, language, voice)
        with _translate_errors():
            config = RealtimeTTSConfig(
                stream_id="unset",
                model=model,
                language=language,
                voice=voice,
                audio_format=TTS_AUDIO_FORMAT,
                speed=speed,
            )
            async with self._connect(self._endpoints.tts_websocket) as websocket:
                async for chunk in _TtsSession(websocket, config).run(text_stream):
                    yield chunk

    def _connect(self, url: str) -> connect:
        """Return a websocket connect context manager authenticated with the API key."""
        return connect(
            url,
            ssl=self._ssl_context,
            additional_headers={"Authorization": f"Bearer {self._api_key}"},
            open_timeout=CONNECT_TIMEOUT_SECONDS,
        )

    async def _async_build_tts_catalog(self, models: list[TtsModel]) -> TtsCatalog | None:
        """Pick a TTS model and collect its built-in and cloned voices."""
        model = _pick_model(models, DEFAULT_TTS_MODEL)
        if model is None:
            return None

        voices = {voice.id: voice.id for voice in model.voices}
        if model.supports_voice_cloning:
            try:
                async for cloned in self._client.voices.list_all():
                    if any(state.model == model.id and state.status == "ready" for state in cloned.models):
                        voices[cloned.id] = f"{cloned.name} (cloned)"
            except SDKSonioxPermissionDeniedError:
                # Cloned voices are optional; keys may lack the permission to list them.
                LOGGER.debug("API key cannot list cloned voices; offering built-in voices only")

        return TtsCatalog(
            model=model.id,
            languages={language.code: language.name for language in model.languages},
            voices=voices,
        )


class _TtsSession:
    """One TTS websocket carrying a sequence of Soniox streams.

    A reader task drains the text source into a queue so the sender can wait on
    it with a timeout without cancelling the source. The sender opens a stream
    on the first text, ends it when text pauses or runs out, and opens another
    when text resumes. The caller of `run` receives audio for all streams in
    order and stops once the source is done and every opened stream terminated.
    """

    def __init__(self, websocket: ClientConnection, config: RealtimeTTSConfig) -> None:
        self._websocket = websocket
        self._config = config
        self._stream_id: str | None = None
        self._opened = 0
        self._terminated = 0

    async def run(self, text_stream: AsyncIterable[str]) -> AsyncGenerator[bytes]:
        """Send `text_stream` and yield audio until all streams terminated."""
        queue: asyncio.Queue[str | _SourceEnd] = asyncio.Queue()
        reader = asyncio.create_task(_read_source(text_stream, queue))
        sender = asyncio.create_task(self._send(queue))
        receive: asyncio.Task[str | bytes] | None = None
        try:
            while True:
                if sender.done():
                    sender.result()  # Re-raise a failed text source.
                    if self._terminated >= self._opened:
                        return
                receive = receive or asyncio.create_task(self._websocket.recv())
                waiting_for_audio = self._opened > self._terminated
                done, _ = await asyncio.wait(
                    {receive} if sender.done() else {receive, sender},
                    timeout=RECEIVE_TIMEOUT_SECONDS if waiting_for_audio else None,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    raise SonioxCommunicationError("Timed out waiting for Soniox audio")
                if receive not in done:
                    continue
                try:
                    message = receive.result()
                except ConnectionClosedOK as err:
                    if sender.done() and self._terminated >= self._opened:
                        continue  # Everything arrived; the top of the loop returns.
                    raise SonioxCommunicationError("Soniox closed the connection before finishing") from err
                finally:
                    receive = None
                event = _parse(RealtimeTTSEvent, message)
                _raise_for_event_error(event.error_code, event.error_message)
                if audio := _audio_bytes(event):
                    yield audio
                if event.terminated:
                    self._terminated += 1
        finally:
            await _cancel(*(task for task in (receive, sender, reader) if task is not None))

    async def _send(self, queue: asyncio.Queue[str | _SourceEnd]) -> None:
        """Forward queued text; on failure cancel the open stream so Soniox stops it."""
        try:
            await self._forward(queue)
        except Exception:
            if self._stream_id:
                with suppress(WebSocketException):
                    await self._websocket.send(RealtimeTTSCancelMessage(stream_id=self._stream_id).model_dump_json())
            raise

    async def _forward(self, queue: asyncio.Queue[str | _SourceEnd]) -> None:
        """Send queued text, ending the stream on pauses and opening a new one on resume."""
        while True:
            try:
                async with asyncio.timeout(TTS_IDLE_SECONDS if self._stream_id else TTS_KEEPALIVE_SECONDS):
                    item = await queue.get()
            except TimeoutError:
                if self._stream_id:
                    await self._end_stream()
                else:
                    await self._websocket.send(RealtimeTTSKeepAliveMessage().model_dump_json())
                continue

            if isinstance(item, _SourceEnd):
                if item.error is not None:
                    raise item.error
                break
            if not item:
                continue
            stream_id = self._stream_id or await self._open_stream()
            for start in range(0, len(item), MAX_TTS_TEXT_CHUNK):
                message = RealtimeTTSTextMessage(text=item[start : start + MAX_TTS_TEXT_CHUNK], stream_id=stream_id)
                await self._websocket.send(message.model_dump_json())
        if self._stream_id:
            await self._end_stream()

    async def _open_stream(self) -> str:
        """Start a new Soniox stream on the connection and return its ID."""
        self._stream_id = uuid4().hex
        self._opened += 1
        config = self._config.model_copy(update={"stream_id": self._stream_id})
        await self._websocket.send(config.model_dump_json(exclude_none=True))
        return self._stream_id

    async def _end_stream(self) -> None:
        """Tell Soniox no more text follows for the open stream."""
        message = RealtimeTTSTextMessage(text="", text_end=True, stream_id=self._stream_id or "")
        self._stream_id = None
        await self._websocket.send(message.model_dump_json())


@dataclass(frozen=True, slots=True)
class _SourceEnd:
    """Queue marker: the text source finished, with `error` if it failed."""

    error: Exception | None = None


async def _read_source(text_stream: AsyncIterable[str], queue: asyncio.Queue[str | _SourceEnd]) -> None:
    """Copy a text source into a queue, ending with a `_SourceEnd` marker."""
    try:
        async for text in text_stream:
            queue.put_nowait(text)
    except Exception as err:  # noqa: BLE001 - handed to the sender, which re-raises it
        queue.put_nowait(_SourceEnd(err))
    else:
        queue.put_nowait(_SourceEnd())


def _build_stt_catalog(models: list[Model]) -> SttCatalog:
    """Pick the realtime STT model to use."""
    model = _pick_model([model for model in models if model.transcription_mode == "real_time"], DEFAULT_STT_MODEL)
    if model is None:
        msg = "No realtime speech-to-text model is available for this API key"
        raise SonioxRequestError(msg)
    return SttCatalog(model=model.id, languages={language.code: language.name for language in model.languages})


def _pick_model[M: (Model, TtsModel)](models: list[M], preferred: str) -> M | None:
    """Return the preferred model, else the highest-versioned non-alias model, else None."""
    for model in models:
        if model.id == preferred:
            return model
    candidates = [model for model in models if model.aliased_model_id is None]
    return max(candidates, key=lambda model: _natural_key(model.id), default=None)


def _natural_key(value: str) -> tuple[str | int, ...]:
    """Sort key comparing digit runs numerically, so `v10` sorts after `v9`."""
    return tuple(int(part) if part.isdigit() else part for part in re.split(r"(\d+)", value))


async def _send_audio(websocket: ClientConnection, audio_stream: AsyncIterable[bytes]) -> None:
    """Forward audio to Soniox, then signal end of audio with an empty frame."""
    async for chunk in audio_stream:
        if chunk:
            await websocket.send(chunk)
    await websocket.send("")


async def _receive_transcript(websocket: ClientConnection) -> str:
    """Collect final tokens until Soniox reports the transcription finished."""
    parts: list[str] = []
    while True:
        try:
            async with asyncio.timeout(RECEIVE_TIMEOUT_SECONDS):
                message = await websocket.recv()
        except ConnectionClosedOK as err:
            raise SonioxCommunicationError("Soniox closed the connection before finishing") from err
        event = _parse(RealtimeEvent, message)
        _raise_for_event_error(event.error_code, event.error_message)
        parts.extend(token.text for token in event.tokens if token.is_final)
        if event.finished:
            return "".join(parts).strip()


def _parse[E: (RealtimeEvent, RealtimeTTSEvent)](event_type: type[E], message: str | bytes) -> E:
    """Parse a realtime event, mapping malformed payloads to SonioxRequestError."""
    try:
        return event_type.validate_event(message)
    except ValueError as err:
        raise SonioxRequestError(f"Malformed Soniox message: {err}") from err


def _audio_bytes(event: RealtimeTTSEvent) -> bytes | None:
    """Decode the audio of a TTS event."""
    try:
        return event.audio_bytes()
    except ValueError as err:
        raise SonioxRequestError(str(err)) from err


async def _cancel(*tasks: asyncio.Future[Any]) -> None:
    """Cancel tasks and wait for them, discarding their results."""
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _raise_for_event_error(code: int | None, message: str | None) -> None:
    """Raise if a realtime event carries an error."""
    if code is None and not message:
        return
    text = f"{message or 'Soniox realtime request failed'} (code {code})"
    if code == 401:
        raise SonioxAuthenticationError(text)
    if code == 403:
        raise SonioxPermissionError(text)
    raise SonioxRequestError(text)


@contextmanager
def _translate_errors() -> Iterator[None]:
    """Map SDK, HTTP and websocket exceptions to this module's exceptions."""
    try:
        yield
    except SonioxError:
        raise
    except SDKSonioxPermissionDeniedError as err:
        raise SonioxPermissionError(str(err)) from err
    except SDKSonioxAuthenticationError as err:
        raise SonioxAuthenticationError(str(err)) from err
    except SDKSonioxAPIError as err:
        if err.status_code is not None and (err.status_code == 429 or err.status_code >= 500):
            raise SonioxCommunicationError(str(err)) from err
        raise SonioxRequestError(str(err)) from err
    except (SDKSonioxError, ValidationError) as err:
        raise SonioxRequestError(str(err)) from err
    except InvalidStatus as err:
        if err.response.status_code == 401:
            raise SonioxAuthenticationError(str(err)) from err
        if err.response.status_code == 403:
            raise SonioxPermissionError(str(err)) from err
        raise SonioxCommunicationError(str(err)) from err
    except (httpx.HTTPError, WebSocketException, OSError, TimeoutError) as err:
        raise SonioxCommunicationError(str(err) or type(err).__name__) from err
