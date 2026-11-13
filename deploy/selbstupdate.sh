#!/bin/bash
# Update aus dem Lehrerbereich (Einstellungen -> Version und Update), als /usr/local/sbin/rebel-sim-selbstupdate
# installiert (root). Gestartet von systemd:
#   pruefen     neuesten Stand holen (git fetch) und mit dem installierten vergleichen -> stand.json
#               (taeglich per Timer und auf Knopfdruck)
#   ausfuehren  Sicherung der Laufzeitdaten, rebel-sim-update, danach pruefen -> lauf.json, protokoll.txt
# Die Anfrage-Dateien legt der Webdienst in /var/lib/rebel-sim/update ab. Sie werden nur geloescht, nie gelesen;
# die Ergebnisse liegen in einem Ordner, der root gehoert (/var/lib/rebel-sim-update, fuer den Webdienst lesbar).
set -uo pipefail
BASIS=/opt/rebel-sim
ANFRAGE=/var/lib/rebel-sim/update
STAND=/var/lib/rebel-sim-update
SICHERUNGEN="$STAND/sicherungen"
BEHALTEN=3
umask 022
export TZ=Europe/Berlin
install -d -o root -g root -m 755 "$STAND"
install -d -o root -g root -m 700 "$SICHERUNGEN"   # enthaelt Konten (Passwort-Hashes): nur root
git config --global --add safe.directory "$BASIS" 2>/dev/null

jetzt() { date '+%Y-%m-%dT%H:%M:%S'; }

schreiben() {   # Datei in $STAND aus stdin: erst fertig schreiben, dann austauschen
  local tmp
  tmp=$(mktemp "$STAND/.neu.XXXXXX")
  cat > "$tmp"
  chmod 644 "$tmp"
  mv -f "$tmp" "$STAND/$1"
}

pruefen() {
  rm -f "$ANFRAGE/pruefen"
  cd "$BASIS" || exit 1
  local fehler=""
  if ! fehler=$(git fetch -q origin 2>&1); then
    fehler=${fehler:-git fetch fehlgeschlagen}
  else
    fehler=""
  fi
  chown -R rebelsim:rebelsim "$BASIS/.git" 2>/dev/null
  python3 - "$fehler" <<'PY' | schreiben stand.json
import datetime, json, subprocess, sys

def git(*a):
    return subprocess.run(["git", *a], capture_output=True, text=True).stdout.strip()

def stand(ref):
    h, _, rest = git("log", "-1", "--format=%h\t%cd\t%s", "--date=format:%d.%m.%Y %H:%M", ref).partition("\t")
    datum, _, text = rest.partition("\t")
    return {"stand": h or None, "datum": datum or None, "text": text or None}

ziel = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}") or "origin/main"
aenderungen = [z.split("\t", 2) for z in git("log", "--format=%h\t%cd\t%s", "--date=format:%d.%m.%Y", "-n", "40",
                                              "HEAD.." + ziel).splitlines() if z.count("\t") >= 2]
print(json.dumps({
    "zeit": datetime.datetime.now().isoformat(timespec="seconds"),
    "fehler": sys.argv[1][-400:] or None,
    "quelle": git("remote", "get-url", "origin"),
    "installiert": stand("HEAD"),
    "verfuegbar": stand(ziel),
    "neu": int(git("rev-list", "--count", "HEAD.." + ziel) or 0),
    "aenderungen": [{"stand": h, "datum": d, "text": t} for h, d, t in aenderungen],
}, ensure_ascii=False))
PY
}

sichern() {   # Laufzeitdaten, die nicht im Repository stehen (Welten, Teile, Backen, Konten, Einstellungen)
  local ziel teile=()
  ziel="$SICHERUNGEN/vor-update-$(date '+%Y-%m-%d_%H-%M-%S').tgz"
  for d in opt/rebel-sim/worlds opt/rebel-sim/teile opt/rebel-sim/backen var/lib/rebel-sim etc/rebel-sim; do
    [ -e "/$d" ] && teile+=("$d")
  done
  tar czf "$ziel" -C / --exclude=var/lib/rebel-sim/irc --exclude=var/lib/rebel-sim/update "${teile[@]}" || return 1
  chmod 600 "$ziel"
  echo "Sicherung: $ziel ($(du -h "$ziel" | cut -f1))"
  ls -1t "$SICHERUNGEN"/vor-update-*.tgz 2>/dev/null | tail -n +$((BEHALTEN + 1)) | xargs -r rm -f
}

ausfuehren() {
  rm -f "$ANFRAGE/ausfuehren"
  local beginn ergebnis=ok
  beginn=$(jetzt)
  printf '{"beginn": "%s", "ende": null, "ergebnis": null}\n' "$beginn" | schreiben lauf.json
  {
    echo "== $(jetzt) Sicherung"
    sichern || ergebnis=fehler
    if [ "$ergebnis" = ok ]; then
      echo "== $(jetzt) Update"
      # ohne Farben, das Protokoll erscheint im Browser
      /usr/local/sbin/rebel-sim-update 2>&1 | sed -u 's/\x1b\[[0-9;]*m//g'
      [ "${PIPESTATUS[0]}" = 0 ] || ergebnis=fehler
    else
      echo "Ohne Sicherung kein Update."
    fi
    echo "== $(jetzt) Ende: $ergebnis"
  } > "$STAND/protokoll.txt" 2>&1
  chmod 644 "$STAND/protokoll.txt"
  printf '{"beginn": "%s", "ende": "%s", "ergebnis": "%s"}\n' "$beginn" "$(jetzt)" "$ergebnis" | schreiben lauf.json
  pruefen
}

case "${1:-}" in
  pruefen) pruefen ;;
  ausfuehren) ausfuehren ;;
  *) echo "Aufruf: rebel-sim-selbstupdate pruefen|ausfuehren"; exit 1 ;;
esac
