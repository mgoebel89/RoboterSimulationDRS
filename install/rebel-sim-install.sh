#!/usr/bin/env bash
# ReBeL-Simulation: Installation UND Update im Container (Ubuntu 24.04, als root).
# Kann beliebig oft laufen; Welten, Teile, Konten, Einstellungen und das Robotermodell bleiben erhalten.
#
#   Neu (im Container):  bash -c "$(curl -fsSL https://raw.githubusercontent.com/mgoebel89/RoboterSimulationDRS/main/install/rebel-sim-install.sh)"
#   Update:              rebel-sim-update        (ruft dieses Skript aus /opt/rebel-sim auf)
#
# Umgebungsvariablen: REPO (Git-Adresse), ZWEIG (Standard main), BASIS (Standard /opt/rebel-sim)
set -euo pipefail

REPO="${REPO:-https://github.com/mgoebel89/RoboterSimulationDRS.git}"
ZWEIG="${ZWEIG:-main}"
BASIS="${BASIS:-/opt/rebel-sim}"
WEBOTS_DEB="https://github.com/cyberbotics/webots/releases/download/R2025a/webots_2025a_amd64.deb"
IRC_DATEN=/var/lib/rebel-sim/irc

gruen() { printf '\033[1;32m%s\033[0m\n' "$*"; }
gelb() { printf '\033[1;33m%s\033[0m\n' "$*"; }
schritt() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

[ "$(id -u)" = 0 ] || { echo "Bitte als root ausfuehren."; exit 1; }
export DEBIAN_FRONTEND=noninteractive

schritt "Pakete (Xvfb, Python, gmsh, gRPC, Git)"
apt-get update -q
apt-get install -y -q git curl ca-certificates sudo rsync xvfb tzdata \
  python3 python3-gmsh python3-grpcio >/dev/null
timedatectl set-timezone Europe/Berlin 2>/dev/null || ln -sf /usr/share/zoneinfo/Europe/Berlin /etc/localtime

schritt "Webots R2025a"
if dpkg -s webots 2>/dev/null | grep -q '^Version: 2025a'; then
  echo "Webots R2025a ist installiert."
else
  tmp=$(mktemp -d)
  curl -fL -o "$tmp/webots.deb" "$WEBOTS_DEB"
  apt-get install -y -q "$tmp/webots.deb" >/dev/null
  rm -rf "$tmp"
  apt-mark hold webots >/dev/null   # Welten und Browser-Ansicht sind auf R2025a abgestimmt
fi

schritt "Programm aus $REPO ($ZWEIG)"
id rebelsim >/dev/null 2>&1 || useradd -m -s /bin/bash rebelsim
if [ -d "$BASIS/.git" ]; then
  git config --global --add safe.directory "$BASIS"
  git -C "$BASIS" fetch -q origin "$ZWEIG"
  git -C "$BASIS" reset -q --hard "origin/$ZWEIG"   # nur Programmdateien; Welten, Teile usw. sind nicht im Repository
elif [ -d "$BASIS" ]; then
  # Umstellung einer Installation ohne Git: Programm neu holen, Laufzeitdaten uebernehmen
  alt="$BASIS.vor-git-$(date +%Y%m%d-%H%M%S)"
  gelb "Vorhandene Installation ohne Git wird nach $alt verschoben, Laufzeitdaten werden uebernommen."
  mv "$BASIS" "$alt"
  git clone -q --branch "$ZWEIG" "$REPO" "$BASIS"
  for d in worlds teile werkzeuge protos/meshes; do
    [ -d "$alt/$d" ] && mkdir -p "$BASIS/$d" && rsync -a "$alt/$d/" "$BASIS/$d/"
  done
  [ -f "$alt/protos/IgusRebel6DOF.proto" ] && cp -p "$alt/protos/IgusRebel6DOF.proto" "$BASIS/protos/"
else
  git clone -q --branch "$ZWEIG" "$REPO" "$BASIS"
fi
git config --global --add safe.directory "$BASIS"
echo "Stand: $(git -C "$BASIS" log -1 --format='%h %cd %s' --date=format:'%d.%m.%Y %H:%M')"

schritt "Dienste, Benutzer und Rechte"
bash "$BASIS/deploy/install.sh" >/dev/null
install -m 755 "$BASIS/install/rebel-sim-update" /usr/local/sbin/rebel-sim-update
install -m 755 "$BASIS/install/rebel-sim-modell" /usr/local/sbin/rebel-sim-modell
install -m 755 "$BASIS/install/rebel-sim-admin" /usr/local/sbin/rebel-sim-admin

schritt "Beispielwelten (nur fehlende)"
mkdir -p "$BASIS/worlds" "$BASIS/teile"
for f in "$BASIS"/vorlagen/welten/*; do
  ziel="$BASIS/worlds/$(basename "$f")"
  [ -e "$ziel" ] || { cp "$f" "$ziel"; echo "  neu: $(basename "$f")"; }
done
chown -R rebelsim:rebelsim "$BASIS"

schritt "Teilebibliothek (nur fehlende, gmsh rechnet je Teil einige Sekunden)"
python3 - "$BASIS" <<'PY' | while IFS='|' read -r name step koerper; do
import json, sys
basis = sys.argv[1]
for t in json.load(open(basis + "/vorlagen/teile.json"))["teile"]:
    print("%s|%s|%s" % (t["name"], t["step"], t.get("koerper", "")))
PY
  if [ -f "$BASIS/teile/$name/teil.json" ]; then continue; fi
  echo "  lese ein: $name"
  sudo -u rebelsim python3 "$BASIS/tools/teile.py" einlesen "$BASIS/$step" "$name" --teile "$BASIS/teile" \
    ${koerper:+--koerper "$koerper"} </dev/null >/dev/null || gelb "  $name konnte nicht eingelesen werden"
done

schritt "Robotermodell (aus iRC-Daten, nicht im Repository)"
if [ -f "$BASIS/protos/IgusRebel6DOF.proto" ] && [ -d "$IRC_DATEN/Data" ]; then
  # neu erzeugen, damit Aenderungen am Generator (z.B. neue Backensaetze) ankommen
  sudo -u rebelsim python3 "$BASIS/tools/erzeuge_rebel_modell.py" --irc "$IRC_DATEN" --robot REBEL-6DOF-03 >/dev/null \
    && echo "Modell neu erzeugt." || gelb "Modell konnte nicht neu erzeugt werden, das alte bleibt."
elif [ -f "$BASIS/protos/IgusRebel6DOF.proto" ]; then
  echo "Modell vorhanden (ohne gespeicherte iRC-Daten, wird nicht neu erzeugt)."
else
  gelb "Noch kein Robotermodell. Auf einem Windows-PC mit iRC tools/irc_daten_export.ps1 ausfuehren,"
  gelb "die Datei irc-daten.zip in den Container kopieren und dann: rebel-sim-modell /root/irc-daten.zip"
fi

schritt "Neustart der Webseite"
systemctl restart rebel-sim-web
ip=$(hostname -I | awk '{print $1}')
gruen "Fertig. Schueler: http://$ip:8080   Lehrkraefte: http://$ip:8080/lehrer"
[ -s /var/lib/rebel-sim/benutzer.json ] || gelb "Noch kein Lehrerkonto: rebel-sim-admin <name>   (fragt nach dem Passwort)"
echo "Laufende Simulationen nutzen neuen Programmcode erst nach 'Neu starten'."
