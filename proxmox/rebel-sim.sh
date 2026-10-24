#!/usr/bin/env bash
# ReBeL-Simulation fuer Proxmox VE: Container anlegen und installieren, oder vorhandenen aktualisieren.
# Auf dem Proxmox-Host (Shell des Knotens) als root ausfuehren:
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/mgoebel89/RoboterSimulationDRS/main/proxmox/rebel-sim.sh)"
#
# Legt einen unprivilegierten LXC mit Ubuntu 24.04 an (nesting=1, Start beim Booten) und ruft darin
# install/rebel-sim-install.sh auf. Richtwerte: 4 Kerne, 6 GB RAM fuer 10 gleichzeitige Simulationen, 20 GB Platte.
set -euo pipefail

ZWEIG="${ZWEIG:-main}"
ROH="https://raw.githubusercontent.com/mgoebel89/RoboterSimulationDRS/$ZWEIG"
TITEL="ReBeL-Simulation (DRS)"

command -v pct >/dev/null || { echo "Dieses Skript laeuft nur auf einem Proxmox-VE-Host."; exit 1; }
[ "$(id -u)" = 0 ] || { echo "Bitte als root ausfuehren."; exit 1; }

frage() {   # frage <Text> <Vorgabe> -> Eingabe (Abbruch beendet das Skript)
  whiptail --title "$TITEL" --inputbox "$1" 10 72 "$2" 3>&1 1>&2 2>&3 || exit 0
}

aktion=$(whiptail --title "$TITEL" --menu "Was soll passieren?" 13 72 2 \
  neu "Neuen Container anlegen und installieren" \
  update "Vorhandenen Container aktualisieren" 3>&1 1>&2 2>&3) || exit 0

if [ "$aktion" = update ]; then
  ctid=$(frage "Nummer (CT-ID) des Containers mit der ReBeL-Simulation:" "")
  pct status "$ctid" >/dev/null 2>&1 || { echo "Container $ctid gibt es nicht."; exit 1; }
  pct exec "$ctid" -- bash -c "if command -v rebel-sim-update >/dev/null; then rebel-sim-update;
    else bash -c \"\$(curl -fsSL $ROH/install/rebel-sim-install.sh)\"; fi"
  exit 0
fi

# --- neuer Container ---
ctid=$(frage "Nummer (CT-ID) des neuen Containers:" "$(pvesh get /cluster/nextid)")
name=$(frage "Hostname (Schueler oeffnen spaeter http://<name>):" "rebel-sim")
kerne=$(frage "CPU-Kerne (4 reichen fuer 10 Simulationen):" "4")
ram=$(frage "Arbeitsspeicher in MB (je Simulation etwa 400 MB, 6144 fuer 10):" "6144")
platte=$(frage "Festplatte in GB:" "20")

speicher=$(pvesm status -content rootdir | awk 'NR>1 {print $1}' | head -1)
speicher=$(frage "Speicher fuer die Container-Festplatte (vorhanden: $(pvesm status -content rootdir | awk 'NR>1 {print $1}' | xargs)):" "$speicher")
vorlagen=$(pvesm status -content vztmpl | awk 'NR>1 {print $1}' | head -1)
vorlagen=$(frage "Speicher fuer die Ubuntu-Vorlage (vorhanden: $(pvesm status -content vztmpl | awk 'NR>1 {print $1}' | xargs)):" "$vorlagen")
bruecke=$(frage "Netzwerk-Bruecke:" "vmbr0")
ip=$(frage "IP-Adresse mit Netzmaske (z.B. 192.168.7.1/16) oder dhcp:" "dhcp")
netz="name=eth0,bridge=$bruecke,ip=$ip"
if [ "$ip" != dhcp ]; then
  gw=$(frage "Gateway:" "")
  [ -n "$gw" ] && netz="$netz,gw=$gw"
fi
pw=$(whiptail --title "$TITEL" --passwordbox "Root-Passwort fuer den Container (Konsole und SSH):" 10 72 3>&1 1>&2 2>&3) || exit 0
[ -n "$pw" ] || { echo "Ohne Passwort geht es nicht."; exit 1; }

whiptail --title "$TITEL" --yesno "Container $ctid '$name' anlegen?\n\n$kerne Kerne, $ram MB RAM, $platte GB auf $speicher\nNetz: $netz\n\nDanach wird die Simulation installiert (dauert einige Minuten)." 14 72 || exit 0

echo "==> Ubuntu-24.04-Vorlage"
pveam update >/dev/null
vorlage=$(pveam available --section system | awk '/ubuntu-24\.04-standard/ {print $2}' | sort -V | tail -1)
[ -n "$vorlage" ] || { echo "Keine Ubuntu-24.04-Vorlage gefunden."; exit 1; }
pveam list "$vorlagen" | grep -q "$vorlage" || pveam download "$vorlagen" "$vorlage"

echo "==> Container $ctid anlegen"
pct create "$ctid" "$vorlagen:vztmpl/$vorlage" --hostname "$name" --cores "$kerne" --memory "$ram" --swap 1024 \
  --rootfs "$speicher:$platte" --net0 "$netz" --unprivileged 1 --features nesting=1 --onboot 1 \
  --ostype ubuntu --password "$pw" --description "ReBeL-Simulation (Webots + iRC), https://github.com/mgoebel89/RoboterSimulationDRS"
pct start "$ctid"

echo "==> Warte auf Netzwerk"
for _ in $(seq 1 60); do
  pct exec "$ctid" -- getent hosts github.com >/dev/null 2>&1 && break
  sleep 2
done
pct exec "$ctid" -- getent hosts github.com >/dev/null || { echo "Container hat kein Internet (DNS/Gateway pruefen)."; exit 1; }

echo "==> Installation im Container"
pct exec "$ctid" -- bash -c "apt-get update -q >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get install -y -q curl ca-certificates >/dev/null && bash -c \"\$(curl -fsSL $ROH/install/rebel-sim-install.sh)\""

admin=$(frage "Name fuer das erste Lehrerkonto (Admin), z.B. Kuerzel:" "")
if [ -n "$admin" ]; then
  echo "Passwort fuer $admin festlegen:"
  pct exec "$ctid" -- rebel-sim-admin "$admin" || echo "Spaeter im Container: rebel-sim-admin $admin"
fi

ip_ist=$(pct exec "$ctid" -- hostname -I | awk '{print $1}')
cat <<TEXT

Fertig. Container $ctid ($name), Adresse $ip_ist
  Startseite:   http://$ip_ist   (Schueler waehlen dort ihre Welt,
                Lehrkraefte melden sich dort an)
Noch zu tun (siehe Anleitung im Repository, docs/ANLEITUNG.md):
  1. Robotermodell: auf einem PC mit iRC tools/irc_daten_export.ps1 ausfuehren, irc-daten.zip nach
     /root/ im Container kopieren, dann im Container: rebel-sim-modell /root/irc-daten.zip
  2. App deploy/irc_app/WebotsSim.zip in iRC auf jedem Schueler-PC installieren und freischalten.
Update spaeter: dieses Skript erneut starten und "aktualisieren" waehlen, oder im Container: rebel-sim-update
TEXT
