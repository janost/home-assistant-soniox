"""Async client for the Soniox speech-to-text and text-to-speech APIs.

REST calls (model and voice catalogs) go through the official SDK. The realtime
websockets are opened here directly because the SDK cannot be handed an SSL
context, and letting `websockets` build its own would load the CA bundle inside
the Home Assistant event loop. Messages are still built and parsed with the SDK
types.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterable, Coroutine, Iterator
from contextlib import aclosing, contextmanager
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
# Soniox caps each TTS stream at 2 minutes of audio. About 15 characters are
# spoken per second, so 1000 characters stay below the cap even at speed 0.7.
TTS_MAX_STREAM_CHARS: Final = 1000
# Keepalive interval for the TTS connection while no stream is open.
TTS_KEEPALIVE_SECONDS: Final = 20.0
# Give up on a text source that stays silent this long. Idle connections count
# against the Soniox connection limit.
TTS_MAX_IDLE_SECONDS: Final = 300.0
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
    strict_language_hints: bool


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


class _SourceError(Exception):
    """Carries an exception raised by a caller-provided audio or text source.

    Keeps the source's own errors (for example a TimeoutError from Home
    Assistant's audio stream) out of the Soniox error mapping.
    """

    def __init__(self, error: Exception) -> None:
        super().__init__(str(error))
        self.error = error


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

    async def async_transcribe(self, audio_stream: AsyncIterable[bytes], *, catalog: SttCatalog, language: str) -> str:
        """Stream 16 kHz mono 16-bit PCM audio and return the final transcript.

        Recognition is biased towards `language`, strictly if the model allows.
        Errors raised by `audio_stream` itself propagate unchanged.
        """
        config = RealtimeSTTConfig(
            model=catalog.model,
            audio_format="pcm_s16le",
            sample_rate=16000,
            num_channels=1,
            language_hints=[language],
            language_hints_strict=True if catalog.strict_language_hints else None,
        )
        LOGGER.debug("Starting Soniox transcription model=%s language=%s", catalog.model, language)

        try:
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
        except _SourceError as failure:
            raise failure.error from None

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

        The text is spoken through consecutive Soniox streams on one connection,
        one at a time, to stay within Soniox's per-stream idle timeout and audio
        length cap. Errors raised by `text_stream` itself propagate unchanged.
        """
        LOGGER.debug("Starting Soniox TTS model=%s language=%s voice=%s", model, language, voice)
        stream_config = {
            "model": model,
            "language": language,
            "voice": voice,
            "audio_format": TTS_AUDIO_FORMAT,
            "speed": speed,
        }
        try:
            with _translate_errors():
                async with self._connect(self._endpoints.tts_websocket) as websocket:
                    session = _TtsSession(websocket, stream_config)
                    async with aclosing(session.run(text_stream)) as audio:
                        async for chunk in audio:
                            yield chunk
        except _SourceError as failure:
            raise failure.error from None

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
    """One TTS websocket speaking a text source through consecutive Soniox streams.

    Three tasks share the connection:

    - reader: copies the text source into a queue, so the sender can wait on it
      with a timeout without cancelling the source.
    - sender: opens a stream on the first text and ends it after a pause or once
      it holds `TTS_MAX_STREAM_CHARS`. It opens the next stream only after the
      previous one terminated, so audio never interleaves.
    - receiver: reads events while a stream is active and forwards its audio.

    `run` yields the forwarded audio and re-raises the first task failure.
    """

    def __init__(self, websocket: ClientConnection, stream_config: dict[str, Any]) -> None:
        self._websocket = websocket
        self._stream_config = stream_config
        self._stream_id: str | None = None  # Stream accepting text, if any.
        self._stream_chars = 0
        self._stream_active = asyncio.Event()  # Set from open until terminated.
        self._stream_done = asyncio.Event()  # Inverse of `_stream_active`.
        self._stream_done.set()
        # Audio chunks, then None when everything was spoken, or an exception.
        self._output: asyncio.Queue[bytes | Exception | None] = asyncio.Queue()

    async def run(self, text_stream: AsyncIterable[str]) -> AsyncGenerator[bytes]:
        """Speak `text_stream` and yield its audio."""
        text: asyncio.Queue[str | _SourceEnd] = asyncio.Queue()
        tasks = [
            asyncio.create_task(_read_source(text_stream, text)),
            asyncio.create_task(self._report(self._send(text), done=True)),
            asyncio.create_task(self._report(self._receive())),
        ]
        try:
            while (item := await self._output.get()) is not None:
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            await _cancel(*tasks)

    async def _report(self, coro: Coroutine[Any, Any, None], *, done: bool = False) -> None:
        """Run a task body and hand its failure, or completion if `done`, to `run`."""
        try:
            await coro
        except Exception as err:  # noqa: BLE001 - re-raised by run()
            self._output.put_nowait(err)
        else:
            if done:
                self._output.put_nowait(None)

    async def _send(self, text: asyncio.Queue[str | _SourceEnd]) -> None:
        """Forward text, rotating streams on pauses and on the length budget."""
        loop = asyncio.get_running_loop()
        idle_since = loop.time()
        while True:
            try:
                async with asyncio.timeout(TTS_IDLE_SECONDS if self._stream_id else TTS_KEEPALIVE_SECONDS):
                    item = await text.get()
            except TimeoutError:
                if self._stream_id:
                    await self._end_stream()
                    idle_since = loop.time()
                elif loop.time() - idle_since >= TTS_MAX_IDLE_SECONDS:
                    raise SonioxCommunicationError(
                        f"No text for {TTS_MAX_IDLE_SECONDS:.0f} s; closing the Soniox connection"
                    ) from None
                else:
                    await self._websocket.send(RealtimeTTSKeepAliveMessage().model_dump_json())
                continue

            if isinstance(item, _SourceEnd):
                if item.error is not None:
                    raise _SourceError(item.error)
                break
            await self._send_text(item)
            idle_since = loop.time()

        if self._stream_id:
            await self._end_stream()
        await self._stream_done.wait()

    async def _send_text(self, text: str) -> None:
        """Send text to the open stream, starting new streams as the budget fills."""
        while text:
            if self._stream_id is None:
                if not text.strip():
                    return  # Separators between messages; nothing to say yet.
                await self._stream_done.wait()
                await self._open_stream()
            room = TTS_MAX_STREAM_CHARS - self._stream_chars
            if len(text) <= room:
                await self._send_chunk(text)
                return
            cut = _split_point(text, room)
            if cut:
                await self._send_chunk(text[:cut])
            await self._end_stream()
            text = text[cut:]

    async def _send_chunk(self, text: str) -> None:
        """Send one text message to the open stream."""
        self._stream_chars += len(text)
        message = RealtimeTTSTextMessage(text=text, stream_id=self._stream_id or "")
        await self._websocket.send(message.model_dump_json())

    async def _open_stream(self) -> None:
        """Start a new Soniox stream on the connection."""
        self._stream_id = uuid4().hex
        self._stream_chars = 0
        self._stream_done.clear()
        self._stream_active.set()
        config = RealtimeTTSConfig(stream_id=self._stream_id, **self._stream_config)
        await self._websocket.send(config.model_dump_json(exclude_none=True))

    async def _end_stream(self) -> None:
        """Tell Soniox no more text follows for the open stream."""
        message = RealtimeTTSTextMessage(text="", text_end=True, stream_id=self._stream_id or "")
        self._stream_id = None
        await self._websocket.send(message.model_dump_json())

    async def _receive(self) -> None:
        """Forward audio of the active stream until the connection ends."""
        while True:
            await self._stream_active.wait()
            try:
                async with asyncio.timeout(RECEIVE_TIMEOUT_SECONDS):
                    message = await self._websocket.recv()
            except ConnectionClosedOK as err:
                raise SonioxCommunicationError("Soniox closed the connection before finishing") from err
            except TimeoutError as err:
                raise SonioxCommunicationError("Timed out waiting for Soniox audio") from err
            event = _parse(RealtimeTTSEvent, message)
            _raise_for_event_error(event.error_code, event.error_message)
            if audio := _audio_bytes(event):
                self._output.put_nowait(audio)
            if event.terminated:
                self._stream_active.clear()
                self._stream_done.set()


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


