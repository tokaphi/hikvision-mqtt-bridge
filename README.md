# hikvision-mqtt-bridge

Version simplifiée de l'add-on `pergolafabio/Hikvision-Addons`, sans `ha_mqtt_discoverable`.
Fait 4 choses : Call-state, relais en impulsion (durée paramétrable), événement
"porte déverrouillée", reboot à distance, et remontée brute des tentatives
d'authentification (badge/code/empreinte/visage). Rien d'autre n'est publié sur MQTT.

## Démarrage rapide

Deux façons de configurer, au choix (voir `config.py`) :

**A. Fichier JSON monté** (build local) :
```bash
cp config.json.example config.json
# éditer config.json : IP/identifiants de tes portiers, host MQTT
docker compose up --build -d
docker compose logs -f
```

**B. Variables d'environnement** (style Portainer/stack.env, comme l'add-on
d'origine) : voir `stack.env.example` et la section "Déploiement via GitHub +
Portainer" ci-dessous. C'est ce mode qui est utilisé si aucun fichier JSON
n'est trouvé.

## Déploiement via GitHub + Portainer (comme l'add-on d'origine)

1. Crée un nouveau dépôt **vide** sur GitHub (sans README/gitignore), ex:
   `hikvision-mqtt-bridge`.
2. Depuis ce dossier, en local :
   ```bash
   git init
   git add .
   git commit -m "Version initiale du pont MQTT simplifié"
   git branch -M main
   git remote add origin git@github.com:<TON_USER>/<TON_REPO>.git
   git push -u origin main
   ```
3. Va dans l'onglet **Actions** de ton dépôt GitHub : le workflow
   `.github/workflows/build.yml` se déclenche automatiquement et construit
   l'image (amd64 + arm64) pour la publier sur `ghcr.io/<TON_USER>/<TON_REPO>`.
   Ça prend quelques minutes la première fois.
4. Par défaut, un package GitHub fraîchement créé est **privé**. Pour que
   Portainer puisse le tirer sans authentification : Profil GitHub -> Packages
   -> `<TON_REPO>` -> Package settings -> Change visibility -> Public.
   (Sinon il faut configurer un `docker login ghcr.io` sur ton hôte Docker.)
5. Dans Portainer, remplace ton stack actuel par le contenu de
   `docker-compose.yml` de ce dépôt (en remplaçant `<TON_USER>/<TON_REPO>`),
   et colle le contenu de `stack.env.example` (rempli avec tes vraies valeurs)
   dans l'onglet "Environment variables" du stack.
6. À chaque modification que tu voudras faire plus tard : modifier le code,
   `git commit` + `git push`, puis dans Portainer "Pull and redeploy" (ou
   "Update the stack" avec "Re-pull image").

## Fichiers

| Fichier | Rôle |
|---|---|
| `sdk/hcnetsdk.py` | Définitions ctypes du SDK Hikvision (structures C), repris tel quel de l'add-on d'origine |
| `sdk/utils.py` | Chargement du SDK, appel ISAPI via le SDK, gestion des erreurs |
| `doorbell.py` | Classe `Doorbell` : connexion, pilotage des relais en impulsion, reboot |
| `events.py` | Décode les événements bruts du SDK (sonnette, raccroché, déverrouillage) |
| `bridge.py` | **Seul fichier qui parle MQTT.** Topics publiés/écoutés documentés en tête de fichier |
| `config.py` | Chargement de `config.json` |
| `main.py` | Point d'entrée : câblage + reconnexion auto des portiers hors-ligne |

## Topics MQTT (racine configurable via `ROOT_TOPIC`, `hmd` par défaut comme l'add-on d'origine)

- `<root>/sensor/<Nom>/Call-state/state` — `idle` / `ringing` / `dismissed`
- `<root>/sensor/<Nom>/Door-unlocked/state` — JSON, publié à chaque ouverture (par MQTT ou toute autre source)
- `<root>/sensor/<Nom>/Access-attempt/state` — JSON brut `{result_raw, type_raw, card_no}` à chaque tentative d'authentification (badge/code/empreinte/visage), réussie ou non. **Encodage non documenté par Hikvision** : sur le matériel testé, `result_raw`/`type_raw` ne distinguaient pas succès/échec — à vérifier au cas par cas avant de câbler une alerte dessus.
- `<root>/sensor/<Nom>/availability/state` — `online` / `offline`
- `<root>/switch/<Nom>/Door-N-relay/state` (portiers extérieurs) ou `Com-N-relay/state` (poste intérieur)
- `<root>/switch/<Nom>/Door-N-relay/set` — **nouveau côté Node-RED** : publier `"ON"` (durée par défaut) ou un nombre de secondes (ex: `"8"`) pour ouvrir
- `<root>/button/<Nom>/Reboot/set` — n'importe quel payload déclenche le reboot
- `<root>/bridge/availability` — `online`/`offline` du service entier (Last Will Testament MQTT)

## Point de conception important

L'état `ON`/`OFF` du switch `Door-N-relay` n'est **pas** piloté directement par la commande MQTT
reçue : il est piloté par l'événement "déverrouillage" que renvoie le device lui-même
(`events.py` → `on_door_unlocked`). Ça veut dire que le switch reflète fidèlement la réalité,
même si la porte a été ouverte autrement que via MQTT (badge, bouton physique...).
La commande `/set` ne fait que déclencher l'ouverture ; c'est l'événement retour qui met à jour l'état.

## Statut

Ce pont tourne en production, testé et validé sur du matériel réel (portiers extérieurs
et poste intérieur Hikvision). Plusieurs corrections ont été apportées suite à des tests
en conditions réelles (état du portail, sens des commandes relais, topic racine codé en
dur...) — voir l'historique des commits. En cas de doute entre la documentation Hikvision
et le comportement observé sur le matériel, le comportement observé fait foi.
