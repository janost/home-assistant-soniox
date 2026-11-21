"""Tests for Soniox config entry setup."""

from __future__ import annotations

import dataclasses
from unittest.mock import MagicMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.soniox.api import SonioxAuthenticationError, SonioxCatalog, SonioxCommunicationError
from custom_components.soniox.const import CONF_REGION, DOMAIN
from custom_components.soniox.diagnostics import async_get_config_entry_diagnostics
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .conftest import STT_ENTITY_ID, TTS_ENTITY_ID


async def test_setup_and_unload(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    assert setup_integration.state is ConfigEntryState.LOADED
    assert hass.states.get(STT_ENTITY_ID) is not None
    assert hass.states.get(TTS_ENTITY_ID) is not None

    # Both entities hang off one service device.
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, setup_integration.entry_id), setup_integration.entry_id
    )
    assert device is not None
    assert device.entry_type is dr.DeviceEntryType.SERVICE

    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    assert setup_integration.state is ConfigEntryState.NOT_LOADED
    mock_client.async_close.assert_awaited_once()


async def test_auth_failure_starts_reauth(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_client.async_get_catalog.side_effect = SonioxAuthenticationError("bad key")
    config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(config_entry.entry_id)

    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]
    # The client is closed even though setup failed.
    mock_client.async_close.assert_awaited_once()


async def test_connection_failure_retries(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_client.async_get_catalog.side_effect = SonioxCommunicationError("down")
    config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(config_entry.entry_id)

    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_no_tts_entity_without_tts_model(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, catalog: SonioxCatalog
) -> None:
    mock_client.async_get_catalog.return_value = dataclasses.replace(catalog, tts=None)
    config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(STT_ENTITY_ID) is not None
    assert hass.states.get(TTS_ENTITY_ID) is None


async def test_diagnostics_redact_key(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    diagnostics = await async_get_config_entry_diagnostics(hass, setup_integration)

    assert diagnostics["entry"]["data"] == {CONF_API_KEY: "**REDACTED**", CONF_REGION: "eu"}
    assert diagnostics["catalog"]["stt"]["model"] == "stt-rt-v6"


async def test_diagnostics_when_setup_failed(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_client.async_get_catalog.side_effect = SonioxCommunicationError("down")
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)

    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)

    assert diagnostics["entry"]["state"] == "setup_retry"
    assert diagnostics["catalog"] is None
