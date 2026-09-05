"""
bridge.py - Le pont MQTT. Seul fichier qui parle MQTT dans tout ce projet.

Contrairement à l'add-on d'origine (ha_mqtt_discoverable), on utilise
paho-mqtt directement : pas de topics de discovery/config générés en douce,
pas d'entités HA fantômes. Juste les topics utiles, publiés explicitement,
listés une fois pour toutes ci-dessous (RELAY_LABEL, et les fonctions
*_topic). Si un jour tu as un doute sur "qui publie quoi", il n'y a que ce
fichier à lire.

Schéma des topics (root = config.root_topic, "hmd" par défaut) :

  hmd/sensor/<Nom>/Call-state/state        publié   idle | ringing | dismissed
  hmd/sensor/<Nom>/Door-unlocked/state     publié   JSON {relay, unlock_type, control_source, card_user_id, timestamp}
  hmd/sensor/<Nom>/availability/state      publié   online | offline
  hmd/switch/<Nom>/Door-N-relay/state      publié   ON (impulsion) puis OFF
  hmd/switch/<Nom>/Door-N-relay/set        écouté   "ON" (durée par défaut) | "<secondes>" (durée custom)
  hmd/switch/<Nom>/Com-N-relay/state       publié   idem, pour un poste intérieur
  hmd/switch/<Nom>/Com-N-relay/set         écouté   idem
  hmd/button/<Nom>/Reboot/set              écouté   n'importe quel payload déclenche le reboot
  hmd/bridge/availability                  publié   online | offline (LWT du service entier)
"""

import asyncio
import json
from datetime import datetime
from typing import Optional

import paho.mqtt.client as mqtt
from loguru import logger

from config import AppConfig
from doorbell import Doorbell, DoorbellType, Registry
from sdk.utils import SDKError

RELAY_LABEL = {
    DoorbellType.OUTDOOR: "Door",
    DoorbellType.INDOOR: "Com",
}


def _relay_topic(root: str, name: str, device_type: DoorbellType, relay_id: int, suffix: str) -> str:
    label = RELAY_LABEL[device_type]
    return f"{root}/switch/{name}/{label}-{relay_id}-relay/{suffix}"


def _call_state_topic(root: str, name: str) -> str:
    return f"{root}/sensor/{name}/Call-state/state"


def _door_unlocked_topic(root: str, name: str) -> str:
    return f"{root}/sensor/{name}/Door-unlocked/state"


def _availability_topic(root: str, name: str) -> str:
    return f"{root}/sensor/{name}/availability/state"


def _reboot_topic(root: str, name: str) -> str:
    return f"{root}/button/{name}/Reboot/set"


