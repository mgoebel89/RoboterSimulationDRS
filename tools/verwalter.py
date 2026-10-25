"""Simulationsverwalter: vergibt Simulationen an Schueler-PCs.

Statt fester Plaetze gibt es einen Pool von Slots 01..max_simulationen. Ein
Schueler-PC (erkannt an seiner IP) bekommt beim Start einen freien Slot; die
Webots-Instanz hoert dann bei der iRC-Steuerung dieses PCs mit.

  - eine Simulation pro PC, hoechstens max_simulationen gleichzeitig
  - automatisches Ende nach leerlauf_minuten ohne iRC-Verbindung
  - automatisches Ende aller Simulationen zu den ende_uhrzeiten (Stundenende)
  - Plaetze mit DAUERHAFT=1 (z.B. Zwilling des echten ReBeL) bleiben unberuehrt

Die Einstellungen stehen in /etc/rebel-sim/verwalter.json. Gestartet und
beendet wird ueber den Helfer /usr/local/sbin/rebel-sim-platz (= deploy/platz.sh),
den der Dienstbenutzer per sudo aufrufen darf.
Nur Python-Standardbibliothek.
"""

import json
import os
import re
import subprocess
import threading
import time

STANDARD = {
    "max_simulationen": 10,
    "leerlauf_minuten": 15,
    "ende_uhrzeiten": [],   # z.B. ["09:30", "11:15", "13:00"]
    "lehrer_ips": [],       # gelten ohne Anmeldung als Admin (nur zum Einrichten, danach leeren)
}
HELFER = os.environ.get("PLATZ_HELFER", "sudo -n /usr/local/sbin/rebel-sim-platz").split()
WELT_DATEI = re.compile(r"[A-Za-z0-9_-]{1,60}\.wbt")
PC_ADRESSE = re.compile(r"[A-Za-z0-9.:-]{1,64}")
# ohne Status-Datei so lange warten, bis eine Simulation als nicht gestartet gilt
ANLAUF_S = 90


class VerwalterFehler(Exception):
    """Fehler mit einer Meldung, die so auf der Schuelerseite stehen kann."""


def lade_einstellungen(konfig_dir, zustand_dir=None):
    """verwalter.json, darueber die Aenderungen aus dem Lehrerbereich (einstellungen.json)."""
    werte = dict(STANDARD)
    pfade = [os.path.join(konfig_dir, "verwalter.json")]
    if zustand_dir:
        pfade.append(os.path.join(zustand_dir, "einstellungen.json"))
    for pfad in pfade:
        try:
            with open(pfad, encoding="utf-8") as f:
                werte.update({k: v for k, v in json.load(f).items() if not k.startswith("_")})
        except (OSError, ValueError):
            pass
    return werte


def _wbt_angaben(pfad):
    """Titel und Info-Text aus WorldInfo der Welt-Datei (Ersatz, wenn keine JSON da ist)."""
    try:
        with open(pfad, encoding="utf-8", errors="replace") as f:
            text = f.read(20000)
    except OSError:
        return "", ""
    titel = re.search(r'\btitle\s+"([^"]*)"', text)
    info = re.search(r'\binfo\s*\[\s*"([^"]*)"', text)
    return (titel.group(1) if titel else ""), (info.group(1) if info else "")


def lese_welten(welten_dir):
    """Alle Welten mit Titel, Aufgabentext, Vorschaubild und Arbeitsblatt.
    Zu welt.wbt gehoeren optional welt.json ({"titel", "aufgabe", "sichtbar"}),
    welt.png/.jpg (Vorschau) und welt.pdf (Arbeitsblatt)."""
    welten = []
    try:
        namen = sorted(os.listdir(welten_dir))
    except OSError:
        return welten
    for datei in namen:
        if not WELT_DATEI.fullmatch(datei):
            continue
        basis = datei[:-4]
        titel, aufgabe = _wbt_angaben(os.path.join(welten_dir, datei))
        angaben = {}
        try:
            with open(os.path.join(welten_dir, basis + ".json"), encoding="utf-8") as f:
                angaben = json.load(f)
        except (OSError, ValueError):
            pass
        bild = next((basis + e for e in (".png", ".jpg") if basis + e in namen), None)
        welten.append({
            "datei": datei,
            "titel": angaben.get("titel") or titel or basis,
            "aufgabe": angaben.get("aufgabe", aufgabe),
            "sichtbar": bool(angaben.get("sichtbar", True)),
            "bild": bild,
            "pdf": basis + ".pdf" if basis + ".pdf" in namen else None,
            "ea": angaben.get("ea") if isinstance(angaben.get("ea"), dict) else {},
            "ablage": angaben.get("ablage") if angaben.get("ablage") in ("auflage", "liegen_lassen") else "auflage",
        })
    return welten


