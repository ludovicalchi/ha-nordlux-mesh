"""Dernières versions du firmware publiées par Nordlux (par type d'ampoule), conservées entre les redémarrages.

Vérifiées avec les identifiants du compte enregistrés dans l'entrée (Configurer → Mises à jour du firmware).
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .cloud import CloudError, NordluxCloud
from .const import CONF_DEVICES, DOMAIN
from .entity import light_meta
from .hub import entry_value, parse_devices
from .nxadd import model_info

_LOGGER = logging.getLogger(__name__)


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
            if beacon := light_beacon(self.entry, unicast, data):
                out.add(beacon)
        return sorted(out)

    async def _api(self) -> NordluxCloud:
        if not self.has_credentials:
            raise CloudError("identifiants Nordlux non enregistrés (Configurer → Mises à jour du firmware)")
        api = NordluxCloud(async_get_clientsession(self.hass), clock=dt_util.now)
        await api.login(self.entry.data[CONF_EMAIL], self.entry.data[CONF_PASSWORD])
        return api

    async def async_check(self) -> None:
        beacons = self.beacons()
        if not self.has_credentials or not beacons:
            return
        try:
            info = await (await self._api()).ota_info(beacons)
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


def light_beacon(entry: ConfigEntry, unicast: int, data: dict) -> str | None:
    meta = light_meta(entry, unicast)
    if model := model_info(meta.get("beacon")):
        return model[1]
    adv = (data.get("adv") or {}).get(unicast)
    if adv and (model := model_info(adv.get("code"))):
        return model[1]
    return None
