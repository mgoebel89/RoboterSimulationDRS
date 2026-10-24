#!/bin/bash
# Startet eine Webots-Instanz ohne Bildschirm fuer einen Platz (Linux/Container).
#
#   start_platz.sh <platz> <irc-rechner> [welt.wbt]
#   start_platz.sh 07 R212-PC07
#
# Browser-Stream auf Port 1240 + Platznummer (Platz 07 -> 1247).
# Log: /var/log/rebel-sim/platz-07.log, Status: /run/rebel-sim/platz-07.json
set -euo pipefail

PLATZ="${1:?Platz fehlt, z.B. 07}"
IRC_HOST="${2:?iRC-Rechner fehlt, z.B. R212-PC07}"
WELT="${3:-${WELT:-rebel_tisch.wbt}}"
BASIS="$(cd "$(dirname "$0")/.." && pwd)"
STREAM_PORT="${STREAM_PORT:-$((1240 + 10#$PLATZ))}"

export IRC_HOST PLATZ
export IRC_PORTS="${IRC_PORTS:-3921,3920}"
export LOG_DIR="${LOG_DIR:-/var/log/rebel-sim}"
export STATUS_DIR="${STATUS_DIR:-/run/rebel-sim}"
mkdir -p "$LOG_DIR" "$STATUS_DIR"

echo "Platz $PLATZ: iRC $IRC_HOST (Ports $IRC_PORTS), Welt $WELT, Stream-Port $STREAM_PORT"
exec xvfb-run --auto-servernum webots --stream --port="$STREAM_PORT" \
     --batch --minimize --no-rendering --mode=realtime --stdout --stderr \
     "$BASIS/worlds/$WELT"
