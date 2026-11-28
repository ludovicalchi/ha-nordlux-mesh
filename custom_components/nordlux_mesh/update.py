"""Mise à jour du firmware des ampoules depuis Home Assistant.

- Vérification : dernière version publiée par type d'ampoule (getOTAInfo), avec les identifiants du compte
  Nordlux enregistrés dans l'entrée (Configurer → Mises à jour du firmware), une fois par jour.
- Installation : téléchargement (MD5 vérifié), passage au bootloader (« 01 02 » sur ff02), flashage par le
  proxy Bluetooth, redémarrage. Voir firmware.py. En cas d'échec, l'ampoule reste en mode mise à jour :
  relancer l'installation (le bootloader déjà présent est détecté).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.components.update import UpdateDeviceClass, UpdateEntity, UpdateEntityFeature
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import NordluxConfigEntry
from .cloud import CloudAuthError, CloudError
from .const import CONF_DEVICES
from .entity import light_device_info, light_mac, light_meta, registry_device
from .fwmanager import light_beacon
from .firmware import ENTER_BOOTLOADER, OTA_CMD, OTA_DATA, OTA_SERVICE, OTAError, ota_mac, parse_hex, run_ota
from .hub import entry_value, parse_devices
from .nxadd import NX_COMPANY, parse_nx_mfr

_LOGGER = logging.getLogger(__name__)
FRESH_S = 15          # une annonce plus ancienne est considérée périmée
BOOTLOADER_WAIT_S = 60
BACK_WAIT_S = 120
BOOTLOADER_VERSION = "mode mise à jour"
OTA_ATTEMPTS = 3


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(x) for x in version.split("."))
# Via un proxy ESPHome : rythme adaptatif (10 ms suffisent en direct depuis un PC ; à ~30 ms, l'ESP32 perd
# 10 à 20 paquets par seconde). Doublé après chaque bloc rejeté, jusqu'au maximum.
PACKET_DELAY_S = 0.05
PACKET_DELAY_MAX_S = 0.25
BLOCK_RETRIES = 8


async def async_setup_entry(hass: HomeAssistant, entry: NordluxConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    hub = entry.runtime_data
    async_add_entities(NordluxFirmwareUpdate(hub, entry, name, unicast)
                       for name, unicast in parse_devices(entry_value(entry, CONF_DEVICES, "")))


class NordluxFirmwareUpdate(CoordinatorEntity, UpdateEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "firmware"
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_entity_category = EntityCategory.CONFIG
    _attr_supported_features = (UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS
                                | UpdateEntityFeature.SPECIFIC_VERSION)

    def __init__(self, hub, entry: NordluxConfigEntry, name: str, unicast: int) -> None:
        super().__init__(hub.coordinator)
        self._hub = hub
        self._entry = entry
        self._unicast = unicast
        self._attr_unique_id = f"{entry.entry_id}_{unicast:04x}_firmware"
        self._attr_device_info = light_device_info(entry, name, unicast)
        self._attr_in_progress = False
        self._attr_update_percentage = None
        self._install_task: asyncio.Task | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._hub.firmware.add_listener(self.async_write_ha_state))

    # --- état
    def _data(self) -> dict:
        return self.coordinator.data or {}

    def _beacon(self) -> str | None:
        return light_beacon(self._entry, self._unicast, self._data(), self.hass)

    def _mac(self) -> str | None:
        return light_mac(self.hass, self._entry, self._unicast, self._data())

    def _in_bootloader(self) -> bool:
        return self._unicast in (self._data().get("bootloader") or set())

    @property
    def available(self) -> bool:
        # Toujours disponible : une ampoule restée en mode mise à jour ne répond plus au mesh, et c'est
        # justement depuis cette entité qu'on relance l'installation (HA ignore les entités indisponibles).
        return True

    @property
    def installed_version(self) -> str | None:
        if self._in_bootloader():
            # Firmware incomplet : « mise à jour disponible », pour afficher le bouton « Installer ».
            return BOOTLOADER_VERSION
        adv = (self._data().get("adv") or {}).get(self._unicast)
        if version := (adv or {}).get("version") or light_meta(self._entry, self._unicast).get("version"):
            return version
        device = registry_device(self.hass, self._entry, self._unicast)
        return device.sw_version if device else None

    @property
    def latest_version(self) -> str | None:
        beacon = self._beacon()
        info = self._hub.firmware.latest.get(beacon) if beacon else None
        return info["version"] if info else None

    @property
    def release_summary(self) -> str | None:
        beacon = self._beacon()
        info = self._hub.firmware.latest.get(beacon) if beacon else None
        return (info or {}).get("description") or None

    @property
    def entity_picture(self) -> str | None:
        # Pas le logo de l'intégration (choix par défaut des entités de mise à jour) : l'icône standard de
        # Home Assistant, qui change selon l'état (à jour / mise à jour disponible / installation).
        return None

    def version_is_newer(self, latest_version: str, installed_version: str) -> bool:
        if installed_version == BOOTLOADER_VERSION:
            return True
        try:
            return _version_tuple(latest_version) > _version_tuple(installed_version)
        except ValueError:
            return latest_version != installed_version

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    # --- installation
    def _progress(self, pct: float) -> None:
        value = int(pct)
        if value != self._attr_update_percentage:
            self._attr_update_percentage = value
            self.async_write_ha_state()

    def _fresh_device(self, address: str):
        """Appareil connectable vu récemment (les annonces anciennes restent en cache dans HA)."""
        si = bluetooth.async_last_service_info(self.hass, address, connectable=True)
        # si.time : horloge monotone (grossière) de habluetooth, même base que time.monotonic() sous Linux.
        if si is None or time.monotonic() - si.time > FRESH_S:
            return None
        return si.device

    async def _wait_device(self, address: str, timeout: float):
        for _ in range(int(timeout)):
            if (device := self._fresh_device(address)) is not None:
                return device
            await asyncio.sleep(1)
        raise OTAError(f"{address} introuvable : le bootloader n'est pas apparu")

    async def _enter_bootloader(self, mac: str) -> None:
        device = bluetooth.async_ble_device_from_address(self.hass, mac, connectable=True)
        if device is None:
            raise OTAError("ampoule introuvable : aucun proxy Bluetooth connectable ne la voit")
        client = await establish_connection(BleakClientWithServiceCache, device, f"nordlux-ota {mac}",
                                            max_attempts=4, use_services_cache=False)
        try:
            svc = client.services.get_service(OTA_SERVICE)
            if svc is None:
                raise OTAError("service de mise à jour absent sur l'ampoule")
            if svc.get_characteristic(OTA_DATA) is not None:
                return  # déjà en bootloader
            try:
                await client.write_gatt_char(OTA_CMD, ENTER_BOOTLOADER, response=False)  # comme l'app
            except Exception:  # noqa: BLE001 - proxy refusant l'écriture sans réponse : avec réponse
                await client.write_gatt_char(OTA_CMD, ENTER_BOOTLOADER, response=True)
            await asyncio.sleep(1)
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001 - l'ampoule redémarre
                pass

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        beacon = self._beacon()
        mac = self._mac()
        if not beacon or not mac:
            raise HomeAssistantError("Type ou adresse Bluetooth de l'ampoule inconnu : attendez une lecture d'état.")
        latest = self.latest_version
        if version and latest and version != latest:
            raise HomeAssistantError(f"Seule la dernière version publiée ({latest}) peut être installée.")
        # Flashage en tâche de fond : si l'appelant abandonne (navigateur fermé, délai d'un client dépassé),
        # l'installation continue au lieu d'être coupée en plein milieu (constaté le 28/11/2026).
        if self._install_task is None or self._install_task.done():
            self._install_task = self._entry.async_create_background_task(
                self.hass, self._async_run_install(beacon, mac), f"nordlux_mesh firmware 0x{self._unicast:04X}")
        await asyncio.shield(self._install_task)

    async def _async_run_install(self, beacon: str, mac: str) -> None:
        self._hub.ota_running.add(self._unicast)
        try:
            await self._async_flash(beacon, mac)
        except HomeAssistantError as err:
            # Journalisé ici aussi : l'appelant a pu abandonner, l'erreur ne lui parviendrait pas.
            _LOGGER.error("Firmware 0x%04X : %s", self._unicast, err)
            raise
        finally:
            self._hub.ota_running.discard(self._unicast)

    async def _async_flash(self, beacon: str, mac: str) -> None:
        self._attr_in_progress = True
        self._attr_update_percentage = None
        self.async_write_ha_state()
        log = lambda msg: _LOGGER.info("Firmware %s (0x%04X) : %s", mac, self._unicast, msg)  # noqa: E731
        try:
            new_version, image = await self._hub.firmware.async_fetch_image(beacon)
            parts = parse_hex(image.decode("ascii", "replace"))
            log(f"image {new_version} téléchargée ({sum(len(p.data) for p in parts)} o, {len(parts)} partitions)")
            boot = ota_mac(mac)
            async with self._hub.exclusive():
                if self._fresh_device(boot) is None:
                    log("passage en mode mise à jour")
                    await self._enter_bootloader(mac)
                else:
                    log("déjà en mode mise à jour (installation précédente interrompue)")
                # Le bootloader accepte de reprendre l'envoi depuis le début après une erreur ou une coupure
                # (constaté le 28/11/2026 : erreur GATT 133 au premier essai, puis reprise acceptée).
                for attempt in range(1, OTA_ATTEMPTS + 1):
                    device = await self._wait_device(boot, BOOTLOADER_WAIT_S)
                    try:
                        client = await establish_connection(
                            BleakClientWithServiceCache, device, f"nordlux-ota {boot}",
                            max_attempts=4, use_services_cache=False)
                        try:
                            await run_ota(client, parts, self._progress, log, retries=BLOCK_RETRIES,
                                          delay=PACKET_DELAY_S, max_delay=PACKET_DELAY_MAX_S)
                        finally:
                            try:
                                await client.disconnect()
                            except Exception:  # noqa: BLE001
                                pass
                        break
                    except Exception as err:  # noqa: BLE001
                        if attempt == OTA_ATTEMPTS:
                            raise
                        log(f"essai {attempt}/{OTA_ATTEMPTS} interrompu ({err}), reprise depuis le début")
                        self._attr_update_percentage = None
                        self.async_write_ha_state()
                        await asyncio.sleep(3)
            await self._wait_back(mac, log)
        except CloudAuthError as err:  # avant tout contact avec l'ampoule
            raise HomeAssistantError(
                "Identifiants Nordlux refusés : saisissez-les de nouveau (notification « Nouvelle "
                "authentification requise »), puis relancez l'installation. L'ampoule n'a pas été modifiée.") from err
        except CloudError as err:  # téléchargement : l'ampoule n'a pas encore été touchée
            raise HomeAssistantError(
                f"Téléchargement du firmware impossible : {err}. L'ampoule n'a pas été modifiée.") from err
        except HomeAssistantError:
            raise
        except OTAError as err:
            raise HomeAssistantError(
                f"Mise à jour impossible : {err}. Si l'ampoule ne se rallume pas, relancez l'installation : "
                "elle est restée en mode mise à jour.") from err
        except Exception as err:
            raise HomeAssistantError(f"Mise à jour interrompue : {err}. Relancez l'installation si besoin.") from err
        finally:
            self._attr_in_progress = False
            self._attr_update_percentage = None
            self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def _wait_back(self, mac: str, log: Callable[[str], None]) -> None:
        """Attend que l'ampoule annonce de nouveau sa configuration (fonctionnement normal)."""
        unicast = self._unicast

        def _back(si: bluetooth.BluetoothServiceInfoBleak) -> bool:
            info = parse_nx_mfr(si.manufacturer_data.get(NX_COMPANY))
            return bool(info and info["configured"] and info["addr"] == unicast)

        try:
            si = await bluetooth.async_process_advertisements(
                self.hass, _back, {"address": mac, "connectable": False},
                bluetooth.BluetoothScanningMode.PASSIVE, BACK_WAIT_S)
            info = parse_nx_mfr(si.manufacturer_data.get(NX_COMPANY))
            log(f"ampoule de retour, firmware {info['version'] if info else '?'}")
        except asyncio.TimeoutError:
            log("pas encore revenue en fonctionnement normal (vérifier à la prochaine lecture)")
