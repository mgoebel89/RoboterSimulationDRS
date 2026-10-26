"""Lehrerbereich: Benutzer mit Rollen, Anmeldung per Cookie, Protokoll,
Einstellungen und Welten pflegen.

Rollen: "admin" verwaltet Benutzer und Einstellungen, "lehrkraft" steuert
Simulationen und pflegt Welten. Alles, was ueber die Webseite geaendert wird,
liegt in Dateien, die der Dienstbenutzer schreiben darf:
  /var/lib/rebel-sim/benutzer.json       Konten (Passwoerter als PBKDF2) und Geheimnis fuer die Cookies
  /var/lib/rebel-sim/protokoll.log       wer wann was getan hat
  /var/lib/rebel-sim/einstellungen.json  Aenderungen an verwalter.json (Leerlauf, Stundenende, ...) und Logo
  /var/lib/rebel-sim/logos/              hochgeladene Logos (PNG, JPG, SVG)
  /opt/rebel-sim/worlds/<welt>.json/.pdf/.png  Titel, Aufgabe, Freigabe, Arbeitsblatt, Vorschau
  /opt/rebel-sim/worlds/papierkorb/      geloeschte Welten (zum Zurueckholen wieder nach worlds/ kopieren)

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
SPERRE_S = 60             # jede weitere Sperre doppelt so lang ...
SPERRE_MAX_S = 15 * 60    # ... aber hoechstens so lang
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
        if signatur in daten.get("abgemeldet", {}):
            return None
    except ValueError:
        return None
    return _oeffentlich(name, b)


def abmelden(zustand_dir, token, jetzt=None):
    """Diese Anmeldung beenden, auch wenn jemand das Cookie kopiert hat (andere Anmeldungen bleiben).
    Gemerkt wird nur die Signatur, bis die Anmeldung ohnehin abgelaufen waere. Rueckgabe: Benutzername oder None."""
    benutzer = sitzung_pruefen(zustand_dir, token, jetzt)
    if benutzer is None:
        return None
    _name, ablauf, _stand, signatur = token.split(".")
    jetzt = time.time() if jetzt is None else jetzt
    daten = _lade(zustand_dir)
    abgemeldet = {s: a for s, a in daten.get("abgemeldet", {}).items() if a >= jetzt}
    abgemeldet[signatur] = int(ablauf)
    daten["abgemeldet"] = abgemeldet
    _speichere(zustand_dir, daten)
    return benutzer["name"]


# --- Protokoll: wer hat wann was gemacht ---

PROTOKOLL_MAX = 2 * 1024 * 1024   # dann wird protokoll.log zu protokoll.log.1 (eine alte Datei bleibt)


def protokolliere(zustand_dir, wer, was, jetzt=None):
    os.makedirs(zustand_dir, exist_ok=True)
    zeit = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() if jetzt is None else jetzt))
    zeile = "%s\t%s\t%s\n" % (zeit, wer.replace("\t", " "), was.replace("\t", " ").replace("\n", " "))
    pfad = os.path.join(zustand_dir, "protokoll.log")
    try:
        if os.path.getsize(pfad) > PROTOKOLL_MAX:
            os.replace(pfad, pfad + ".1")
    except OSError:
        pass
    with open(pfad, "a", encoding="utf-8") as f:
        f.write(zeile)


def lies_protokoll(zustand_dir, anzahl=200):
    """Die letzten Eintraege, neueste zuerst: [{"zeit", "wer", "was"}]. Liest nur das Ende der Datei."""
    try:
        with open(os.path.join(zustand_dir, "protokoll.log"), "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 400 * anzahl))
            zeilen = f.read().decode("utf-8", "replace").splitlines(True)
            if f.tell() > 400 * anzahl:   # erste Zeile ist angeschnitten
                zeilen = zeilen[1:]
            zeilen = zeilen[-anzahl:]
    except OSError:
        return []
    eintraege = []
    for z in reversed(zeilen):
        teile = z.rstrip("\r\n").split("\t")
        if len(teile) == 3:
            eintraege.append({"zeit": teile[0], "wer": teile[1], "was": teile[2]})
    return eintraege


class Sperre:
    """Bremst Passwort-Raten: nach FEHLVERSUCHE Fehlern von einem PC SPERRE_S Pause, jede weitere Sperre doppelt so
    lang (hoechstens SPERRE_MAX_S), bis eine Anmeldung von dort klappt."""

    def __init__(self, uhr=time.time):
        self.uhr = uhr
        self.pcs = {}   # PC -> (Fehlversuche, gesperrt bis, Anzahl Sperren bisher)

    def wartezeit(self, pc):
        bis = self.pcs.get(pc, (0, 0.0, 0))[1]
        return max(0.0, bis - self.uhr())

    def fehlversuch(self, pc):
        """Zaehlt einen Fehlversuch. Rueckgabe: Dauer der jetzt beginnenden Sperre in s, sonst 0."""
        fehler, _bis, stufe = self.pcs.get(pc, (0, 0.0, 0))
        fehler += 1
        if len(self.pcs) > 1000:   # alte Eintraege vergessen (nichts gesperrt, lange her)
            jetzt = self.uhr()
            self.pcs = {p: e for p, e in self.pcs.items() if e[1] > jetzt - SPERRE_MAX_S}
        if fehler < FEHLVERSUCHE:
            self.pcs[pc] = (fehler, 0.0, stufe)
            return 0
        dauer = min(SPERRE_MAX_S, SPERRE_S * 2 ** stufe)
        self.pcs[pc] = (0, self.uhr() + dauer, stufe + 1)
        return dauer

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
    alle = _lies_json(einstellungen_datei(zustand_dir))   # z.B. das gewaehlte Logo bleibt
    alle.update(werte)
    schreibe_json(einstellungen_datei(zustand_dir), alle)
    return werte


# --- Welten ---

def _welt_basis(welten_dir, datei):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,60}\.wbt", datei or "") \
            or not os.path.isfile(os.path.join(welten_dir, datei)):
        raise LehrerFehler("Diese Welt gibt es nicht.")
    return os.path.join(welten_dir, datei[:-4])


ABLAGE = ("auflage", "liegen_lassen")   # zu tief abgelegte Teile: auf die Auflage (realistisch) oder liegen lassen


def speichere_weltangaben(welten_dir, datei, titel, aufgabe, sichtbar, ablage=None):
    basis = _welt_basis(welten_dir, datei)
    titel, aufgabe = str(titel or "").strip(), str(aufgabe or "").strip()
    if len(titel) > 100 or len(aufgabe) > 5000:
        raise LehrerFehler("Titel hoechstens 100, Aufgabe hoechstens 5000 Zeichen.")
    if ablage is not None and ablage not in ABLAGE:
        raise LehrerFehler("Ablage: 'auflage' oder 'liegen_lassen'.")
    angaben = _lies_json(basis + ".json")   # unbekannte Schluessel bleiben erhalten
    angaben.update({"titel": titel, "aufgabe": aufgabe, "sichtbar": bool(sichtbar)})
    if ablage is not None:
        angaben["ablage"] = ablage
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


# Alles, was zu einer Welt gehoert (Baukasten-Entwurf, Angaben, Vorschau, Arbeitsblatt)
WELT_ENDUNGEN = (".wbt", ".json", ".baukasten.json", ".png", ".jpg", ".pdf")


def loesche_welt(welten_dir, datei, jetzt=None):
    """Welt mit allen Dateien in den Papierkorb (worlds/papierkorb/<welt>-<zeit>/) verschieben.
    Zurueckholen: die Dateien wieder nach worlds/ kopieren. Rueckgabe: verschobene Dateinamen."""
    basis = _welt_basis(welten_dir, datei)
    name = os.path.basename(basis)
    ziel = os.path.join(welten_dir, "papierkorb", "%s-%s" % (name, time.strftime("%Y%m%d-%H%M%S", time.localtime(jetzt))))
    os.makedirs(ziel, exist_ok=True)
    verschoben = []
    for e in WELT_ENDUNGEN:
        if os.path.exists(basis + e):
            os.replace(basis + e, os.path.join(ziel, name + e))
            verschoben.append(name + e)
    return verschoben


GREIFER_FELD = re.compile(r'(\bgreifer\s+")([^"]*)(")')


def welt_greifer(welten_dir, datei):
    """Greifer der Welt (Feld greifer im ReBeL der .wbt) oder None."""
    try:
        with open(_welt_basis(welten_dir, datei) + ".wbt", encoding="utf-8", errors="replace") as f:
            m = GREIFER_FELD.search(f.read())
    except (OSError, LehrerFehler):
        return None
    return m.group(2) if m else None


def setze_greifer(welten_dir, datei, greifer, erlaubt):
    """Greifer einer Welt umstellen (in der .wbt und, falls vorhanden, im Baukasten-Entwurf)."""
    basis = _welt_basis(welten_dir, datei)
    if greifer not in erlaubt:
        raise LehrerFehler("Den Greifer %r kennt das Robotermodell nicht." % greifer)
    with open(basis + ".wbt", encoding="utf-8") as f:
        text = f.read()
    if not GREIFER_FELD.search(text):
        raise LehrerFehler("In %s steht kein Greifer (Feld greifer am ReBeL fehlt)." % datei)
    tmp = basis + ".wbt.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(GREIFER_FELD.sub(lambda m: m.group(1) + greifer + m.group(3), text, count=1))
    os.replace(tmp, basis + ".wbt")
    if os.path.exists(basis + ".baukasten.json"):
        entwurf = _lies_json(basis + ".baukasten.json")
        entwurf["greifer"] = greifer
        schreibe_json(basis + ".baukasten.json", entwurf)


# --- Logo (Einstellungen): hochgeladene Bilder in /var/lib/rebel-sim/logos, gewaehlt in einstellungen.json ---

LOGO_NAME = re.compile(r"[a-z0-9_-]{1,40}\.(png|jpg|svg)")
LOGO_TYPEN = {"png": "image/png", "jpg": "image/jpeg", "svg": "image/svg+xml"}


def logo_dir(zustand_dir):
    return os.path.join(zustand_dir, "logos")


def logos(zustand_dir):
    try:
        return sorted(n for n in os.listdir(logo_dir(zustand_dir)) if LOGO_NAME.fullmatch(n))
    except OSError:
        return []


def gewaehltes_logo(zustand_dir):
    """Dateiname des gewaehlten Logos (muss noch vorhanden sein) oder None."""
    name = _lies_json(einstellungen_datei(zustand_dir)).get("logo")
    return name if name in logos(zustand_dir) else None


def speichere_logo(zustand_dir, name, inhalt):
    """Logo hochladen: PNG, JPG oder SVG (ohne Skripte). Rueckgabe: Dateiname."""
    name = re.sub(r"[^a-z0-9_-]", "_", str(name or "").lower().rsplit(".", 1)[0])[:40].strip("_") or "logo"
    if len(inhalt) > 2 * 1024 * 1024:
        raise LehrerFehler("Das Logo ist groesser als 2 MB.")
    endung = next((e[1:] for magie, e in DATEIARTEN if inhalt.startswith(magie) and e != ".pdf"), None)
    if endung is None:
        kopf = inhalt[:2048].decode("utf-8", "replace").lstrip("﻿ \r\n\t").lower()
        if not (kopf.startswith("<svg") or kopf.startswith("<?xml")) or "<svg" not in inhalt.decode("utf-8", "replace").lower():
            raise LehrerFehler("Nur PNG, JPG oder SVG.")
        text = inhalt.decode("utf-8", "replace").lower()
        if re.search(r"<script|\bon[a-z]+\s*=|javascript:|<foreignobject", text):
            raise LehrerFehler("Das SVG enthaelt Skripte oder Ereignisse; bitte als einfaches SVG oder PNG speichern.")
        endung = "svg"
    os.makedirs(logo_dir(zustand_dir), exist_ok=True)
    datei = "%s.%s" % (name, endung)
    tmp = os.path.join(logo_dir(zustand_dir), datei + ".tmp")
    with open(tmp, "wb") as f:
        f.write(inhalt)
    os.replace(tmp, os.path.join(logo_dir(zustand_dir), datei))
    return datei


def waehle_logo(zustand_dir, name):
    """Logo fuer alle Seiten waehlen ("" = keins). Bleibt in einstellungen.json neben den anderen Werten."""
    if name and name not in logos(zustand_dir):
        raise LehrerFehler("Dieses Logo gibt es nicht.")
    werte = _lies_json(einstellungen_datei(zustand_dir))
    werte["logo"] = name or ""
    os.makedirs(zustand_dir, exist_ok=True)
    schreibe_json(einstellungen_datei(zustand_dir), werte)


def loesche_logo(zustand_dir, name):
    if name not in logos(zustand_dir):
        raise LehrerFehler("Dieses Logo gibt es nicht.")
    os.remove(os.path.join(logo_dir(zustand_dir), name))
    if _lies_json(einstellungen_datei(zustand_dir)).get("logo") == name:
        waehle_logo(zustand_dir, "")


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
