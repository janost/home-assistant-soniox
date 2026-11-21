"""Text-to-speech platform for Soniox."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Any

from custom_components.soniox.api import SonioxError, TtsCatalog
from custom_components.soniox.const import ATTR_SPEED, CONF_TTS_VOICE, DEFAULT_TTS_VOICE, MAX_SPEED, MIN_SPEED
from custom_components.soniox.data import SonioxConfigEntry
from custom_components.soniox.entity import SonioxEntity
from custom_components.soniox.utils import base_language
from homeassistant.components.tts import ATTR_VOICE, TextToSpeechEntity, Voice
from homeassistant.components.tts.entity import TTSAudioRequest, TTSAudioResponse
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

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

    Only the streaming entry point is implemented. Home Assistant routes every
    request through it, including plain messages from `tts.speak`, so Assist
    can start playback while an LLM is still writing its reply.
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

    async def async_stream_tts_audio(self, request: TTSAudioRequest) -> TTSAudioResponse:
        """Synthesize a message, which may still be arriving, into MP3 audio."""
        voice, speed = self._voice_and_speed(request.options)
        stream = self._client.async_synthesize_stream(
            request.message_gen, model=self._model, language=request.language, voice=voice, speed=speed
        )
        return TTSAudioResponse(AUDIO_EXTENSION, self._wrap_errors(stream))

    async def _wrap_errors(self, stream: AsyncGenerator[bytes]) -> AsyncGenerator[bytes]:
        """Re-raise client errors as HomeAssistantError and close the stream when done."""
        async with aclosing(stream):
            try:
                async for chunk in stream:
                    yield chunk
            except SonioxError as err:
                self._handle_error(err)
                raise HomeAssistantError(f"Soniox text-to-speech failed: {err}") from err

    def _voice_and_speed(self, options: dict[str, Any]) -> tuple[str, float | None]:
        """Read the voice and speed options of a request."""
        voice = options.get(ATTR_VOICE) or self._default_voice
        if (raw_speed := options.get(ATTR_SPEED)) is None:
            return voice, None
        try:
            speed = float(raw_speed)
        except TypeError, ValueError:
            speed = -1.0
        if not MIN_SPEED <= speed <= MAX_SPEED:
            raise HomeAssistantError(f"Soniox speed must be a number between {MIN_SPEED} and {MAX_SPEED}")
        return voice, speed


def _pick_default_language(ha_language: str, supported: list[str]) -> str:
    """Use the Home Assistant language if Soniox supports it, else English."""
    for candidate in (base_language(ha_language), "en"):
        if candidate in supported:
            return candidate
    return supported[0] if supported else "en"
