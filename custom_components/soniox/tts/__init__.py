"""Text-to-speech platform for Soniox."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

from homeassistant.components.tts import ATTR_VOICE, TextToSpeechEntity, TtsAudioType, Voice
from homeassistant.components.tts.entity import TTSAudioRequest, TTSAudioResponse
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from custom_components.soniox.api import SonioxError, TtsCatalog
from custom_components.soniox.const import ATTR_SPEED, CONF_TTS_VOICE, DEFAULT_TTS_VOICE, MAX_SPEED, MIN_SPEED
from custom_components.soniox.data import SonioxConfigEntry
from custom_components.soniox.entity import SonioxEntity
from custom_components.soniox.utils import base_language

# Each request opens its own websocket; don't serialize them.
PARALLEL_UPDATES = 0
AUDIO_EXTENSION = "mp3"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SonioxConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Soniox TTS entity if the API key has access to a TTS model."""
    if (catalog := entry.runtime_data.catalog.tts) is not None:
        async_add_entities([SonioxTextToSpeechEntity(hass, entry, catalog)])


class SonioxTextToSpeechEntity(SonioxEntity, TextToSpeechEntity):
    """Soniox realtime text-to-speech.

    Assist streams LLM responses through `async_stream_tts_audio`, so speech
    starts before the full response is known. `tts.speak` and other one-shot
    callers go through `async_get_tts_audio`.
    """

    _attr_supported_options = [ATTR_VOICE, ATTR_SPEED]

    def __init__(self, hass: HomeAssistant, entry: SonioxConfigEntry, catalog: TtsCatalog) -> None:
        """Initialize the entity."""
        super().__init__(entry, "text_to_speech")
        self._model = catalog.model
        self._attr_supported_languages = list(catalog.languages)
        self._attr_default_language = _pick_default_language(hass.config.language, self._attr_supported_languages)

        default_voice = entry.options.get(CONF_TTS_VOICE, DEFAULT_TTS_VOICE)
        if default_voice not in catalog.voices:
            default_voice = next(iter(catalog.voices), DEFAULT_TTS_VOICE)
        self._default_voice: str = default_voice
        self._attr_default_options = {ATTR_VOICE: default_voice}

        # Default voice first, the rest alphabetically.
        self._voices = sorted(
            (Voice(voice_id, name) for voice_id, name in catalog.voices.items()),
            key=lambda voice: (voice.voice_id != default_voice, voice.name.casefold()),
        )

    def async_get_supported_voices(self, language: str) -> list[Voice]:
        """Return the available voices. Soniox voices are not tied to a language."""
        return self._voices

    async def async_get_tts_audio(self, message: str, language: str, options: dict[str, Any]) -> TtsAudioType:
        """Synthesize a complete message."""
        voice, speed = self._voice_and_speed(options)
        try:
            audio = await self._client.async_synthesize(
                message, model=self._model, language=language, voice=voice, speed=speed
            )
        except SonioxError as err:
            raise HomeAssistantError(f"Soniox text-to-speech failed: {err}") from err
        return AUDIO_EXTENSION, audio

    async def async_stream_tts_audio(self, request: TTSAudioRequest) -> TTSAudioResponse:
        """Synthesize a message while it is still being generated."""
        voice, speed = self._voice_and_speed(request.options)
        stream = self._client.async_synthesize_stream(
            request.message_gen, model=self._model, language=request.language, voice=voice, speed=speed
        )
        return TTSAudioResponse(AUDIO_EXTENSION, _wrap_errors(stream))

    def _voice_and_speed(self, options: dict[str, Any]) -> tuple[str, float | None]:
        """Read the voice and speed options of a request."""
        voice = options.get(ATTR_VOICE) or self._default_voice
        if (raw_speed := options.get(ATTR_SPEED)) is None:
            return voice, None
        try:
            speed = float(raw_speed)
        except (TypeError, ValueError):
            speed = -1.0
        if not MIN_SPEED <= speed <= MAX_SPEED:
            raise HomeAssistantError(f"Soniox speed must be a number between {MIN_SPEED} and {MAX_SPEED}")
        return voice, speed


async def _wrap_errors(stream: AsyncIterator[bytes]) -> AsyncGenerator[bytes]:
    """Re-raise client errors from an audio stream as HomeAssistantError."""
    try:
        async for chunk in stream:
            yield chunk
    except SonioxError as err:
        raise HomeAssistantError(f"Soniox text-to-speech failed: {err}") from err


def _pick_default_language(ha_language: str, supported: list[str]) -> str:
    """Use the Home Assistant language if Soniox supports it, else English."""
    for candidate in (base_language(ha_language), "en"):
        if candidate in supported:
            return candidate
    return supported[0] if supported else "en"
