#!/bin/bash
# Erzeugt das Vorschaubild einer Welt fuer die Weltenliste (Linux/Container).
#
#   vorschau.sh rebel_tisch.wbt      -> worlds/rebel_tisch.jpg
#
# Webots startet kurz mit Bild (ohne --no-rendering), der Controller speichert
# die Ansicht aus dem Viewpoint der Welt und beendet Webots wieder.
set -euo pipefail
WELT="${1:?Welt fehlt, z.B. rebel_tisch.wbt}"
BASIS="$(cd "$(dirname "$0")/.." && pwd)"
BILD="$BASIS/worlds/${WELT%.wbt}.jpg"
[ -f "$BASIS/worlds/$WELT" ] || { echo "Welt nicht gefunden: $WELT"; exit 1; }

export VORSCHAU_BILD="$BILD" IRC_HOST=127.0.0.1 IRC_PORTS=1 LOG_DIR="$(mktemp -d)"
timeout 120 xvfb-run --auto-servernum -s "-screen 0 1024x768x24" \
  webots --batch --mode=realtime --stdout --stderr "$BASIS/worlds/$WELT" >/dev/null 2>&1 || true
rm -rf "$LOG_DIR"
[ -s "$BILD" ] && echo "Vorschaubild: $BILD" || { echo "Kein Bild entstanden"; exit 1; }
