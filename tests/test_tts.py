"""Tests for the Soniox text-to-speech entity."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.soniox.api import SonioxAuthenticationError, SonioxRequestError
from homeassistant.components import tts
from homeassistant.components.tts import TextToSpeechEntity
from homeassistant.components.tts.const import DATA_COMPONENT
from homeassistant.components.tts.entity import TTSAudioRequest
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .conftest import TTS_ENTITY_ID

pytestmark = pytest.mark.usefixtures("setup_integration")


class FakeStream:
    """Stand-in for `async_synthesize_stream` that records its arguments."""

    def __init__(self, *chunks: bytes, error: Exception | None = None) -> None:
        self.chunks = chunks
        self.error = error
        self.text: list[str] = []
        self.kwargs: dict[str, Any] = {}

    async def __call__(self, text_stream: AsyncIterator[str], **kwargs: Any) -> AsyncGenerator[bytes]:
        self.kwargs = kwargs
        self.text.extend([chunk async for chunk in text_stream])
        for chunk in self.chunks:
            yield chunk
        if self.error is not None:
            raise self.error


def _entity(hass: HomeAssistant) -> TextToSpeechEntity:
    entity = hass.data[DATA_COMPONENT].get_entity(TTS_ENTITY_ID)
    assert entity is not None
    return entity


async def _text(*chunks: str) -> AsyncGenerator[str]:
    for chunk in chunks:
        yield chunk


async def _speak(hass: HomeAssistant, options: dict[str, Any], *text: str) -> list[bytes]:
    response = await _entity(hass).async_stream_tts_audio(TTSAudioRequest("en", options, _text(*text)))
    assert response.extension == "mp3"
    return [chunk async for chunk in response.data_gen]


async def test_voices_and_defaults(hass: HomeAssistant) -> None:
    entity = _entity(hass)

    assert entity.supported_languages == ["en", "de"]
    assert entity.default_language == "en"
    assert entity.default_options == {"voice": "Adrian"}
    voices = entity.async_get_supported_voices("en")
    assert voices is not None
    # Default voice first, then by name.
    assert [voice.voice_id for voice in voices] == ["Adrian", "Maya", "clone-1"]


async def test_stream_uses_defaults(hass: HomeAssistant, mock_client: MagicMock) -> None:
    stream = FakeStream(b"a", b"b")
    mock_client.async_synthesize_stream = stream

    assert await _speak(hass, {}, "Hel", "lo") == [b"a", b"b"]
    assert stream.text == ["Hel", "lo"]
    assert stream.kwargs == {"model": "tts-rt-v2", "language": "en", "voice": "Adrian", "speed": None}


async def test_stream_passes_voice_and_speed(hass: HomeAssistant, mock_client: MagicMock) -> None:
    stream = FakeStream(b"a")
    mock_client.async_synthesize_stream = stream

    await _speak(hass, {"voice": "Maya", "speed": "1.2"}, "Hallo")

    assert stream.kwargs["voice"] == "Maya"
    assert stream.kwargs["speed"] == 1.2


@pytest.mark.parametrize("speed", [2.0, "fast"])
async def test_invalid_speed_is_rejected(hass: HomeAssistant, speed: object) -> None:
    with pytest.raises(HomeAssistantError, match="speed"):
        await _entity(hass).async_stream_tts_audio(TTSAudioRequest("en", {"speed": speed}, _text("Hi")))


async def test_stream_error(hass: HomeAssistant, mock_client: MagicMock) -> None:
    mock_client.async_synthesize_stream = FakeStream(b"a", error=SonioxRequestError("boom"))

    with pytest.raises(HomeAssistantError, match="boom"):
        await _speak(hass, {}, "Hi")
    assert not hass.config_entries.flow.async_progress()


async def test_rejected_key_starts_reauth(hass: HomeAssistant, mock_client: MagicMock) -> None:
    mock_client.async_synthesize_stream = FakeStream(error=SonioxAuthenticationError("revoked"))

    with pytest.raises(HomeAssistantError):
        await _speak(hass, {}, "Hi")
    await hass.async_block_till_done()

    assert [flow["context"]["source"] for flow in hass.config_entries.flow.async_progress()] == [SOURCE_REAUTH]


async def test_tts_speak_path_through_home_assistant(hass: HomeAssistant, mock_client: MagicMock) -> None:
    """A plain message goes through HA's TTS manager, option validation and the stream entry point."""
    stream = FakeStream(b"mp3-1", b"mp3-2")
    mock_client.async_synthesize_stream = stream

    result = tts.async_create_stream(hass, TTS_ENTITY_ID, language="de", options={"voice": "Maya", "speed": 0.9})
    result.async_set_message("Hallo Welt")
    audio = b"".join([chunk async for chunk in result.async_stream_result()])

    assert audio == b"mp3-1mp3-2"
    assert result.extension == "mp3"
    assert stream.text == ["Hallo Welt"]
    assert stream.kwargs == {"model": "tts-rt-v2", "language": "de", "voice": "Maya", "speed": 0.9}
