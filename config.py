"""
config.py - Chargement de la configuration.

Volontairement simple : pas de framework de config (goodconf/pydantic comme
dans l'add-on d'origine), juste des dataclasses avec validation minimale.

Deux sources possibles, dans cet ordre de priorité :
  1. Fichier JSON pointé par la variable d'env CONFIG_FILE (défaut: /data/config.json)
     si ce fichier existe -> voir config.json.example
  2. Sinon, variables d'environnement (le style "stack.env" de Portainer,
     identique à celui de l'add-on d'origine) :
       DOORBELLS   = JSON, ex: [{"name":"Portier-Ext","ip":"192.168.0.70","username":"admin","password":"xxx","type":"outdoor"}]
       MQTT__HOST, MQTT__PORT, MQTT__USERNAME, MQTT__PASSWORD
       ROOT_TOPIC, DEFAULT_PULSE_SECONDS, CALL_STATE_POLL_SECONDS,
       RING_TIMEOUT_SECONDS, LOG_LEVEL
"""

import json
import os
from dataclasses import dataclass, field
from typing import Optional
from loguru import logger

from doorbell import DoorbellType


@dataclass
class MQTTConfig:
    host: str
    port: int = 1883
    username: Optional[str] = None
    password: Optional[str] = None


@dataclass
class DoorbellConfig:
    name: str
    """Nom utilisé tel quel dans les topics MQTT, ex: 'Portier-Ext'"""
    ip: str
    username: str
    password: str
    port: int = 8000
    type: DoorbellType = DoorbellType.OUTDOOR
    num_relays: int = 2
    """Nombre de relais exposés en MQTT pour ce portier (Door-1..N ou Com-1..N)."""
    relay_pulse: dict[int, float] = field(default_factory=dict)
    """Durée d'impulsion par relais en secondes, ex: {1: 3, 2: 5}. Optionnel,
    voir default_pulse_seconds pour la valeur par défaut globale."""


@dataclass
class AppConfig:
    mqtt: MQTTConfig
    doorbells: list[DoorbellConfig]
    root_topic: str = "hmd"
    default_pulse_seconds: float = 3.0
    call_state_poll_seconds: float = 5.0
    """Intervalle de sondage ISAPI du call-state, utilisé en complément de
    l'événement SDK (nécessaire sur les firmwares >= 3.7.x qui n'envoient
    plus l'événement de sonnerie, cf. DOCS.md de l'add-on d'origine)."""
    ring_timeout_seconds: float = 60.0
    """Durée après laquelle le call-state repasse à 'idle' si aucun événement
    de raccroché n'est reçu (garde-fou, comme dans l'add-on d'origine)."""
    log_level: str = "INFO"


def _parse_doorbell(raw: dict) -> DoorbellConfig:
    relay_pulse_raw = raw.get("relay_pulse", {}) or {}
    relay_pulse = {int(k): float(v) for k, v in relay_pulse_raw.items()}

    return DoorbellConfig(
        name=raw["name"],
        ip=raw["ip"],
        username=raw["username"],
        password=raw["password"],
        port=int(raw.get("port", 8000)),
        type=DoorbellType(raw.get("type", "outdoor")),
        num_relays=int(raw.get("num_relays", 2)),
        relay_pulse=relay_pulse,
    )


def _load_from_json(config_path: str) -> AppConfig:
    with open(config_path, "r") as f:
        raw = json.load(f)

    try:
        mqtt_raw = raw["mqtt"]
        mqtt = MQTTConfig(
            host=mqtt_raw["host"],
            port=int(mqtt_raw.get("port", 1883)),
            username=mqtt_raw.get("username"),
            password=mqtt_raw.get("password"),
        )

        doorbells = [_parse_doorbell(d) for d in raw["doorbells"]]
        if not doorbells:
            raise ValueError("La liste 'doorbells' est vide, il faut au moins un portier configuré")

        return AppConfig(
            mqtt=mqtt,
            doorbells=doorbells,
            root_topic=raw.get("root_topic", "hmd"),
            default_pulse_seconds=float(raw.get("default_pulse_seconds", 3.0)),
            call_state_poll_seconds=float(raw.get("call_state_poll_seconds", 5.0)),
            ring_timeout_seconds=float(raw.get("ring_timeout_seconds", 60.0)),
            log_level=raw.get("log_level", "INFO"),
        )
    except KeyError as err:
        raise ValueError(f"Champ de configuration manquant dans {config_path}: {err}") from err


def _load_from_env() -> AppConfig:
    """Charge la config depuis des variables d'environnement, style
    stack.env de Portainer (même convention que l'add-on d'origine pour
    DOORBELLS et MQTT__*, afin de pouvoir réutiliser tel quel un stack.env
    existant)."""
    doorbells_json = os.getenv("DOORBELLS")
    if not doorbells_json:
        raise RuntimeError(
            "Aucune configuration trouvée : ni fichier JSON (CONFIG_FILE), "
            "ni variable d'environnement DOORBELLS définie."
        )

    try:
        doorbells_raw = json.loads(doorbells_json)
    except json.JSONDecodeError as err:
        raise ValueError(f"DOORBELLS n'est pas un JSON valide: {err}") from err

    doorbells = [_parse_doorbell(d) for d in doorbells_raw]
    if not doorbells:
        raise ValueError("DOORBELLS est vide, il faut au moins un portier configuré")

    mqtt_host = os.getenv("MQTT__HOST")
    if not mqtt_host:
        raise RuntimeError("Variable d'environnement MQTT__HOST manquante")

    mqtt = MQTTConfig(
        host=mqtt_host,
        port=int(os.getenv("MQTT__PORT", "1883")),
        username=os.getenv("MQTT__USERNAME"),
        password=os.getenv("MQTT__PASSWORD"),
    )

    return AppConfig(
        mqtt=mqtt,
        doorbells=doorbells,
        root_topic=os.getenv("ROOT_TOPIC", "hmd"),
        default_pulse_seconds=float(os.getenv("DEFAULT_PULSE_SECONDS", "3.0")),
        call_state_poll_seconds=float(os.getenv("CALL_STATE_POLL_SECONDS", "5.0")),
        ring_timeout_seconds=float(os.getenv("RING_TIMEOUT_SECONDS", "60.0")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )


def load_config() -> AppConfig:
    """Point d'entrée unique : fichier JSON si disponible, sinon variables
    d'environnement. Lève une exception explicite si rien n'est utilisable :
    mieux vaut planter au démarrage avec un message clair que de tourner
    avec une config à moitié valide."""
    config_path = os.getenv("CONFIG_FILE", "/data/config.json")

    if os.path.exists(config_path):
        logger.info("Configuration chargée depuis le fichier {}", config_path)
        config = _load_from_json(config_path)
    else:
        logger.info("Pas de fichier de config ({} introuvable), lecture des variables d'environnement", config_path)
        config = _load_from_env()

    logger.info("Configuration chargée: {} portier(s), MQTT={}:{}",
                len(config.doorbells), config.mqtt.host, config.mqtt.port)
    return config
