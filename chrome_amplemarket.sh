#!/usr/bin/env bash
# Chrome dedicado (CDP) para leer los leads de las secuencias de Amplemarket.
#
# La API pública de Amplemarket no lista los leads de una secuencia: solo la UI.
# Este Chrome usa tu propia sesión, así que la app lee lo mismo que verías tú.
#
# Uso:
#   1. bash chrome_amplemarket.sh      # abre el Chrome dedicado
#   2. Inicia sesión en Amplemarket    # solo la primera vez; queda guardada
#   3. Deja esta ventana ABIERTA (puede estar minimizada) y sincroniza desde la app
set -euo pipefail

PORT="${AM_CDP_PORT:-9361}"
PROFILE_DIR="${AM_CHROME_PROFILE:-$(cd "$(dirname "$0")" && pwd)/datos/chrome-perfil-amplemarket}"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

mkdir -p "$PROFILE_DIR"

if curl -s "http://127.0.0.1:${PORT}/json/version" >/dev/null 2>&1; then
  echo "✔ Ya hay un Chrome con CDP en :${PORT} (no abro otro)."
  echo "  Verifica que estés logueado en https://app.amplemarket.com"
  exit 0
fi

if [ ! -x "$CHROME" ]; then
  echo "✖ No encuentro Google Chrome en: $CHROME" >&2
  exit 1
fi

echo "▶ Abriendo Chrome dedicado para Amplemarket (CDP :${PORT})"
echo "  Perfil: $PROFILE_DIR"
echo "  → Inicia sesión en Amplemarket y DEJA esta ventana abierta."

"$CHROME" \
  --remote-debugging-port="${PORT}" \
  --user-data-dir="$PROFILE_DIR" \
  --no-first-run --no-default-browser-check \
  --disable-features=HighEfficiencyModeAvailable,IntensiveWakeUpThrottling \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding \
  --disable-background-timer-throttling \
  "https://app.amplemarket.com/dashboard/sequences" \
  >/dev/null 2>&1 &

for _ in $(seq 1 15); do
  sleep 1
  if curl -s "http://127.0.0.1:${PORT}/json/version" >/dev/null 2>&1; then
    echo "✔ CDP activo en http://127.0.0.1:${PORT}"
    exit 0
  fi
done
echo "⚠ El CDP no respondió; revisa que Chrome haya abierto."
exit 1
