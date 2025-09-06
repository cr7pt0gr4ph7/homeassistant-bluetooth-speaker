"""Config flow for Bluetooth Speaker integration."""

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class BluetoothSpeakerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Bluetooth speakers."""

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovered: dict[str, Any] = {}
        self._discovered_devices: dict[str, str] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initialized by the user."""

        errors: dict[str, str] = {}

        if user_input is not None:
            mac = user_input[CONF_ADDRESS]
            name = self._discovered_devices.get(mac, mac)

            await self.async_set_unique_id(format_mac(mac))
            self._abort_if_unique_id_configured()

            if not errors:
                return self.async_create_entry(
                    title=name,
                    data={
                        CONF_ADDRESS: mac,
                        CONF_NAME: name,
                    },
                )

        for device in async_discovered_service_info(self.hass):
            self._discovered_devices[device.address] = device.name

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        options = [
            SelectOptionDict(
                value=device_mac,
                label=f"{device_name} ({device_mac})",
            )
            for device_mac, device_name in self._discovered_devices.items()
        ]

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a discovered Bluetooth device."""

        self._discovered[CONF_ADDRESS] = discovery_info.address
        self._discovered[CONF_NAME] = discovery_info.name

        await self.async_set_unique_id(format_mac(discovery_info.address))
        self._abort_if_unique_id_configured()

        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle confirmation of Bluetooth discovery."""

        if user_input is not None:
            return self.async_create_entry(
                title=self._discovered[CONF_NAME],
                data={
                    CONF_ADDRESS: self._discovered[CONF_ADDRESS],
                    CONF_NAME: self._discovered[CONF_NAME],
                },
            )

        self.context["title_placeholders"] = placeholders = {
            CONF_NAME: self._discovered[CONF_NAME]
        }

        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders=placeholders,
        )
