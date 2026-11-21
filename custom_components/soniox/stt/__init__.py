"""Speech-to-text platform for Soniox."""

from __future__ import annotations

from collections.abc import AsyncIterable
import dataclasses

from custom_components.soniox.api import SonioxError
from custom_components.soniox.const import AUTO_LANGUAGE, CONF_STT_LANGUAGE, LOGGER
from custom_components.soniox.data import SonioxConfigEntry
from custom_components.soniox.entity import SonioxEntity
from custom_components.soniox.utils import base_language
from homeassistant.components import stt
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

# Each Assist run opens its own websocket; don't serialize them.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SonioxConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Soniox STT entity."""
    async_add_entities([SonioxSpeechToTextEntity(entry)])


class SonioxSpeechToTextEntity(SonioxEntity, stt.SpeechToTextEntity):
    """Soniox realtime transcription as an Assist speech-to-text provider."""

    def __init__(self, entry: SonioxConfigEntry) -> None:
        """Initialize the entity."""
        super().__init__(entry, "speech_to_text")
        catalog = entry.runtime_data.catalog.stt
        self._model = catalog.model
        self._available_languages = list(catalog.languages)
        self._fixed_language: str | None = entry.options.get(CONF_STT_LANGUAGE, AUTO_LANGUAGE)
        if self._fixed_language == AUTO_LANGUAGE:
            self._fixed_language = None

    @property
    def supported_languages(self) -> list[str]:
        """Return the languages offered to Assist pipelines."""
        if self._fixed_language is not None:
            return [self._fixed_language]
        return self._available_languages

    @property
    def supported_formats(self) -> list[stt.AudioFormats]:
        """Return the supported container formats."""
        return [stt.AudioFormats.WAV]

    @property
    def supported_codecs(self) -> list[stt.AudioCodecs]:
        """Return the supported audio codecs."""
        return [stt.AudioCodecs.PCM]

    @property
    def supported_bit_rates(self) -> list[stt.AudioBitRates]:
        """Return the supported audio bit depths."""
        return [stt.AudioBitRates.BITRATE_16]

    @property
    def supported_sample_rates(self) -> list[stt.AudioSampleRates]:
        """Return the supported audio sample rates."""
        return [stt.AudioSampleRates.SAMPLERATE_16000]

    @property
    def supported_channels(self) -> list[stt.AudioChannels]:
        """Return the supported channel counts."""
        return [stt.AudioChannels.CHANNEL_MONO]

    def check_metadata(self, metadata: stt.SpeechMetadata) -> bool:
        """Accept regional variants (e.g. en-US) of supported base languages."""
        return super().check_metadata(dataclasses.replace(metadata, language=base_language(metadata.language)))

    async def async_process_audio_stream(
        self,
        metadata: stt.SpeechMetadata,
        stream: AsyncIterable[bytes],
    ) -> stt.SpeechResult:
        """Transcribe an audio stream with Soniox."""
        language = self._fixed_language or base_language(metadata.language)
        try:
            text = await self._client.async_transcribe(stream, model=self._model, language=language)
        except SonioxError as err:
            LOGGER.error("Soniox transcription failed: %s", err)
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)

        if not text:
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)
        return stt.SpeechResult(text, stt.SpeechResultState.SUCCESS)
