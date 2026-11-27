"""Alertes (Paramètres → Réparations) : ampoule qui ne répond plus, aucun proxy joignable.

Les alertes ne sont pas conservées au redémarrage (is_persistent=False) : elles réapparaissent si le
problème persiste, et disparaissent dès la première réponse.
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

    def _light_issue(self, unicast: int) -> str:
        return f"unresponsive_{self.entry.entry_id}_{unicast:04x}"

    def _proxy_issue(self) -> str:
        return f"proxies_unreachable_{self.entry.entry_id}"

    def _minutes(self) -> str:
        return str(round(UNRESPONSIVE_AFTER * STATE_POLL_S / 60))

    @callback
    def poll_ok(self, names: dict[int, str], result: dict[int, int | None]) -> None:
        """Lecture réussie (proxy joignable) : compte les ampoules muettes, efface les alertes résolues."""
        self._proxy_failures = 0
        ir.async_delete_issue(self.hass, DOMAIN, self._proxy_issue())
        for unicast, value in result.items():
            if value is not None:
                self._misses.pop(unicast, None)
                ir.async_delete_issue(self.hass, DOMAIN, self._light_issue(unicast))
                continue
            misses = self._misses[unicast] = self._misses.get(unicast, 0) + 1
            if misses == UNRESPONSIVE_AFTER:
                ir.async_create_issue(
                    self.hass, DOMAIN, self._light_issue(unicast),
                    is_fixable=False, is_persistent=False, severity=ir.IssueSeverity.WARNING,
                    translation_key="light_unresponsive",
                    translation_placeholders={"name": names.get(unicast, "?"), "address": f"0x{unicast:04X}",
                                              "minutes": self._minutes()},
                )

    @callback
    def poll_failed(self, error: Exception) -> None:
        """Lecture impossible : aucun proxy joignable (on ne sait rien des ampoules)."""
        self._proxy_failures += 1
        if self._proxy_failures == UNRESPONSIVE_AFTER:
            ir.async_create_issue(
                self.hass, DOMAIN, self._proxy_issue(),
                is_fixable=False, is_persistent=False, severity=ir.IssueSeverity.WARNING,
                translation_key="proxies_unreachable",
                translation_placeholders={"error": str(error)[:300], "minutes": self._minutes()},
            )


@callback
def async_clear_issues(hass: HomeAssistant, entry_id: str, keep: set[int] | None = None) -> None:
    """Efface les alertes de cette entrée (sauf celles des ampoules de `keep`, si fourni)."""
    registry = ir.async_get(hass)
    for domain, issue_id in list(registry.issues):
        if domain != DOMAIN or entry_id not in issue_id:
            continue
        if keep is not None and issue_id.startswith("unresponsive_"):
            try:
                if int(issue_id.rsplit("_", 1)[1], 16) in keep:
                    continue
            except ValueError:
                pass
        if keep is not None and issue_id.startswith("proxies_unreachable_"):
            continue
        ir.async_delete_issue(hass, DOMAIN, issue_id)
