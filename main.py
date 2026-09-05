"""
main.py - Point d'entrée. Assemble config -> SDK -> portiers -> events -> MQTT.

Rien de métier ici, juste le câblage et la gestion du cycle de vie
(connexion initiale, reconnexion des portiers hors-ligne, arrêt propre).
"""

import asyncio
import signal
import sys

from loguru import logger

from bridge import MQTTBridge, _call_state_topic
from config import load_config
from doorbell import Doorbell, Registry
from events import EventManager
from sdk.utils import SDKConfig, SDKError, SDKLogLevel, loadSDK, setupSDK, shutdownSDK


async def _connect_doorbell(doorbell: Doorbell) -> bool:
    """Authentifie et arme un portier. Retourne True si ça a réussi."""
    try:
        await asyncio.to_thread(doorbell.authenticate)
        await asyncio.to_thread(doorbell.setup_alarm)
        return True
    except SDKError as err:
        logger.error("[{}] Connexion impossible: {}", doorbell.name, err)
        return False


async def _retry_offline_doorbells(registry: Registry, offline_names: set[str],
                                    bridge: MQTTBridge, retry_seconds: float = 30.0):
    """Boucle de fond : retente indéfiniment les portiers qui n'ont pas pu se
    connecter au démarrage (ou qui tombent hors-ligne plus tard). Tourne en
    tâche de fond pendant toute la vie du service."""
    while True:
        await asyncio.sleep(retry_seconds)
        for name in list(offline_names):
            doorbell = registry[name]
            logger.info("[{}] Nouvelle tentative de connexion...", name)
            if await _connect_doorbell(doorbell):
                offline_names.discard(name)
                bridge.set_availability(doorbell, online=True)
                logger.info("[{}] Portier de nouveau en ligne", name)
            else:
                bridge.set_availability(doorbell, online=False)


async def _poll_call_state(doorbell: Doorbell, bridge: MQTTBridge, root_topic: str,
                            interval_seconds: float):
    """Sondage ISAPI du call-state, en complément de l'événement SDK.
    Nécessaire sur les firmwares récents (>= 3.7.x) qui n'envoient plus
    l'événement de sonnerie nativement (cf. DOCS.md de l'add-on d'origine).
    On ne publie que sur CHANGEMENT, pour ne pas spammer le broker.

    Le topic est reconstruit avec le même helper que bridge.py (racine
    configurée via ROOT_TOPIC), et non plus codé en dur : un ancien "hmd"
    hérité de l'add-on d'origine se republiait ici indépendamment du
    ROOT_TOPIC choisi, et donc réapparaissait après chaque redémarrage
    même une fois le topic supprimé côté broker."""
    last_state = None
    while True:
        await asyncio.sleep(interval_seconds)
        state = await asyncio.to_thread(doorbell.get_call_status)
        if state and state != last_state:
            last_state = state
            bridge.publish(_call_state_topic(root_topic, doorbell.name), state)
            logger.debug("[{}] Call-state (polling) -> {}", doorbell.name, state)


async def main():
    config = load_config()

    logger.remove()
    logger.add(sys.stdout, colorize=True, level=config.log_level)

    sdk = loadSDK()
    setupSDK(sdk, SDKConfig(log_level=SDKLogLevel.NONE, log_dir="./SDKLogs"))
    logger.info("SDK Hikvision chargé et initialisé")

    registry = Registry()
    for doorbell_config in config.doorbells:
        registry[doorbell_config.name] = Doorbell(
            name=doorbell_config.name,
            ip=doorbell_config.ip,
            username=doorbell_config.username,
            password=doorbell_config.password,
            port=doorbell_config.port,
            sdk=sdk,
            device_type=doorbell_config.type,
            relay_pulse=doorbell_config.relay_pulse,
            default_pulse=config.default_pulse_seconds,
        )

    offline_names: set[str] = set()
    for doorbell_config in config.doorbells:
        doorbell = registry[doorbell_config.name]
        if not await _connect_doorbell(doorbell):
            offline_names.add(doorbell_config.name)

    bridge = MQTTBridge(config, registry)
    bridge.connect()

    for name in offline_names:
        bridge.set_availability(registry[name], online=False)

    # EventManager doit être démarré APRÈS que tous les portiers en ligne
    # soient armés, et après la connexion MQTT (il publie via bridge dès
    # qu'un événement arrive).
    event_manager = EventManager(
        sdk, registry,
        on_call_state=bridge.on_call_state,
        on_door_unlocked=bridge.on_door_unlocked,
        on_access_attempt=bridge.on_access_attempt,
    )
    event_manager.start()

    background_tasks = [
        asyncio.create_task(_retry_offline_doorbells(registry, offline_names, bridge)),
    ]
    for doorbell_config in config.doorbells:
        doorbell = registry[doorbell_config.name]
        background_tasks.append(asyncio.create_task(
            _poll_call_state(doorbell, bridge, config.root_topic, config.call_state_poll_seconds)))

    logger.info("Service prêt ({} portier(s) configuré(s), {} hors-ligne au démarrage)",
                len(registry), len(offline_names))

    stop_event = asyncio.Event()

    def _handle_stop_signal():
        logger.info("Signal d'arrêt reçu")
        stop_event.set()

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, _handle_stop_signal)
    loop.add_signal_handler(signal.SIGTERM, _handle_stop_signal)

    await stop_event.wait()

    logger.info("Arrêt du service...")
    for task in background_tasks:
        task.cancel()
    for doorbell in registry.values():
        await asyncio.to_thread(doorbell.logout)
    bridge.disconnect()
    shutdownSDK(sdk)


if __name__ == "__main__":
    asyncio.run(main())
