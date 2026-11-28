"""Dernières versions du firmware publiées par Nordlux (par type d'ampoule), conservées entre les redémarrages.

Vérifiées avec les identifiants du compte enregistrés dans l'entrée (Configurer → Mises à jour du firmware).
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Callable
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .cloud import CloudAuthError, CloudError, NordluxCloud
from .const import CONF_DEVICES, DOMAIN
from .entity import light_mac, light_meta, registry_device
from .firmware import ota_mac
from .hub import entry_value, parse_devices
from .nxadd import model_info

_LOGGER = logging.getLogger(__name__)
PHYPLUS_COMPANY = 0x0504
_BOOTLOADER_NAME = re.compile(r"^nx[0-9a-f]{4}v\d+$", re.IGNORECASE)
BOOTLOADER_SEEN_S = 60


class FirmwareManager:
    """Dernières versions publiées (par type d'ampoule), conservées entre les redémarrages."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.firmware")
        self.latest: dict[str, dict[str, str]] = {}
        self.last_check: str | None = None
        self._listeners: list[Callable[[], None]] = []

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.latest = data.get("latest", {})
        self.last_check = data.get("last_check")

    @property
    def has_credentials(self) -> bool:
        return bool(self.entry.data.get(CONF_EMAIL) and self.entry.data.get(CONF_PASSWORD))

    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    def beacons(self) -> list[str]:
        """Types (nx…6222) des ampoules configurées : enregistrés à l'ajout, sinon déduits des annonces."""
        data = self.entry.runtime_data.coordinator.data or {}
        out = set()
        for _, unicast in parse_devices(entry_value(self.entry, CONF_DEVICES, "")):
            if beacon := light_beacon(self.entry, unicast, data, self.hass):
                out.add(beacon)
        return sorted(out)

    async def _api(self) -> NordluxCloud:
        if not self.has_credentials:
            raise CloudError("identifiants Nordlux non enregistrés (Configurer → Mises à jour du firmware)")
        api = NordluxCloud(async_get_clientsession(self.hass), clock=dt_util.now)
        try:
            await api.login(self.entry.data[CONF_EMAIL], self.entry.data[CONF_PASSWORD])
        except CloudAuthError:
            # Mot de passe modifié dans l'app, par exemple : notification « Nouvelle authentification requise ».
            # Sans doublon si une demande est déjà ouverte ; l'intégration reste chargée.
            self.entry.async_start_reauth(self.hass)
            raise
        return api

    async def async_check(self) -> None:
        beacons = self.beacons()
        if not self.has_credentials:
            _LOGGER.debug("Vérification du firmware sautée : aucun identifiant Nordlux enregistré")
            return
        if not beacons:
            _LOGGER.debug("Vérification du firmware sautée : type d'ampoule encore inconnu")
            return
        try:
            info = await (await self._api()).ota_info(beacons)
        except CloudAuthError as err:
            _LOGGER.warning("Identifiants Nordlux refusés (%s) : nouvelle authentification demandée", err)
            return
        except CloudError as err:
            _LOGGER.warning("Vérification des mises à jour du firmware impossible : %s", err)
            return
        self.latest = {b: {k: v for k, v in i.items() if k != "url"} for b, i in info.items()}
        self.last_check = dt_util.utcnow().isoformat()
        await self._store.async_save({"latest": self.latest, "last_check": self.last_check})
        _LOGGER.debug("Firmware publié : %s", {b: i["version"] for b, i in self.latest.items()})
        for cb in list(self._listeners):
            cb()

    async def async_fetch_image(self, beacon: str) -> tuple[str, bytes]:
        """Télécharge la dernière image publiée (URL signée fraîche), MD5 vérifié."""
        api = await self._api()
        info = (await api.ota_info([beacon])).get(beacon)
        if not info or not info.get("url"):
            raise CloudError(f"aucun firmware publié pour {beacon}")
        image = await api.download(info["url"])
        if info.get("md5") and hashlib.md5(image).hexdigest() != info["md5"]:
            raise CloudError("MD5 du firmware téléchargé incorrect")
        return info["version"], image


def bootloader_unicasts(hass: HomeAssistant, entry: ConfigEntry, unicasts: list[int], data: dict) -> set[int]:
    """Ampoules restées en mode mise à jour : le bootloader s'annonce à la MAC + 1 (« nx0062v1 », société
    0x0504 PHYPLUS) ; en fonctionnement normal, cette adresse n'émet jamais."""
    out: set[int] = set()
    for unicast in unicasts:
        if not (mac := light_mac(hass, entry, unicast, data)):
            continue
        si = bluetooth.async_last_service_info(hass, ota_mac(mac), connectable=False)
        # si.time : horloge monotone de habluetooth (cf. update.py) ; le bootloader annonce toutes les 250 ms.
        if si is None or time.monotonic() - si.time > BOOTLOADER_SEEN_S:
            continue
        if PHYPLUS_COMPANY in si.manufacturer_data or _BOOTLOADER_NAME.match(si.name or ""):
            out.add(unicast)
    return out


def light_beacon(entry: ConfigEntry, unicast: int, data: dict, hass: HomeAssistant | None = None) -> str | None:
    """Type (nx…6222) : enregistré à l'ajout, sinon annonces, sinon fiche de l'appareil (model_id)."""
    meta = light_meta(entry, unicast)
    if model := model_info(meta.get("beacon")):
        return model[1]
    adv = (data.get("adv") or {}).get(unicast)
    if adv and (model := model_info(adv.get("code"))):
        return model[1]
    if hass is not None and (device := registry_device(hass, entry, unicast)) is not None:
        if model := model_info(device.model_id):
            return model[1]
    return None
