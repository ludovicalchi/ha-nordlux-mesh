"""Lecture (seule) de la configuration mesh depuis le compte Nordlux Smart, via l'API cloud Yankon.

Utilisé une seule fois, par l'assistant de configuration : connexion, liste des maisons, puis
clés du réseau, ampoules et proxys de chaque maison. Rien n'est modifié côté cloud, et ni le mot
de passe ni le jeton de session ne sont conservés.

Les clés applicatives sont celles codées en dur dans l'app Android Nordlux v2.6.2
(wifi/ykutils/SecurityUtil.java) : identiques pour tous les utilisateurs, elles ne donnent accès à
aucun compte sans l'e-mail et le mot de passe.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

HOST = "https://api.yankon-xm.com/"
ACCESS_KEY_ID = "d780d5f3c706851ad386e7d7acdaf6f7"
SECRET_KEY = "df1acb570370615d"
APP = {"appCode": "nordlux", "version": "v2.0.0", "appVersion": "v2.6.2", "buildVersion": 137,
       "mobileSystemType": "android", "mobileSystemVersion": "14",
       "mobileBrand": "generic", "mobileModel": "generic"}
UA = "okhttp/4.9.0"
TIMEOUT = aiohttp.ClientTimeout(total=30)

# thingType du catalogue de l'app (assets/device/device_type.json)
THING_LIGHT = 1
THING_BRIDGE = 2


class CloudError(Exception):
    """Erreur de communication avec le cloud ou réponse inattendue."""


class CloudAuthError(CloudError):
    """Identifiants refusés par le cloud."""


@dataclass
class CloudLight:
    name: str
    unicast: int
    mac: str  # AA:BB:CC:DD:EE:FF, ou "" si inconnue
    proxy: bool
    beacon: str = ""   # type complet, ex. nx00626222
    version: str = ""  # firmware connu du cloud, ex. 27.3
    room: str = ""     # nom de la pièce dans l'app

    def meta(self) -> dict[str, str]:
        """Informations conservées par l'intégration (origine : compte Nordlux)."""
        return {"source": "cloud", "mac": self.mac, "beacon": self.beacon, "version": self.version,
                "room": self.room}


@dataclass
class CloudHouse:
    house_id: str
    name: str
    netkey: str
    appkey: str
    iv_index: int
    lights: list[CloudLight] = field(default_factory=list)
    proxies: list[str] = field(default_factory=list)  # bridges d'abord, puis ampoules proxy

    def meta(self) -> dict[str, dict[str, str]]:
        """Informations par ampoule, indexées par adresse « 0xNNNN »."""
        return {f"0x{light.unicast:04X}": light.meta() for light in self.lights}

    def devices_text(self) -> str:
        """Format attendu par l'intégration : une ligne « Nom = 0xNNNN » par ampoule."""
        used: dict[str, int] = {}
        lines = []
        for light in self.lights:
            base = (light.name or "Ampoule").replace("\n", " ").replace("=", "-").strip() or "Ampoule"
            used[base] = used.get(base, 0) + 1
            name = base if used[base] == 1 else f"{base} {used[base]}"
            lines.append(f"{name} = 0x{light.unicast:04X}")
        return "\n".join(lines)


def _fmt_mac(raw: str | None) -> str:
    h = (raw or "").replace(":", "").replace("-", "").strip().upper()
    if len(h) != 12 or any(c not in "0123456789ABCDEF" for c in h):
        return ""
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))


