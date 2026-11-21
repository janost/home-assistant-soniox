"""Base entity for the Soniox integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity

from .api import SonioxAuthenticationError, SonioxError
from .const import CONSOLE_URL, DOMAIN, LOGGER, TITLE
from .data import SonioxConfigEntry


class SonioxEntity(Entity):
    """Entity attached to the shared Soniox service device of a config entry."""

    _attr_has_entity_name = True

    def __init__(self, entry: SonioxConfigEntry, key: str) -> None:
        """Initialize the entity. `key` is the translation key and unique ID suffix."""
        self._entry = entry
        self._client = entry.runtime_data.client
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer="Soniox",
            name=TITLE,
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=CONSOLE_URL,
        )

    def _handle_error(self, err: SonioxError) -> None:
        """Log a failed request and ask for a new API key if Soniox rejected this one."""
        LOGGER.error("Soniox request failed: %s", err)
        if isinstance(err, SonioxAuthenticationError):
            self._entry.async_start_reauth(self.hass)
