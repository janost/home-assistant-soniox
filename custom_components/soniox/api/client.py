"""Async client for the Soniox speech-to-text and text-to-speech APIs.

REST calls (model and voice catalogs) go through the official SDK. The realtime
websockets are opened here directly because the SDK cannot be handed an SSL
context, and letting `websockets` build its own would load the CA bundle inside
the Home Assistant event loop. Messages are still built and parsed with the SDK
types.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
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
    RealtimeTTSTextMessage,
)

CONNECT_TIMEOUT_SECONDS: Final = 10.0
# Longest gap allowed between two websocket messages from Soniox.
RECEIVE_TIMEOUT_SECONDS: Final = 60.0
# Soniox rejects realtime TTS text messages longer than this.
MAX_TTS_TEXT_CHUNK: Final = 5000
TTS_AUDIO_FORMAT: Final = "mp3"
AUTH_ERROR_CODES: Final = frozenset({401, 403})


class SonioxError(Exception):
    """Base exception for Soniox client errors."""


class SonioxCommunicationError(SonioxError):
    """Soniox could not be reached or the connection failed."""


class SonioxAuthenticationError(SonioxError):
    """Soniox rejected the API key."""


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
        self._client = AsyncSonioxClient(
            api_key=api_key,
            api_base_url=self._endpoints.api,
            websocket_base_url=self._endpoints.stt_websocket,
            tts_websocket_base_url=self._endpoints.tts_websocket,
            verify=ssl_context,
        )

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

    async def async_transcribe(self, audio_stream: AsyncIterable[bytes], *, model: str, language: str | None) -> str:
        """Stream 16 kHz mono 16-bit PCM audio and return the final transcript.

        `language` biases recognition strongly towards one language. Pass None
        to let Soniox detect the language.
        """
        config = RealtimeSTTConfig(
            model=model,
            audio_format="pcm_s16le",
            sample_rate=16000,
            num_channels=1,
            language_hints=[language] if language else None,
            language_hints_strict=True if language else None,
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
                    if sender.done() and (err := sender.exception()) is not None:
                        raise err
                    transcript = await receiver
                finally:
                    await _cancel(sender, receiver)

        LOGGER.debug("Soniox transcription finished chars=%s", len(transcript))
        return transcript

    async def async_synthesize(self, text: str, *, model: str, language: str, voice: str, speed: float | None) -> bytes:
        """Synthesize `text` and return the complete MP3 audio."""

        async def single_message() -> AsyncIterator[str]:
            yield text

        stream = self.async_synthesize_stream(
            single_message(), model=model, language=language, voice=voice, speed=speed
        )
        return b"".join([chunk async for chunk in stream])

    async def async_synthesize_stream(
        self,
        text_stream: AsyncIterable[str],
        *,
        model: str,
        language: str,
        voice: str,
        speed: float | None,
    ) -> AsyncGenerator[bytes]:
        """Synthesize text as it arrives and yield MP3 audio chunks as they are produced."""
        LOGGER.debug("Starting Soniox TTS stream model=%s language=%s voice=%s", model, language, voice)

        with _translate_errors():
            config = RealtimeTTSConfig(
                stream_id=uuid4().hex,
                model=model,
                language=language,
                voice=voice,
                audio_format=TTS_AUDIO_FORMAT,
                speed=speed,
            )
            async with self._connect(self._endpoints.tts_websocket) as websocket:
                await websocket.send(config.model_dump_json(exclude_none=True))
                sender = asyncio.create_task(_send_text(websocket, config.stream_id, text_stream))
                try:
                    async for message in _iter_messages(websocket):
                        event = RealtimeTTSEvent.validate_event(message)
                        _raise_for_event_error(event.error_code, event.error_message)
                        if audio := event.audio_bytes():
                            yield audio
                        if event.terminated:
                            break
                    # Surfaces a failure of the text source after the stream was cancelled.
                    await sender
                finally:
                    await _cancel(sender)

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
            async for cloned in self._client.voices.list_all():
                if any(state.model == model.id and state.status == "ready" for state in cloned.models):
                    voices[cloned.id] = f"{cloned.name} (cloned)"

        return TtsCatalog(
            model=model.id,
            languages={language.code: language.name for language in model.languages},
            voices=voices,
        )


def _build_stt_catalog(models: list[Model]) -> SttCatalog:
    """Pick the realtime STT model to use."""
    model = _pick_model([model for model in models if model.transcription_mode == "real_time"], DEFAULT_STT_MODEL)
    if model is None:
        msg = "No realtime speech-to-text model is available for this API key"
        raise SonioxRequestError(msg)
    return SttCatalog(model=model.id, languages={language.code: language.name for language in model.languages})


def _pick_model[M: (Model, TtsModel)](models: list[M], preferred: str) -> M | None:
    """Return the preferred model, else the newest non-alias model, else None."""
    for model in models:
        if model.id == preferred:
            return model
    candidates = [model for model in models if model.aliased_model_id is None]
    return max(candidates, key=lambda model: model.id, default=None)


async def _send_audio(websocket: ClientConnection, audio_stream: AsyncIterable[bytes]) -> None:
    """Forward audio to Soniox, then signal end of audio with an empty frame."""
    async for chunk in audio_stream:
        if chunk:
            await websocket.send(chunk)
    await websocket.send("")


async def _receive_transcript(websocket: ClientConnection) -> str:
    """Collect final tokens until Soniox reports the transcription finished."""
    parts: list[str] = []
    async for message in _iter_messages(websocket):
        event = RealtimeEvent.validate_event(message)
        _raise_for_event_error(event.error_code, event.error_message)
        parts.extend(token.text for token in event.tokens if token.is_final)
        if event.finished:
            break
    return "".join(parts).strip()


async def _send_text(websocket: ClientConnection, stream_id: str, text_stream: AsyncIterable[str]) -> None:
    """Forward text to a TTS stream and mark its end.

    If the text source fails, the stream is cancelled so Soniox terminates it
    and the receive loop ends; the original error is then re-raised.
    """
    try:
        async for text in text_stream:
            for start in range(0, len(text), MAX_TTS_TEXT_CHUNK):
                message = RealtimeTTSTextMessage(text=text[start : start + MAX_TTS_TEXT_CHUNK], stream_id=stream_id)
                await websocket.send(message.model_dump_json())
    except Exception:
        with suppress(WebSocketException):
            await websocket.send(RealtimeTTSCancelMessage(stream_id=stream_id).model_dump_json())
        raise
    await websocket.send(RealtimeTTSTextMessage(text="", text_end=True, stream_id=stream_id).model_dump_json())


async def _iter_messages(websocket: ClientConnection) -> AsyncIterator[str | bytes]:
    """Yield websocket messages until the server closes the connection normally."""
    while True:
        try:
            async with asyncio.timeout(RECEIVE_TIMEOUT_SECONDS):
                message = await websocket.recv()
        except ConnectionClosedOK:
            return
        yield message


async def _cancel(*tasks: asyncio.Task[Any]) -> None:
    """Cancel tasks and wait for them, discarding their results."""
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _raise_for_event_error(code: int | None, message: str | None) -> None:
    """Raise if a realtime event carries an error."""
    if code is None and not message:
        return
    text = f"{message or 'Soniox realtime request failed'} (code {code})"
    if code in AUTH_ERROR_CODES:
        raise SonioxAuthenticationError(text)
    raise SonioxRequestError(text)


@contextmanager
def _translate_errors() -> Iterator[None]:
    """Map SDK, HTTP and websocket exceptions to this module's exceptions."""
    try:
        yield
    except SonioxError:
        raise
    except (SDKSonioxAuthenticationError, SDKSonioxPermissionDeniedError) as err:
        raise SonioxAuthenticationError(str(err)) from err
    except SDKSonioxAPIError as err:
        if err.status_code is not None and (err.status_code == 429 or err.status_code >= 500):
            raise SonioxCommunicationError(str(err)) from err
        raise SonioxRequestError(str(err)) from err
    except (SDKSonioxError, ValidationError) as err:
        raise SonioxRequestError(str(err)) from err
    except InvalidStatus as err:
        if err.response.status_code in AUTH_ERROR_CODES:
            raise SonioxAuthenticationError(str(err)) from err
        raise SonioxCommunicationError(str(err)) from err
    except (httpx.HTTPError, WebSocketException, OSError, TimeoutError) as err:
        raise SonioxCommunicationError(str(err) or type(err).__name__) from err
