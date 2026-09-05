"""
events.py - Décodage des événements bruts envoyés par le SDK Hikvision.

Rôle unique de ce fichier : recevoir le callback C du SDK (thread natif, hors
de la boucle asyncio), l'identifier, le décoder en objet Python, puis
transmettre ça à bridge.py via deux callbacks simples (on_call_state,
on_door_unlocked). Ce fichier ne connaît RIEN à MQTT : c'est le seul endroit
qui parle le "dialecte" du SDK, tout le reste manipule des types Python de base.

Volontairement, on ignore tout ce qui n'est pas dans le périmètre retenu :
motion, sabotage, contrôle d'accès facial, alarmes ISAPI custom, etc.
L'add-on d'origine gère tout ça dans event.py + mqtt.py si un jour c'est
nécessaire, mais ce n'est pas ce qu'on utilise ici.
"""

import asyncio
from ctypes import CDLL, CFUNCTYPE, POINTER, c_void_p, cast
from typing import Awaitable, Callable, Optional
from loguru import logger

from doorbell import Doorbell, Registry
from sdk.hcnetsdk import (
    BOOL,
    DWORD,
    LONG,
    COMM_ALARM_VIDEO_INTERCOM,
    COMM_UPLOAD_VIDEO_INTERCOM_EVENT,
    NET_DVR_ALARMER,
    NET_DVR_VIDEO_INTERCOM_ALARM,
    NET_DVR_VIDEO_INTERCOM_EVENT,
    MessageCallbackAlarmInfoUnion,
    VideoInterComAlarmType,
    VideoInterComEventType,
    UnlockType,
)
from sdk.utils import SDKError

# Signatures des callbacks fournis par bridge.py
OnCallState = Callable[[Doorbell, str], Awaitable[None]]
OnDoorUnlocked = Callable[[Doorbell, int, dict], Awaitable[None]]


class EventManager:
    """Enregistre le callback global du SDK et route les événements reçus
    vers les portiers concernés (identifiés par leur numéro de série)."""

    def __init__(
        self,
        sdk: CDLL,
        doorbells: Registry,
        on_call_state: OnCallState,
        on_door_unlocked: OnDoorUnlocked,
    ):
        self._sdk = sdk
        self._doorbells = doorbells
        self._on_call_state = on_call_state
        self._on_door_unlocked = on_door_unlocked
        # On garde une référence à la boucle asyncio courante : le callback SDK
        # arrive sur un thread natif (C), il faut basculer explicitement dans
        # la boucle asyncio pour pouvoir faire du MQTT/await proprement.
        self._loop = asyncio.get_running_loop()
        self._callback_func = None  # référence gardée en vie, sinon ctypes la GC et le SDK segfault

    def start(self):
        """Enregistre le callback auprès du SDK. À appeler une seule fois,
        après que tous les portiers ont été authentifiés et armés."""
        self._callback_func = self._make_callback()
        result = self._sdk.NET_DVR_SetDVRMessageCallBack_V50(0, self._callback_func, None)
        if not result:
            raise SDKError(self._sdk, "Échec de l'enregistrement du callback SDK")
        logger.debug("Callback SDK enregistré")

    def _make_callback(self):
        @CFUNCTYPE(BOOL, LONG, POINTER(NET_DVR_ALARMER), POINTER(MessageCallbackAlarmInfoUnion), DWORD, c_void_p)
        def callback(command, alarm_device_pointer, alarm_info_pointer, buffer_length, user_pointer):
            self._dispatch(command, alarm_device_pointer, alarm_info_pointer)
            return True
        return callback

    def _dispatch(self, command: int, alarm_device_pointer, alarm_info_pointer):
        device = alarm_device_pointer.contents
        doorbell = self._doorbells.get_by_serial(device.serialNumber())
        if doorbell is None:
            logger.warning("Événement reçu d'un appareil non configuré (numéro de série: {})",
                            device.serialNumber())
            return

        if command == COMM_ALARM_VIDEO_INTERCOM:
            alarm_info = cast(alarm_info_pointer, POINTER(NET_DVR_VIDEO_INTERCOM_ALARM)).contents
            coro = self._handle_alarm(doorbell, alarm_info)
        elif command == COMM_UPLOAD_VIDEO_INTERCOM_EVENT:
            event_info = cast(alarm_info_pointer, POINTER(NET_DVR_VIDEO_INTERCOM_EVENT)).contents
            coro = self._handle_event(doorbell, event_info)
        else:
            logger.debug("[{}] Commande SDK {} hors périmètre, ignorée", doorbell.name, hex(command))
            return

        # IMPORTANT: on attend la fin du traitement (future.result()) avant de
        # rendre la main au SDK. Les structures pointées par alarm_info_pointer
        # ne sont garanties valides que pendant la durée de ce callback natif :
        # si on laissait la coroutine s'exécuter en fond sans l'attendre, elle
        # pourrait lire une zone mémoire déjà réutilisée par le SDK une fois le
        # callback terminé.
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            future.result()
        except Exception:
            logger.exception("[{}] Erreur lors du traitement d'un événement SDK", doorbell.name)

    async def _handle_alarm(self, doorbell: Doorbell, alarm_info: NET_DVR_VIDEO_INTERCOM_ALARM):
        try:
            alarm_type = VideoInterComAlarmType(alarm_info.byAlarmType)
        except ValueError:
            logger.debug("[{}] Type d'alarme inconnu ({}), ignoré", doorbell.name, alarm_info.byAlarmType)
            return

        if alarm_type == VideoInterComAlarmType.DOORBELL_RINGING:
            await self._on_call_state(doorbell, "ringing")
        elif alarm_type == VideoInterComAlarmType.DISMISS_INCOMING_CALL:
            await self._on_call_state(doorbell, "dismissed")
        # Tout le reste (sabotage, SOS, zone...) est hors périmètre : on ignore.

    async def _handle_event(self, doorbell: Doorbell, event_info: NET_DVR_VIDEO_INTERCOM_EVENT):
        try:
            event_type = VideoInterComEventType(event_info.byEventType)
        except ValueError:
            logger.debug("[{}] Type d'événement inconnu ({}), ignoré", doorbell.name, event_info.byEventType)
            return

        if event_type != VideoInterComEventType.UNLOCK_LOG:
            # authentication_log, plaques, cartes... hors périmètre.
            return

        record = event_info.uEventInfo.struUnlockRecord
        # wLockID est 0-based côté SDK, +1 pour retrouver le numéro affiché
        # dans les topics MQTT (Door-1-relay = relay 1), voir doorbell.py.
        relay_id = record.wLockID + 1

        try:
            unlock_type_name = UnlockType(record.byUnlockType).name
        except ValueError:
            unlock_type_name = "UNKNOWN"

        info = {
            "relay": relay_id,
            "unlock_type": unlock_type_name,
            "control_source": record.controlSource_decoded() or record.controlSource(),
            "card_user_id": record.dwCardUserID,
        }
        await self._on_door_unlocked(doorbell, relay_id, info)
