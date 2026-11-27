"""Nordlux Smart (Bluetooth Mesh local) : pilotage des ampoules Nordlux PHY6222 sans le cloud."""
from __future__ import annotations

import logging
import time
from datetime import timedelta

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from homeassistant.util import dt as dt_util

from .alerts import ResponseTracker, async_clear_issues
from .const import CONF_DEVICES, DOMAIN, FIRST_POLL_DELAY_S, STATE_POLL_S
from .hub import NordluxMeshHub, adv_info, async_remove_light, entry_value, parse_devices
from .nxadd import model_info

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.LIGHT, Platform.SENSOR]

type NordluxConfigEntry = ConfigEntry[NordluxMeshHub]


def _configured_ids(entry: ConfigEntry) -> set[str]:
    """Identifiants d'appareil des ampoules actuellement dans la liste (cf. light.py : entry_id_unicast)."""
    try:
        devices = parse_devices(entry_value(entry, CONF_DEVICES, ""))
    except ValueError:
        return set()
    return {f"{entry.entry_id}_{unicast:04x}" for _, unicast in devices}


def _is_stale(entry: ConfigEntry, device: dr.DeviceEntry) -> bool:
    ids = {ident for domain, ident in device.identifiers if domain == DOMAIN}
    return bool(ids) and not ids & _configured_ids(entry)


def _async_update_devices(hass: HomeAssistant, entry: ConfigEntry, adv: dict[int, dict]) -> None:
    """Fiche de l'appareil : modèle, firmware et adresse Bluetooth d'après les annonces de l'ampoule."""
    registry = dr.async_get(hass)
    for unicast, a in adv.items():
        device = registry.async_get_device(identifiers={(DOMAIN, f"{entry.entry_id}_{unicast:04x}")})
        if device is None:
            continue
        changes: dict = {}
        if a.get("version") and device.sw_version != a["version"]:
            changes["sw_version"] = a["version"]
        if (model := model_info(a.get("code"))) is not None:
            if device.model != model[0]:
                changes["model"] = model[0]
            if device.model_id != model[1]:
                changes["model_id"] = model[1]
        connection = (dr.CONNECTION_BLUETOOTH, a["mac"])
        if connection not in device.connections:
            changes["merge_connections"] = {connection}
        if changes:
            registry.async_update_device(device.id, **changes)


async def async_setup_entry(hass: HomeAssistant, entry: NordluxConfigEntry) -> bool:
    hub = NordluxMeshHub(hass, entry)
    await hub.async_setup()
    entry.runtime_data = hub

    try:
        names = {u: n for n, u in parse_devices(entry_value(entry, CONF_DEVICES, ""))}
    except ValueError:
        names = {}
    # Alertes d'ampoules qui ne sont plus dans la liste : effacées.
    async_clear_issues(hass, entry.entry_id, keep=set(names))
    tracker = ResponseTracker(hass, entry)

    # Lecture périodique de l'état réel (Light CTL Get) de toutes les ampoules, sur une seule connexion.
    async def _async_update() -> dict:
        # read_at : début de la lecture ; une entité ignore une lecture commencée avant sa dernière commande.
        read_at = time.monotonic()
        if not names:
            return {"read_at": read_at, "lightness": {}, "adv": {}, "seen": {}}
        try:
            result = await hub.async_read_lightness(list(names))
        except Exception as err:  # noqa: BLE001
            tracker.poll_failed(err)
            raise UpdateFailed(f"lecture de l'état impossible : {err}") from err
        tracker.poll_ok(names, result)
        now = dt_util.utcnow()
        for unicast, value in result.items():
            if value is not None:
                hub.last_seen[unicast] = now
        adv = {u: a for u, a in adv_info(hass).items() if u in names}
        _async_update_devices(hass, entry, adv)
        return {"read_at": read_at, "lightness": result, "adv": adv, "seen": dict(hub.last_seen)}

    hub.coordinator = DataUpdateCoordinator(
        hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} état",
        update_method=_async_update, update_interval=timedelta(seconds=STATE_POLL_S),
    )

    async def _async_first_poll(_now) -> None:
        await hub.coordinator.async_refresh()

    entry.async_on_unload(async_call_later(hass, FIRST_POLL_DELAY_S, _async_first_poll))
    # Appareils d'ampoules retirées de la liste (changement d'adresse, ampoule supprimée) : on les retire.
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        if _is_stale(entry, device):
            _LOGGER.info("Suppression de l'appareil %s, absent de la liste des ampoules", device.name)
            registry.async_update_device(device.id, remove_config_entry_id=entry.entry_id)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_remove_config_entry_device(hass: HomeAssistant, entry: NordluxConfigEntry,
                                           device: dr.DeviceEntry) -> bool:
    """Bouton « Supprimer » de l'appareil : réinitialisation à distance (au mieux) puis retrait de la liste.

    Appareil orphelin : supprimé directement. Ampoule configurée : réinitialisée à distance puis retirée de la
    liste. Si aucun proxy n'est joignable, elle est retirée quand même et une notification demande de la
    réinitialiser à la main."""
    if _is_stale(entry, device):
        return True
    prefix = f"{entry.entry_id}_"
    unicast = next((int(ident[len(prefix):], 16) for domain, ident in device.identifiers
                    if domain == DOMAIN and ident.startswith(prefix)), None)
    if unicast is None:
        return False
    new_options, reset_error = await async_remove_light(hass, entry, unicast, reset=True)
    if reset_error:
        _LOGGER.warning("Ampoule 0x%04X retirée sans réinitialisation : %s", unicast, reset_error)
        persistent_notification.async_create(
            hass,
            f"L'ampoule « {device.name} » (0x{unicast:04X}) a été retirée de Home Assistant, mais la "
            f"réinitialisation n'a pas pu lui être envoyée ({reset_error}).\n\n"
            "Elle fait donc toujours partie du réseau mesh. Pour la réutiliser, réinitialisez-la à la main : "
            "3 extinctions/rallumages à 2–4 s d'intervalle (elle clignote).",
            title="Nordlux : ampoule non réinitialisée",
            notification_id=f"{DOMAIN}_reset_{entry.entry_id}_{unicast:04x}",
        )
    else:
        _LOGGER.info("Ampoule 0x%04X réinitialisée et retirée de la liste", unicast)
    hass.config_entries.async_update_entry(entry, options=new_options)  # rechargement par l'écouteur
    return True


async def _async_reload(hass: HomeAssistant, entry: NordluxConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: NordluxConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.async_close()
    return ok


async def async_remove_entry(hass: HomeAssistant, entry: NordluxConfigEntry) -> None:
    async_clear_issues(hass, entry.entry_id)
