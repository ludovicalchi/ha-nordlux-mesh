"""Entités light pour les ampoules Nordlux Smart (commandes de l'app : Light CTL Set détourné).

État :
  - après une commande, l'état affiché est mis à jour tout de suite (optimiste) ;
  - toutes les STATE_POLL_S secondes (et peu après le démarrage), l'état réel est relu : « present lightness »
    de Light CTL Status = luminosité en % (0 = éteinte). Les statuts Generic OnOff / Light Lightness, eux,
    ne reflètent pas la lumière. Sans réponse, le dernier état connu est conservé ;
  - l'état est aussi restauré au redémarrage.
`assumed_state` n'est pas utilisé, pour que Home Assistant affiche un interrupteur standard.
"""
from __future__ import annotations

import time
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS, ATTR_TRANSITION, ColorMode, LightEntity, LightEntityFeature,
)
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import NordluxConfigEntry
from .const import CONF_DEVICES, DOMAIN
from .hub import NordluxMeshHub, entry_value, parse_devices

DEFAULT_TRANSITION_MS = 400  # valeur utilisée par l'app


async def async_setup_entry(hass: HomeAssistant, entry: NordluxConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    hub = entry.runtime_data
    async_add_entities(NordluxLight(hub, entry, name, unicast)
                       for name, unicast in parse_devices(entry_value(entry, CONF_DEVICES, "")))


class NordluxLight(CoordinatorEntity, RestoreEntity, LightEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_supported_features = LightEntityFeature.TRANSITION

    def __init__(self, hub: NordluxMeshHub, entry: NordluxConfigEntry, name: str, unicast: int) -> None:
        super().__init__(hub.coordinator)
        self._hub = hub
        self._unicast = unicast
        self._attr_unique_id = f"{entry.entry_id}_{unicast:04x}"
        self._attr_is_on = None
        self._attr_brightness = 255
        self._last_command = 0.0  # time.monotonic() de la dernière commande envoyée
        self._attr_extra_state_attributes = {"mesh_address": f"0x{unicast:04X}"}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._attr_unique_id)},
            name=name,
            manufacturer="Nordlux",
            model="Smart Light (Bluetooth Mesh, PHY6222)",
        )

    @property
    def available(self) -> bool:
        # Une lecture manquée (proxy momentanément injoignable) ne rend pas l'ampoule indisponible :
        # le dernier état connu est conservé et les commandes restent possibles.
        return True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._attr_is_on = last.state == STATE_ON
            if last.attributes.get(ATTR_BRIGHTNESS) is not None:
                self._attr_brightness = int(last.attributes[ATTR_BRIGHTNESS])

    @callback
    def _handle_coordinator_update(self) -> None:
        data = self.coordinator.data or {}
        # Lecture échouée (anciennes données) ou commencée avant notre dernière commande : on l'ignore.
        if not self.coordinator.last_update_success or data.get("read_at", 0) < self._last_command:
            return
        pct = data.get("lightness", {}).get(self._unicast)
        if pct is not None:
            self._attr_is_on = pct > 0
            if pct > 0:
                self._attr_brightness = max(1, min(255, round(min(pct, 100) * 255 / 100)))
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._last_command = time.monotonic()
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
        self._last_command = time.monotonic()
        dst = self._unicast
        await self._hub.run(lambda link: link.nx_off(dst))
        self._attr_is_on = False
        self.async_write_ha_state()
