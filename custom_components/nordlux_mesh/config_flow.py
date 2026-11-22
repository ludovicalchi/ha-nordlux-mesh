"""Assistant de configuration : clés du réseau mesh, proxys BLE et ampoules (nom = adresse unicast)."""
from __future__ import annotations

import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig

from .const import (
    CONF_APPKEY, CONF_DEVICES, CONF_IV_INDEX, CONF_NETKEY, CONF_PROXIES, CONF_SRC, DEFAULT_SRC, DOMAIN,
)
from .hub import parse_devices
from .mesh import k3

_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")
_MAC = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")
_MULTILINE = TextSelector(TextSelectorConfig(multiline=True))


def _norm_proxies(text: str) -> str:
    macs = [m.strip().upper() for m in (text or "").replace(";", ",").split(",") if m.strip()]
    if not macs or not all(_MAC.match(m) for m in macs):
        raise ValueError
    return ", ".join(macs)


def _validate_common(user_input: dict[str, Any], errors: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        out[CONF_PROXIES] = _norm_proxies(user_input[CONF_PROXIES])
    except ValueError:
        errors[CONF_PROXIES] = "invalid_proxies"
    try:
        if not parse_devices(user_input[CONF_DEVICES]):
            raise ValueError
        out[CONF_DEVICES] = user_input[CONF_DEVICES].strip()
    except ValueError:
        errors[CONF_DEVICES] = "invalid_devices"
    iv = user_input.get(CONF_IV_INDEX, 0)
    if not isinstance(iv, int) or iv < 0:
        errors[CONF_IV_INDEX] = "invalid_iv_index"
    out[CONF_IV_INDEX] = iv
    return out


class NordluxMeshConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _validate_common(user_input, errors)
            for key in (CONF_NETKEY, CONF_APPKEY):
                if not _HEX32.match(user_input[key].strip()):
                    errors[key] = "invalid_key"
            try:
                src = int(str(user_input[CONF_SRC]).strip(), 0)
                if not 0x0001 <= src <= 0x7FFF:
                    raise ValueError
            except ValueError:
                errors[CONF_SRC] = "invalid_source"
            if not errors:
                netkey = user_input[CONF_NETKEY].strip().lower()
                network_id = k3(bytes.fromhex(netkey)).hex()
                await self.async_set_unique_id(network_id)
                self._abort_if_unique_id_configured()
                data.update({
                    CONF_NAME: user_input[CONF_NAME].strip() or "Nordlux",
                    CONF_NETKEY: netkey,
                    CONF_APPKEY: user_input[CONF_APPKEY].strip().lower(),
                    CONF_SRC: src,
                })
                return self.async_create_entry(title=f"{data[CONF_NAME]} ({network_id})", data=data)

        defaults = user_input or {}
        schema = vol.Schema({
            vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "Nordlux")): str,
            vol.Required(CONF_NETKEY, default=defaults.get(CONF_NETKEY, "")): str,
            vol.Required(CONF_APPKEY, default=defaults.get(CONF_APPKEY, "")): str,
            vol.Required(CONF_IV_INDEX, default=defaults.get(CONF_IV_INDEX, 0)): vol.Coerce(int),
            vol.Required(CONF_SRC, default=defaults.get(CONF_SRC, f"0x{DEFAULT_SRC:04X}")): str,
            vol.Required(CONF_PROXIES, default=defaults.get(CONF_PROXIES, "")): str,
            vol.Required(CONF_DEVICES, default=defaults.get(CONF_DEVICES, "")): _MULTILINE,
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return NordluxMeshOptionsFlow()


class NordluxMeshOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _validate_common(user_input, errors)
            if not errors:
                return self.async_create_entry(data=data)
        cur = {**self.config_entry.data, **self.config_entry.options, **(user_input or {})}
        schema = vol.Schema({
            vol.Required(CONF_IV_INDEX, default=cur.get(CONF_IV_INDEX, 0)): vol.Coerce(int),
            vol.Required(CONF_PROXIES, default=cur.get(CONF_PROXIES, "")): str,
            vol.Required(CONF_DEVICES, default=cur.get(CONF_DEVICES, "")): _MULTILINE,
        })
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
