"""Nordlux Smart (Bluetooth Mesh local) : pilotage des ampoules Nordlux PHY6222 sans le cloud."""
from __future__ import annotations

import logging

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import CONF_DEVICES, DOMAIN
from .hub import NordluxMeshHub, async_remove_light, entry_value, parse_devices

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.LIGHT]

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


async def async_setup_entry(hass: HomeAssistant, entry: NordluxConfigEntry) -> bool:
    hub = NordluxMeshHub(hass, entry)
    await hub.async_setup()
    entry.runtime_data = hub
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
