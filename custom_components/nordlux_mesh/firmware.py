"""Mise à jour du firmware des ampoules PHY62xx (Nordlux) par OTA Bluetooth, sans dépendance à Home Assistant.

Repris de tools/phy_ota.py (ampoule récupérée le 21/11/2026, reflashée le 28/11/2026) et du SDK de l'app
(com/phy/ota/sdk) :
  - passage au bootloader : écriture SANS réponse de « 01 02 » sur ff02 (service 5833ff01, présent en
    fonctionnement normal sans ff04) ; le bootloader s'annonce à la MAC + 1 (octet de poids faible) ;
  - ff02 (écriture) : 01 <nb_partitions> 00 -> 00 81 ; 02 <idx> <flash LE32> <run LE32> <len LE32> <crc16> -> 00 84 ;
  - ff04 (sans réponse) : paquets de 20 octets ; bloc = 16*(MTU-3) octets -> 00 87 ;
    fin de partition 00 85 ; fin d'OTA 00 83 ; 68 87 = bloc à renvoyer ;
  - ff02 : 04 01 (ou 04) = redémarrage -> 00 8a.
Les paquets de 237 ou 197 octets bloquent le bootloader : toujours 20 octets.
Après mise à jour, l'ampoule garde ses clés et son adresse mesh (vérifié le 28/11/2026).
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

OTA_SERVICE = "5833ff01-9b8b-5191-6142-22a4536ef123"
OTA_CMD = "5833ff02-9b8b-5191-6142-22a4536ef123"
OTA_NOTIFY = "5833ff03-9b8b-5191-6142-22a4536ef123"
OTA_DATA = "5833ff04-9b8b-5191-6142-22a4536ef123"
ENTER_BOOTLOADER = bytes([0x01, 0x02])

FLASH_LO, FLASH_HI = 0x11000000, 0x1107FFFF
PKTS_PER_BLOCK = 16
CHUNK = 20
R_START, R_PART, R_BLOCK, R_PART_DONE, R_OTA_DONE, R_REBOOT, R_BLOCK_ERR = (
    b"\x00\x81", b"\x00\x84", b"\x00\x87", b"\x00\x85", b"\x00\x83", b"\x00\x8a", b"\x68\x87")


class OTAError(Exception):
    pass


@dataclass
class Partition:
    addr: int
    data: bytes

    @property
    def in_flash(self) -> bool:
        return FLASH_LO <= self.addr <= FLASH_HI


def ota_mac(mac: str) -> str:
    """Adresse du bootloader : MAC + 1 sur l'octet de poids faible (BleUtils.getOTAMac)."""
    return mac[:15] + f"{(int(mac[15:], 16) + 1) & 0xFF:02X}"


def crc16(data: bytes) -> int:
    c = 0
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return c


def parse_hex(text: str) -> list[Partition]:
    """Intel HEX -> partitions (une par enregistrement 04, comme FirmWareFile.analyzeFile)."""
    parts: list[Partition] = []
    buf, addr_str, have = bytearray(), "", False
    for ln, line in enumerate(text.splitlines(), 1):
        t = line.strip()
        if not t:
            continue
        if not t.startswith(":"):
            raise OTAError(f"ligne {ln} : pas un enregistrement Intel HEX")
        raw = bytes.fromhex(t[1:])
        if sum(raw) & 0xFF:
            raise OTAError(f"ligne {ln} : somme de contrôle Intel HEX invalide")
        n, rtype = raw[0], t[7:9]
        if rtype == "04":
            if buf:
                parts.append(Partition(int(addr_str, 16), bytes(buf)))
            addr_str, buf, have = t[9:13], bytearray(), False
        elif rtype in ("05", "01"):
            break
        else:
            if not have:
                addr_str += t[3:7]
                have = True
            buf += raw[4:4 + n]
    if buf:
        parts.append(Partition(int(addr_str, 16), bytes(buf)))
    if not parts:
        raise OTAError("image vide")
    return parts


def plan(parts: list[Partition]) -> list[tuple[int, bytes]]:
    """(adresse flash, commande 02) par partition, comme BleCallBack/BleUtils (format .hex/.hex16)."""
    out, flash_acc = [], 0
    for i, p in enumerate(parts):
        fa = p.addr if p.in_flash else flash_acc
        cmd = (bytes([0x02, i]) + fa.to_bytes(4, "little") + p.addr.to_bytes(4, "little")
               + len(p.data).to_bytes(4, "little") + crc16(p.data).to_bytes(2, "little"))
        out.append((fa, cmd))
        if not p.in_flash:
            flash_acc += len(p.data) + 8
    return out


