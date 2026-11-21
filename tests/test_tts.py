"""Tests for the Soniox text-to-speech entity."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from unittest.mock import MagicMock

import pytest

from custom_components.soniox.api import SonioxRequestError
from homeassistant.components.tts import TextToSpeechEntity
from homeassistant.components.tts.const import DATA_COMPONENT
from homeassistant.components.tts.entity import TTSAudioRequest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .conftest import TTS_ENTITY_ID

pytestmark = pytest.mark.usefixtures("setup_integration")


def _entity(hass: HomeAssistant) -> TextToSpeechEntity:
    entity = hass.data[DATA_COMPONENT].get_entity(TTS_ENTITY_ID)
    assert entity is not None
    return entity


async def _text(*chunks: str) -> AsyncGenerator[str]:
    for chunk in chunks:
        yield chunk


async def test_voices_and_defaults(hass: HomeAssistant) -> None:
    entity = _entity(hass)

    assert entity.supported_languages == ["en", "de"]
    assert entity.default_language == "en"
    assert entity.default_options == {"voice": "Adrian"}
    voices = entity.async_get_supported_voices("en")
    assert voices is not None
    # Default voice first, then by name.
    assert [voice.voice_id for voice in voices] == ["Adrian", "Maya", "clone-1"]


async def test_get_tts_audio_uses_defaults_and_options(hass: HomeAssistant, mock_client: MagicMock) -> None:
    entity = _entity(hass)

    assert await entity.async_get_tts_audio("Hello", "en", {}) == ("mp3", b"mp3-data")
    assert mock_client.async_synthesize.await_args.kwargs == {
        "model": "tts-rt-v2",
        "language": "en",
        "voice": "Adrian",
        "speed": None,
    }

    await entity.async_get_tts_audio("Hallo", "de", {"voice": "Maya", "speed": "1.2"})
    assert mock_client.async_synthesize.await_args.kwargs["voice"] == "Maya"
    assert mock_client.async_synthesize.await_args.kwargs["speed"] == 1.2


@pytest.mark.parametrize("speed", [2.0, "fast"])
async def test_invalid_speed_is_rejected(hass: HomeAssistant, mock_client: MagicMock, speed: object) -> None:
    with pytest.raises(HomeAssistantError, match="speed"):
        await _entity(hass).async_get_tts_audio("Hello", "en", {"speed": speed})
    mock_client.async_synthesize.assert_not_awaited()


async def test_get_tts_audio_error(hass: HomeAssistant, mock_client: MagicMock) -> None:
    mock_client.async_synthesize.side_effect = SonioxRequestError("rejected")
    with pytest.raises(HomeAssistantError, match="rejected"):
        await _entity(hass).async_get_tts_audio("Hello", "en", {})


async def test_stream_tts_audio(hass: HomeAssistant, mock_client: MagicMock) -> None:
    received: list[str] = []

    async def fake_stream(text_stream: AsyncIterator[str], **kwargs: object) -> AsyncIterator[bytes]:
        received.extend([chunk async for chunk in text_stream])
        yield b"a"
        yield b"b"

    mock_client.async_synthesize_stream = fake_stream
    entity = _entity(hass)
    assert entity.async_supports_streaming_input()

    response = await entity.async_stream_tts_audio(TTSAudioRequest("en", {}, _text("Hel", "lo")))

    assert response.extension == "mp3"
    assert [chunk async for chunk in response.data_gen] == [b"a", b"b"]
    assert received == ["Hel", "lo"]


async def test_stream_tts_audio_error(hass: HomeAssistant, mock_client: MagicMock) -> None:
    async def failing_stream(text_stream: AsyncIterator[str], **kwargs: object) -> AsyncIterator[bytes]:
        yield b"a"
        raise SonioxRequestError("boom")

    mock_client.async_synthesize_stream = failing_stream
    response = await _entity(hass).async_stream_tts_audio(TTSAudioRequest("en", {}, _text("Hi")))

    with pytest.raises(HomeAssistantError, match="boom"):
        _ = [chunk async for chunk in response.data_gen]
