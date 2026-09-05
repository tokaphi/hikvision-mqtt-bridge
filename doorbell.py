"""
doorbell.py - Classe Doorbell épurée.

Ne garde que ce qui sert au pont MQTT minimal :
  - authentification SDK + armement du canal d'alarme
  - pilotage des relais en impulsion, avec durée PARAMÉTRABLE par relais
  - reboot à distance
  - lecture du call-state par ISAPI (utilisé en fallback polling par bridge.py)

Tout ce qui concerne l'audio bidirectionnel, le snapshot, les scènes, le
backlight, les boutons d'appel (answer/hangup/reject) a été retiré : ce
n'est pas utilisé dans ce pont, ça vivait dans l'add-on complet si besoin.
"""

import asyncio
import json
from ctypes import CDLL, byref, cast, c_byte, c_char_p, sizeof
from enum import Enum
from typing import Optional
from loguru import logger

from sdk.hcnetsdk import (
    NET_DVR_DEVICEINFO_V30,
    NET_DVR_SETUPALARM_PARAM_V50,
    NET_DVR_CONTROL_GATEWAY,
)
from sdk.utils import SDKError, call_ISAPI


class DoorbellType(str, Enum):
    """Type de poste : détermine comment les relais sont pilotés.
    - OUTDOOR : portier extérieur (Portier-Ext, Portier-garage). Les relais
      "Door" sont des gâches électriques : on envoie une commande d'OUVERTURE,
      la fermeture est mécanique (le SDK n'a pas de commande de fermeture pour ça).
    - INDOOR : poste intérieur (Portier-Int). Les sorties "Com" sont des relais
      généralistes pilotés en ISAPI, qui supportent ouverture ET fermeture
      explicites (utile par ex. pour piloter un portail/lumière plus longtemps)."""
    OUTDOOR = "outdoor"
    INDOOR = "indoor"


