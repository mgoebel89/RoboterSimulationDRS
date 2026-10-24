#!/bin/bash
# Plaetze verwalten (als root ausfuehren). install.sh legt eine Kopie als
# /usr/local/sbin/rebel-sim-platz ab; die darf der Simulationsverwalter per sudo
# aufrufen. Deshalb werden alle Angaben streng geprueft.
#
#   platz.sh anlegen 07 R212-PC07 [welt.wbt] [dauerhaft]   Platz anlegen bzw. aendern und starten
#   platz.sh neustart 07                                   Simulation zuruecksetzen (Szene neu laden)
#   platz.sh entfernen 07                                  Platz stoppen und loeschen
#   platz.sh liste                                         alle Plaetze mit Zustand
#
# "dauerhaft": Platz startet auch nach einem Neustart des Containers und wird vom
# Verwalter nie automatisch beendet (z.B. Zwilling des echten ReBeL fuer den Beamer).
set -euo pipefail
CONF=/etc/rebel-sim
WELTEN=/opt/rebel-sim/worlds

# Platznummer immer zweistellig: 7 -> 07 (Dateinamen und Dienstname passen dann zusammen)
if [ -n "${2:-}" ] && [[ "$2" =~ ^[0-9]{1,2}$ ]]; then
  set -- "$1" "$(printf '%02d' $((10#$2)))" "${@:3}"
fi
pruefe_platz() { [[ "${1:-}" =~ ^[0-9]{2}$ ]] || { echo "Platz muss eine Zahl 1-99 sein" >&2; exit 1; }; }

case "${1:-liste}" in
  anlegen)
    PLATZ="${2:-}"; HOST="${3:-}"; WELT="${4:-rebel_tisch.wbt}"; DAUERHAFT="${5:-}"
    pruefe_platz "$PLATZ"
    [[ "$HOST" =~ ^[A-Za-z0-9.:-]{1,64}$ ]] || { echo "iRC-Rechner ungueltig: $HOST" >&2; exit 1; }
    [[ "$WELT" =~ ^[A-Za-z0-9_-]{1,60}\.wbt$ ]] || { echo "Weltname ungueltig: $WELT" >&2; exit 1; }
    [ -f "$WELTEN/$WELT" ] || { echo "Welt nicht gefunden: $WELT" >&2; exit 1; }
    [ -z "$DAUERHAFT" ] || [ "$DAUERHAFT" = dauerhaft ] || { echo "5. Angabe nur 'dauerhaft'" >&2; exit 1; }
    mkdir -p "$CONF"
    printf 'IRC_HOST=%s\nWELT=%s\nGESTARTET=%s\nDAUERHAFT=%s\n' \
      "$HOST" "$WELT" "$(date +%s)" "$([ -n "$DAUERHAFT" ] && echo 1 || echo 0)" > "$CONF/$PLATZ.env"
    if [ -n "$DAUERHAFT" ]; then
      systemctl enable "rebel-sim@$PLATZ" >/dev/null 2>&1
    else
      systemctl disable "rebel-sim@$PLATZ" >/dev/null 2>&1 || true
    fi
    systemctl restart "rebel-sim@$PLATZ"
    echo "Platz $PLATZ -> $HOST ($WELT), Stream-Port $((1240 + 10#$PLATZ))${DAUERHAFT:+, dauerhaft}"
    ;;
  neustart)
    pruefe_platz "${2:-}"
    [ -f "$CONF/$2.env" ] || { echo "Platz $2 ist nicht angelegt" >&2; exit 1; }
    systemctl restart "rebel-sim@$2"
    ;;
  entfernen)
    PLATZ="${2:-}"
    pruefe_platz "$PLATZ"
    systemctl disable --now "rebel-sim@$PLATZ" >/dev/null 2>&1 || true
    rm -f "$CONF/$PLATZ.env" "/run/rebel-sim/platz-$PLATZ.json"
    echo "Platz $PLATZ entfernt"
    ;;
  liste)
    for f in "$CONF"/*.env; do
      [ -e "$f" ] || { echo "keine Plaetze angelegt"; break; }
      p=$(basename "$f" .env)
      printf '%-4s %-10s %-16s %s\n' "$p" "$(systemctl is-active "rebel-sim@$p")" \
        "$(grep -h '^IRC_HOST=' "$f" | cut -d= -f2)" "$(grep -h '^WELT=' "$f" | cut -d= -f2)"
    done
    ;;
  *)
    sed -n '2,13p' "$0"; exit 1
    ;;
esac
