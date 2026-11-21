"""Diagnostics for the Soniox integration."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.helpers.redact import async_redact_data

from .data import SonioxConfigEntry

TO_REDACT = {CONF_API_KEY}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: SonioxConfigEntry) -> dict[str, Any]:
    """Return config entry diagnostics with the API key redacted."""
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "catalog": asdict(entry.runtime_data.catalog),
    }
