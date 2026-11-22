# ha-nordlux-mesh

Intégration Home Assistant pour piloter **localement**, sans le cloud Nordlux, les ampoules **Nordlux Smart Light** en Bluetooth Mesh (puce Phyplus PHY6222).

> **Statut : 0.1.0.** Testée le 22/11/2026 dans Home Assistant 2026.11 (Container), via un ESP32 proxy Bluetooth ESPHome, sur une Candle DIM (`nx00626222`, firmware 27.3) : **extinction, rallumage et luminosité validés visuellement**. Les commandes reprennent celles du script de référence du dépôt [nordlux-phy-ota](https://github.com/ludovicalchi/nordlux-phy-ota). Un seul modèle d'ampoule a été testé à ce jour.

## Fonctionnalités

- Une entité `light` par ampoule : marche/arrêt et luminosité (avec transition).
- Connexion au réseau mesh via le Bluetooth de Home Assistant : adaptateur local ou **proxy Bluetooth ESPHome** en mode actif.
- Aucune dépendance au cloud une fois les clés du réseau connues.

**Non géré pour l'instant** : la température de couleur (le format est connu mais n'a pas été validé sur un modèle à blanc réglable), les groupes et pièces Nordlux, et la lecture de l'état réel.

## Prérequis

1. Home Assistant 2025.2 ou plus récent.
2. **Un accès Bluetooth LE pour Home Assistant, à portée de l'ampoule ou du bridge Nordlux** :
   - soit un adaptateur Bluetooth local, reconnu par l'intégration Bluetooth ;
   - soit, le plus simple, un **ESP32 sous ESPHome configuré en proxy Bluetooth actif** :

     ```yaml
     esphome:
       name: proxy-ble-nordlux

     esp32:
       board: esp32dev
       framework:
         type: esp-idf

     wifi:
       ssid: !secret wifi_ssid
       password: !secret wifi_password

     api:
       encryption:
         key: !secret api_key

     ota:
       - platform: esphome

     logger:

     esp32_ble_tracker:
       scan_parameters:
         active: true

     bluetooth_proxy:
       active: true   # indispensable : l'intégration ouvre une connexion GATT
     ```
3. **Les clés du réseau mesh et les adresses des ampoules.** Les outils du dépôt [nordlux-phy-ota](https://github.com/ludovicalchi/nordlux-phy-ota) les récupèrent depuis votre propre compte Nordlux :
   - `tools/nordlux_mesh_keys.py` donne la NetKey, l'AppKey, l'IV index et l'adresse unicast de chaque ampoule ;
   - `tools/mesh_find_node.py` retrouve l'adresse d'une ampoule par radio, par exemple si elle a été supprimée de l'app sans avoir été réinitialisée.

## Installation via HACS

1. Dans HACS, ouvrez le menu ⋮, puis **Dépôts personnalisés**.
2. Ajoutez `https://github.com/ludovicalchi/ha-nordlux-mesh` avec la catégorie **Intégration**.
3. Installez **Nordlux Smart (Bluetooth Mesh local)**, puis redémarrez Home Assistant.

L'installation manuelle reste possible : copiez `custom_components/nordlux_mesh/` dans le dossier `/config/custom_components/` de Home Assistant.

## Configuration

**Paramètres → Appareils et services → Ajouter une intégration → Nordlux Smart (Bluetooth Mesh local)** :

| Champ | Exemple | Remarque |
|---|---|---|
| NetKey / AppKey | 32 caractères hexadécimaux | issues de `nordlux_mesh_keys.py` |
| IV index | `0` | |
| Adresse source | `0x7F10` | adresse unicast **libre**, propre à Home Assistant, différente de celle de l'app (0x2001…) et de tout autre outil |
| Proxys | `AA:BB:CC:DD:EE:FF, 11:22:33:44:55:66` | adresses BLE des nœuds proxy, essayées dans l'ordre. Le bridge Nordlux, en premier, s'est montré le plus fiable. |
| Ampoules | `Salon = 0x2005` | une ligne par ampoule : nom = adresse unicast |

Les proxys, les ampoules et l'IV index restent modifiables ensuite dans les **options** de l'intégration.

## Fonctionnement

- L'app Nordlux ne pilote pas la lumière avec les commandes mesh standard : Generic OnOff et Light Lightness sont acquittées par l'ampoule, mais sans effet. Elle **détourne Light CTL Set**, et l'intégration envoie exactement les mêmes messages :

  | Action | lightness | température | transition |
  |---|---|---|---|
  | Allumer | 0 | 10000 | 0 |
  | Éteindre | 0 | 15000 | 0 |
  | Luminosité | pourcentage 0–100 | 800 + teinte en % (900 par défaut) | 400 ms |

- **L'état est supposé** (`assumed_state`) : les réponses de l'ampoule ne reflètent pas sa lumière réelle. Home Assistant mémorise donc le dernier état envoyé. Un changement fait depuis l'app Nordlux n'est pas vu par Home Assistant, et inversement.
- **Anti-rejeu** : chaque message mesh porte un numéro de séquence qui ne doit jamais être réutilisé pour une même adresse source. L'intégration le réserve par blocs et l'enregistre dans le stockage de Home Assistant. Le prochain numéro reprend toujours au-delà du dernier bloc réservé, y compris après un redémarrage. N'utilisez pas la même adresse source dans un autre outil.
- La connexion au proxy est ouverte à la demande et fermée après 30 s d'inactivité, pour ne pas occuper durablement les connexions limitées d'un proxy ESPHome.
- Le bridge Nordlux peut rester allumé : il n'annule pas les commandes locales.

## Avertissement

Projet personnel, non affilié à Nordlux ni à Yankon. À utiliser à vos risques.
