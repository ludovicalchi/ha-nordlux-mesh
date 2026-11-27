"""Hub : connexion au proxy mesh via le Bluetooth de Home Assistant, SEQ persistant, sérialisation."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store

from .const import (
    CONF_APPKEY, CONF_DEVICES, CONF_IV_INDEX, CONF_META, CONF_NETKEY, CONF_PROXIES, CONF_SRC, DOMAIN,
    IDLE_DISCONNECT_S, SEQ_BLOCK, STORAGE_VERSION,
)
from .mesh import PROXY_SERVICE, MeshNetwork, ProxyLink
from .nxadd import NX_COMPANY, parse_nx_mfr

_LOGGER = logging.getLogger(__name__)


def parse_devices(text: str) -> list[tuple[str, int]]:
    """« Nom = 0x2005 » par ligne -> [(nom, adresse unicast)]. Lève ValueError si invalide."""
    out: list[tuple[str, int]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"ligne sans « = » : {line}")
        name, addr = (p.strip() for p in line.rsplit("=", 1))
        unicast = int(addr, 0)
        if not name or not 0x0001 <= unicast <= 0x7FFF:
            raise ValueError(f"adresse unicast invalide : {line}")
        out.append((name, unicast))
    if len({u for _, u in out}) != len(out):
        raise ValueError("adresse unicast en double")
    return out


def entry_value(entry: ConfigEntry, key: str, default: Any = None) -> Any:
    """Les options (modifiables après coup) priment sur les données initiales."""
    return entry.options.get(key, entry.data.get(key, default))


def adv_info(hass: HomeAssistant) -> dict[int, dict[str, Any]]:
    """Ampoules configurées visibles, d'après leurs annonces (données 0x6E78), indexées par adresse unicast :
    mac, rssi, source (adaptateur ou proxy qui la reçoit), code du modèle, version du firmware."""
    out: dict[int, dict[str, Any]] = {}
    for si in bluetooth.async_discovered_service_info(hass, connectable=False):
        info = parse_nx_mfr(si.manufacturer_data.get(NX_COMPANY))
        if not info or not info["configured"]:
            continue
        prev = out.get(info["addr"])
        if prev is None or si.rssi > prev["rssi"]:
            out[info["addr"]] = {"mac": si.address.upper(), "rssi": si.rssi, "source": si.source,
                                 "code": info["beacon"], "version": info["version"]}
    return out


def mac_of_unicast(hass: HomeAssistant, unicast: int) -> str | None:
    """Adresse BLE de l'ampoule qui annonce (données 0x6E78) cette adresse unicast, si elle est visible."""
    found = adv_info(hass).get(unicast)
    return found["mac"] if found else None


async def async_remove_light(hass: HomeAssistant, entry: ConfigEntry, unicast: int,
                             reset: bool) -> tuple[dict[str, Any], str | None]:
    """Réinitialise l'ampoule (si demandé, au mieux) et renvoie (nouvelles options sans elle, erreur de réinitialisation).

    La suppression n'est jamais bloquée : si aucun proxy n'est joignable, l'erreur est renvoyée à l'appelant."""
    mac = mac_of_unicast(hass, unicast)
    reset_error: str | None = None
    if reset:
        try:
            await entry.runtime_data.run(lambda link: link.nx_reset(unicast))
        except Exception as err:  # noqa: BLE001
            reset_error = str(err)
    devices = parse_devices(entry_value(entry, CONF_DEVICES, ""))
    proxies = [m.strip() for m in entry_value(entry, CONF_PROXIES, "").split(",") if m.strip()]
    # Une ampoule réinitialisée reste un proxy, mais d'un autre réseau : on la retire des proxys.
    if mac and mac in proxies and len(proxies) > 1:
        proxies.remove(mac)
    meta = dict(entry_value(entry, CONF_META, {}) or {})
    meta.pop(f"0x{unicast:04X}", None)
    return {
        CONF_IV_INDEX: entry_value(entry, CONF_IV_INDEX, 0),
        CONF_PROXIES: ", ".join(proxies),
        CONF_DEVICES: "\n".join(f"{n} = 0x{u:04X}" for n, u in devices if u != unicast),
        CONF_META: meta,
    }, reset_error


