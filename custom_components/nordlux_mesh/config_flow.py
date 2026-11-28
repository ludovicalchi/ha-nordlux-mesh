"""Assistant de configuration : depuis le compte Nordlux (clés, ampoules et proxys récupérés
automatiquement) ou par saisie manuelle des clés du réseau mesh, des proxys BLE et des ampoules."""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
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
    CONF_APPKEY, CONF_DEVICES, CONF_HOUSE, CONF_IV_INDEX, CONF_META, CONF_NETKEY, CONF_PROXIES, CONF_SRC,
    DEFAULT_SRC, DOMAIN,
)
from .hub import parse_devices
from .mesh import k3
from .nxadd import NX_COMPANY, NX_WRITE, model_info, next_free_address, nx_config_messages, parse_nx_mfr

_LOGGER = logging.getLogger(__name__)
CONF_MAC = "mac"
CONF_FORGET = "forget"
VERIFY_TIMEOUT_S = 45


class NxAddError(Exception):
    """Échec de l'ajout d'une ampoule réinitialisée."""


def _reset_lights(hass: HomeAssistant) -> dict[str, tuple[dict, int]]:
    """Ampoules Nordlux réinitialisées (clés non configurées) vues par un adaptateur ou proxy connectable."""
    found: dict[str, tuple[dict, int]] = {}
    for si in bluetooth.async_discovered_service_info(hass, connectable=True):
        info = parse_nx_mfr(si.manufacturer_data.get(NX_COMPANY))
        if info and not info["configured"]:
            found[si.address.upper()] = (info, si.rssi)
    return found