class NordluxCloud:
    def __init__(self, session: aiohttp.ClientSession,
                 clock: Callable[[], datetime.datetime] = datetime.datetime.now) -> None:
        self._session = session
        self._akid = ACCESS_KEY_ID
        self._sk = SECRET_KEY.encode()
        # L'app signe avec l'heure locale du téléphone : Home Assistant passe son propre fuseau.
        self._clock = clock
        self._auth: dict[str, Any] | None = None
        self._last_house: str | None = None

    # --- chiffrement « cipher » : AES-128-ECB / PKCS7, hexadécimal majuscule
    def _enc(self, plain: str) -> str:
        p = padding.PKCS7(128).padder()
        data = p.update(plain.encode()) + p.finalize()
        e = Cipher(algorithms.AES(self._sk), modes.ECB()).encryptor()
        return (e.update(data) + e.finalize()).hex().upper()

    def _dec(self, hexstr: str) -> str:
        d = Cipher(algorithms.AES(self._sk), modes.ECB()).decryptor()
        data = d.update(bytes.fromhex(hexstr)) + d.finalize()
        u = padding.PKCS7(128).unpadder()
        return (u.update(data) + u.finalize()).decode()

    def _headers(self) -> dict[str, str]:
        t = self._clock().strftime("%Y%m%d%H%M%S")
        return {
            "authorization": base64.b64encode(f"{self._akid}:{t}".encode()).decode(),
            "sign": hashlib.md5(f"{self._akid}{self._sk.decode()}{t}".encode()).hexdigest().upper(),
            "User-Agent": UA,
        }

    async def _post(self, path: str, payload: dict[str, Any], form: bool = False) -> dict[str, Any]:
        body = {"cipher": self._enc(json.dumps(payload))}
        kwargs: dict[str, Any] = {"data": body} if form else {"json": body}
        try:
            async with self._session.post(HOST + path, headers=self._headers(), timeout=TIMEOUT, **kwargs) as r:
                text = await r.text()
                status = r.status
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CloudError(f"cloud injoignable : {err}") from err
        try:
            j = json.loads(text)
            if isinstance(j, dict) and isinstance(j.get("cipher"), str):
                j = json.loads(self._dec(j["cipher"]))
        except ValueError as err:
            raise CloudError(f"réponse illisible (HTTP {status})") from err
        if not isinstance(j, dict):
            raise CloudError(f"réponse inattendue (HTTP {status})")
        return j

    async def login(self, email: str, password: str) -> None:
        uniq = uuid.uuid4().hex
        j = await self._post("smartLight/api/account/emailLogin",
                             dict(APP, email=email, password=password, pushToken="123456",
                                  languages="en", uniqueIndication=uniq), form=True)
        d = j.get("data") or {}
        if not j.get("isSuccess") or not d.get("token"):
            raise CloudAuthError(str(j.get("message") or j.get("msg") or j.get("code") or "connexion refusée"))
        self._auth = dict(APP, token=d["token"], accountId=d.get("accountId"), uniqueIndication=uniq)
        self._last_house = d.get("lastHouse")

    async def ota_info(self, beacons: list[str]) -> dict[str, dict[str, str]]:
        """Dernier firmware publié par type (getOTAInfo) : version, url (signée, valable ~100 h), md5,
        nom de fichier, description. Nécessite login()."""
        if self._auth is None:
            raise CloudError("non connecté")
        j = await self._post("smartLight/api/ota/getOTAInfo", dict(
            self._auth, languageCode="en", deviceList=[{"deviceTypeBeacon": b} for b in beacons]))
        if not j.get("isSuccess"):
            raise CloudError(str(j.get("msg") or j.get("message") or "getOTAInfo refusé"))
        out: dict[str, dict[str, str]] = {}
        for it in (j.get("data") or {}).get("deviceList") or []:
            if it.get("deviceTypeBeacon") and it.get("lastVersion"):
                out[it["deviceTypeBeacon"]] = {
                    "version": str(it["lastVersion"]), "url": it.get("lastVersionURL") or "",
                    "md5": (it.get("lastVersionMD5") or "").lower(),
                    "filename": it.get("lastVersionFileName") or "",
                    "description": it.get("lastVersionDescription") or "",
                }
        return out

    async def download(self, url: str) -> bytes:
        try:
            async with self._session.get(url, headers={"User-Agent": UA},
                                         timeout=aiohttp.ClientTimeout(total=120)) as r:
                r.raise_for_status()
                return await r.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CloudError(f"téléchargement impossible : {err}") from err

    async def houses(self) -> list[CloudHouse]:
        if self._auth is None:
            raise CloudError("non connecté")
        # Lecture seule : syncHouse sans houseList ni syncTime renvoie les maisons sans rien modifier.
        ids: list[str] = []
        j = await self._post("smartLight/api/sync/syncHouse", dict(self._auth))
        if j.get("isSuccess"):
            for h in (j.get("data") or {}).get("houseList") or []:
                if h.get("houseId") and h["houseId"] not in ids:
                    ids.append(h["houseId"])
        if self._last_house and self._last_house not in ids:
            ids.append(self._last_house)
        out = []
        for hid in ids:
            j = await self._post("smartLight/api/house/getHouseInfo", dict(self._auth, houseId=hid))
            if j.get("isSuccess") and j.get("data"):
                house = parse_house(j["data"])
                if house is not None:
                    out.append(house)
        return out


def parse_house(h: dict[str, Any]) -> CloudHouse | None:
    """Réponse getHouseInfo -> clés, ampoules (unicast depuis meshJson) et proxys."""
    netkey, appkey = (h.get("netKey") or "").lower(), (h.get("appKeyValue") or "").lower()
    if len(netkey) != 32 or len(appkey) != 32:
        return None
    try:
        nodes = json.loads(h.get("meshJson") or "{}").get("nodes") or []
    except (ValueError, AttributeError):
        nodes = []
    by_name = {(n.get("name") or "").lower(): n for n in nodes}
    rooms = {r.get("roomId"): r.get("roomName") or "" for r in h.get("roomList") or []}

    house = CloudHouse(str(h.get("houseId")), h.get("houseName") or "Nordlux", netkey, appkey,
                       int(h.get("ivIndex") or 0))
    bridges: list[str] = []
    light_proxies: list[str] = []
    for dv in h.get("deviceList") or []:
        # Nœud mesh = MAC BLE en minuscules (le bridge a aussi une MAC Wi-Fi dans deviceMac).
        ble = (dv.get("bleMacAddress") or dv.get("deviceMac") or "").replace(":", "").lower()
        node: dict[str, Any] = {}
        try:
            node = json.loads(dv.get("nodeJson") or "{}") or {}
        except ValueError:
            pass
        if not node.get("unicastAddress"):
            node = by_name.get(ble, {})
        mac = _fmt_mac(ble)
        is_proxy = (node.get("features") or {}).get("proxy") == 1
        thing = int(dv.get("thingType") or 0)
        if thing == THING_BRIDGE:
            if mac:
                bridges.append(mac)
            continue
        if thing != THING_LIGHT:
            continue
        try:
            unicast = int(str(node.get("unicastAddress")), 16)
        except ValueError:
            continue
        if not 0x0001 <= unicast <= 0x7FFF:
            continue
        house.lights.append(CloudLight(dv.get("deviceName") or "", unicast, mac, is_proxy,
                                       beacon=str(dv.get("firmwareTypeBeacon") or ""),
                                       version=str(dv.get("deviceVer") or ""),
                                       room=rooms.get(dv.get("roomId"), "")))
        if is_proxy and mac:
            light_proxies.append(mac)
    house.proxies = list(dict.fromkeys(bridges + light_proxies))
    return house
