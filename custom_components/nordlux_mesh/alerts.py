"""Disponibilité des ampoules et alerte « aucun proxy Bluetooth disponible ».

- Une ampoule qui ne répond pas à UNRESPONSIVE_AFTER lectures de suite devient « Indisponible » (comme une
  entité Zigbee), et redevient disponible à la première réponse. Pas d'alerte pour ce cas.
- Si Home Assistant n'a plus aucun adaptateur ni proxy Bluetooth connectable, une alerte (Paramètres →
  Réparations) est créée après UNRESPONSIVE_AFTER lectures, et les ampoules deviennent indisponibles.
  L'alerte n'est pas conservée au redémarrage (is_persistent=False) et disparaît dès qu'une lecture réussit.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, STATE_POLL_S, UNRESPONSIVE_AFTER


class ResponseTracker:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._misses: dict[int, int] = {}
        self._proxy_failures = 0

    def _proxy_issue(self) -> str:
        return f"proxies_unreachable_{self.entry.entry_id}"

    def unresponsive(self) -> set[int]:
        """Ampoules à afficher comme indisponibles."""
        return {u for u, n in self._misses.items() if n >= UNRESPONSIVE_AFTER}

    def _count(self, result: dict[int, int | None]) -> None:
        for unicast, value in result.items():
            if value is None:
                self._misses[unicast] = self._misses.get(unicast, 0) + 1
            else:
                self._misses.pop(unicast, None)

    @callback
    def poll_ok(self, result: dict[int, int | None]) -> None:
        """Lecture réussie (un nœud mesh joignable) : compte les ampoules muettes."""
        self._proxy_failures = 0
        ir.async_delete_issue(self.hass, DOMAIN, self._proxy_issue())
        self._count(result)

    @callback
    def poll_failed(self, unicasts: list[int], error: Exception, bluetooth_available: bool) -> None:
        """Lecture impossible : aucune ampoule n'a répondu. Alerte seulement si le Bluetooth de HA a disparu
        (sinon, ce sont les nœuds mesh qui sont injoignables, ex. ampoule éteinte qui était le seul proxy)."""
        self._count({u: None for u in unicasts})
        if bluetooth_available:
            self._proxy_failures = 0
            ir.async_delete_issue(self.hass, DOMAIN, self._proxy_issue())
            return
        self._proxy_failures += 1
        if self._proxy_failures == UNRESPONSIVE_AFTER:
            ir.async_create_issue(
                self.hass, DOMAIN, self._proxy_issue(),
                is_fixable=False, is_persistent=False, severity=ir.IssueSeverity.WARNING,
                translation_key="proxies_unreachable",
                translation_placeholders={"error": str(error)[:300],
                                          "minutes": str(round(UNRESPONSIVE_AFTER * STATE_POLL_S / 60))},
            )


@callback
def async_update_bootloader_issues(hass: HomeAssistant, entry: ConfigEntry, names: dict[int, str],
                                   bootloader: set[int], has_credentials: bool) -> None:
    """Alerte « ampoule bloquée en mode mise à jour » (firmware incomplet) : créée tant que le bootloader
    s'annonce, effacée dès que l'ampoule revient en fonctionnement normal."""
    for unicast, name in names.items():
        issue_id = f"bootloader_{entry.entry_id}_{unicast:04x}"
        if unicast not in bootloader:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
            continue
        ir.async_create_issue(
            hass, DOMAIN, issue_id,
            is_fixable=False, is_persistent=False, severity=ir.IssueSeverity.ERROR,
            translation_key="bulb_in_bootloader" if has_credentials else "bulb_in_bootloader_no_credentials",
            translation_placeholders={"name": name, "address": f"0x{unicast:04X}"},
        )


@callback
def async_clear_issues(hass: HomeAssistant, entry_id: str) -> None:
    """Efface les alertes de cette entrée (y compris les anciennes alertes « ampoule muette » de la 0.8.x)."""
    registry = ir.async_get(hass)
    for domain, issue_id in list(registry.issues):
        if domain == DOMAIN and entry_id in issue_id:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
