"""Einrichtung im Browser (https://<container>/einrichtung): Einrichtungscode, iRC-Daten, Test zum Schueler-PC.

Solange es kein Lehrerkonto gibt, fuehrt die Webseite durch die Einrichtung. Das erste Admin-Konto legt dort nur an,
wer den Einrichtungscode kennt (sonst koennte sich jeder im Schulnetz, der die Seite zuerst oeffnet, zum Admin
machen). Den Code zeigt das Proxmox-Skript am Ende, sonst im Container:
  rebel-sim-einrichtung          Code zeigen (legt ihn an, solange es kein Konto gibt)
  rebel-sim-einrichtung --neu    neuer Code, der alte gilt nicht mehr
Mit dem ersten Konto wird der Code geloescht. Danach bleibt der Assistent fuer Admins erreichbar
(Lehrerbereich -> Einstellungen -> Einrichtung), z.B. fuer neue iRC-Daten nach einem iRC-Update.

  /var/lib/rebel-sim/einrichtung.json   Code, letzter Verbindungstest, "abgeschlossen"

Ausserdem (fuer rebel-sim-modell, als root):
  python3 einrichtung.py entpacken <irc-daten.zip> <ziel>
Nur Python-Standardbibliothek.
"""

import hmac
import os
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import zipfile

import lehrer

ROBOTER = "REBEL-6DOF-03"
MAX_IRC_ZIP = 400 * 1024 * 1024      # hochgeladene ZIP (auch der ganze Data-Ordner von iRC passt, etwa 270 MB)
MAX_ENTPACKT = 200 * 1024 * 1024     # davon gebraucht: Roboter etwa 5 MB, Schunk-Greifer etwa 0,5 MB
# Ports am Schueler-PC: iRC-Oberflaeche mit lokaler Simulation, iRC-Steuerung, App-Schnittstelle (Taster, Sensoren)
PC_PORTS = ((3921, "iRC-Simulation (CRI)"), (3920, "iRC-Steuerung (CRI)"), (5000, "App-Schnittstelle (Taster und Sensoren)"))
HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,62})")
_KONTO_SPERRE = threading.Lock()


class EinrichtungFehler(lehrer.LehrerFehler):
    """Fehler mit einer Meldung, die so im Assistenten stehen kann."""


def _datei(zustand_dir):
    return os.path.join(zustand_dir, "einrichtung.json")


def lies(zustand_dir):
    return lehrer._lies_json(_datei(zustand_dir))


def merke(zustand_dir, **werte):
    daten = lies(zustand_dir)
    daten.update(werte)
    os.makedirs(zustand_dir, exist_ok=True)
    lehrer.schreibe_json(_datei(zustand_dir), daten, modus=0o600)
    return daten


# --- Einrichtungscode ---

def _ziffern(text):
    return re.sub(r"\D", "", str(text or ""))


def code_holen(zustand_dir, neu=False):
    """Einrichtungscode (1234-5678-9012), solange es kein Konto gibt; sonst None."""
    if lehrer.hat_benutzer(zustand_dir):
        return None
    code = lies(zustand_dir).get("code")
    if neu or len(_ziffern(code)) != 12:
        z = "%012d" % secrets.randbelow(10 ** 12)
        code = "%s-%s-%s" % (z[:4], z[4:8], z[8:])
        merke(zustand_dir, code=code, code_erzeugt=time.strftime("%Y-%m-%d %H:%M"))
    return code


def code_pruefen(zustand_dir, eingabe):
    """Stimmt der eingegebene Code? Leerzeichen und Striche sind egal."""
    soll = _ziffern(lies(zustand_dir).get("code"))
    return len(soll) == 12 and hmac.compare_digest(_ziffern(eingabe)[:20], soll) and not lehrer.hat_benutzer(zustand_dir)


