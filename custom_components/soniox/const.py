"""Constants for the Soniox integration."""

from enum import StrEnum
from logging import Logger, getLogger
from typing import Final

LOGGER: Logger = getLogger(__package__)

DOMAIN: Final = "soniox"
TITLE: Final = "Soniox"
CONSOLE_URL: Final = "https://console.soniox.com"

CONF_REGION: Final = "region"
CONF_STT_LANGUAGE: Final = "stt_language"
CONF_TTS_VOICE: Final = "tts_voice"

# Preferred models. If the API key cannot use them, the first model of the
# matching kind returned by the API is used instead.
DEFAULT_STT_MODEL: Final = "stt-rt-v6"
DEFAULT_TTS_MODEL: Final = "tts-rt-v2"
DEFAULT_TTS_VOICE: Final = "Adrian"

# STT option value meaning "use the Assist pipeline language".
AUTO_LANGUAGE: Final = "auto"

# TTS request option for the speaking rate, accepted by Soniox from 0.7 to 1.3.
ATTR_SPEED: Final = "speed"
MIN_SPEED: Final = 0.7
MAX_SPEED: Final = 1.3


class Region(StrEnum):
    """Soniox data residency regions. API keys are bound to one region."""

    US = "us"
    EU = "eu"
    JP = "jp"
    IN = "in"


DEFAULT_REGION: Final = Region.US
