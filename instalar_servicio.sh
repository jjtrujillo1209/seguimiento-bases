#!/usr/bin/env bash
# Deja la app de seguimiento corriendo siempre (launchd) para que la sincronización diaria
# ocurra aunque no haya ninguna terminal abierta. La hora se configura con SYNC_HORA en .env.
#
# Uso:   bash instalar_servicio.sh            # instala y arranca
#        bash instalar_servicio.sh --quitar   # detiene y desinstala
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
ETIQUETA="com.platzi.seguimiento-bases"
PLIST="$HOME/Library/LaunchAgents/$ETIQUETA.plist"
LOG="$HOME/Library/Logs/platzi/$ETIQUETA.log"

launchctl bootout "gui/$(id -u)/$ETIQUETA" 2>/dev/null || true
if [ "${1:-}" = "--quitar" ]; then
  rm -f "$PLIST"
  echo "✔ Servicio detenido y desinstalado."
  exit 0
fi

# libera el puerto si la app quedó corriendo a mano
lsof -ti tcp:8680 | xargs kill 2>/dev/null || true
mkdir -p "$(dirname "$LOG")"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$ETIQUETA</string>
    <key>ProgramArguments</key>
    <array>
        <string>$DIR/.venv/bin/python</string>
        <string>-m</string><string>uvicorn</string><string>api:app</string>
        <string>--host</string><string>127.0.0.1</string>
        <string>--port</string><string>8680</string>
    </array>
    <key>WorkingDirectory</key><string>$DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>PYTHONPATH</key><string>$DIR</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>AbandonProcessGroup</key><true/>
    <key>StandardOutPath</key><string>$LOG</string>
    <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF

launchctl bootstrap "gui/$(id -u)" "$PLIST"
sleep 3
if curl -s http://127.0.0.1:8680/api/sincronizar >/dev/null; then
  echo "✔ App corriendo en http://127.0.0.1:8680 (se actualiza sola cada día). Log: $LOG"
else
  echo "⚠ No respondió todavía; revisa el log: $LOG"
fi
