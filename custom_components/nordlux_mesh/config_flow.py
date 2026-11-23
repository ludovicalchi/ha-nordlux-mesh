"""Assistant de configuration : depuis le compte Nordlux (clés, ampoules et proxys récupérés
automatiquement) ou par saisie manuelle des clés du réseau mesh, des proxys BLE et des ampoules."""
from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_EMAIL, CONF_NAME, CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from homeassistant.helpers.selector import (
    SelectOptionDict, SelectSelector, SelectSelectorConfig, TextSelector, TextSelectorConfig, TextSelectorType,
)

from .cloud import CloudAuthError, CloudError, CloudHouse, NordluxCloud
from .const import (
    CONF_APPKEY, CONF_DEVICES, CONF_HOUSE, CONF_IV_INDEX, CONF_NETKEY, CONF_PROXIES, CONF_SRC, DEFAULT_SRC,
    DOMAIN,
)
from .hub import parse_devices
from .mesh import k3

_LOGGER = logging.getLogger(__name__)

_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")
_MAC = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")
_MULTILINE = TextSelector(TextSelectorConfig(multiline=True))
_PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
_EMAIL = TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username"))


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


def _parse_src(value: Any, errors: dict[str, str]) -> int:
    try:
        src = int(str(value).strip(), 0)
        if not 0x0001 <= src <= 0x7FFF:
            raise ValueError
    except ValueError:
        errors[CONF_SRC] = "invalid_source"
        return 0
    return src


def _network_id(netkey: str) -> str:
    return k3(bytes.fromhex(netkey)).hex()


def _cloud_schema(defaults: dict[str, Any]) -> vol.Schema:
    # Le mot de passe n'est jamais pré-rempli ni conservé.
    return vol.Schema({
        vol.Required(CONF_EMAIL, default=defaults.get(CONF_EMAIL, "")): _EMAIL,
        vol.Required(CONF_PASSWORD): _PASSWORD,
    })


async def _fetch_houses(hass: HomeAssistant, user_input: dict[str, Any],
                        errors: dict[str, str]) -> list[CloudHouse]:
    """Connexion au compte et lecture des maisons ; renseigne `errors` en cas d'échec."""
    try:
        api = NordluxCloud(async_get_clientsession(hass), clock=dt_util.now)
        await api.login(user_input[CONF_EMAIL].strip(), user_input[CONF_PASSWORD])
        houses = await api.houses()
    except CloudAuthError as err:
        _LOGGER.debug("Connexion au compte Nordlux refusée : %s", err)
        errors["base"] = "invalid_auth"
        return []
    except CloudError as err:
        _LOGGER.warning("Cloud Nordlux : %s", err)
        errors["base"] = "cannot_connect"
        return []
    houses = [h for h in houses if h.lights]
    if not houses:
        errors["base"] = "no_lights"
    return houses


def _house_selector(houses: list[CloudHouse]) -> SelectSelector:
    return SelectSelector(SelectSelectorConfig(options=[
        SelectOptionDict(value=h.house_id, label=f"{h.name} ({len(h.lights)} ampoule(s))") for h in houses
    ]))


class NordluxMeshConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._houses: list[CloudHouse] = []
        self._house: CloudHouse | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=["cloud", "manual"])

    # --- Depuis le compte Nordlux
    async def async_step_cloud(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._houses = await _fetch_houses(self.hass, user_input, errors)
            if not errors:
                if len(self._houses) == 1:
                    return await self._async_select_house(self._houses[0])
                return await self.async_step_house()
        return self.async_show_form(step_id="cloud", data_schema=_cloud_schema(user_input or {}), errors=errors)

    async def async_step_house(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            house = next(h for h in self._houses if h.house_id == user_input[CONF_HOUSE])
            return await self._async_select_house(house)
        return self.async_show_form(step_id="house", data_schema=vol.Schema({
            vol.Required(CONF_HOUSE): _house_selector(self._houses),
        }))

    async def _async_select_house(self, house: CloudHouse) -> ConfigFlowResult:
        self._house = house
        await self.async_set_unique_id(_network_id(house.netkey))
        self._abort_if_unique_id_configured()
        return await self.async_step_cloud_confirm()

    async def async_step_cloud_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        house = self._house
        assert house is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _validate_common({**user_input, CONF_IV_INDEX: house.iv_index}, errors)
            src = _parse_src(user_input[CONF_SRC], errors)
            if not errors:
                data.update({
                    CONF_NAME: user_input[CONF_NAME].strip() or "Nordlux",
                    CONF_NETKEY: house.netkey,
                    CONF_APPKEY: house.appkey,
                    CONF_SRC: src,
                })
                return self.async_create_entry(title=f"{data[CONF_NAME]} ({self.unique_id})", data=data)
        cur = user_input or {}
        schema = vol.Schema({
            vol.Required(CONF_NAME, default=cur.get(CONF_NAME, house.name)): str,
            vol.Required(CONF_SRC, default=cur.get(CONF_SRC, f"0x{DEFAULT_SRC:04X}")): str,
            vol.Required(CONF_PROXIES, default=cur.get(CONF_PROXIES, ", ".join(house.proxies))): str,
            vol.Required(CONF_DEVICES, default=cur.get(CONF_DEVICES, house.devices_text())): _MULTILINE,
        })
        return self.async_show_form(
            step_id="cloud_confirm", data_schema=schema, errors=errors,
            description_placeholders={"house": house.name, "count": str(len(house.lights))},
        )

    # --- Saisie manuelle
    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _validate_common(user_input, errors)
            for key in (CONF_NETKEY, CONF_APPKEY):
                if not _HEX32.match(user_input[key].strip()):
                    errors[key] = "invalid_key"
            src = _parse_src(user_input[CONF_SRC], errors)
            if not errors:
                netkey = user_input[CONF_NETKEY].strip().lower()
                network_id = _network_id(netkey)
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
        return self.async_show_form(step_id="manual", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return NordluxMeshOptionsFlow()


class NordluxMeshOptionsFlow(OptionsFlow):
    def __init__(self) -> None:
        self._house: CloudHouse | None = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["cloud", "settings"])

    def _current(self) -> dict[str, Any]:
        return {**self.config_entry.data, **self.config_entry.options}

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _validate_common(user_input, errors)
            if not errors:
                return self.async_create_entry(data=data)
        cur = {**self._current(), **(user_input or {})}
        schema = vol.Schema({
            vol.Required(CONF_IV_INDEX, default=cur.get(CONF_IV_INDEX, 0)): vol.Coerce(int),
            vol.Required(CONF_PROXIES, default=cur.get(CONF_PROXIES, "")): str,
            vol.Required(CONF_DEVICES, default=cur.get(CONF_DEVICES, "")): _MULTILINE,
        })
        return self.async_show_form(step_id="settings", data_schema=schema, errors=errors)

    # --- Mise à jour depuis le compte Nordlux (nouvelles ampoules, proxys, IV index)
    async def async_step_cloud(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            houses = await _fetch_houses(self.hass, user_input, errors)
            if not errors:
                netkey = self.config_entry.data[CONF_NETKEY]
                self._house = next((h for h in houses if h.netkey == netkey), None)
                if self._house is None:
                    errors["base"] = "house_not_found"
                else:
                    return await self.async_step_cloud_confirm()
        return self.async_show_form(step_id="cloud", data_schema=_cloud_schema(user_input or {}), errors=errors)

    async def async_step_cloud_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        house = self._house
        assert house is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _validate_common(user_input, errors)
            if not errors:
                return self.async_create_entry(data=data)
        cur = user_input or {}
        # Les proxys déjà configurés restent en tête (ordre choisi par l'utilisateur), suivis des nouveaux.
        old = [m.strip() for m in self._current().get(CONF_PROXIES, "").split(",") if m.strip()]
        proxies = ", ".join(dict.fromkeys(old + house.proxies))
        # Liste des ampoules = celle du compte, en gardant le nom déjà choisi pour une adresse connue.
        try:
            known = {u: n for n, u in parse_devices(self._current().get(CONF_DEVICES, ""))}
        except ValueError:
            known = {}
        devices = "\n".join(
            f"{known.get(u, n)} = 0x{u:04X}" for n, u in parse_devices(house.devices_text())
        )
        schema = vol.Schema({
            vol.Required(CONF_IV_INDEX, default=cur.get(CONF_IV_INDEX, house.iv_index)): vol.Coerce(int),
            vol.Required(CONF_PROXIES, default=cur.get(CONF_PROXIES, proxies)): str,
            vol.Required(CONF_DEVICES, default=cur.get(CONF_DEVICES, devices)): _MULTILINE,
        })
        return self.async_show_form(
            step_id="cloud_confirm", data_schema=schema, errors=errors,
            description_placeholders={"house": house.name, "count": str(len(house.lights))},
        )
