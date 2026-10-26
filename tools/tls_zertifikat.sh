#!/bin/bash
# HTTPS fuer den Lehrerbereich: eigene kleine Zertifizierungsstelle (CA) im Container und das Zertifikat der
# Webseite. Laeuft als root (install.sh legt eine root-eigene Kopie als /usr/local/sbin/rebel-sim-tls ab; der
# Webdienst ruft sie vor jedem Start auf, ein Timer jede Woche).
#
#   rebel-sim-tls              CA anlegen, falls noch keine da ist; Webseiten-Zertifikat erneuern, wenn es fehlt,
#                              in weniger als 30 Tagen ablaeuft oder die IP-Adresse des Containers nicht enthaelt
#   rebel-sim-tls --neustart   dasselbe, und bei einem neuen Zertifikat den Webdienst neu starten (Timer)
#
# Die CA darf nur Zertifikate fuer das Netz des Containers (z.B. 192.168.0.0/16) und seinen Namen ausstellen
# (Namensbeschraenkung). Wer ihr Stammzertifikat auf einem Lehrer-PC installiert, vertraut ihr also nur dafuer,
# nicht fuer fremde Webseiten - auch dann nicht, wenn jemand den Container uebernimmt.
#
# Dateien in /var/lib/rebel-sim/tls (Ordner root:rebelsim 750):
#   ca.key      Schluessel der CA          root 600 (verlaesst den Container nie)
#   ca.crt      Stammzertifikat der CA     644      (Lehrer-PCs installieren es; Download: http://<container>/ca.crt)
#   server.key  Schluessel der Webseite    root:rebelsim 640
#   server.crt  Zertifikat der Webseite    644      (397 Tage, wird vorher erneuert)
set -euo pipefail
DIR="${TLS_DIR:-/var/lib/rebel-sim/tls}"
NAME="$(hostname -s)"
NEUSTART=""
[ "${1:-}" = "--neustart" ] && NEUSTART=1

install -d -o root -g rebelsim -m 750 "$DIR"
cd "$DIR"

# IPv4-Adressen des Containers (ohne 127.x) und ihr /16-Netz fuer die Namensbeschraenkung der CA
ips=$(echo "${TLS_IPS:-$(hostname -I)}" | tr ' ' '\n' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | grep -v '^127\.' || true)
[ -n "$ips" ] || { echo "rebel-sim-tls: keine IPv4-Adresse gefunden" >&2; exit 1; }

# Container in ein anderes Netz umgezogen (z.B. 192.168.x.x -> 172.20.x.x)? Die alte CA darf dafuer nicht
# ausstellen (Browser lehnen das ab): sichern und eine neue anlegen. Die Lehrer-PCs installieren sie dann neu.
if [ -s ca.crt ]; then
  erlaubt_alt=$(openssl x509 -in ca.crt -noout -ext nameConstraints 2>/dev/null || true)
  for ip in $ips; do
    if ! printf '%s\n' "$erlaubt_alt" | grep -qF "IP:${ip%.*.*}.0.0/255.255.0.0"; then
      alt="ca-alt-$(date +%Y%m%d-%H%M%S)"
      mv ca.crt "$alt.crt"; mv ca.key "$alt.key"
      echo "rebel-sim-tls: Adresse $ip liegt ausserhalb der bisherigen CA, alte CA gesichert als $alt.*;" \
           "Lehrer-PCs muessen das neue Stammzertifikat installieren"
      break
    fi
  done
fi

if [ ! -s ca.key ] || [ ! -s ca.crt ]; then
  netze=$(for ip in $ips; do echo "${ip%.*.*}.0.0"; done | sort -u)
  erlaubt="permitted;DNS:$NAME,permitted;DNS:$NAME.local"
  for n in $netze; do erlaubt="$erlaubt,permitted;IP:$n/255.255.0.0"; done
  ( umask 077; openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out ca.key 2>/dev/null )
  openssl req -x509 -new -key ca.key -sha256 -days 3650 -out ca.crt \
    -subj "/O=ReBeL-Simulation/CN=ReBeL-Simulation $NAME (nur Lehrerbereich)" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -addext "nameConstraints=critical,$erlaubt"
  chmod 600 ca.key; chmod 644 ca.crt
  rm -f server.crt   # neue CA: neues Webseiten-Zertifikat
  echo "rebel-sim-tls: neue Zertifizierungsstelle (erlaubt: $erlaubt)"
fi

erneuern=""
if [ ! -s server.key ] || [ ! -s server.crt ]; then
  erneuern="fehlt"
elif ! openssl x509 -in server.crt -noout -checkend $((30 * 86400)) >/dev/null; then
  erneuern="laeuft bald ab"
elif ! openssl verify -CAfile ca.crt server.crt >/dev/null 2>&1; then
  erneuern="passt nicht zur CA"
else
  for ip in $ips; do
    openssl x509 -in server.crt -noout -ext subjectAltName 2>/dev/null | grep -qE "IP Address:${ip//./\\.}(,|$)" \
      || erneuern="neue IP $ip"
  done
fi

if [ -n "$erneuern" ]; then
  san="DNS:$NAME,DNS:$NAME.local"
  for ip in $ips; do san="$san,IP:$ip"; done
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  ( umask 077; openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$tmp/server.key" 2>/dev/null )
  openssl req -new -key "$tmp/server.key" -subj "/O=ReBeL-Simulation/CN=$NAME" -out "$tmp/server.csr"
  printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\nsubjectAltName=%s\n' \
    "$san" > "$tmp/ext.cnf"
  openssl x509 -req -in "$tmp/server.csr" -CA ca.crt -CAkey ca.key -CAcreateserial -sha256 -days 397 \
    -extfile "$tmp/ext.cnf" -out "$tmp/server.crt" 2>/dev/null
  openssl verify -CAfile ca.crt "$tmp/server.crt" >/dev/null
  install -o root -g rebelsim -m 640 "$tmp/server.key" server.key
  install -o root -g root -m 644 "$tmp/server.crt" server.crt
  echo "rebel-sim-tls: Zertifikat der Webseite neu ($erneuern): $san"
  if [ -n "$NEUSTART" ]; then
    systemctl try-restart rebel-sim-web.service
  fi
fi
