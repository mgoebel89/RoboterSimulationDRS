"""Update aus dem Lehrerbereich (Einstellungen -> Version und Update, fuer jede Lehrkraft).

Der Webdienst (Benutzer rebelsim) darf nichts installieren. Er legt nur eine Anfrage-Datei in <zustand>/update/ ab:
"pruefen" oder "ausfuehren". systemd-.path-Units sehen die Datei und starten als root rebel-sim-selbstupdate
(deploy/selbstupdate.sh). Das holt den neuesten Stand von GitHub, sichert vor einem Update die Laufzeitdaten und
schreibt die Ergebnisse nach /var/lib/rebel-sim-update:
  stand.json     letzte Pruefung: installiert, verfuegbar, Aenderungen (taeglich und auf Knopfdruck)
  lauf.json      letztes Update: beginn, ende, ergebnis (ok | fehler)
  protokoll.txt  Ausgabe des letzten Updates
"""
import datetime
import json
import os
import subprocess

STAND_DIR = os.environ.get("REBELSIM_UPDATE_DIR", "/var/lib/rebel-sim-update")
EINHEIT = os.environ.get("REBELSIM_UPDATE_EINHEIT", "/etc/systemd/system/rebel-sim-update-ausfuehren.path")
PROJEKT_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
ARTEN = ("pruefen", "ausfuehren")
# eine Anfrage, die so lange liegen bleibt, hat systemd nicht abgeholt (Einheit aus, Container ohne systemd)
ANFRAGE_VERFALLEN_S = 120


class UpdateFehler(Exception):
    pass


def _json(name):
    try:
        with open(os.path.join(STAND_DIR, name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _protokoll():
    try:
        with open(os.path.join(STAND_DIR, "protokoll.txt"), encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-200:])
    except OSError:
        return ""


def installiert(projekt_dir=None):
    """Stand des Programms direkt aus Git (gilt auch nach einem Update ueber die Konsole)."""
    try:
        aus = subprocess.run(["git", "-C", projekt_dir or PROJEKT_DIR, "log", "-1", "--format=%h\t%cd\t%s",
                              "--date=format:%d.%m.%Y %H:%M"], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        aus = ""
    teile = aus.split("\t", 2)
    if len(teile) < 3:
        return None
    return {"stand": teile[0], "datum": teile[1], "text": teile[2]}


def eingerichtet():
    return os.path.exists(EINHEIT)


def _anfrage(zustand_dir, art):
    return os.path.join(zustand_dir, "update", art)


def _wartet(zustand_dir, art, jetzt=None):
    try:
        alter = (jetzt or datetime.datetime.now().timestamp()) - os.path.getmtime(_anfrage(zustand_dir, art))
    except OSError:
        return False
    return alter < ANFRAGE_VERFALLEN_S


def kurz(projekt_dir=None):
    """Fuer /api/lehrer/ich: Zahl neuer Aenderungen auf GitHub (0, wenn nichts Neues oder nicht eingerichtet)."""
    s = _json("stand.json") or {}
    jetzt = installiert(projekt_dir) or {}
    if not eingerichtet() or not s.get("neu") or (s.get("installiert") or {}).get("stand") != jetzt.get("stand"):
        return 0
    return int(s["neu"])


def stand(zustand_dir, projekt_dir=None):
    lauf = _json("lauf.json")
    pruefung = _json("stand.json")
    jetzt = installiert(projekt_dir)
    # nach einem Update ueber die Konsole ist die letzte Pruefung veraltet: dann gilt sie nicht
    if pruefung and jetzt and (pruefung.get("installiert") or {}).get("stand") != jetzt["stand"]:
        pruefung = dict(pruefung, neu=None, aenderungen=[], veraltet=True)
    return {
        "eingerichtet": eingerichtet(),
        "installiert": jetzt,
        "pruefung": pruefung,
        "lauf": lauf,
        "laeuft": bool(lauf and lauf.get("beginn") and not lauf.get("ende")),
        "wartet": {a: _wartet(zustand_dir, a) for a in ARTEN},
        "protokoll": _protokoll(),
    }


def anfordern(zustand_dir, art):
    if art not in ARTEN:
        raise UpdateFehler("Unbekannte Aktion.")
    if not eingerichtet():
        raise UpdateFehler("Das Update über diese Seite ist noch nicht eingerichtet. Einmal von Hand aktualisieren "
                           "(im Container: rebel-sim-update), danach geht es hier.")
    s = stand(zustand_dir)
    if s["laeuft"] or s["wartet"]["ausfuehren"]:
        raise UpdateFehler("Es läuft schon ein Update.")
    if art == "ausfuehren" and not (s["pruefung"] or {}).get("neu"):
        raise UpdateFehler("Es gibt keinen neueren Stand. Bitte zuerst „Jetzt prüfen“.")
    ordner = os.path.join(zustand_dir, "update")
    os.makedirs(ordner, exist_ok=True)
    pfad = _anfrage(zustand_dir, art)
    with open(pfad, "w", encoding="utf-8") as f:
        f.write(datetime.datetime.now().isoformat(timespec="seconds"))