async def _async_nx_add(hass: HomeAssistant, mac: str, netkey: bytes, appkey: bytes, new_addr: int,
                        room_num: int = 1) -> None:
    """Envoie à l'ampoule les clés du réseau et sa nouvelle adresse, puis attend qu'elle l'annonce."""
    si = bluetooth.async_last_service_info(hass, mac, connectable=True)
    info = parse_nx_mfr(si.manufacturer_data.get(NX_COMPANY)) if si else None
    if info is None:
        raise NxAddError("ampoule plus visible")
    if info["configured"]:
        raise NxAddError("l'ampoule a déjà des clés : réinitialisez-la d'abord")
    device = bluetooth.async_ble_device_from_address(hass, mac, connectable=True)
    if device is None:
        raise NxAddError("aucun proxy Bluetooth connectable ne voit l'ampoule")
    msgs = nx_config_messages(info["addr_raw"], netkey, appkey, new_addr, room_num)
    for attempt in (1, 2):
        try:
            client = await establish_connection(BleakClientWithServiceCache, device, f"nordlux-add {mac}",
                                                max_attempts=3)
        except Exception as err:  # noqa: BLE001
            raise NxAddError(f"connexion impossible : {err}") from err
        try:
            if client.services.get_characteristic(NX_WRITE) is None:
                # Cache GATT périmé : l'ampoule réorganise ses services après une réinitialisation.
                await client.clear_cache()
                if attempt == 2:
                    raise NxAddError("service de configuration Nordlux introuvable")
                continue
            for msg in msgs:
                await client.write_gatt_char(NX_WRITE, msg, response=True)
                await asyncio.sleep(0.3)
            await asyncio.sleep(2)
            break
        except NxAddError:
            raise
        except Exception as err:  # noqa: BLE001
            raise NxAddError(f"écriture impossible : {err}") from err
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    def _configured(service_info: bluetooth.BluetoothServiceInfoBleak) -> bool:
        new = parse_nx_mfr(service_info.manufacturer_data.get(NX_COMPANY))
        return bool(new and new["configured"] and new["addr"] == new_addr)

    try:
        await bluetooth.async_process_advertisements(
            hass, _configured, {"address": mac, "connectable": False},
            bluetooth.BluetoothScanningMode.PASSIVE, VERIFY_TIMEOUT_S,
        )
    except asyncio.TimeoutError as err:
        raise NxAddError("l'ampoule n'annonce pas sa nouvelle configuration") from err

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
                    CONF_META: house.meta(),
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

    # --- Nouvelle authentification : identifiants refusés par le cloud (vérification du firmware)
    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            try:
                api = NordluxCloud(async_get_clientsession(self.hass), clock=dt_util.now)
                await api.login(email, user_input[CONF_PASSWORD])
            except CloudAuthError:
                errors["base"] = "invalid_auth"
            except CloudError:
                errors["base"] = "cannot_connect"
            else:
                # Rechargement par l'écouteur de l'entrée : nouvelle vérification une minute plus tard.
                self.hass.config_entries.async_update_entry(
                    entry, data={**entry.data, CONF_EMAIL: email, CONF_PASSWORD: user_input[CONF_PASSWORD]})
                return self.async_abort(reason="reauth_successful")
        defaults = user_input or {CONF_EMAIL: entry.data.get(CONF_EMAIL, "")}
        return self.async_show_form(step_id="reauth_confirm", data_schema=_cloud_schema(defaults), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return NordluxMeshOptionsFlow()


class NordluxMeshOptionsFlow(OptionsFlow):
    def __init__(self) -> None:
        self._house: CloudHouse | None = None
        self._add: dict[str, Any] = {}
        self._add_task: asyncio.Task | None = None
        self._add_error = ""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        # Suppression : bouton « Supprimer » de l'appareil (réinitialisation à distance, cf. __init__.py).
        return self.async_show_menu(step_id="init", menu_options=["add_light", "cloud", "firmware"])

    # --- Mises à jour du firmware : identifiants du compte, enregistrés pour la vérification quotidienne
    async def async_step_firmware(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        data = self.config_entry.data
        if user_input is not None:
            if user_input.get(CONF_FORGET):
                new_data = {k: v for k, v in data.items() if k not in (CONF_EMAIL, CONF_PASSWORD)}
                self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
                return self.async_create_entry(data=dict(self.config_entry.options))
            email = (user_input.get(CONF_EMAIL) or "").strip()
            password = user_input.get(CONF_PASSWORD) or data.get(CONF_PASSWORD, "")
            if not email or not password:
                errors["base"] = "invalid_auth"
            else:
                try:
                    api = NordluxCloud(async_get_clientsession(self.hass), clock=dt_util.now)
                    await api.login(email, password)
                except CloudAuthError:
                    errors["base"] = "invalid_auth"
                except CloudError:
                    errors["base"] = "cannot_connect"
                else:
                    self.hass.config_entries.async_update_entry(
                        self.config_entry, data={**data, CONF_EMAIL: email, CONF_PASSWORD: password})
                    # Rechargement (écouteur) : vérification des mises à jour une minute plus tard.
                    return self.async_create_entry(data=dict(self.config_entry.options))
        schema = vol.Schema({
            vol.Optional(CONF_EMAIL, default=data.get(CONF_EMAIL, "")): _EMAIL,
            vol.Optional(CONF_PASSWORD): _PASSWORD,
            vol.Optional(CONF_FORGET, default=False): bool,
        })
        return self.async_show_form(
            step_id="firmware", data_schema=schema, errors=errors,
            description_placeholders={"state": "✔" if data.get(CONF_PASSWORD) else "✘"},
        )

    # --- Ajout d'une ampoule réinitialisée, sans l'app
    async def async_step_add_light(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        found = _reset_lights(self.hass)
        errors: dict[str, str] = {}
        if user_input is not None and user_input.get(CONF_MAC):
            mac = user_input[CONF_MAC]
            if mac in found:
                try:
                    used = {u for _, u in parse_devices(self._current().get(CONF_DEVICES, ""))}
                    addr = next_free_address(used)
                except ValueError:
                    return self.async_abort(reason="no_free_address")
                name = (user_input.get(CONF_NAME) or "").replace("=", "-").replace("\n", " ").strip()
                info = found[mac][0]
                self._add = {"mac": mac, "name": name or "Ampoule Nordlux", "addr": addr,
                             "code": info["beacon"], "version": info["version"]}
                self._add_task = None
                return await self.async_step_add_light_run()
            errors["base"] = "light_gone"
        if not found:
            # Formulaire vide : valider relance la recherche.
            return self.async_show_form(step_id="add_light", data_schema=vol.Schema({}),
                                        errors=errors or {"base": "no_reset_lights"})
        options = [SelectOptionDict(value=mac, label=f"{mac} · {info['beacon']} v{info['version']} · {rssi} dBm")
                   for mac, (info, rssi) in sorted(found.items(), key=lambda x: -x[1][1])]
        schema = vol.Schema({
            vol.Required(CONF_MAC, default=options[0]["value"]): SelectSelector(SelectSelectorConfig(options=options)),
            vol.Required(CONF_NAME, default="Ampoule Nordlux"): str,
        })
        return self.async_show_form(step_id="add_light", data_schema=schema, errors=errors)

    async def async_step_add_light_run(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._add_task is None:
            data = self.config_entry.data
            self._add_task = self.hass.async_create_task(_async_nx_add(
                self.hass, self._add["mac"], bytes.fromhex(data[CONF_NETKEY]), bytes.fromhex(data[CONF_APPKEY]),
                self._add["addr"]))
        if not self._add_task.done():
            return self.async_show_progress(
                step_id="add_light_run", progress_action="adding_light", progress_task=self._add_task,
                description_placeholders={"name": self._add["name"], "address": f"0x{self._add['addr']:04X}"},
            )
        err = None if self._add_task.cancelled() else self._add_task.exception()
        if self._add_task.cancelled() or err is not None:
            self._add_error = str(err) if err else "annulé"
            _LOGGER.warning("Ajout de l'ampoule %s impossible : %s", self._add["mac"], self._add_error)
            return self.async_show_progress_done(next_step_id="add_light_failed")
        return self.async_show_progress_done(next_step_id="add_light_done")

    async def async_step_add_light_done(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        cur = self._current()
        devices = "\n".join(filter(None, [cur.get(CONF_DEVICES, "").strip(),
                                          f"{self._add['name']} = 0x{self._add['addr']:04X}"]))
        proxies = [m.strip() for m in cur.get(CONF_PROXIES, "").split(",") if m.strip()]
        if self._add["mac"] not in proxies:
            proxies.append(self._add["mac"])  # chaque ampoule est aussi un proxy mesh
        meta = dict(cur.get(CONF_META) or {})
        model = model_info(self._add["code"])
        meta[f"0x{self._add['addr']:04X}"] = {
            "source": "ha", "mac": self._add["mac"], "beacon": model[1] if model else "",
            "version": self._add["version"], "room": "",
        }
        return self.async_create_entry(data={
            CONF_IV_INDEX: cur.get(CONF_IV_INDEX, 0),
            CONF_PROXIES: ", ".join(proxies),
            CONF_DEVICES: devices,
            CONF_META: meta,
        })

    async def async_step_add_light_failed(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_abort(reason="add_failed", description_placeholders={"error": self._add_error})

    def _current(self) -> dict[str, Any]:
        return {**self.config_entry.data, **self.config_entry.options}

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
                # Informations du compte pour ses ampoules ; celles ajoutées sans l'app gardent les leurs.
                data[CONF_META] = {**(self._current().get(CONF_META) or {}), **house.meta()}
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