class MQTTBridge:
    """Pont entre le broker MQTT et les objets Doorbell.

    Utilisation : instancier, appeler `connect()`, puis brancher les méthodes
    `on_call_state` / `on_door_unlocked` sur un `events.EventManager`.
    """

    def __init__(self, config: AppConfig, doorbells: Registry):
        self._config = config
        self._doorbells = doorbells
        self._loop = asyncio.get_running_loop()

        self._client = mqtt.Client(client_id="hikvision-mqtt-bridge", protocol=mqtt.MQTTv311)
        if config.mqtt.username:
            self._client.username_pw_set(config.mqtt.username, config.mqtt.password)

        bridge_availability = f"{config.root_topic}/bridge/availability"
        self._client.will_set(bridge_availability, "offline", qos=1, retain=True)

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    # ------------------------------------------------------------------
    # Cycle de vie
    # ------------------------------------------------------------------

    def connect(self):
        logger.info("Connexion au broker MQTT {}:{}", self._config.mqtt.host, self._config.mqtt.port)
        self._client.connect(self._config.mqtt.host, self._config.mqtt.port, keepalive=30)
        # loop_start() gère la connexion dans un thread dédié à paho-mqtt.
        # Les callbacks (_on_message, ...) arrivent donc sur CE thread, pas sur
        # la boucle asyncio : d'où le run_coroutine_threadsafe utilisé plus bas.
        self._client.loop_start()

    def disconnect(self):
        self.publish(f"{self._config.root_topic}/bridge/availability", "offline", retain=True)
        self._client.loop_stop()
        self._client.disconnect()

    def publish(self, topic: str, payload: str, retain: bool = True):
        self._client.publish(topic, payload, qos=1, retain=retain)

    # ------------------------------------------------------------------
    # Connexion MQTT : abonnements + état initial
    # ------------------------------------------------------------------

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            logger.error("Connexion MQTT refusée (code {})", rc)
            return
        logger.info("Connecté au broker MQTT")

        root = self._config.root_topic
        client.publish(f"{root}/bridge/availability", "online", qos=1, retain=True)

        for doorbell_config in self._config.doorbells:
            name = doorbell_config.name
            doorbell = self._doorbells[name]

            for relay_id in range(1, doorbell_config.num_relays + 1):
                set_topic = _relay_topic(root, name, doorbell.device_type, relay_id, "set")
                client.subscribe(set_topic, qos=1)
                # État initial connu : OFF (les relais sont pilotés en impulsion,
                # jamais persistants), pour éviter un état "bloqué" affiché à tort.
                client.publish(_relay_topic(root, name, doorbell.device_type, relay_id, "state"),
                                "OFF", qos=1, retain=True)

            client.subscribe(_reboot_topic(root, name), qos=1)
            client.publish(_call_state_topic(root, name), "idle", qos=1, retain=True)
            client.publish(_availability_topic(root, name), "online", qos=1, retain=True)

        logger.info("Abonnements MQTT prêts pour {} portier(s)", len(self._config.doorbells))

    def _on_disconnect(self, client, userdata, rc):
        if rc != 0:
            logger.warning("Déconnexion MQTT inattendue (code {}), paho-mqtt va tenter de se reconnecter", rc)

    # ------------------------------------------------------------------
    # Réception des commandes MQTT (Door-N-relay/set, Com-N-relay/set, Reboot/set)
    # ------------------------------------------------------------------

    def _on_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode("utf-8").strip()
        except UnicodeDecodeError:
            logger.warning("Payload MQTT non décodable sur {}", msg.topic)
            return

        parts = msg.topic.split("/")
        if len(parts) < 4:
            return
        _, domain, name, entity = parts[0], parts[1], parts[2], parts[3]

        doorbell = self._doorbells.get(name)
        if doorbell is None:
            logger.warning("Commande reçue pour un portier inconnu: {} (topic: {})", name, msg.topic)
            return

        if domain == "switch" and entity.endswith("-relay"):
            self._handle_relay_command(doorbell, entity, payload)
        elif domain == "button" and entity == "Reboot":
            self._handle_reboot_command(doorbell)
        else:
            logger.debug("Message MQTT ignoré (hors périmètre): {}", msg.topic)

    def _handle_relay_command(self, doorbell: Doorbell, entity: str, payload: str):
        """
        entity ressemble à "Door-1-relay" ou "Com-2-relay".
        payload :
          - "ON"  -> ouverture avec la durée par défaut (get_pulse_duration)
          - "OFF" -> ignoré : les relais sont pilotés en impulsion, pas en toggle
          - un nombre (ex: "8") -> ouverture avec CETTE durée en secondes
        """
        try:
            relay_id = int(entity.split("-")[1])
        except (IndexError, ValueError):
            logger.warning("[{}] Impossible d'extraire le numéro de relais depuis '{}'", doorbell.name, entity)
            return

        payload_upper = payload.upper()
        duration: Optional[float]
        if payload_upper == "ON":
            duration = None  # pulse_relay utilisera get_pulse_duration()
        elif payload_upper == "OFF":
            logger.debug("[{}] Commande OFF ignorée sur {} (relais en impulsion)", doorbell.name, entity)
            return
        else:
            try:
                duration = float(payload)
            except ValueError:
                logger.warning("[{}] Payload de commande relais non reconnu: {!r}", doorbell.name, payload)
                return

        future = asyncio.run_coroutine_threadsafe(
            self._pulse_and_publish(doorbell, relay_id, duration), self._loop)
        future.add_done_callback(self._log_task_error)

    def _handle_reboot_command(self, doorbell: Doorbell):
        future = asyncio.run_coroutine_threadsafe(self._do_reboot(doorbell), self._loop)
        future.add_done_callback(self._log_task_error)

    @staticmethod
    def _log_task_error(future: "asyncio.Future"):
        error = future.exception()
        if error:
            logger.error("Erreur lors du traitement d'une commande MQTT: {}", error)

    async def _pulse_and_publish(self, doorbell: Doorbell, relay_id: int, duration: Optional[float]):
        root = self._config.root_topic
        topic = _relay_topic(root, doorbell.name, doorbell.device_type, relay_id, "state")
        self.publish(topic, "ON")
        await doorbell.pulse_relay(relay_id, duration)
        self.publish(topic, "OFF")

    async def _do_reboot(self, doorbell: Doorbell):
        try:
            await asyncio.to_thread(doorbell.reboot_device)
        except SDKError as err:
            logger.error("[{}] Échec du reboot: {}", doorbell.name, err)

    # ------------------------------------------------------------------
    # Callbacks appelés par events.EventManager (côté SDK -> MQTT)
    # ------------------------------------------------------------------

    async def on_call_state(self, doorbell: Doorbell, state: str):
        """Publie le nouveau call-state, puis remet automatiquement 'idle'
        après un délai (garde-fou pour les firmwares qui n'envoient pas
        toujours l'événement de fin d'appel)."""
        topic = _call_state_topic(self._config.root_topic, doorbell.name)
        self.publish(topic, state)
        logger.info("[{}] Call-state -> {}", doorbell.name, state)

        if state == "ringing":
            await asyncio.sleep(self._config.ring_timeout_seconds)
            self.publish(topic, "idle")
        elif state == "dismissed":
            await asyncio.sleep(1)
            self.publish(topic, "idle")

    async def on_door_unlocked(self, doorbell: Doorbell, relay_id: int, info: dict):
        """Appelé pour CHAQUE déverrouillage détecté par le device, que ce
        soit suite à une commande MQTT de ce pont, ou à toute autre source
        (badge, code, bouton physique...). C'est la source de vérité pour
        l'état du switch Door/Com-N-relay, pas la commande MQTT elle-même."""
        root = self._config.root_topic

        payload = json.dumps({**info, "timestamp": datetime.now().isoformat(timespec="seconds")})
        self.publish(_door_unlocked_topic(root, doorbell.name), payload, retain=False)
        logger.info("[{}] Relais {} déverrouillé (source: {})", doorbell.name, relay_id, info.get("unlock_type"))

        relay_state_topic = _relay_topic(root, doorbell.name, doorbell.device_type, relay_id, "state")
        self.publish(relay_state_topic, "ON")
        await asyncio.sleep(doorbell.get_pulse_duration(relay_id))
        self.publish(relay_state_topic, "OFF")

    def set_availability(self, doorbell: Doorbell, online: bool):
        self.publish(_availability_topic(self._config.root_topic, doorbell.name),
                      "online" if online else "offline")
