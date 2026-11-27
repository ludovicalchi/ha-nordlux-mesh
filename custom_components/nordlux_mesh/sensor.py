"""Capteurs de diagnostic par ampoule : signal Bluetooth, dernière réponse, origine (compte Nordlux ou non)."""
from __future__ import annotations

from homeassistant.components import bluetooth
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import SIGNAL_STRENGTH_DECIBELS_MILLIWATT, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import NordluxConfigEntry
from .const import CONF_DEVICES
from .entity import ORIGIN_CLOUD, ORIGIN_HA, ORIGIN_UNKNOWN, light_device_info, light_origin, light_meta
from .hub import entry_value, parse_devices
from .nxadd import model_info


async def async_setup_entry(hass: HomeAssistant, entry: NordluxConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data.coordinator
    entities: list[SensorEntity] = []
    for name, unicast in parse_devices(entry_value(entry, CONF_DEVICES, "")):
        entities += [
            NordluxSignalSensor(coordinator, entry, name, unicast),
            NordluxLastResponseSensor(coordinator, entry, name, unicast),
            NordluxOriginSensor(entry, name, unicast),
        ]
    async_add_entities(entities)


class _Base(SensorEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def _setup(self, entry: NordluxConfigEntry, name: str, unicast: int, key: str) -> None:
        self._unicast = unicast
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{unicast:04x}_{key}"
        self._attr_device_info = light_device_info(entry, name, unicast)


class NordluxSignalSensor(CoordinatorEntity, _Base):
    """Intensité reçue (RSSI) de la dernière annonce, et proxy qui la reçoit le mieux."""
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_entity_registry_enabled_default = True

    def __init__(self, coordinator, entry, name, unicast) -> None:
        super().__init__(coordinator)
        self._setup(entry, name, unicast, "signal")

    @callback
    def _handle_coordinator_update(self) -> None:
        adv = ((self.coordinator.data or {}).get("adv") or {}).get(self._unicast)
        if adv:
            self._attr_native_value = adv["rssi"]
            scanner = bluetooth.async_scanner_by_source(self.hass, adv["source"])
            self._attr_extra_state_attributes = {
                "proxy": scanner.name if scanner else adv["source"],
                "bluetooth_address": adv["mac"],
            }
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._attr_native_value is not None


class NordluxLastResponseSensor(CoordinatorEntity, _Base):
    """Heure de la dernière lecture d'état réussie."""
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator, entry, name, unicast) -> None:
        super().__init__(coordinator)
        self._setup(entry, name, unicast, "last_response")

    @callback
    def _handle_coordinator_update(self) -> None:
        seen = ((self.coordinator.data or {}).get("seen") or {}).get(self._unicast)
        if seen is not None:
            self._attr_native_value = seen
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return True


class NordluxOriginSensor(_Base):
    """Ampoule connue du compte Nordlux (app/cloud) ou ajoutée sans l'app (Home Assistant)."""
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [ORIGIN_CLOUD, ORIGIN_HA, ORIGIN_UNKNOWN]
    _attr_should_poll = False

    def __init__(self, entry, name, unicast) -> None:
        self._setup(entry, name, unicast, "origin")
        self._attr_native_value = light_origin(entry, unicast)
        meta = light_meta(entry, unicast)
        model = model_info(meta.get("beacon"))
        attrs = {"mesh_address": f"0x{unicast:04X}"}
        if model:
            attrs["color_temperature_range"] = (f"{model[2]} K" if model[2] == model[3]
                                                else f"{model[2]}–{model[3]} K")
        if meta.get("version"):
            attrs["firmware_at_registration"] = meta["version"]
        if meta.get("room"):
            attrs["nordlux_room"] = meta["room"]
        self._attr_extra_state_attributes = attrs