def helfer_aufrufen(*argumente):
    """Ruft den Platz-Helfer auf (anlegen/neustart/entfernen)."""
    try:
        r = subprocess.run(HELFER + list(argumente), capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.SubprocessError) as e:
        raise VerwalterFehler("Simulation konnte nicht geschaltet werden (%s)" % e)
    if r.returncode != 0:
        raise VerwalterFehler("Simulation konnte nicht geschaltet werden: %s"
                              % (r.stderr.strip() or r.stdout.strip() or "Fehler %d" % r.returncode))
    return r.stdout


def leerlauf_s(status, gestartet, jetzt):
    """Wie lange die Simulation schon ohne iRC ist (Sekunden)."""
    if status is None or status.get("_alter_s", 1e9) > 30:
        return jetzt - gestartet   # keine frische Statusdatei: seit dem Start rechnen
    if status.get("sekunden_seit_irc") is not None:
        return status["sekunden_seit_irc"]
    return status.get("laufzeit_s", jetzt - gestartet)


def _zeitpunkt_heute(hhmm, jetzt):
    h, m = (int(x) for x in hhmm.split(":"))
    t = time.localtime(jetzt)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, h, m, 0, 0, 0, -1))


class Verwalter:
    """Vergibt, setzt zurueck und beendet Simulationen. Alle Zugriffe auf Dateien
    und Dienste laufen ueber die uebergebenen Funktionen (austauschbar fuer Tests)."""

    def __init__(self, plaetze_fn, status_fn, aktiv_fn, einstellungen_fn, welten_fn,
                 helfer=helfer_aufrufen, uhr=time.time, log=print, befehl_fn=None):
        self.plaetze_fn = plaetze_fn
        self.befehl_fn = befehl_fn   # befehl_fn(platz, befehl): Befehl an den Controller der Simulation
        self.status_fn = status_fn
        self.aktiv_fn = aktiv_fn
        self.einstellungen_fn = einstellungen_fn
        self.welten_fn = welten_fn
        self.helfer = helfer
        self.uhr = uhr
        self.log = log
        self.lock = threading.Lock()
        self.letzte_pruefung = uhr()
        self.meldungen = {}   # PC -> Grund, warum seine Simulation beendet wurde

    def platz_von(self, pc):
        return next((p for p in self.plaetze_fn() if p["irc_host"] == pc), None)

    def welt(self, datei, nur_sichtbare=True):
        w = next((w for w in self.welten_fn() if w["datei"] == datei), None)
        if w is None or (nur_sichtbare and not w["sichtbar"]):
            raise VerwalterFehler("Diese Welt gibt es nicht.")
        return w

    def frei(self):
        max_sim = int(self.einstellungen_fn()["max_simulationen"])
        return max(0, max_sim - len([p for p in self.plaetze_fn() if int(p["platz"]) <= max_sim]))

    def starten(self, pc, datei):
        """Startet (oder wechselt) die Simulation dieses PCs. Gibt die Platznummer zurueck."""
        if not PC_ADRESSE.fullmatch(pc or ""):
            raise VerwalterFehler("Dein PC ist nicht erkennbar.")
        self.welt(datei)
        max_sim = int(self.einstellungen_fn()["max_simulationen"])
        with self.lock:
            self.meldungen.pop(pc, None)
            eigener = self.platz_von(pc)
            if eigener is not None:
                if eigener["welt"] != datei:
                    self.helfer("anlegen", eigener["platz"], pc, datei)
                    self.log("Platz %s: %s wechselt zu %s" % (eigener["platz"], pc, datei))
                return eigener["platz"]
            belegt = {int(p["platz"]) for p in self.plaetze_fn()}
            nummer = next((n for n in range(1, max_sim + 1) if n not in belegt), None)
            if nummer is None:
                raise VerwalterFehler("Alle %d Simulationen sind belegt. Bitte später noch einmal versuchen."
                                      % max_sim)
            platz = "%02d" % nummer
            self.helfer("anlegen", platz, pc, datei)
            self.log("Platz %s: gestartet fuer %s mit %s" % (platz, pc, datei))
            return platz

    def zuruecksetzen(self, pc):
        eigener = self.platz_von(pc)
        if eigener is None:
            raise VerwalterFehler("Du hast gerade keine Simulation.")
        self.helfer("neustart", eigener["platz"])
        self.log("Platz %s: zurueckgesetzt von %s" % (eigener["platz"], pc))

    def _teile_zurueck(self, platz):
        """Werkstuecke auf die Startlage, ohne Webots neu zu starten (der Controller liest den Befehl)."""
        status = self.status_fn(platz["platz"])
        if status is None or status.get("_alter_s", 999) > 5:
            raise VerwalterFehler("Die Simulation laeuft gerade nicht. Bitte 'Neu starten' nehmen.")
        self.befehl_fn(platz["platz"], "teile_zuruecksetzen")

    def teile_zuruecksetzen(self, pc):
        eigener = self.platz_von(pc)
        if eigener is None:
            raise VerwalterFehler("Du hast gerade keine Simulation.")
        self._teile_zurueck(eigener)
        self.log("Platz %s: Teile zurueckgesetzt von %s" % (eigener["platz"], pc))

    def kollision_quittieren(self, pc):
        """Knopf "Kollision quittieren" (nur der Schueler): der Controller hebt den Crash-Stopp auf."""
        eigener = self.platz_von(pc)
        if eigener is None:
            raise VerwalterFehler("Du hast gerade keine Simulation.")
        status = self.status_fn(eigener["platz"])
        if status is None or status.get("_alter_s", 999) > 5:
            raise VerwalterFehler("Die Simulation laeuft gerade nicht. Bitte 'Neu starten' nehmen.")
        self.befehl_fn(eigener["platz"], "kollision_quittieren")
        self.log("Platz %s: Kollision quittiert von %s" % (eigener["platz"], pc))

    def taster(self, pc, nr, an):
        """Taster im Browser gedrueckt (an=True) oder losgelassen; der Controller setzt den Eingang."""
        eigener = self.platz_von(pc)
        if eigener is None:
            raise VerwalterFehler("Du hast gerade keine Simulation.")
        if isinstance(nr, bool) or not isinstance(nr, int) or not 0 <= nr < 16 or not isinstance(an, bool):
            raise VerwalterFehler("Ungueltiger Taster.")
        self.befehl_fn(eigener["platz"], "taster %d %d" % (nr, 1 if an else 0))

    def beenden(self, platz, grund="beendet"):
        with self.lock:
            self.helfer("entfernen", platz["platz"])
        self.log("Platz %s: %s (%s)" % (platz["platz"], grund, platz["irc_host"]))

    def beenden_fuer(self, pc):
        eigener = self.platz_von(pc)
        if eigener is None:
            raise VerwalterFehler("Du hast gerade keine Simulation.")
        self.beenden(eigener, "vom Schueler beendet")

    # --- fuer die Lehrkraft: jede Simulation ueber ihre Platznummer ---

    def platz(self, nummer):
        p = next((p for p in self.plaetze_fn() if str(nummer).isdigit() and int(p["platz"]) == int(nummer)), None)
        if p is None:
            raise VerwalterFehler("Platz %s laeuft nicht." % nummer)
        return p

    def zuruecksetzen_platz(self, nummer):
        p = self.platz(nummer)
        self.helfer("neustart", p["platz"])
        self.log("Platz %s: von der Lehrkraft zurueckgesetzt" % p["platz"])

    def teile_zuruecksetzen_platz(self, nummer):
        p = self.platz(nummer)
        self._teile_zurueck(p)
        self.log("Platz %s: Teile von der Lehrkraft zurueckgesetzt" % p["platz"])

    def beenden_platz(self, nummer):
        p = self.platz(nummer)
        self.beenden(p, "von der Lehrkraft beendet")
        self.meldungen[p["irc_host"]] = "von der Lehrkraft beendet"

    def alle_beenden(self):
        """Alle Simulationen ausser den dauerhaften. Gibt die Anzahl zurueck."""
        anzahl = 0
        for p in self.plaetze_fn():
            if not p.get("dauerhaft"):
                self.beenden(p, "von der Lehrkraft beendet (alle)")
                self.meldungen[p["irc_host"]] = "von der Lehrkraft beendet"
                anzahl += 1
        return anzahl

    def pruefen(self):
        """Regelmaessig aufrufen: beendet Simulationen ohne iRC und zum Stundenende.
        Gibt [(Platz, Grund)] der beendeten Simulationen zurueck."""
        e = self.einstellungen_fn()
        jetzt = self.uhr()
        stundenende = False
        for hhmm in e.get("ende_uhrzeiten") or []:
            try:
                if self.letzte_pruefung < _zeitpunkt_heute(hhmm, jetzt) <= jetzt:
                    stundenende = True
            except ValueError:
                continue
        self.letzte_pruefung = jetzt
        grenze = float(e["leerlauf_minuten"]) * 60
        beendet = []
        for p in self.plaetze_fn():
            if p.get("dauerhaft"):
                continue
            grund = None
            if stundenende:
                grund = "Stundenende"
            elif not self.aktiv_fn(p["platz"]):
                if jetzt - p["gestartet"] > ANLAUF_S:
                    grund = "Simulation lief nicht mehr"
            elif leerlauf_s(self.status_fn(p["platz"]), p["gestartet"], jetzt) > grenze:
                grund = "%d Minuten ohne Verbindung zu iRC" % round(grenze / 60)
            if grund is None:
                continue
            try:
                self.beenden(p, grund)
            except VerwalterFehler as fehler:
                self.log("Platz %s: Beenden fehlgeschlagen: %s" % (p["platz"], fehler))
                continue
            self.meldungen[p["irc_host"]] = grund
            beendet.append((p["platz"], grund))
        return beendet

    def laufen(self, takt_s=30):
        """Endlosschleife fuer einen Hintergrund-Thread."""
        while True:
            time.sleep(takt_s)
            try:
                self.pruefen()
            except Exception as fehler:   # noqa: BLE001 - Pruefung darf nie den Dienst beenden
                self.log("Pruefung fehlgeschlagen: %r" % fehler)
