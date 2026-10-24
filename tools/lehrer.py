"""Lehrerbereich: Benutzer mit Rollen, Anmeldung per Cookie, Protokoll,
Einstellungen und Welten pflegen.

Rollen: "admin" verwaltet Benutzer und Einstellungen, "lehrkraft" steuert
Simulationen und pflegt Welten. Alles, was ueber die Webseite geaendert wird,
liegt in Dateien, die der Dienstbenutzer schreiben darf:
  /var/lib/rebel-sim/benutzer.json       Konten (Passwoerter als PBKDF2) und Geheimnis fuer die Cookies
  /var/lib/rebel-sim/protokoll.log       wer wann was getan hat
  /var/lib/rebel-sim/einstellungen.json  Aenderungen an verwalter.json (Leerlauf, Stundenende, ...)
  /opt/rebel-sim/worlds/<welt>.json/.pdf/.png  Titel, Aufgabe, Freigabe, Arbeitsblatt, Vorschau

Im Container (ersten Admin anlegen, oder Passwort vergessen):
  sudo -u rebelsim python3 /opt/rebel-sim/tools/lehrer.py admin <name>
  sudo -u rebelsim python3 /opt/rebel-sim/tools/lehrer.py liste
Nur Python-Standardbibliothek.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time

ZUSTAND_DIR = os.environ.get("ZUSTAND_DIR", "/var/lib/rebel-sim")
SITZUNG_S = 8 * 3600
ITERATIONEN = 200_000
FEHLVERSUCHE = 5          # danach ist die Anmeldung von diesem PC eine Weile gesperrt
SPERRE_S = 60
UHRZEIT = re.compile(r"([01]?\d|2[0-3]):[0-5]\d")
MAX_DATEI = 20 * 1024 * 1024
# Dateiart am Inhalt erkennen, nicht an Namen oder Content-Type
DATEIARTEN = ((b"%PDF", ".pdf"), (b"\x89PNG", ".png"), (b"\xff\xd8\xff", ".jpg"))


class LehrerFehler(Exception):
    """Fehler mit einer Meldung, die so auf der Lehrerseite stehen kann."""


def schreibe_json(pfad, daten, modus=0o644):
    """Erst in eine Hilfsdatei, dann ersetzen: nie eine halbe Datei."""
    tmp = pfad + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(daten, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, modus)
    os.replace(tmp, pfad)


def _lies_json(pfad):
    try:
        with open(pfad, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# --- Benutzer, Anmeldung und Sitzungen ---

ROLLEN = ("admin", "lehrkraft")
NAME = re.compile(r"[a-z0-9_-]{2,32}")
MIN_PASSWORT = 8


def _benutzer_datei(zustand_dir):
    return os.path.join(zustand_dir, "benutzer.json")


def _lade(zustand_dir):
    daten = _lies_json(_benutzer_datei(zustand_dir))
    daten.setdefault("benutzer", {})
    return daten


def _speichere(zustand_dir, daten):
    os.makedirs(zustand_dir, exist_ok=True)
    daten.setdefault("geheimnis", secrets.token_hex(32))
    schreibe_json(_benutzer_datei(zustand_dir), daten, modus=0o600)


def _hash(passwort, salz, iterationen=ITERATIONEN):
    return hashlib.pbkdf2_hmac("sha256", passwort.encode("utf-8"), bytes.fromhex(salz), iterationen).hex()


def _passwort_hash(passwort):
    if len(passwort or "") < MIN_PASSWORT:
        raise LehrerFehler("Das Passwort braucht mindestens %d Zeichen." % MIN_PASSWORT)
    if len(passwort) > 100:
        raise LehrerFehler("Das Passwort ist zu lang.")
    salz = secrets.token_hex(16)
    return "pbkdf2_sha256$%d$%s$%s" % (ITERATIONEN, salz, _hash(passwort, salz))


def _passwort_stimmt(gespeichert, passwort):
    try:
        _verfahren, iterationen, salz, soll = gespeichert.split("$")
        return hmac.compare_digest(_hash(passwort or "", salz, int(iterationen)), soll)
    except ValueError:
        return False


def _oeffentlich(name, b):
    """Angaben ueber einen Benutzer ohne Passwort-Hash."""
    return {"name": name, "anzeige": b.get("anzeige") or name, "rolle": b["rolle"],
            "gesperrt": bool(b.get("gesperrt")), "muss_aendern": bool(b.get("muss_aendern")),
            "angelegt": b.get("angelegt")}


def hat_benutzer(zustand_dir):
    return bool(_lade(zustand_dir)["benutzer"])


def benutzer_liste(zustand_dir):
    return [_oeffentlich(n, b) for n, b in sorted(_lade(zustand_dir)["benutzer"].items())]


def benutzer_anlegen(zustand_dir, name, anzeige, rolle, passwort, muss_aendern=True):
    """Neues Konto. Mit vorlaeufigem Passwort muss es beim ersten Anmelden geaendert werden."""
    name = (name or "").strip().lower()
    if not NAME.fullmatch(name):
        raise LehrerFehler("Benutzername: 2 bis 32 Zeichen, nur a-z, 0-9, - und _ (z.B. goebel).")
    if rolle not in ROLLEN:
        raise LehrerFehler("Unbekannte Rolle.")
    daten = _lade(zustand_dir)
    if name in daten["benutzer"]:
        raise LehrerFehler("Den Benutzer %s gibt es schon." % name)
    daten["benutzer"][name] = {"anzeige": str(anzeige or "").strip()[:60] or name, "rolle": rolle,
                               "pw": _passwort_hash(passwort), "stand": 1, "gesperrt": False,
                               "muss_aendern": bool(muss_aendern), "angelegt": time.strftime("%Y-%m-%d")}
    _speichere(zustand_dir, daten)
    return _oeffentlich(name, daten["benutzer"][name])


def _aktive_admins(benutzer, ohne=None):
    return [n for n, b in benutzer.items() if n != ohne and b["rolle"] == "admin" and not b.get("gesperrt")]


def benutzer_aendern(zustand_dir, name, rolle=None, gesperrt=None, anzeige=None):
    daten = _lade(zustand_dir)
    b = daten["benutzer"].get(name)
    if b is None:
        raise LehrerFehler("Den Benutzer %s gibt es nicht." % name)
    if rolle is not None and rolle not in ROLLEN:
        raise LehrerFehler("Unbekannte Rolle.")
    neu_admin = (rolle or b["rolle"]) == "admin" and not (b.get("gesperrt") if gesperrt is None else gesperrt)
    if not neu_admin and not _aktive_admins(daten["benutzer"], ohne=name):
        raise LehrerFehler("Es muss mindestens ein aktiver Admin bleiben.")
    if rolle is not None:
        b["rolle"] = rolle
    if gesperrt is not None:
        b["gesperrt"] = bool(gesperrt)
        b["stand"] = b.get("stand", 1) + 1   # Sperren beendet laufende Anmeldungen
    if anzeige is not None:
        b["anzeige"] = str(anzeige).strip()[:60] or name
    _speichere(zustand_dir, daten)
    return _oeffentlich(name, b)


def benutzer_loeschen(zustand_dir, name):
    daten = _lade(zustand_dir)
    if name not in daten["benutzer"]:
        raise LehrerFehler("Den Benutzer %s gibt es nicht." % name)
    if not _aktive_admins(daten["benutzer"], ohne=name):
        raise LehrerFehler("Es muss mindestens ein aktiver Admin bleiben.")
    del daten["benutzer"][name]
    _speichere(zustand_dir, daten)


def passwort_setzen(zustand_dir, name, passwort, muss_aendern=False):
    """Neues Passwort. Alle bisherigen Anmeldungen dieses Benutzers enden."""
    daten = _lade(zustand_dir)
    b = daten["benutzer"].get(name)
    if b is None:
        raise LehrerFehler("Den Benutzer %s gibt es nicht." % name)
    b["pw"] = _passwort_hash(passwort)
    b["stand"] = b.get("stand", 1) + 1
    b["muss_aendern"] = bool(muss_aendern)
    _speichere(zustand_dir, daten)


def passwort_aendern(zustand_dir, name, alt, neu):
    b = _lade(zustand_dir)["benutzer"].get(name)
    if b is None or not _passwort_stimmt(b["pw"], alt):
        raise LehrerFehler("Das bisherige Passwort stimmt nicht.")
    if alt == neu:
        raise LehrerFehler("Das neue Passwort muss anders sein als das bisherige.")
    passwort_setzen(zustand_dir, name, neu)


def _signatur(geheimnis, text):
    return hmac.new(bytes.fromhex(geheimnis), text.encode(), hashlib.sha256).hexdigest()


def sitzung_erzeugen(zustand_dir, name, jetzt=None):
    daten = _lade(zustand_dir)
    b = daten["benutzer"][name]
    ablauf = int((time.time() if jetzt is None else jetzt) + SITZUNG_S)
    text = "%s.%d.%d" % (name, ablauf, b.get("stand", 1))
    return "%s.%s" % (text, _signatur(daten["geheimnis"], text))


def anmelden(zustand_dir, name, passwort, jetzt=None):
    """Prueft Name und Passwort. Gibt (Cookie-Wert, Benutzerangaben) zurueck."""
    name = (name or "").strip().lower()
    b = _lade(zustand_dir)["benutzer"].get(name)
    # auch bei unbekanntem Namen rechnen, damit die Antwortzeit nichts verraet
    stimmt = _passwort_stimmt(b["pw"] if b else "pbkdf2_sha256$%d$00$00" % ITERATIONEN, passwort)
    if b is None or not stimmt:
        raise LehrerFehler("Benutzername oder Passwort stimmt nicht.")
    if b.get("gesperrt"):
        raise LehrerFehler("Dieses Konto ist gesperrt.")
    return sitzung_erzeugen(zustand_dir, name, jetzt), _oeffentlich(name, b)


def sitzung_pruefen(zustand_dir, token, jetzt=None):
    """Benutzerangaben zur Anmeldung im Cookie, oder None."""
    try:
        name, ablauf, stand, signatur = (token or "").split(".")
        daten = _lade(zustand_dir)
        b = daten["benutzer"].get(name)
        if b is None or b.get("gesperrt") or "geheimnis" not in daten:
            return None
        if int(ablauf) < (time.time() if jetzt is None else jetzt) or int(stand) != b.get("stand", 1):
            return None
        if not hmac.compare_digest(signatur, _signatur(daten["geheimnis"], "%s.%s.%s" % (name, ablauf, stand))):
            return None
    except ValueError:
        return None
    return _oeffentlich(name, b)


# --- Protokoll: wer hat wann was gemacht ---

def protokolliere(zustand_dir, wer, was, jetzt=None):
    os.makedirs(zustand_dir, exist_ok=True)
    zeit = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() if jetzt is None else jetzt))
    zeile = "%s\t%s\t%s\n" % (zeit, wer.replace("\t", " "), was.replace("\t", " ").replace("\n", " "))
    with open(os.path.join(zustand_dir, "protokoll.log"), "a", encoding="utf-8") as f:
        f.write(zeile)


def lies_protokoll(zustand_dir, anzahl=200):
    """Die letzten Eintraege, neueste zuerst: [{"zeit", "wer", "was"}]."""
    try:
        with open(os.path.join(zustand_dir, "protokoll.log"), encoding="utf-8") as f:
            zeilen = f.readlines()[-anzahl:]
    except OSError:
        return []
    eintraege = []
    for z in reversed(zeilen):
        teile = z.rstrip("\n").split("\t")
        if len(teile) == 3:
            eintraege.append({"zeit": teile[0], "wer": teile[1], "was": teile[2]})
    return eintraege


class Sperre:
    """Bremst Passwort-Raten: nach FEHLVERSUCHE Fehlern von einem PC SPERRE_S Pause."""

    def __init__(self, uhr=time.time):
        self.uhr = uhr
        self.pcs = {}   # PC -> (Fehlversuche, gesperrt bis)

    def wartezeit(self, pc):
        _fehler, bis = self.pcs.get(pc, (0, 0.0))
        return max(0.0, bis - self.uhr())

    def fehlversuch(self, pc):
        fehler, _bis = self.pcs.get(pc, (0, 0.0))
        fehler += 1
        self.pcs[pc] = (0, self.uhr() + SPERRE_S) if fehler >= FEHLVERSUCHE else (fehler, 0.0)

    def erfolg(self, pc):
        self.pcs.pop(pc, None)


# --- Einstellungen ---

def einstellungen_datei(zustand_dir):
    return os.path.join(zustand_dir, "einstellungen.json")


def pruefe_einstellungen(daten):
    """Prueft die Werte aus dem Formular und gibt die zu speichernden zurueck."""
    try:
        max_sim = int(daten["max_simulationen"])
        leerlauf = int(daten["leerlauf_minuten"])
    except (KeyError, TypeError, ValueError):
        raise LehrerFehler("Bitte ganze Zahlen eintragen.")
    if not 1 <= max_sim <= 20:
        raise LehrerFehler("Gleichzeitige Simulationen: 1 bis 20.")
    if not 1 <= leerlauf <= 240:
        raise LehrerFehler("Ende ohne iRC: 1 bis 240 Minuten.")
    uhrzeiten = daten.get("ende_uhrzeiten") or []
    if isinstance(uhrzeiten, str):
        uhrzeiten = [u for u in re.split(r"[\s,;]+", uhrzeiten) if u]
    for u in uhrzeiten:
        if not UHRZEIT.fullmatch(str(u)):
            raise LehrerFehler("Uhrzeit '%s' bitte als SS:MM eintragen, z.B. 09:30." % u)
    uhrzeiten = sorted({"%02d:%s" % (int(u.split(":")[0]), u.split(":")[1]) for u in uhrzeiten})
    return {"max_simulationen": max_sim, "leerlauf_minuten": leerlauf, "ende_uhrzeiten": uhrzeiten}


def speichere_einstellungen(zustand_dir, daten):
    werte = pruefe_einstellungen(daten)
    os.makedirs(zustand_dir, exist_ok=True)
    schreibe_json(einstellungen_datei(zustand_dir), werte)
    return werte


# --- Welten ---

def _welt_basis(welten_dir, datei):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,60}\.wbt", datei or "") \
            or not os.path.isfile(os.path.join(welten_dir, datei)):
        raise LehrerFehler("Diese Welt gibt es nicht.")
    return os.path.join(welten_dir, datei[:-4])


def speichere_weltangaben(welten_dir, datei, titel, aufgabe, sichtbar):
    basis = _welt_basis(welten_dir, datei)
    titel, aufgabe = str(titel or "").strip(), str(aufgabe or "").strip()
    if len(titel) > 100 or len(aufgabe) > 5000:
        raise LehrerFehler("Titel hoechstens 100, Aufgabe hoechstens 5000 Zeichen.")
    angaben = _lies_json(basis + ".json")   # unbekannte Schluessel bleiben erhalten
    angaben.update({"titel": titel, "aufgabe": aufgabe, "sichtbar": bool(sichtbar)})
    schreibe_json(basis + ".json", angaben)
    return angaben


def speichere_weltdatei(welten_dir, datei, inhalt):
    """Arbeitsblatt (PDF) oder Vorschaubild (PNG/JPG) einer Welt speichern."""
    basis = _welt_basis(welten_dir, datei)
    if len(inhalt) > MAX_DATEI:
        raise LehrerFehler("Die Datei ist groesser als 20 MB.")
    endung = next((e for magie, e in DATEIARTEN if inhalt.startswith(magie)), None)
    if endung is None:
        raise LehrerFehler("Nur PDF (Arbeitsblatt) oder PNG/JPG (Vorschaubild).")
    if endung != ".pdf":   # nur ein Vorschaubild je Welt
        for alt in (".png", ".jpg"):
            if os.path.exists(basis + alt):
                os.remove(basis + alt)
    tmp = basis + endung + ".tmp"
    with open(tmp, "wb") as f:
        f.write(inhalt)
    os.replace(tmp, basis + endung)
    return os.path.basename(basis + endung)


def loesche_weltdatei(welten_dir, datei, art):
    basis = _welt_basis(welten_dir, datei)
    endungen = {"pdf": (".pdf",), "bild": (".png", ".jpg")}.get(art)
    if endungen is None:
        raise LehrerFehler("Unbekannte Dateiart.")
    for e in endungen:
        if os.path.exists(basis + e):
            os.remove(basis + e)


def main():
    import getpass
    befehl, name = (sys.argv + ["", ""])[1:3]
    if befehl == "admin" and name:
        pw = getpass.getpass("Passwort fuer %s: " % name)
        if pw != getpass.getpass("Passwort wiederholen: "):
            sys.exit("Die beiden Eingaben sind verschieden.")
        try:
            if name.lower() in {b["name"] for b in benutzer_liste(ZUSTAND_DIR)}:
                passwort_setzen(ZUSTAND_DIR, name.lower(), pw)
                benutzer_aendern(ZUSTAND_DIR, name.lower(), rolle="admin", gesperrt=False)
                print("Passwort neu gesetzt, %s ist Admin und entsperrt." % name.lower())
            else:
                benutzer_anlegen(ZUSTAND_DIR, name, name, "admin", pw, muss_aendern=False)
                print("Admin %s angelegt." % name.lower())
            protokolliere(ZUSTAND_DIR, "Container", "Admin %s angelegt bzw. Passwort gesetzt" % name.lower())
        except LehrerFehler as e:
            sys.exit(str(e))
    elif befehl == "liste":
        for b in benutzer_liste(ZUSTAND_DIR):
            print("%-20s %-10s %s%s" % (b["name"], b["rolle"], b["anzeige"], " (gesperrt)" if b["gesperrt"] else ""))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
