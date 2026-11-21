"""The Soniox integration: speech-to-text and text-to-speech for Home Assistant."""

from __future__ import annotations

from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.util.ssl import get_default_context

from .api import SonioxApiClient, SonioxAuthenticationError, SonioxError
from .const import CONF_REGION, Region
from .data import SonioxConfigEntry, SonioxRuntimeData

PLATFORMS: list[Platform] = [Platform.STT, Platform.TTS]


async def async_setup_entry(hass: HomeAssistant, entry: SonioxConfigEntry) -> bool:
    """Set up Soniox from a config entry."""
    client = SonioxApiClient(
        api_key=entry.data[CONF_API_KEY],
        region=Region(entry.data[CONF_REGION]),
        ssl_context=get_default_context(),
    )
    # Runs on unload and also when setup fails below.
    entry.async_on_unload(client.async_close)

    try:
        catalog = await client.async_get_catalog()
    except SonioxAuthenticationError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except SonioxError as err:
        raise ConfigEntryNotReady(str(err)) from err

    entry.runtime_data = SonioxRuntimeData(client=client, catalog=catalog)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SonioxConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
