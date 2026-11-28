"""Description commune des appareils (ampoules) : fiche Home Assistant et origine."""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo

from .const import CONF_META, DOMAIN
from .hub import entry_value
from .nxadd import ADD_RANGE, model_info

ORIGIN_CLOUD = "cloud"
ORIGIN_HA = "home_assistant"
ORIGIN_UNKNOWN = "unknown"


def light_meta(entry: ConfigEntry, unicast: int) -> dict[str, Any]:
    return dict((entry_value(entry, CONF_META, {}) or {}).get(f"0x{unicast:04X}") or {})


def registry_device(hass: HomeAssistant, entry: ConfigEntry, unicast: int) -> dr.DeviceEntry | None:
    """Fiche de l'ampoule dans le registre des appareils. Elle garde la MAC (connexion Bluetooth), le modèle
    (model_id = nx…6222) et le firmware vus dans les annonces, même quand l'ampoule n'annonce plus rien
    (bootloader, redémarrage de Home Assistant)."""
    ident = f"{entry.entry_id}_{unicast:04x}"
    for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id):
        if (DOMAIN, ident) in device.identifiers:
            return device
    return None


def registry_mac(device: dr.DeviceEntry | None) -> str | None:
    if device is None:
        return None
    return next((value for kind, value in device.connections if kind == CONNECTION_BLUETOOTH), None)


def light_mac(hass: HomeAssistant, entry: ConfigEntry, unicast: int, data: dict) -> str | None:
    """Adresse Bluetooth : annonces récentes, sinon enregistrée à l'ajout, sinon fiche de l'appareil."""
    adv = (data.get("adv") or {}).get(unicast)
    return ((adv or {}).get("mac") or light_meta(entry, unicast).get("mac")
            or registry_mac(registry_device(hass, entry, unicast)) or None)


def light_origin(entry: ConfigEntry, unicast: int) -> str:
    """Compte Nordlux (ampoule connue du cloud à la dernière lecture du compte), ajoutée sans l'app, ou inconnue."""
    source = light_meta(entry, unicast).get("source")
    if source == "cloud":
        return ORIGIN_CLOUD
    if source == "ha" or unicast in ADD_RANGE:
        return ORIGIN_HA
    return ORIGIN_UNKNOWN


def light_device_info(entry: ConfigEntry, name: str, unicast: int) -> DeviceInfo:
    meta = light_meta(entry, unicast)
    model = model_info(meta.get("beacon"))
    info = DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_{unicast:04x}")},
        name=name,
        manufacturer="Nordlux",
        model=model[0] if model else "Smart Light (Bluetooth Mesh, PHY6222)",
    )
    if model:
        info["model_id"] = model[1]
    if meta.get("version"):
        info["sw_version"] = meta["version"]
    if meta.get("room"):
        info["suggested_area"] = meta["room"]
    if meta.get("mac"):
        info["connections"] = {(CONNECTION_BLUETOOTH, meta["mac"])}
    return info
