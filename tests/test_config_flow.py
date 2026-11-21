"""Tests for the Soniox config and options flows."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.soniox.api import SonioxAuthenticationError, SonioxCommunicationError
from custom_components.soniox.const import CONF_REGION, CONF_STT_LANGUAGE, CONF_TTS_VOICE, DOMAIN, Region
from homeassistant.components import stt
from homeassistant.components.tts.const import DATA_COMPONENT as TTS_DATA_COMPONENT
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import API_KEY, STT_ENTITY_ID, TTS_ENTITY_ID


async def test_user_flow_creates_entry_with_region(hass: HomeAssistant, mock_client: MagicMock) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: API_KEY, CONF_REGION: Region.EU}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_API_KEY: API_KEY, CONF_REGION: "eu"}
    assert mock_client.factory.call_args.kwargs["region"] is Region.EU


@pytest.mark.parametrize(
    ("error", "expected"),
    [(SonioxAuthenticationError("bad key"), "invalid_auth"), (SonioxCommunicationError("down"), "cannot_connect")],
)
async def test_user_flow_errors_then_recovers(
    hass: HomeAssistant, mock_client: MagicMock, error: Exception, expected: str
) -> None:
    mock_client.async_get_catalog.side_effect = error
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: API_KEY, CONF_REGION: Region.US}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}

    mock_client.async_get_catalog.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: API_KEY, CONF_REGION: Region.US}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_single_instance(hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock) -> None:
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_reauth_keeps_region(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    result = await setup_integration.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_API_KEY: "new-key"})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert setup_integration.data == {CONF_API_KEY: "new-key", CONF_REGION: "eu"}
    assert mock_client.factory.call_args.kwargs["region"] is Region.EU


async def test_reconfigure_changes_region(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    result = await setup_integration.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: API_KEY, CONF_REGION: Region.JP}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert setup_integration.data[CONF_REGION] == "jp"


async def test_options_flow(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_STT_LANGUAGE: "de", CONF_TTS_VOICE: "Maya"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert setup_integration.options == {CONF_STT_LANGUAGE: "de", CONF_TTS_VOICE: "Maya"}
    # The entry reloaded, so the entities picked up the new options.
    stt_entity = stt.async_get_speech_to_text_entity(hass, STT_ENTITY_ID)
    assert stt_entity is not None
    assert stt_entity.supported_languages == ["de"]
    tts_entity = hass.data[TTS_DATA_COMPONENT].get_entity(TTS_ENTITY_ID)
    assert tts_entity is not None
    assert tts_entity.default_options == {"voice": "Maya"}


async def test_options_flow_aborts_when_not_loaded(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_loaded"