class NordluxMeshHub:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store: Store[dict[str, int]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.seq")
        self._seq = 1
        self._hwm = 1
        self._lock = asyncio.Lock()
        self._idle_unsub: Callable[[], None] | None = None
        self.proxies: list[str] = [m.strip().upper() for m in entry_value(entry, CONF_PROXIES, "").split(",") if m.strip()]
        self.net: MeshNetwork | None = None
        self.link: ProxyLink | None = None
        self.coordinator: Any = None  # DataUpdateCoordinator de l'état réel, créé dans __init__.py
        self.last_seen: dict[int, Any] = {}  # adresse unicast -> datetime de la dernière réponse

    async def async_setup(self) -> None:
        data = await self._store.async_load() or {}
        # On repart de la borne haute enregistrée : les SEQ réservés mais non utilisés sont perdus,
        # jamais réutilisés.
        self._seq = self._hwm = int(data.get("seq_hwm", 1))
        self.net = MeshNetwork(
            bytes.fromhex(entry_value(self.entry, CONF_NETKEY)),
            bytes.fromhex(entry_value(self.entry, CONF_APPKEY)),
            int(entry_value(self.entry, CONF_IV_INDEX, 0)),
            int(entry_value(self.entry, CONF_SRC)),
            self._alloc_seq,
        )
        self.link = ProxyLink(self.net, self._connect)
        _LOGGER.debug("Hub Nordlux prêt : src 0x%04X, SEQ %d, proxys %s", self.net.src, self._seq, self.proxies)

    def _alloc_seq(self) -> int:
        if self._seq >= self._hwm:
            # Ne devrait pas arriver : _reserve_seq() est appelée avant chaque commande.
            raise HomeAssistantError("SEQ mesh non réservé")
        s = self._seq
        self._seq += 1
        return s

    async def _reserve_seq(self, needed: int = 8) -> None:
        if self._seq + needed >= self._hwm:
            self._hwm = self._seq + needed + SEQ_BLOCK
            await self._store.async_save({"seq_hwm": self._hwm})

    async def _connect(self):
        errors = []
        for mac in self.proxies:
            device = bluetooth.async_ble_device_from_address(self.hass, mac, connectable=True)
            if device is None:
                errors.append(f"{mac} : non visible")
                continue
            try:
                client = await establish_connection(BleakClientWithServiceCache, device, f"nordlux {mac}", max_attempts=3)
            except Exception as err:  # noqa: BLE001
                errors.append(f"{mac} : {err}")
                continue
            if client.services.get_service(PROXY_SERVICE) is None:
                await client.clear_cache()
                await client.disconnect()
                errors.append(f"{mac} : pas de service Mesh Proxy")
                continue
            _LOGGER.debug("Connecté au proxy mesh %s", mac)
            return client
        raise HomeAssistantError("Aucun proxy Nordlux joignable (" + " ; ".join(errors or ["aucun proxy configuré"]) + ")")

    async def async_read_lightness(self, unicasts: list[int]) -> dict[int, int | None]:
        """Luminosité réelle (%) de chaque ampoule, sur une seule connexion ; None si pas de réponse."""
        async def _read(link: ProxyLink) -> dict[int, int | None]:
            return {u: await link.nx_read_lightness(u) for u in unicasts}
        return await self.run(_read, needed=4 + 2 * len(unicasts))

    async def run(self, action: Callable[[ProxyLink], Awaitable[Any]], needed: int = 8) -> Any:
        """Exécute une commande sur le lien proxy (une seule à la fois, une nouvelle tentative).

        `needed` : nombre de messages mesh que l'action peut émettre (réservation des SEQ)."""
        assert self.link is not None
        async with self._lock:
            self._cancel_idle()
            try:
                for attempt in (1, 2):
                    await self._reserve_seq(needed)
                    try:
                        return await action(self.link)
                    except HomeAssistantError:
                        raise
                    except Exception as err:  # noqa: BLE001 - lien BLE perdu, on reconnecte une fois
                        _LOGGER.debug("Commande mesh en échec (essai %d) : %s", attempt, err)
                        # Cache GATT vidé : l'essai suivant redécouvre les services (handles modifiés).
                        await self.link.close(clear_cache=True)
                        if attempt == 2:
                            raise HomeAssistantError(f"Échec de la commande Nordlux : {err}") from err
            finally:
                self._idle_unsub = async_call_later(self.hass, IDLE_DISCONNECT_S, self._on_idle)

    @callback
    def _cancel_idle(self) -> None:
        if self._idle_unsub:
            self._idle_unsub()
            self._idle_unsub = None

    async def _on_idle(self, _now) -> None:
        self._idle_unsub = None
        async with self._lock:
            if self.link:
                await self.link.close()

    async def async_close(self) -> None:
        self._cancel_idle()
        if self.link:
            await self.link.close()