def erstes_konto(zustand_dir, code, name, anzeige, passwort):
    """Erstes Admin-Konto mit dem Einrichtungscode anlegen; danach gilt der Code nicht mehr."""
    with _KONTO_SPERRE:   # zwei Browser gleichzeitig: nur einer bekommt das erste Konto
        if lehrer.hat_benutzer(zustand_dir):
            raise EinrichtungFehler("Es gibt schon ein Konto. Bitte im Lehrerbereich anmelden.")
        if not code_pruefen(zustand_dir, code):
            raise EinrichtungFehler("Der Einrichtungscode stimmt nicht.")
        benutzer = lehrer.benutzer_anlegen(zustand_dir, name, anzeige, "admin", passwort, muss_aendern=False)
        daten = lies(zustand_dir)
        daten.pop("code", None)
        lehrer.schreibe_json(_datei(zustand_dir), daten, modus=0o600)
    return benutzer


# --- iRC-Daten (Robotermodell) ---

def irc_daten_entpacken(zip_pfad, ziel):
    """Roboter und Schunk-Greifer aus einer ZIP-Datei nach <ziel>/Data/... legen.

    Akzeptiert die ZIP aus tools/irc_daten_export.ps1 (Pfade mit Backslash), einen gepackten Data-Ordner von iRC
    und eine von Hand gepackte ZIP mit dem Ordner REBEL-6DOF-03 und den Dateien Schunk_EGP*. Alles andere bleibt
    liegen. Rueckgabe: {"dateien": Anzahl, "greifer": [Werkzeugdateien]}."""
    try:
        quelle = zipfile.ZipFile(zip_pfad)
    except (zipfile.BadZipFile, OSError):
        raise EinrichtungFehler("Das ist keine ZIP-Datei.")
    with quelle:
        eintraege = []
        for eintrag in quelle.infolist():
            # Windows PowerShell 5.1 schreibt Pfade mit Backslash; ".." und Laufwerke fallen weg
            teile = [t for t in eintrag.filename.replace("\\", "/").split("/") if t not in ("", ".", "..") and ":" not in t]
            if teile and not eintrag.filename.endswith(("/", "\\")):
                eintraege.append((eintrag, teile))
        roboter = next((teile[:-1] for _e, teile in eintraege if teile[-1].lower() == ROBOTER.lower() + ".xml"), None)
        if roboter is None:
            raise EinrichtungFehler("In der ZIP-Datei fehlt der Roboter (Ordner %s mit %s.xml aus iRC)." % (ROBOTER, ROBOTER))
        os.makedirs(ziel, exist_ok=True)
        summe, dateien, greifer = 0, 0, []
        for eintrag, teile in eintraege:
            if teile[:len(roboter)] == roboter:
                pfad = os.path.join(ziel, "Data", "Robots", "REBEL", ROBOTER, *teile[len(roboter):])
            elif teile[-1].startswith("Schunk_EGP"):
                pfad = os.path.join(ziel, "Data", "Tools", teile[-1])
                if teile[-1].endswith(".xml"):
                    greifer.append(teile[-1])
            else:
                continue
            summe += eintrag.file_size
            if summe > MAX_ENTPACKT:
                raise EinrichtungFehler("Die Daten in der ZIP-Datei sind unerwartet gross.")
            os.makedirs(os.path.dirname(pfad), exist_ok=True)
            with quelle.open(eintrag) as q, open(pfad, "wb") as f:
                shutil.copyfileobj(q, f)
            dateien += 1
    if not any(re.fullmatch(r"Schunk_EGP(40|25)_RebelMountingPlate(_Finger)?\.xml", g) for g in greifer):
        raise EinrichtungFehler("In der ZIP-Datei fehlen die Schunk-Greifer (Data\\Tools\\Schunk_EGP40_RebelMountingPlate.xml).")
    return {"dateien": dateien, "greifer": sorted(set(greifer))}


def irc_daten_einspielen(zip_pfad, irc_dir, erzeugen):
    """iRC-Daten entpacken, gegen die bisherigen tauschen und erzeugen() aufrufen (Modell bauen).
    Schlaegt das fehl, kommen die bisherigen Daten zurueck."""
    neu, alt = irc_dir + ".neu", irc_dir + ".alt"
    for d in (neu, alt):
        shutil.rmtree(d, ignore_errors=True)
    try:
        bericht = irc_daten_entpacken(zip_pfad, neu)
    except Exception:
        shutil.rmtree(neu, ignore_errors=True)
        raise
    if os.path.isdir(irc_dir):
        os.rename(irc_dir, alt)
    os.rename(neu, irc_dir)
    try:
        bericht["modell"] = erzeugen()
    except Exception:
        shutil.rmtree(irc_dir, ignore_errors=True)
        if os.path.isdir(alt):
            os.rename(alt, irc_dir)
        raise
    shutil.rmtree(alt, ignore_errors=True)
    return bericht