class Doorbell:
    """
    Représente la connexion à un portier ou poste intérieur Hikvision.

    Args:
        name: nom utilisé dans les topics MQTT (ex: "Portier-Ext")
        ip, port, username, password: identifiants de connexion au device
        sdk: handle CDLL du SDK déjà chargé (loadSDK())
        device_type: OUTDOOR ou INDOOR, détermine le pilotage des relais
        relay_pulse: durées d'impulsion spécifiques par relais, ex: {1: 3.0, 2: 5.0}
        default_pulse: durée utilisée pour un relais non listé dans relay_pulse (secondes)
    """

    def __init__(
        self,
        name: str,
        ip: str,
        username: str,
        password: str,
        sdk: CDLL,
        port: int = 8000,
        device_type: DoorbellType = DoorbellType.OUTDOOR,
        relay_pulse: Optional[dict[int, float]] = None,
        default_pulse: float = 3.0,
    ):
        self.name = name
        self.ip = ip
        self.port = port
        self.username = username
        self.password = password
        self.device_type = device_type
        self._sdk = sdk
        self._relay_pulse: dict[int, float] = dict(relay_pulse or {})
        self._default_pulse = default_pulse

        self.user_id: int = -1
        self._device_info = NET_DVR_DEVICEINFO_V30()

    def __repr__(self) -> str:
        return f"Doorbell({self.name}, {self.ip}, type={self.device_type.value})"

    # ------------------------------------------------------------------
    # Connexion / cycle de vie
    # ------------------------------------------------------------------

    def authenticate(self):
        """Login SDK sur le device. Lève SDKError si les identifiants sont
        refusés ou si le device est injoignable."""
        logger.debug("[{}] Connexion à {}:{}", self.name, self.ip, self.port)
        self.user_id = self._sdk.NET_DVR_Login_V30(
            bytes(self.ip, "utf8"),
            self.port,
            bytes(self.username, "utf8"),
            bytes(self.password, "utf8"),
            self._device_info,
        )
        if self.user_id < 0:
            raise SDKError(self._sdk, f"Échec de connexion à {self.name}")
        logger.info("[{}] Connecté (user_id={})", self.name, self.user_id)

    def setup_alarm(self):
        """Arme le canal d'alarme du device : c'est ce qui permet de recevoir
        les événements (sonnette, déverrouillage...) via le callback SDK dans
        bridge.py. Doit être appelé après authenticate()."""
        alarm_param = NET_DVR_SETUPALARM_PARAM_V50()
        alarm_param.dwSize = sizeof(NET_DVR_SETUPALARM_PARAM_V50)
        alarm_param.byLevel = 1
        alarm_param.byAlarmInfoType = 1
        alarm_param.byDeployType = 1
        # Bit 1 à 0 : on demande au device de NE PAS renvoyer tout l'historique
        # d'événements en backlog au premier armement (sinon inondation de faux
        # événements pendant plusieurs heures, cf. changelog upstream 3.0.33).
        alarm_param.bySupport = alarm_param.bySupport & ~0x02

        handle = self._sdk.NET_DVR_SetupAlarmChan_V50(self.user_id, alarm_param, None, 0)
        if handle < 0:
            raise SDKError(self._sdk, f"Échec de l'armement de l'alarme sur {self.name}")
        logger.debug("[{}] Canal d'alarme armé (handle={})", self.name, handle)

    def logout(self):
        if self.user_id >= 0:
            self._sdk.NET_DVR_Logout_V30(self.user_id)
            logger.debug("[{}] Déconnecté", self.name)
            self.user_id = -1

    # ------------------------------------------------------------------
    # Paramétrage de la durée d'impulsion des relais
    # ------------------------------------------------------------------

    def get_pulse_duration(self, relay_id: int) -> float:
        """
        Fonction dédiée pour récupérer la durée d'impulsion (en secondes) d'un
        relais donné. Centraliser la logique ici (plutôt qu'une constante en
        dur dispersée dans le code) permet de la faire évoluer facilement :
        valeur par relais dans la config, override ponctuel via MQTT
        (voir bridge.py), etc.

        Priorité : durée spécifique à ce relais > durée par défaut du portier.
        """
        return self._relay_pulse.get(relay_id, self._default_pulse)

    def set_pulse_duration(self, relay_id: int, seconds: float):
        """Change la durée d'impulsion d'un relais à chaud (ex: appelée par
        bridge.py suite à une commande MQTT de configuration)."""
        if seconds <= 0:
            raise ValueError("La durée d'impulsion doit être positive")
        logger.info("[{}] Durée d'impulsion du relais {} réglée à {}s", self.name, relay_id, seconds)
        self._relay_pulse[relay_id] = seconds

    # ------------------------------------------------------------------
    # Pilotage des relais
    # ------------------------------------------------------------------

    async def pulse_relay(self, relay_id: int, duration: Optional[float] = None) -> float:
        """
        Ouvre le relais `relay_id` (1 ou 2) puis attend `duration` secondes
        (celle configurée via get_pulse_duration si non précisée) avant de
        considérer le cycle terminé.

        Retourne la durée effectivement utilisée, pour que bridge.py sache
        combien de temps attendre avant de republier l'état MQTT à OFF.

        Ne lève pas d'exception si la commande échoue au niveau de l'appareil
        (log en warning) : mieux vaut que le bridge continue de tourner
        plutôt que de planter pour une porte qui n'a pas voulu s'ouvrir.
        """
        pulse_duration = duration if duration is not None else self.get_pulse_duration(relay_id)

        # Les appels SDK/ISAPI sont bloquants (I/O réseau synchrone) : on les
        # exécute dans un thread pour ne pas geler la boucle asyncio pendant
        # ce temps (sinon plus aucun autre événement/commande MQTT ne serait
        # traité pendant l'appel).
        try:
            if self.device_type == DoorbellType.OUTDOOR:
                await asyncio.to_thread(self._open_door_relay, relay_id)
            else:
                await asyncio.to_thread(self._open_com_relay, relay_id)
            logger.info("[{}] Relais {} ouvert ({}s)", self.name, relay_id, pulse_duration)
        except SDKError as err:
            logger.error("[{}] Échec ouverture relais {}: {}", self.name, relay_id, err)

        await asyncio.sleep(pulse_duration)

        if self.device_type == DoorbellType.INDOOR:
            # Les sorties "Com" savent se refermer explicitement, on le fait.
            try:
                await asyncio.to_thread(self._close_com_relay, relay_id)
            except SDKError as err:
                logger.error("[{}] Échec fermeture relais {}: {}", self.name, relay_id, err)
        # Pour un portier extérieur, la gâche se referme mécaniquement :
        # on ne fait rien côté device, seul l'état MQTT repasse à OFF (bridge.py).

        logger.debug("[{}] Fin d'impulsion relais {}", self.name, relay_id)
        return pulse_duration

    def _open_door_relay(self, relay_id: int):
        """Ouvre une gâche de portier extérieur.

        ATTENTION à la numérotation (vérifié contre le code d'origine) :
        - Le topic MQTT / l'API publique de ce module utilisent une
          numérotation 1-based ("Door-1-relay" = relay_id=1).
        - Le SDK (wLockID de NET_DVR_CONTROL_GATEWAY) attend lui une
          numérotation 0-based.
        - L'ISAPI de secours (/ISAPI/AccessControl/RemoteControl/door/N) attend
          en revanche du 1-based.
        D'où la conversion `relay_id - 1` uniquement pour le SDK ci-dessous.

        Essaie d'abord NET_DVR_RemoteControl (commande 16009), puis bascule
        sur ISAPI si le SDK échoue (cf. issue #83 de l'add-on d'origine :
        certains modèles ne supportent que l'une des deux méthodes)."""
        gw = NET_DVR_CONTROL_GATEWAY()
        gw.dwSize = sizeof(NET_DVR_CONTROL_GATEWAY)
        gw.dwGatewayIndex = 1
        gw.byCommand = 1  # commande d'ouverture
        gw.byLockType = 0  # gâche normale (pas une serrure "smart lock")
        gw.wLockID = relay_id - 1  # 0-based côté SDK
        gw.byControlSrc = (c_byte * 32)(*[97, 98, 99, 100])  # requis non-vide, valeur arbitraire
        gw.byControlType = 1

        result = self._sdk.NET_DVR_RemoteControl(self.user_id, 16009, byref(gw), gw.dwSize)
        if result:
            return

        logger.debug("[{}] NET_DVR_RemoteControl a échoué (code {}), fallback ISAPI",
                     self.name, self._sdk.NET_DVR_GetLastError())
        url = f"/ISAPI/AccessControl/RemoteControl/door/{relay_id}"  # 1-based côté ISAPI
        self._call_isapi("PUT", url, "<RemoteControlDoor><cmd>open</cmd></RemoteControlDoor>")

    def _open_com_relay(self, relay_id: int):
        """Ouvre une sortie 'Com' de poste intérieur via ISAPI.
        Ici l'URL ISAPI est 0-based (vérifié contre le code d'origine, contrairement
        à la porte d'accès ci-dessus) : Com-1-relay (relay_id=1) -> .../outputs/0"""
        url = f"/ISAPI/SecurityCP/control/outputs/{relay_id - 1}?format=json"
        self._call_isapi("PUT", url, json.dumps({"OutputsCtrl": {"switch": "open"}}))

    def _close_com_relay(self, relay_id: int):
        """Referme explicitement une sortie 'Com' de poste intérieur via ISAPI (0-based, voir ci-dessus)."""
        url = f"/ISAPI/SecurityCP/control/outputs/{relay_id - 1}?format=json"
        self._call_isapi("PUT", url, json.dumps({"OutputsCtrl": {"switch": "close"}}))

    # ------------------------------------------------------------------
    # Divers : reboot, call-state (pour le polling de secours dans bridge.py)
    # ------------------------------------------------------------------

    def reboot_device(self):
        """Redémarre le portier. Le SDK renvoie systématiquement une erreur
        de timeout (code 10) puisque le device coupe la connexion avant de
        répondre : on l'ignore volontairement, toute autre erreur est levée."""
        try:
            self._call_isapi("PUT", "/ISAPI/System/reboot")
        except SDKError as err:
            error_code = err.args[1]
            if error_code != 10:  # NET_DVR_NETWORK_RECV_TIMEOUT
                raise
        logger.info("[{}] Commande de reboot envoyée", self.name)

    def get_call_status(self) -> Optional[str]:
        """Lit l'état d'appel courant via ISAPI (idle/ringing/dismissed...).
        Utilisé par bridge.py en polling pour les firmwares qui n'envoient
        plus l'événement de sonnerie nativement (>= 3.7.x)."""
        try:
            response = self._call_isapi("GET", "/ISAPI/VideoIntercom/callStatus?format=json")
            data = json.loads(response)
            return data.get("CallStatus", {}).get("status")
        except (SDKError, json.JSONDecodeError) as err:
            logger.debug("[{}] Impossible de lire le call-state: {}", self.name, err)
            return None

    def _call_isapi(self, http_method: str, url: str, request_body: str = "") -> str:
        """Appelle un endpoint ISAPI via le SDK et retourne la réponse texte brute."""
        output = call_ISAPI(self._sdk, self.user_id, http_method, url, request_body)
        output_char_p = cast(output.lpOutBuffer, c_char_p)
        return output_char_p.value.decode("utf-8", errors="replace") if output_char_p.value else ""


class Registry(dict[str, Doorbell]):
    """Registre des portiers connectés, indexé par leur nom (plus simple à
    déboguer qu'un index numérique : les logs et les topics MQTT parlent
    directement le même langage)."""

    def get_by_serial(self, serial: str) -> Optional[Doorbell]:
        for doorbell in self.values():
            if serial and serial in doorbell._device_info.serialNumber():
                return doorbell
        return None
