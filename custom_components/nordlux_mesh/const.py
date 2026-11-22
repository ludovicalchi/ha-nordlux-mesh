"""Constantes de l'intégration Nordlux Smart (Bluetooth Mesh local)."""
DOMAIN = "nordlux_mesh"

CONF_NETKEY = "netkey"
CONF_APPKEY = "appkey"
CONF_IV_INDEX = "iv_index"
CONF_SRC = "source_address"
CONF_PROXIES = "proxies"
CONF_DEVICES = "devices"

# Adresse source propre à Home Assistant (le script PC utilise 0x7F01) : chaque émetteur
# doit avoir sa propre adresse, car l'anti-rejeu des nœuds suit le SEQ par adresse source.
DEFAULT_SRC = 0x7F10
# Déconnexion du proxy après N s d'inactivité (les proxys ESPHome ont peu de connexions).
IDLE_DISCONNECT_S = 30
# Le SEQ est réservé par blocs : on enregistre la borne haute avant de l'atteindre, et on
# repart de cette borne au démarrage, pour ne jamais réutiliser un SEQ après un redémarrage.
SEQ_BLOCK = 64
STORAGE_VERSION = 1
