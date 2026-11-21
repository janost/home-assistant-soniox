"""Config flow for the Soniox integration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import probatio as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_API_KEY
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.util.ssl import get_default_context

from .api import SonioxApiClient, SonioxAuthenticationError, SonioxError
from .const import (
    AUTO_LANGUAGE,
    CONF_REGION,
    CONF_STT_LANGUAGE,
    CONF_TTS_VOICE,
    CONSOLE_URL,
    DEFAULT_REGION,
    DEFAULT_TTS_VOICE,
    DOMAIN,
    LOGGER,
    TITLE,
    Region,
)
from .data import SonioxConfigEntry

API_KEY_SELECTOR = selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))
REGION_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[region.value for region in Region],
        translation_key=CONF_REGION,
        mode=selector.SelectSelectorMode.DROPDOWN,
    )
)


async def _async_validate(api_key: str, region: Region) -> dict[str, str]:
    """Check an API key against a region. Return form errors, empty on success."""
    client = SonioxApiClient(api_key=api_key, region=region, ssl_context=get_default_context())
    try:
        await client.async_get_catalog()
    except SonioxAuthenticationError:
        return {"base": "invalid_auth"}
    except SonioxError as err:
        LOGGER.debug("Soniox validation failed: %s", err)
        return {"base": "cannot_connect"}
    finally:
        await client.async_close()
    return {}


def _credentials_schema(region: str) -> vol.Schema:
    """Return the API key and region form."""
    return vol.Schema(
        {
            vol.Required(CONF_API_KEY): API_KEY_SELECTOR,
            vol.Required(CONF_REGION, default=region): REGION_SELECTOR,
        }
    )


class SonioxConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up Soniox with an API key and region."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SonioxOptionsFlow:
        """Return the options flow."""
        return SonioxOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the API key and region."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await _async_validate(user_input[CONF_API_KEY], Region(user_input[CONF_REGION]))
            if not errors:
                return self.async_create_entry(title=TITLE, data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(_credentials_schema(DEFAULT_REGION), user_input),
            errors=errors,
            description_placeholders={"console_url": CONSOLE_URL},
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start reauthentication after Soniox rejected the stored API key."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for a new API key for the configured region."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            region = Region(entry.data.get(CONF_REGION, DEFAULT_REGION))
            errors = await _async_validate(user_input[CONF_API_KEY], region)
            if not errors:
                return self.async_update_reload_and_abort(entry, data_updates=user_input)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_API_KEY): API_KEY_SELECTOR}),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change the API key or region."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await _async_validate(user_input[CONF_API_KEY], Region(user_input[CONF_REGION]))
            if not errors:
                return self.async_update_reload_and_abort(entry, data_updates=user_input)

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_credentials_schema(entry.data.get(CONF_REGION, DEFAULT_REGION)),
            errors=errors,
        )


class SonioxOptionsFlow(OptionsFlowWithReload):
    """Choose the STT language and default TTS voice."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the options form."""
        entry: SonioxConfigEntry = self.config_entry
        if entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="not_loaded")

        if user_input is not None:
            return self.async_create_entry(data=user_input)

        catalog = entry.runtime_data.catalog
        stt_languages = [
            selector.SelectOptionDict(value=code, label=f"{name} ({code})")
            for code, name in sorted(catalog.stt.languages.items(), key=lambda item: item[1])
        ]
        schema: dict[vol.Marker, Any] = {
            vol.Required(CONF_STT_LANGUAGE, default=AUTO_LANGUAGE): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value=AUTO_LANGUAGE, label="Automatic (Assist pipeline language)"),
                        *stt_languages,
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        }
        if catalog.tts is not None and catalog.tts.voices:
            voices = [
                selector.SelectOptionDict(value=voice_id, label=label)
                for voice_id, label in sorted(catalog.tts.voices.items(), key=lambda item: item[1].casefold())
            ]
            default_voice = DEFAULT_TTS_VOICE if DEFAULT_TTS_VOICE in catalog.tts.voices else voices[0]["value"]
            schema[vol.Required(CONF_TTS_VOICE, default=default_voice)] = selector.SelectSelector(
                selector.SelectSelectorConfig(options=voices, mode=selector.SelectSelectorMode.DROPDOWN)
            )

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(vol.Schema(schema), entry.options),
        )
