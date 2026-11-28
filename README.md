# ha-nordlux-mesh

Intégration Home Assistant pour piloter **localement**, sans le cloud Nordlux, les ampoules **Nordlux Smart Light** en Bluetooth Mesh (puce Phyplus PHY6222).

> **Statut : 0.10.6** : rythme d'envoi adaptatif pour le flashage via un proxy ESPHome. Les écritures sans réponse n'y ont aucun contrôle de flux : envoyés trop vite, environ un paquet tous les 30 ms, 10 à 20 paquets par seconde sont perdus. Le bloc arrive alors incomplet et le bootloader le rejette. L'envoi démarre à 50 ms entre paquets, et cet écart double après chaque bloc rejeté, jusqu'à 250 ms. Comptez de 6 à 20 minutes. Pas encore testée.
>
> **0.10.5, réparation vérifiée** (réparation « bloquée en mode mise à jour » et bouton « Installer » affichés le 28/11/2026 ; le flashage échouait encore à cause des pertes de paquets) : flashage par blocs de 16×(MTU−3) octets dès le départ, comme depuis le PC. L'essai d'un bloc plus court faisait rejeter tous les blocs suivants par le bootloader. L'installation tourne en tâche de fond : fermer le navigateur ne la coupe plus.
> - **Ampoule bloquée en mode mise à jour** (mise à jour interrompue, ampoule éteinte) : elle est détectée à chaque lecture, grâce à l'annonce du bootloader. Une réparation « … est bloquée en mode mise à jour » apparaît dans Paramètres → Réparations. L'entité « Firmware » affiche « mode mise à jour » avec le bouton « Installer » : un clic reflashe le dernier firmware publié. Sans identifiants Nordlux enregistrés, la réparation indique d'abord comment les saisir.
>
> **0.10.4, vérifiée** : une ampoule restée en mode mise à jour, et donc sans annonce mesh, peut être réparée depuis Home Assistant, même après un redémarrage. Son adresse Bluetooth, son modèle et son firmware sont repris de la fiche de l'appareil, et l'entité « Firmware » reste toujours disponible.
>
> **0.10.3** : correction de l'installation du firmware via un proxy ESPHome. Le premier essai, le 28/11/2026, a échoué sur la détection de la taille des blocs. Le bootloader attend des blocs de 16×(MTU−3) octets. Reprise automatique (3 essais) en cas d'erreur Bluetooth, et paquets espacés de 20 ms. Pas encore testée dans Home Assistant.
>
> **0.10.2, démarrage vérifié** : si le cloud refuse les identifiants enregistrés (mot de passe changé dans l'app, par exemple), Home Assistant affiche « Nouvelle authentification requise ». Ce cas n'a pas encore été testé. Correction d'un appel déprécié du registre des appareils.
>
> **0.10.1** : mise à jour du firmware depuis Home Assistant. Vérification quotidienne auprès du compte Nordlux, et installation par le proxy Bluetooth.
> - **Vérification testée** le 28/11/2026 : l'entité « Firmware » affiche « À jour » (27.3, « Bug fixes and improvements. »).
> - Passage en mode mise à jour et flashage validés le 28/11/2026 depuis un PC sur une Candle DIM : 194 s, clés et adresse conservées.
> - L'installation depuis Home Assistant, à travers un proxy ESPHome, n'a pas encore été testée.
>
> **0.9.0, testée** : une ampoule injoignable devient « Indisponible », au lieu de l'alerte « ne répond plus » ; l'alerte « Aucun proxy Bluetooth disponible » est conservée.
>
> **0.8.2** : alertes testées dans Home Assistant (ampoule coupée à l'interrupteur, perte du proxy).
>
> **0.8.1, testée** le 27/11/2026 dans Home Assistant, sur une Candle DIM :
> - **Fiche de l'appareil :** modèle Candle DIM (`nx00626222`), firmware 27.3 et adresse Bluetooth.
> - **Capteurs de diagnostic :**
>   - Signal Bluetooth, en dBm, avec le proxy qui reçoit l'ampoule ;
>   - Dernière réponse ;
>   - Origine, avec le modèle et la plage de température de couleur, complétés depuis les annonces de l'ampoule.
>
>
> **0.7.1, testée** le 27/11/2026 : lecture de l'état toutes les 2 minutes, au lieu de 5.
> - Un écart entre l'affichage de Home Assistant (20 %) et l'ampoule (44 %) a été corrigé automatiquement à la lecture suivante.
> - La lecture suit aussi les réglages faits dans l'app Nordlux : 30 % réglé dans l'app, 29 lu.
>
> **0.7.0, testée** le 27/11/2026 : lecture de l'état réel des ampoules (Light CTL Get), toutes les 5 minutes.
> - Une extinction envoyée hors de Home Assistant, depuis un PC, a été affichée à la lecture suivante.
> - La lecture avait aussi été vérifiée depuis un PC : éteinte, allumée, et après une coupure à l'interrupteur.
>
> **0.6.0, testée** du 22 au 26/11/2026, dans Home Assistant 2026.11 (Container), via un ESP32 proxy Bluetooth ESPHome, sur une Candle DIM (`nx00626222`, firmware 27.3). Vérifié visuellement dans Home Assistant :
> - extinction, rallumage et luminosité, avec l'interrupteur standard ;
> - ajout d'une ampoule réinitialisée sans l'app ;
> - suppression par le bouton de l'appareil : l'ampoule clignote, quitte le réseau, et son appareil disparaît ;
> - nettoyage des appareils orphelins ;
> - mise à jour de la liste depuis le compte Nordlux (compte à une seule maison).
>
> Les commandes reprennent celles du script de référence du dépôt [nordlux-phy-ota](https://github.com/ludovicalchi/nordlux-phy-ota).
>
> **Non testé à ce jour :**
> - les autres modèles d'ampoule ;
> - la température de couleur ;
> - l'ajout initial depuis le compte, déroulé de bout en bout ;
> - la suppression quand aucun proxy n'est joignable ;
> - un compte à plusieurs maisons.

## Fonctionnalités

- Une entité `light` par ampoule : marche/arrêt et luminosité (avec transition), avec relecture périodique de l'état réel.
- Connexion au réseau mesh via le Bluetooth de Home Assistant : adaptateur local ou **proxy Bluetooth ESPHome** en mode actif.
- Configuration automatique depuis le compte Nordlux (clés, ampoules et proxys), ou saisie manuelle.
- Ajout d'une ampoule réinitialisée directement depuis Home Assistant, sans l'app Nordlux, et suppression avec réinitialisation à distance.
- Aucune dépendance au cloud une fois les clés du réseau connues.

**Non géré pour l'instant** : la température de couleur (le format est connu mais n'a pas été validé sur un modèle à blanc réglable), et les groupes et pièces Nordlux.

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
       cpu_frequency: 80MHZ     # économie : minimum de l'ESP32 avec Wi-Fi/Bluetooth (240 MHz par défaut)

     wifi:
       ssid: !secret wifi_ssid
       password: !secret wifi_password
       output_power: 13dB       # économie : seulement si le signal est bon (voir ci-dessous)

     api:
       encryption:
         key: !secret api_key

     ota:
       - platform: esphome

     logger:

     esp32_ble_tracker:
       scan_parameters:
         # Économie : écoute 30 ms toutes les 320 ms (~10 %). Sans ces lignes, ESPHome
         # écoute en continu (100 %). Le scan passif suffit : les annonces Mesh Proxy sont connectables.
         interval: 320ms
         window: 30ms
         active: false

     bluetooth_proxy:
       active: true   # indispensable : l'intégration ouvre une connexion GATT
     ```

     **Réglages d'économie d'énergie** (testés sur un ESP32 classique, avec une Candle DIM) : sans eux, le proxy écoute le Bluetooth en permanence, processeur à 240 MHz. Ils ne ralentissent pas le pilotage, car les ampoules s'annoncent plusieurs fois par seconde. Pour revenir en arrière :
     - si Home Assistant signale une ampoule « non visible », augmentez `window` (par exemple 60 ms, puis 100 ms) ;
     - si l'ESP32 se déconnecte du Wi-Fi, augmentez `output_power` (15, 17, puis 20 dB) ou supprimez la ligne. Son journal ESPHome indique le signal reçu : vers -70 dBm, ne réduisez plus la puissance ;
     - si les commandes deviennent lentes, passez `cpu_frequency` à 160MHZ.

     `power_save_mode: HIGH` est déconseillé pour un proxy : il économise peu et risque des déconnexions de Home Assistant. Le mode par défaut, `LIGHT`, suffit.
3. **Pour la configuration depuis le compte Nordlux** (recommandée) : l'e-mail et le mot de passe du compte, c'est tout. Les ampoules doivent d'abord avoir été ajoutées dans l'app Nordlux.
4. **Pour la saisie manuelle** : les clés du réseau mesh et les adresses des ampoules. Les outils du dépôt [nordlux-phy-ota](https://github.com/ludovicalchi/nordlux-phy-ota) les récupèrent depuis votre propre compte Nordlux :
   - `tools/nordlux_mesh_keys.py` donne la NetKey, l'AppKey, l'IV index et l'adresse unicast de chaque ampoule ;
   - `tools/mesh_find_node.py` retrouve l'adresse d'une ampoule par radio, par exemple si elle a été supprimée de l'app sans avoir été réinitialisée.

## Installation via HACS

1. Dans HACS, ouvrez le menu ⋮, puis **Dépôts personnalisés**.
2. Ajoutez `https://github.com/ludovicalchi/ha-nordlux-mesh` avec la catégorie **Intégration**.
3. Installez **Nordlux Smart (Bluetooth Mesh local)**, puis redémarrez Home Assistant.

L'installation manuelle reste possible : copiez `custom_components/nordlux_mesh/` dans le dossier `/config/custom_components/` de Home Assistant.

## Configuration

**Paramètres → Appareils et services → Ajouter une intégration → Nordlux Smart (Bluetooth Mesh local)**, puis choisissez l'une des deux méthodes.

### Depuis le compte Nordlux

1. Saisissez l'e-mail et le mot de passe du compte.
2. L'assistant se connecte au cloud **en lecture seule**. Il récupère la NetKey, l'AppKey, l'IV index, les ampoules (nom et adresse unicast) et les nœuds proxy : le bridge d'abord, puis les ampoules.
3. Si le compte a plusieurs maisons, choisissez-en une.
4. Vérifiez la liste des proxys et des ampoules, modifiable, puis validez.

Le mot de passe n'est **pas enregistré** : seules les clés du réseau mesh, les proxys et les ampoules sont conservés. Après la configuration, l'intégration n'utilise plus du tout le cloud.

Pour dialoguer avec le cloud, l'intégration utilise les clés d'API codées en dur dans l'app Android Nordlux (v2.6.2). Ces clés sont les mêmes pour tous les utilisateurs et ne donnent accès à aucun compte sans son e-mail et son mot de passe. Si une future version de l'app en change, l'assistant cessera de fonctionner jusqu'à une mise à jour de l'intégration. La saisie manuelle restera alors possible.

**Nouvelle ampoule ajoutée dans l'app** : dans l'intégration, ouvrez **Configurer → Mettre à jour depuis mon compte Nordlux**. Les noms déjà choisis sont conservés.

### Ajouter une ampoule sans l'app

**Configurer → Ajouter une ampoule Nordlux réinitialisée (sans l'app)** :

1. Réinitialisez l'ampoule : 3 extinctions/rallumages à 2–4 s d'intervalle. Elle clignote 3 fois.
2. L'intégration liste les ampoules Nordlux en attente de clés, vues par les adaptateurs ou proxys Bluetooth de Home Assistant. Choisissez-la et donnez-lui un nom.
3. L'intégration lui envoie les clés du réseau et une adresse libre entre 0x7E00 et 0x7EFF, comme le fait l'app Nordlux. Elle attend ensuite que l'ampoule annonce sa nouvelle configuration.
4. L'ampoule est ajoutée à la liste, et à celle des proxys.

Après une réinitialisation, ces ampoules n'utilisent pas le provisioning Bluetooth Mesh standard. Elles rejoignent seules un réseau d'usine, puis l'app leur écrit en clair, sur un service GATT Nordlux, la NetKey, l'AppKey, l'adresse et le groupe de la maison (6 messages). L'intégration reproduit cet échange.

### Supprimer une ampoule

Utilisez le bouton **Supprimer** standard de l'appareil (⋮ sur la page de l'appareil).

L'intégration envoie d'abord à l'ampoule, par le réseau mesh, la réinitialisation que l'app utilise pour les firmwares 18 et plus : message vendeur `E1 11 11 0A 00`. L'ampoule clignote, quitte le réseau, et peut être ajoutée de nouveau, ici ou dans l'app. Il n'est pas nécessaire d'être à portée directe de l'ampoule : n'importe quel proxy du réseau convient.

L'ampoule est ensuite retirée de la liste, et de celle des proxys si elle est identifiée, puis son appareil disparaît.

La suppression fonctionne même si l'ampoule est injoignable :
- **Éteinte ou hors de portée, mais un autre proxy répond** : le message part, mais l'ampoule ne le reçoit pas.
- **Aucun proxy joignable** : la réinitialisation ne peut pas être envoyée. Une notification Home Assistant vous demande alors de réinitialiser l'ampoule à la main (3 extinctions/rallumages).

Dans les deux cas, l'ampoule reste dans le réseau mesh jusqu'à sa réinitialisation.

Une ampoule ajoutée sans l'app **n'apparaît pas dans l'app Nordlux**. Le cloud la garde à son ancienne adresse si elle y était. N'utilisez donc pas « Mettre à jour depuis mon compte » pour elle : il proposerait cette ancienne adresse. Pour la rendre à l'app, réinitialisez-la, puis ajoutez-la normalement depuis l'app.

### Saisie manuelle

| Champ | Exemple | Remarque |
|---|---|---|
| NetKey / AppKey | 32 caractères hexadécimaux | issues de `nordlux_mesh_keys.py` |
| IV index | `0` | |
| Adresse source | `0x7F10` | adresse unicast **libre**, propre à Home Assistant, différente de celle de l'app (0x2001…) et de tout autre outil |
| Proxys | `AA:BB:CC:DD:EE:FF, 11:22:33:44:55:66` | adresses BLE des nœuds proxy, essayées dans l'ordre. Le bridge Nordlux, en premier, s'est montré le plus fiable. |
| Ampoules | `Salon = 0x2005` | une ligne par ampoule : nom = adresse unicast |

Ensuite, les ampoules s'ajoutent et se suppriment comme décrit plus haut. Il n'y a plus d'écran pour modifier la liste à la main.

## Fonctionnement

- L'app Nordlux ne pilote pas la lumière avec les commandes mesh standard : Generic OnOff et Light Lightness sont acquittées par l'ampoule, mais sans effet. Elle **détourne Light CTL Set**, et l'intégration envoie exactement les mêmes messages :

  | Action | lightness | température | transition |
  |---|---|---|---|
  | Allumer | 0 | 10000 | 0 |
  | Éteindre | 0 | 15000 | 0 |
  | Luminosité | pourcentage 0–100 | 800 + teinte en % (900 par défaut) | 400 ms |

- **L'état réel est relu** toutes les 2 minutes, et 20 s après le démarrage, sur une seule connexion au proxy pour toutes les ampoules.
  - La lecture utilise la *present lightness* de **Light CTL Status** : la luminosité en %, 0 quand l'ampoule est éteinte.
  - Vérifié sur une Candle DIM : 0 éteinte, la valeur envoyée une fois allumée, et la même valeur après une coupure de courant à l'interrupteur.
  - Les statuts standard Generic OnOff et Light Lightness, eux, ne reflètent pas la lumière.
  - Après une commande, l'affichage change aussitôt, puis la lecture suivante le confirme ou le corrige.
  - Si l'ampoule ne répond pas à une lecture isolée, le dernier état connu est conservé.
  - L'entité s'affiche avec un interrupteur standard.
- **Fiche de l'appareil** :
  - **Modèle** : nom du catalogue de l'app, par exemple « Candle DIM », avec son type `nx00626222`.
  - **Firmware et adresse Bluetooth** : lus dans les annonces de l'ampoule, sans connexion, et tenus à jour.
  - **Pièce Nordlux** : proposée comme zone, pour les ampoules venant du compte.
- **Capteurs de diagnostic** :
  - **Signal Bluetooth** : RSSI, avec le proxy qui reçoit l'ampoule.
  - **Dernière réponse** : heure de la dernière lecture d'état réussie.
  - **Origine** :
    - « Compte Nordlux » : ampoule connue du cloud lors de la dernière lecture du compte ;
    - « Home Assistant » : ampoule ajoutée sans l'app, par Home Assistant ;
    - « Inconnue » : saisie manuelle.

    Ses attributs donnent l'adresse mesh, la plage de température de couleur du modèle, et le firmware et la pièce connus à l'enregistrement.
- **Mise à jour du firmware** : une entité « Firmware » par ampoule, l'entité de mise à jour standard de Home Assistant.
  - **Vérification** : avec les identifiants enregistrés via Configurer → **Mises à jour du firmware (compte Nordlux)**, l'intégration demande au cloud, une fois par jour, la dernière version publiée pour chaque modèle (`getOTAInfo`). Le mot de passe est enregistré dans Home Assistant.
  - **Identifiants refusés** (mot de passe modifié dans l'app, par exemple) : Home Assistant affiche « Nouvelle authentification requise ». On saisit le nouveau mot de passe, et la vérification reprend environ une minute plus tard. Le pilotage des ampoules n'est pas concerné. Une coupure réseau ou un cloud indisponible ne déclenche pas cette demande : seulement un avertissement dans les journaux.
  - **Installation** : le bouton « Installer » affiche une barre de progression et se déroule en 4 étapes.
    1. Téléchargement de l'image, vérifiée par son MD5.
    2. Passage de l'ampoule en mode mise à jour : `01 02` sur le service OTA Phyplus.
    3. Flashage par le proxy Bluetooth, en paquets de 20 octets.
    4. Redémarrage. L'ampoule garde ses clés et son adresse.
  - **Durée** : environ 3 à 4 minutes en direct depuis un PC. Elle peut être plus longue via un proxy ESPHome.
  - **En cas d'échec** : l'ampoule reste en mode mise à jour, éteinte. Relancez l'installation : l'intégration détecte ce mode et reprend le flashage.
  - **Réinstaller la même version** : action `update.install`, avec `version` égale à la version publiée.
- **Ampoule injoignable** : après 3 lectures de suite sans réponse, soit environ 6 minutes, l'ampoule devient **« Indisponible »**, comme une entité Zigbee. Elle redevient disponible à la première réponse.
  - Causes possibles : hors tension, hors de portée, ou réinitialisée ou ajoutée ailleurs.
  - Cela vaut aussi quand l'ampoule éteinte était le seul proxy mesh joignable.
  - Le capteur « Dernière réponse » indique depuis quand elle ne répond plus.
- **Alerte (Paramètres → Réparations) « Aucun proxy Bluetooth disponible »** : Home Assistant n'a plus aucun adaptateur ni proxy Bluetooth connectable depuis 3 lectures (proxy ESPHome débranché ou déconnecté). Les ampoules deviennent alors aussi indisponibles. L'alerte disparaît dès qu'une lecture réussit, et n'est pas conservée au redémarrage.
- **Anti-rejeu** : chaque message mesh porte un numéro de séquence qui ne doit jamais être réutilisé pour une même adresse source. L'intégration le réserve par blocs et l'enregistre dans le stockage de Home Assistant. Le prochain numéro reprend toujours au-delà du dernier bloc réservé, y compris après un redémarrage. N'utilisez pas la même adresse source dans un autre outil.
- La connexion au proxy est ouverte à la demande et fermée après 30 s d'inactivité, pour ne pas occuper durablement les connexions limitées d'un proxy ESPHome.
- Le bridge Nordlux peut rester allumé : il n'annule pas les commandes locales.
- Une ampoule retirée de la liste, ou dont l'adresse a changé, disparaît aussi des appareils au rechargement de l'intégration. Son appareil peut également être supprimé à la main.

## Avertissement

Projet personnel, non affilié à Nordlux ni à Yankon. À utiliser à vos risques.
