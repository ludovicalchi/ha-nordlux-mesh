"""Nordlux Smart (Bluetooth Mesh local) : pilotage des ampoules Nordlux PHY6222 sans le cloud."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .hub import NordluxMeshHub

PLATFORMS = [Platform.LIGHT]

type NordluxConfigEntry = ConfigEntry[NordluxMeshHub]


async def async_setup_entry(hass: HomeAssistant, entry: NordluxConfigEntry) -> bool:
    hub = NordluxMeshHub(hass, entry)
    await hub.async_setup()
    entry.runtime_data = hub
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: NordluxConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: NordluxConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.async_close()
    return ok
