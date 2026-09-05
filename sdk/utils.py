"""
sdk/utils.py - Fonctions bas niveau pour charger et piloter le SDK Hikvision (HCNetSDK).

Ce fichier est repris quasiment à l'identique de l'add-on d'origine : ce n'est PAS
la partie "usine à gaz". C'est le moteur qui parle réellement au .so Hikvision,
il est stable et il n'y a pas de raison de le réinventer.

Seuls changements par rapport à l'original :
  - Suppression des imports/structures liées à des fonctionnalités qu'on ne garde
    pas (scènes, JPEG, appel vidéo) -> voir sdk/hcnetsdk.py qui reste complet
    (les structs ne coûtent rien à garder, seule la logique métier a été élaguée).
  - Commentaires en français.
"""

from ctypes import CDLL, POINTER, c_char, c_char_p, c_int, c_long, c_void_p, cast, cdll, sizeof
from enum import IntEnum
import os
import platform
from typing import Optional, TypedDict
from loguru import logger
from sdk.hcnetsdk import (
    DWORD, LONG, NET_DVR_SETUPALARM_PARAM_V50, NET_DVR_XML_CONFIG_INPUT,
    NET_DVR_XML_CONFIG_OUTPUT, WORD, NET_DVR_DEVICEINFO_V30, fMessageCallBack,
)


class SDKLogLevel(IntEnum):
    """Niveau de verbosité des logs internes du SDK Hikvision (pas ceux de notre app)."""
    NONE = 0
    ERROR = 1
    INFO = 2
    DEBUG = 3


class SDKConfig(TypedDict):
    """Configuration passée à setupSDK().

    Attributes:
        log_level: verbosité du SDK (par défaut NONE, à monter en DEBUG uniquement
                   pour du dépannage ponctuel : ça écrit beaucoup sur le disque).
        log_dir: dossier où le SDK écrit ses fichiers de logs.
    """
    log_level: SDKLogLevel
    log_dir: str


def loadSDK() -> CDLL:
    """
    Charge la librairie native Hikvision (.so) correspondant à l'architecture courante
    et retourne le handle ctypes. Il faut appeler setupSDK() ensuite avant de l'utiliser.
    """
    logger.info("OS détecté : {} / architecture : {}", platform.uname()[0], platform.uname()[4])

    if platform.uname()[0] != "Linux":
        raise RuntimeError("Cette version simplifiée ne cible que Linux (conteneur Docker)")

    if platform.uname()[4] == "x86_64":
        hcnetsdk_path = os.path.join("lib-amd64", "libhcnetsdk.so")
    elif platform.uname()[4] == "aarch64":
        hcnetsdk_path = os.path.join("lib-aarch64", "libhcnetsdk.so")
    else:
        raise RuntimeError(f"Architecture non supportée: {platform.uname()[4]}")

    logger.debug("Chargement de la librairie: {}", hcnetsdk_path)
    lib = cdll.LoadLibrary(hcnetsdk_path)
    _setup_function_types(lib)
    return lib


def _setup_function_types(lib: CDLL):
    """Déclare les types d'arguments ctypes des fonctions C utilisées.
    Ça permet à ctypes de râler tout de suite si on se trompe de type,
    plutôt que de planter le process avec un segfault silencieux."""
    lib.NET_DVR_Login_V30.argtypes = [c_char_p, WORD, c_char_p, c_char_p, POINTER(NET_DVR_DEVICEINFO_V30)]
    lib.NET_DVR_Logout_V30.argtypes = [c_int]
    lib.NET_DVR_GetErrorMsg.argtypes = [POINTER(c_long)]
    lib.NET_DVR_SetDVRMessageCallBack_V50.argtypes = [c_int, fMessageCallBack, c_void_p]
    lib.NET_DVR_SetupAlarmChan_V50.argtypes = [LONG, NET_DVR_SETUPALARM_PARAM_V50, c_char_p, DWORD]
    lib.NET_DVR_RemoteControl.argtypes = [LONG, DWORD, c_void_p, DWORD]
    lib.NET_DVR_STDXMLConfig.argtypes = [LONG, POINTER(NET_DVR_XML_CONFIG_INPUT), POINTER(NET_DVR_XML_CONFIG_OUTPUT)]

    lib.NET_DVR_GetErrorMsg.restype = c_char_p


