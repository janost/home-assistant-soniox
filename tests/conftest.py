"""Shared fixtures for Soniox tests."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, create_autospec, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.soniox.api import SonioxApiClient, SonioxCatalog, SttCatalog, TtsCatalog
from custom_components.soniox.const import CONF_REGION, DOMAIN, Region
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant

API_KEY = "test-api-key"
STT_ENTITY_ID = "stt.soniox_speech_to_text"
TTS_ENTITY_ID = "tts.soniox_text_to_speech"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load integrations from custom_components in every test."""


@pytest.fixture(autouse=True)
def tts_cache_dir(tmp_path: Path) -> Generator[None]:
    """Keep HA's TTS file cache per test; the default lives in the shared testing config."""
    with patch("homeassistant.components.tts.DEFAULT_CACHE_DIR", str(tmp_path / "tts")):
        yield


@pytest.fixture
def catalog() -> SonioxCatalog:
    """Model catalog returned by the mocked client."""
    return SonioxCatalog(
        stt=SttCatalog(model="stt-rt-v6", languages={"en": "English", "de": "German"}, strict_language_hints=True),
        tts=TtsCatalog(
            model="tts-rt-v2",
            languages={"en": "English", "de": "German"},
            voices={"Maya": "Maya", "Adrian": "Adrian", "clone-1": "Me (cloned)"},
        ),
    )


@pytest.fixture
def mock_client(catalog: SonioxCatalog) -> Generator[MagicMock]:
    """Replace SonioxApiClient everywhere the integration constructs it."""
    client = create_autospec(SonioxApiClient, instance=True)
    client.async_get_catalog.return_value = catalog
    client.async_transcribe.return_value = "turn on the lights"
    with (
        patch("custom_components.soniox.SonioxApiClient", return_value=client) as factory,
        patch("custom_components.soniox.config_flow.SonioxApiClient", new=factory),
    ):
        client.factory = factory  # type: ignore[attr-defined]
        yield client


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """A Soniox config entry in the EU region."""
    return MockConfigEntry(domain=DOMAIN, title="Soniox", data={CONF_API_KEY: API_KEY, CONF_REGION: Region.EU})


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock
) -> MockConfigEntry:
    """Set up the integration with the mocked client."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry
