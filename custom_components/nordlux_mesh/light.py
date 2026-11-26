"""Entités light pour les ampoules Nordlux Smart (commandes de l'app : Light CTL Set détourné).

Les statuts renvoyés par l'ampoule ne reflètent pas l'état réel de la lumière : l'état affiché est
le dernier envoyé (optimiste), mémorisé et restauré au redémarrage. `assumed_state` n'est pas utilisé,
pour que Home Assistant affiche un interrupteur standard plutôt que deux boutons « éclair ».
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS, ATTR_TRANSITION, ColorMode, LightEntity, LightEntityFeature,
)
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import NordluxConfigEntry
from .const import CONF_DEVICES, DOMAIN
from .hub import NordluxMeshHub, entry_value, parse_devices

DEFAULT_TRANSITION_MS = 400  # valeur utilisée par l'app


async def async_setup_entry(hass: HomeAssistant, entry: NordluxConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    hub = entry.runtime_data
    async_add_entities(NordluxLight(hub, entry, name, unicast)
                       for name, unicast in parse_devices(entry_value(entry, CONF_DEVICES, "")))


class NordluxLight(RestoreEntity, LightEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_supported_features = LightEntityFeature.TRANSITION

    def __init__(self, hub: NordluxMeshHub, entry: NordluxConfigEntry, name: str, unicast: int) -> None:
        self._hub = hub
        self._unicast = unicast
        self._attr_unique_id = f"{entry.entry_id}_{unicast:04x}"
        self._attr_is_on = None
        self._attr_brightness = 255
        self._attr_extra_state_attributes = {"mesh_address": f"0x{unicast:04X}"}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._attr_unique_id)},
            name=name,
            manufacturer="Nordlux",
            model="Smart Light (Bluetooth Mesh, PHY6222)",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._attr_is_on = last.state == STATE_ON
            if last.attributes.get(ATTR_BRIGHTNESS) is not None:
                self._attr_brightness = int(last.attributes[ATTR_BRIGHTNESS])

    async def async_turn_on(self, **kwargs: Any) -> None:
        dst = self._unicast
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        transition_ms = int(kwargs[ATTR_TRANSITION] * 1000) if ATTR_TRANSITION in kwargs else DEFAULT_TRANSITION_MS
        if brightness is None:
            await self._hub.run(lambda link: link.nx_on(dst))
        else:
            pct = max(1, round(brightness * 100 / 255))
            if not self._attr_is_on:
                await self._hub.run(lambda link: link.nx_on(dst))
            await self._hub.run(lambda link: link.nx_brightness(dst, pct, transition_ms=transition_ms))
            self._attr_brightness = brightness
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        dst = self._unicast
        await self._hub.run(lambda link: link.nx_off(dst))
        self._attr_is_on = False
        self.async_write_ha_state()