def setupSDK(sdk: CDLL, config: Optional[SDKConfig] = None):
    """Initialise le SDK. À appeler une seule fois au démarrage, avant tout login.
    Ne pas oublier shutdownSDK() en sortie pour libérer les ressources proprement."""
    logger.debug("Initialisation du SDK")
    if not sdk.NET_DVR_Init():
        raise RuntimeError("Impossible d'initialiser le SDK (NET_DVR_Init a échoué)")

    if config:
        result = sdk.NET_DVR_SetLogToFile(config["log_level"].value, bytes(config["log_dir"], "utf8"), False)
        if not result:
            logger.warning("Impossible de configurer les logs du SDK (résultat: {})", result)

    if not sdk.NET_DVR_SetValidIP(0, True):
        logger.warning("NET_DVR_SetValidIP a retourné une erreur (non bloquant)")

    logger.debug("SDK initialisé")


def shutdownSDK(sdk: CDLL):
    """Libère les ressources du SDK. À appeler à l'arrêt propre du service."""
    logger.debug("Arrêt du SDK")
    sdk.NET_DVR_Cleanup()


def call_ISAPI(sdk: CDLL, user_id: int, http_method: str, url: str, requestBody: str = "") -> NET_DVR_XML_CONFIG_OUTPUT:
    """
    Appelle un endpoint ISAPI en le faisant transiter par le SDK (NET_DVR_STDXMLConfig),
    au lieu d'un appel HTTP direct. C'est indispensable pour les postes intérieurs qui
    n'exposent pas le port 80 : le SDK, lui, y a accès via son propre canal.

    Args:
        sdk: handle CDLL du SDK chargé
        user_id: identifiant retourné par NET_DVR_Login_V30
        http_method: GET, PUT, POST...
        url: chemin ISAPI, doit commencer par /ISAPI
        requestBody: corps de la requête (XML ou JSON selon l'endpoint), optionnel

    Returns:
        La struct NET_DVR_XML_CONFIG_OUTPUT contenant la réponse brute.
    """
    inUrl = f"{http_method} {url}"
    logger.debug("Appel ISAPI: {} {} body={}", http_method, url, requestBody)

    inputStruct = NET_DVR_XML_CONFIG_INPUT()
    urlSize = (c_char * 256)()

    requestUrlBuffer = bytes(inUrl, "ascii")
    inputStruct.lpRequestUrl = cast(c_char_p(requestUrlBuffer), c_void_p)
    inputStruct.dwRequestUrlLen = len(urlSize)

    inputBuffer = bytes(requestBody, "ascii")
    inputStruct.lpInBuffer = cast(c_char_p(inputBuffer), c_void_p)
    inputStruct.dwInBufferSize = len(inputBuffer)
    inputStruct.dwSize = sizeof(inputStruct)

    outputStruct = NET_DVR_XML_CONFIG_OUTPUT()
    outputBufferSize = 1024 * 1024
    responseStatusBuffer = (c_char * outputBufferSize)()
    outputStruct.lpStatusBuffer = cast(responseStatusBuffer, c_void_p)
    outputStruct.dwStatusSize = outputBufferSize

    outputSize = 1024 * 1024
    outputBuffer = (c_char * outputSize)()
    outputStruct.lpOutBuffer = cast(outputBuffer, c_void_p)
    outputStruct.dwOutBufferSize = outputSize
    outputStruct.dwSize = sizeof(outputStruct)

    result = sdk.NET_DVR_STDXMLConfig(user_id, inputStruct, outputStruct)

    if not result:
        logger.debug("Statut de la réponse en erreur: {}", responseStatusBuffer.value.decode("utf-8", errors="replace"))
        raise SDKError(sdk, f"Erreur lors de l'appel ISAPI {url}")

    logger.debug("Réponse ISAPI reçue ({} octets)", len(outputBuffer.value))
    return outputStruct


class SDKError(RuntimeError):
    """
    Exception levée pour toute erreur remontée par le SDK Hikvision.
    Le code et le message d'erreur natifs sont automatiquement récupérés
    et stockés dans `.args` : (message_utilisateur, code_erreur, message_sdk).
    """
    def __init__(self, sdk: CDLL, user_message: str, *args: object) -> None:
        super().__init__(*args)
        error_code = sdk.NET_DVR_GetLastError()
        error_message: str = sdk.NET_DVR_GetErrorMsg(c_long(error_code)).decode("utf-8", errors="replace")
        self.args = (user_message, error_code, error_message, *self.args)
