"""Description commune des appareils (ampoules) : fiche Home Assistant et origine."""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo

from .const import CONF_META, DOMAIN
from .hub import entry_value
from .nxadd import ADD_RANGE, model_info

ORIGIN_CLOUD = "cloud"
ORIGIN_HA = "home_assistant"
ORIGIN_UNKNOWN = "unknown"


def light_meta(entry: ConfigEntry, unicast: int) -> dict[str, Any]:
    return dict((entry_value(entry, CONF_META, {}) or {}).get(f"0x{unicast:04X}") or {})


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
