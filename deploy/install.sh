#!/bin/bash
# Richtet die ReBeL-Simulation im Container ein (als root ausfuehren).
# Voraussetzung: Webots, xvfb und python3 sind installiert, Projekt liegt in /opt/rebel-sim.
#
#   /opt/rebel-sim/deploy/install.sh
set -euo pipefail
BASIS=/opt/rebel-sim

# gmsh: Teile aus STEP-Dateien (Baukasten); grpcio: Taster setzen Eingaenge in iRC (App-Schnittstelle)
DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3-gmsh python3-grpcio >/dev/null

id rebelsim >/dev/null 2>&1 || useradd -m -s /bin/bash rebelsim
mkdir -p /etc/rebel-sim
chown -R rebelsim: "$BASIS"
# Lehrerbereich: Konten, Protokoll und Einstellungen aus der Webseite (nur der Dienst darf hinein)
install -d -o rebelsim -g rebelsim -m 750 /var/lib/rebel-sim
chmod +x "$BASIS"/tools/*.sh "$BASIS"/deploy/*.sh

# Platz-Helfer fuer den Simulationsverwalter: eigene Kopie, die root gehoert,
# damit der Dienstbenutzer sie nicht veraendern kann; nur sie darf er per sudo starten
install -o root -g root -m 755 "$BASIS/deploy/platz.sh" /usr/local/sbin/rebel-sim-platz
printf 'rebelsim ALL=(root) NOPASSWD: /usr/local/sbin/rebel-sim-platz\n' > /etc/sudoers.d/rebel-sim.neu
chmod 440 /etc/sudoers.d/rebel-sim.neu
visudo -cf /etc/sudoers.d/rebel-sim.neu >/dev/null
mv /etc/sudoers.d/rebel-sim.neu /etc/sudoers.d/rebel-sim
[ -f /etc/rebel-sim/verwalter.json ] || install -m 644 "$BASIS/deploy/verwalter.json" /etc/rebel-sim/verwalter.json

# HTTPS fuer den Lehrerbereich: Zertifikatsskript als root-eigene Kopie (laeuft als root), woechentlich pruefen
install -o root -g root -m 755 "$BASIS/tools/tls_zertifikat.sh" /usr/local/sbin/rebel-sim-tls
install -m 644 "$BASIS/deploy/rebel-sim-tls.service" /etc/systemd/system/rebel-sim-tls.service
install -m 644 "$BASIS/deploy/rebel-sim-tls.timer" /etc/systemd/system/rebel-sim-tls.timer
/usr/local/sbin/rebel-sim-tls || echo "Zertifikat konnte nicht erzeugt werden; die Seite laeuft dann ohne HTTPS"

# Firewall: von aussen nur Webseite und SSH (Regeln in /etc/rebel-sim, gehoert root)
install -o root -g root -m 644 "$BASIS/deploy/firewall.nft" /etc/rebel-sim/firewall.nft
install -m 644 "$BASIS/deploy/rebel-sim-firewall.service" /etc/systemd/system/rebel-sim-firewall.service

# Mailserver der Ubuntu-Vorlage wird nicht gebraucht
if systemctl cat postfix.service >/dev/null 2>&1; then
  systemctl disable --now postfix.service >/dev/null 2>&1 || true
fi

install -m 644 "$BASIS/deploy/rebel-sim.tmpfiles" /etc/tmpfiles.d/rebel-sim.conf
systemd-tmpfiles --create /etc/tmpfiles.d/rebel-sim.conf
install -m 644 "$BASIS/deploy/rebel-sim@.service" /etc/systemd/system/rebel-sim@.service
install -m 644 "$BASIS/deploy/rebel-sim-web.service" /etc/systemd/system/rebel-sim-web.service
systemctl daemon-reload
systemctl enable --now rebel-sim-tls.timer >/dev/null 2>&1
systemctl enable rebel-sim-firewall.service >/dev/null 2>&1
systemctl restart rebel-sim-firewall.service || echo "Firewall konnte nicht geladen werden (nft im Container erlaubt?)"
systemctl enable rebel-sim-web.service
systemctl restart rebel-sim-web.service

echo "Fertig. Schueler oeffnen http://<container> und starten ihre Simulation selbst."
echo "Lehrerbereich: https://<container>/lehrer (verschluesselt; Stammzertifikat fuer die Lehrer-PCs: http://<container>/ca.crt)."
echo "Ersten Admin anlegen: sudo -u rebelsim python3 $BASIS/tools/lehrer.py admin <name>"
echo "Einstellungen: /etc/rebel-sim/verwalter.json. Fester Platz: rebel-sim-platz anlegen 99 192.168.3.11 rebel_tisch.wbt dauerhaft"
