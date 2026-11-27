"""Ajout d'une ampoule Nordlux réinitialisée au réseau, sans l'app (sans dépendance à Home Assistant).

Après réinitialisation, une ampoule PHY6222 rejoint seule un réseau d'usine ; ses données fabricant (0x6E78)
indiquent alors « clés non configurées ». L'app Nordlux (InformationConfigOfDevices2 / ConfigNodeRequest)
lui écrit en clair, sur un service GATT propriétaire, 6 messages de 18 octets : NetKey, AppKey, nouvelle
adresse unicast, n° de pièce et adresse de publication. L'ampoule se configure ensuite seule.
Validé le 26/11/2026 sur une Candle DIM (tools/mesh_provision.py nx-add).
"""
from __future__ import annotations

NX_COMPANY = 0x6E78
NX_SERVICE = "4d124bf0-fee0-44bd-947f-e668f1228a9d"
NX_WRITE = "da3c026f-4f76-48e5-ab29-be11daa3604c"
NX_NOTIFY = "3db871af-558c-40ea-bb4f-f9991217a72f"
HOUSE_GROUP = 0xC000
ADD_RANGE = range(0x7E00, 0x7F00)  # adresses attribuées hors de l'app


# Catalogue des modèles PHY6222 (plateforme 2) de l'app : code annoncé (octets 0–3 des données fabricant)
# -> (nom, type complet, température min, max en K ; 1–100 = teinte en %).
PHY_MODELS: dict[str, tuple[str, str, int, int]] = {
    "0056": ("E27 SMD", "nx00566222", 2200, 6500),
    "0057": ("E27 DIM", "nx00576222", 2700, 2700),
    "0058": ("GU10 Plastic", "nx00586222", 2200, 6500),
    "0059": ("GU10 DIM", "nx00596222", 2700, 2700),
    "0060": ("GU10 Glass", "nx00606222", 2200, 6500),
    "0061": ("E14 Candle", "nx00616222", 2200, 6500),
    "0062": ("Candle DIM", "nx00626222", 2700, 2700),
    "0063": ("E14 Globe", "nx00636222", 2200, 6500),
    "0064": ("LANDON", "nx00646222", 2700, 6500),
    "0065": ("E27 A60 Filament", "nx00656222", 2200, 6500),
    "0066": ("E27 Globe Filament", "nx00666222", 2200, 6500),
    "0067": ("E27 Giant 95", "nx00676222", 2200, 6500),
    "0068": ("E27 Decorative", "nx00686222", 2700, 2700),
    "0069": ("Spot", "nx00696222", 2200, 6500),
    "0070": ("A60 Colour", "nx00706222", 2200, 6500),
    "0071": ("E27 Globe 95 Colour", "nx00716222", 2200, 6500),
    "0072": ("GU10 Colour", "nx00726222", 2200, 6500),
    "0073": ("DON Downlight Colour", "nx00736222", 2200, 6500),
    "0074": ("STRIP", "nx00746222", 2700, 2700),
    "0075": ("OJA", "nx00756222", 2200, 6500),
}


def model_info(code_or_beacon: str | None) -> tuple[str, str, int, int] | None:
    """Modèle d'après le code annoncé (« 0062 ») ou le type complet du cloud (« nx00626222 »)."""
    if not code_or_beacon:
        return None
    c = code_or_beacon.lower()
    if c.startswith("nx") and len(c) >= 6:
        c = c[2:6]
    return PHY_MODELS.get(c)


def parse_nx_mfr(d: bytes | None) -> dict | None:
    """Données fabricant 0x6E78 (24 o) : type, version, état des clés, adresse actuelle ; None si invalide."""
    if d is None or len(d) < 24 or (sum(b if b < 128 else b - 256 for b in d[:23]) & 0xFF) != d[23]:
        return None
    return {
        "beacon": d[0:4][::-1].decode("ascii", "replace"),
        "version": f"{d[6]}.{d[19]}",
        "configured": d[7:9] != b"\x00\x00" and d[9:11] != b"\x00\x00",
        "addr_raw": bytes(d[17:19]),
        "addr": int.from_bytes(d[17:19], "little"),
    }


def nx_config_messages(cur_addr_raw: bytes, netkey: bytes, appkey: bytes, new_addr: int,
                       room_num: int = 1, pub_addr: int = HOUSE_GROUP) -> list[bytes]:
    """Les 6 écritures de l'app (opcode 0x21), à envoyer dans l'ordre sur NX_WRITE."""
    head = b"CMD\x01\x21\x00\x00" + cur_addr_raw + b"\x08"
    return [head + body for body in (
        b"\x01" + netkey[0:7],
        b"\x02" + netkey[7:14],
        b"\x03\x64" + netkey[14:16] + new_addr.to_bytes(2, "little") + bytes([room_num & 0xFF, 0x64]),
        b"\x04" + appkey[0:7],
        b"\x05" + appkey[7:14],
        b"\x06\x00" + appkey[14:16] + pub_addr.to_bytes(2, "little") + b"\x00\x00",
    )]


def next_free_address(used: set[int]) -> int:
    for a in ADD_RANGE:
        if a not in used:
            return a
    raise ValueError("plus d'adresse libre dans 0x7E00–0x7EFF")
