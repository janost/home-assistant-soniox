"""Tests for the Soniox speech-to-text entity."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.soniox.api import SonioxRequestError
from custom_components.soniox.const import CONF_STT_LANGUAGE
from homeassistant.components import stt
from homeassistant.core import HomeAssistant

from .conftest import STT_ENTITY_ID


def _metadata(language: str) -> stt.SpeechMetadata:
    return stt.SpeechMetadata(
        language=language,
        format=stt.AudioFormats.WAV,
        codec=stt.AudioCodecs.PCM,
        bit_rate=stt.AudioBitRates.BITRATE_16,
        sample_rate=stt.AudioSampleRates.SAMPLERATE_16000,
        channel=stt.AudioChannels.CHANNEL_MONO,
    )


async def _audio() -> AsyncIterator[bytes]:
    yield b"\x00\x01"


def _entity(hass: HomeAssistant) -> stt.SpeechToTextEntity:
    entity = stt.async_get_speech_to_text_entity(hass, STT_ENTITY_ID)
    assert entity is not None
    return entity


@pytest.mark.usefixtures("setup_integration")
async def test_transcribes_with_pipeline_language(hass: HomeAssistant, mock_client: MagicMock) -> None:
    entity = _entity(hass)
    assert entity.supported_languages == ["en", "de"]
    metadata = _metadata("de-CH")
    assert entity.check_metadata(metadata)

    result = await entity.async_process_audio_stream(metadata, _audio())

    assert result == stt.SpeechResult("turn on the lights", stt.SpeechResultState.SUCCESS)
    assert mock_client.async_transcribe.await_args.kwargs == {"model": "stt-rt-v6", "language": "de"}


async def test_fixed_language_overrides_pipeline(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, options={CONF_STT_LANGUAGE: "de"})
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    entity = _entity(hass)

    assert entity.supported_languages == ["de"]
    assert not entity.check_metadata(_metadata("en-US"))
    await entity.async_process_audio_stream(_metadata("de"), _audio())
    assert mock_client.async_transcribe.await_args.kwargs["language"] == "de"


@pytest.mark.usefixtures("setup_integration")
@pytest.mark.parametrize("outcome", [SonioxRequestError("rejected"), ""])
async def test_failure_or_empty_transcript_is_error(
    hass: HomeAssistant, mock_client: MagicMock, outcome: Exception | str
) -> None:
    if isinstance(outcome, Exception):
        mock_client.async_transcribe.side_effect = outcome
    else:
        mock_client.async_transcribe.return_value = outcome

    result = await _entity(hass).async_process_audio_stream(_metadata("en"), _audio())

    assert result == stt.SpeechResult(None, stt.SpeechResultState.ERROR)
