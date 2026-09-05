#!/bin/sh
# entrypoint.sh - Détecte l'architecture au démarrage et positionne
# LD_LIBRARY_PATH sur le bon dossier de libs Hikvision (lib-amd64 ou
# lib-aarch64). Fait exprès de ne PAS mettre les deux dans le path en même
# temps : sur une architecture donnée, le linker dynamique qui tomberait sur
# un .so de l'autre architecture planterait au lieu d'essayer le suivant.

set -e

ARCH="$(uname -m)"
case "$ARCH" in
    x86_64)
        LIBDIR="/app/lib-amd64"
        ;;
    aarch64)
        LIBDIR="/app/lib-aarch64"
        ;;
    *)
        echo "Architecture non supportée par le SDK Hikvision: $ARCH" >&2
        exit 1
        ;;
esac

export LD_LIBRARY_PATH="${LIBDIR}:${LIBDIR}/HCNetSDKCom:${LD_LIBRARY_PATH}"
echo "Architecture détectée: $ARCH -> LD_LIBRARY_PATH=$LD_LIBRARY_PATH"

exec python main.py
