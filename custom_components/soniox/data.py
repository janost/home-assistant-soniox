"""Runtime data for the Soniox integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry

from .api import SonioxApiClient, SonioxCatalog


@dataclass(slots=True)
class SonioxRuntimeData:
    """Client and model catalog shared by the STT and TTS entities."""

    client: SonioxApiClient
    catalog: SonioxCatalog


type SonioxConfigEntry = ConfigEntry[SonioxRuntimeData]
