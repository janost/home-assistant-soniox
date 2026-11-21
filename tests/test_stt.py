"""Tests for the Soniox speech-to-text entity."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import pytest

from custom_components.soniox.api import SonioxAuthenticationError, SonioxRequestError
from homeassistant.components import stt
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import HomeAssistant

from .conftest import STT_ENTITY_ID

pytestmark = pytest.mark.usefixtures("setup_integration")


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


async def test_transcribes_in_pipeline_language(hass: HomeAssistant, mock_client: MagicMock) -> None:
    entity = _entity(hass)
    assert entity.supported_languages == ["en", "de"]
    metadata = _metadata("de-CH")
    assert entity.check_metadata(metadata)
    assert not entity.check_metadata(_metadata("fr-FR"))

    result = await entity.async_process_audio_stream(metadata, _audio())

    assert result == stt.SpeechResult("turn on the lights", stt.SpeechResultState.SUCCESS)
    assert mock_client.async_transcribe.await_args.kwargs == {"model": "stt-rt-v6", "language": "de"}


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
    assert not hass.config_entries.flow.async_progress()


async def test_rejected_key_starts_reauth(hass: HomeAssistant, mock_client: MagicMock) -> None:
    mock_client.async_transcribe.side_effect = SonioxAuthenticationError("revoked")

    result = await _entity(hass).async_process_audio_stream(_metadata("en"), _audio())
    await hass.async_block_till_done()

    assert result.result is stt.SpeechResultState.ERROR
    assert [flow["context"]["source"] for flow in hass.config_entries.flow.async_progress()] == [SOURCE_REAUTH]
