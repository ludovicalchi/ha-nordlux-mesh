"""Constantes de l'intégration Nordlux Smart (Bluetooth Mesh local)."""
DOMAIN = "nordlux_mesh"

CONF_NETKEY = "netkey"
CONF_APPKEY = "appkey"
CONF_IV_INDEX = "iv_index"
CONF_SRC = "source_address"
CONF_PROXIES = "proxies"
CONF_DEVICES = "devices"
# Informations par ampoule, indexées par « 0xNNNN » : source (cloud / ha), mac, beacon, version, pièce.
CONF_META = "meta"
# Assistant « compte Nordlux » : choix de la maison (l'e-mail et le mot de passe ne sont jamais enregistrés).
CONF_HOUSE = "house"

# Adresse source propre à Home Assistant (le script PC utilise 0x7F01) : chaque émetteur
# doit avoir sa propre adresse, car l'anti-rejeu des nœuds suit le SEQ par adresse source.
DEFAULT_SRC = 0x7F10
# Déconnexion du proxy après N s d'inactivité (les proxys ESPHome ont peu de connexions).
IDLE_DISCONNECT_S = 30
# Le SEQ est réservé par blocs : on enregistre la borne haute avant de l'atteindre, et on
# repart de cette borne au démarrage, pour ne jamais réutiliser un SEQ après un redémarrage.
SEQ_BLOCK = 64
# Lecture de l'état réel des ampoules (Light CTL Get) : intervalle, et délai après le démarrage
# (laisse le temps aux proxys Bluetooth d'être découverts). Chaque lecture ouvre une connexion au proxy.
STATE_POLL_S = 120
FIRST_POLL_DELAY_S = 20
# Alerte (Réparations) après N lectures consécutives sans réponse (ampoule) ou en échec (aucun proxy).
UNRESPONSIVE_AFTER = 3
# Firmware : vérification auprès du compte Nordlux (identifiants enregistrés dans l'entrée).
FIRMWARE_CHECK_HOURS = 24
FIRST_FIRMWARE_CHECK_S = 60
STORAGE_VERSION = 1