def _split_point(text: str, limit: int) -> int:
    """Where to cut `text` to fit `limit` characters.

    Prefers the last sentence end, then the last whitespace, in the second half
    of the window, so streams don't become tiny. Falls back to a hard cut.
    """
    window = text[:limit]
    for pattern in (r"[.!?…]\s", r"\s"):
        if matches := list(re.finditer(pattern, window[limit // 2 :])):
            return limit // 2 + matches[-1].end()
    return limit


def _build_stt_catalog(models: list[Model]) -> SttCatalog:
    """Pick the realtime STT model to use."""
    model = _pick_model([model for model in models if model.transcription_mode == "real_time"], DEFAULT_STT_MODEL)
    if model is None:
        msg = "No realtime speech-to-text model is available for this API key"
        raise SonioxRequestError(msg)
    return SttCatalog(
        model=model.id,
        languages={language.code: language.name for language in model.languages},
        strict_language_hints=model.supports_language_hints_strict,
    )


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
    chunks = aiter(audio_stream)
    while True:
        try:
            chunk = await anext(chunks)
        except StopAsyncIteration:
            break
        except Exception as err:
            raise _SourceError(err) from err
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
    """Map SDK, HTTP and websocket exceptions to this module's exceptions.

    `_SourceError` passes through untouched.
    """
    try:
        yield
    except SonioxError, _SourceError:
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