async def run_ota(client, parts: list[Partition], progress: Callable[[float], None],
                  log: Callable[[str], None], timeout: float = 10, part_timeout: float = 25,
                  retries: int = 3, delay: float = 0.01, max_delay: float | None = None) -> float:
    """Flashe `parts` sur un client bleak (ou équivalent) connecté au bootloader. Renvoie la durée (s).

    `max_delay` : rythme adaptatif. Après chaque bloc rejeté, l'écart entre paquets double, jusqu'à cette
    limite. Via un proxy ESPHome, les écritures sans réponse n'ont aucun contrôle de flux : trop rapides, la
    file Bluetooth de l'ESP32 déborde (« status=143 » congestion, « write_char failed ») et des paquets sont
    perdus, d'où un bloc incomplet (68 87). Constaté le 28/11/2026 : 10 à 20 pertes par seconde à ~30 ms."""
    svc = client.services.get_service(OTA_SERVICE)
    if svc is None or any(svc.get_characteristic(u) is None for u in (OTA_CMD, OTA_NOTIFY, OTA_DATA)):
        raise OTAError("service OTA du bootloader introuvable")
    mtu = getattr(client, "mtu_size", 23) or 23
    chunk = min(CHUNK, mtu - 3)
    quick = mtu > 23
    # Bloc acquitté (00 87) tous les 16*(MTU-3) octets, quelle que soit la taille des paquets : validé depuis
    # le PC (21 et 28/11/2026). Ne pas « sonder » avec un bloc plus court : le bootloader signale un bloc
    # incomplet (68 87) après ~1 s de silence, puis rejette tous les blocs suivants (constaté via l'ESP32).
    block = PKTS_PER_BLOCK * (mtu - 3)
    queue: asyncio.Queue[bytes] = asyncio.Queue()
    await client.start_notify(OTA_NOTIFY, lambda _h, d: queue.put_nowait(bytes(d)))
    await asyncio.sleep(0.3)
    log(f"bootloader : MTU {mtu}, paquets de {chunk} o, blocs de {block} o, {delay * 1000:.0f} ms entre paquets")

    async def notif(wait: float, optional: bool = False) -> bytes | None:
        try:
            return await asyncio.wait_for(queue.get(), wait)
        except asyncio.TimeoutError:
            if optional:
                return None
            if not client.is_connected:
                raise OTAError("déconnecté par l'ampoule") from None
            raise OTAError(f"pas de réponse du bootloader en {wait:.0f} s") from None

    async def expect(want: bytes, wait: float) -> None:
        v = await notif(wait)
        if v != want:
            raise OTAError(f"réponse inattendue {v.hex(' ')} (attendu {want.hex(' ')})")

    pace = delay

    async def send_range(data: bytes, a: int, b: int) -> None:
        for k in range(a, b, chunk):
            await client.write_gatt_char(OTA_DATA, data[k:min(k + chunk, b)], response=False)
            if pace:
                await asyncio.sleep(pace)

    total = sum(len(p.data) for p in parts)
    sent, t0 = 0, time.monotonic()
    await client.write_gatt_char(OTA_CMD, bytes([0x01, len(parts), 0x00]), response=True)
    await expect(R_START, timeout)
    ota_done = False
    for i, (p, (_fa, pcmd)) in enumerate(zip(parts, plan(parts))):
        await client.write_gatt_char(OTA_CMD, pcmd, response=True)
        await expect(R_PART, part_timeout)
        data, length = p.data, len(p.data)
        off, err, part_done = 0, 0, False
        while off < length:
            end = min(off + block, length)
            await send_range(data, off, end)
            v = await notif(timeout)
            last = end == length
            if v == R_BLOCK:
                sent += end - off
                off, err = end, 0
            elif v == R_BLOCK_ERR:
                err += 1
                if max_delay is not None and pace < max_delay:
                    pace = min(max_delay, max(pace * 2, 0.02))
                log(f"bloc à l'offset {off} rejeté, renvoi {err}/{retries}"
                    + (f", {pace * 1000:.0f} ms entre paquets" if max_delay is not None else ""))
                if err > retries:
                    raise OTAError("trop d'erreurs sur un bloc")
                if max_delay is not None:
                    await asyncio.sleep(0.5)  # laisse la file du proxy se vider avant de renvoyer le bloc
            elif v == R_PART_DONE and last:
                sent += end - off
                off, part_done = end, True
            elif v == R_OTA_DONE and last and i == len(parts) - 1:
                sent += end - off
                off, part_done, ota_done = end, True, True
            else:
                raise OTAError(f"réponse inattendue {v.hex(' ')} à l'offset {off}/{length}")
            progress(100 * sent / total)
        if not part_done:
            await expect(R_PART_DONE, part_timeout)
    if not ota_done:
        v = await notif(part_timeout)
        if v != R_OTA_DONE:
            raise OTAError(f"fin de mise à jour non confirmée ({v.hex(' ')})")
    duration = time.monotonic() - t0
    log(f"image envoyée en {duration:.0f} s, redémarrage")
    try:
        await client.write_gatt_char(OTA_CMD, b"\x04\x01" if quick else b"\x04", response=quick)
        await notif(10, optional=True)
    except Exception:  # noqa: BLE001 - l'ampoule redémarre, la déconnexion est normale
        pass
    return duration