EXPORT_CMD_KOPF = [
    "@echo off",
    "rem ReBeL-Simulation: iRC-Daten fuer das Robotermodell einpacken (irc-daten.zip auf dem Desktop).",
    "rem Doppelklicken auf einem PC mit igus Robot Control V15, dann die Datei im Einrichtungsassistenten hochladen.",
    'set "REBEL_SELBST=%~f0"',
    "powershell -NoProfile -ExecutionPolicy Bypass -Command \"$t = [IO.File]::ReadAllText($env:REBEL_SELBST); "
    "$i = $t.IndexOf([char]10 + '#POWERSHELL'); & ([scriptblock]::Create($t.Substring($i + 1)))\"",
    "echo.",
    "pause",
    "exit /b",
    "#POWERSHELL",
]


def export_cmd(ps1_text):
    """irc_daten_export.ps1 als .cmd zum Doppelklicken (PowerShell-Skripte starten unter Windows nicht per Doppelklick)."""
    zeilen = EXPORT_CMD_KOPF + ps1_text.replace("\r\n", "\n").rstrip("\n").split("\n")
    return ("\r\n".join(zeilen) + "\r\n").encode("ascii")


# --- Verbindung zu einem Schueler-PC ---

def pc_pruefen(host, verbinden=None, ports=PC_PORTS, wartezeit=1.5):
    """Sind die iRC-Ports am PC vom Container aus erreichbar? [{"port", "name", "offen", "fehler"}]"""
    host = str(host or "").strip()
    if not HOST.fullmatch(host):
        raise EinrichtungFehler("Bitte die IP-Adresse oder den Namen des PCs angeben (z.B. 192.168.7.21).")
    verbinden = verbinden or socket.create_connection
    ergebnis = []
    for port, name in ports:
        try:
            verbinden((host, port), wartezeit).close()
            ergebnis.append({"port": port, "name": name, "offen": True, "fehler": None})
        except socket.timeout:
            ergebnis.append({"port": port, "name": name, "offen": False,
                             "fehler": "keine Antwort (PC aus, falsche Adresse oder Firewall)"})
        except socket.gaierror:
            raise EinrichtungFehler("Den PC %s gibt es im Netz nicht (Name unbekannt)." % host)
        except OSError as e:
            abgelehnt = isinstance(e, ConnectionRefusedError)
            ergebnis.append({"port": port, "name": name, "offen": False,
                             "fehler": "abgelehnt (läuft iRC?)" if abgelehnt else str(e.strerror or e)})
    return ergebnis


def main():
    argumente = sys.argv[1:]
    if argumente[:1] == ["code"]:
        code = code_holen(lehrer.ZUSTAND_DIR, neu="--neu" in argumente)
        if "--nur-code" in argumente:
            print(code or "")
        elif code is None:
            print("Es gibt schon ein Lehrerkonto. Die Einrichtung steht jetzt im Lehrerbereich unter Einstellungen.")
            print("Passwort vergessen: rebel-sim-admin <name>")
        else:
            print("Einrichtungscode: %s" % code)
            print("Im Browser: https://<container>/einrichtung (gilt bis zum ersten Lehrerkonto)")
    elif argumente[:1] == ["entpacken"] and len(argumente) == 3:
        try:
            bericht = irc_daten_entpacken(argumente[1], argumente[2])
        except EinrichtungFehler as e:
            sys.exit(str(e))
        print("%d Dateien, Greifer: %s" % (bericht["dateien"], ", ".join(bericht["greifer"])))
    else:
        sys.exit("Aufruf: einrichtung.py code [--neu] [--nur-code] | entpacken <zip> <ziel>")


if __name__ == "__main__":
    main()
