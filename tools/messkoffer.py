"""Messkoffer: Tests in der Schule sammeln, ohne dass jemand von aussen ins Schulnetz schaut.

Ein Menue fuehrt durch die Schritte; jeder Schritt ist einzeln aufrufbar und wiederholbar. Alles landet in
/var/lib/rebel-sim/messungen/<Zeitstempel>/, "Fertig" packt den Ordner zu messung_<Zeitstempel>.zip
(Download im Lehrerbereich unter "Messungen").

  1  Container: Version, Dienste, Netz, Erreichbarkeit von SPS, Schueler-PC und ReBeL
  2  SPS lesen (OPC UA)              3  SPS schreiben (ROB_OUT-Testmuster)
  4  App-Schnittstelle Schueler-PC   5  App-Schnittstelle echter ReBeL (lesen, auf Wunsch ein GSig)
  6  CRI-Aufzeichnung: feste Stellungen nahe Home, Gelenke und X/Y/Z/A/B/C wie gemeldet
  7  Notiz                           8  Fertig: ZIP schreiben

Nie in der ZIP: Passwoerter (werden abgefragt und nur im Speicher gehalten), Lehrerkonten, Welten, Protokoll des
Lehrerbereichs. Am echten ReBeL startet der Messkoffer keine Programme; bewegt wird nur von Hand.

  rebel-sim-messung            (im Container als root)
"""

import contextlib
import io
import json
import math
import os
import re
import socket
import subprocess
import sys
import time
import zipfile

HIER = os.path.dirname(os.path.abspath(__file__))
BASIS = os.path.dirname(HIER)
sys.path.insert(0, HIER)
sys.path.insert(0, os.path.join(BASIS, "controllers", "irc_bridge"))

import rebel_kinematik as kin  # noqa: E402

ORDNER = os.environ.get("MESSUNGEN", "/var/lib/rebel-sim/messungen")
HOME = [0.0, 0.0, 90.0, 0.0, 90.0, 0.0]
# Nur A1, A4, A5, A6 aendern sich gegenueber Home: der Arm bleibt oben, der Greifer dreht bzw. kippt hoechstens 30 Grad
STELLUNGEN = [
    ("Home als Bezug", [0, 0, 90, 0, 90, 0]),
    ("nur A4", [0, 0, 90, 30, 90, 0]),
    ("Kippen und Drehen", [0, 0, 90, 0, 60, 45]),
    ("alles gemischt", [20, 0, 90, 30, 60, 45]),
    ("Gegenrichtung", [0, 0, 90, -40, 110, -60]),
]
ABWEICHUNG_GRAD = 2.0
GERAETE = (("sps", "SPS (AXCF 2152)", (4840,)), ("pc", "Schueler-PC mit iRC", (5000, 3921)),
           ("rebel", "echter ReBeL", (5000, 3920)))
DIENSTE = ["rebel-sim-web", "rebel-sim-firewall", "rebel-sim-tls.timer"]


# ---------------------------------------------------------------- Auswertung (ohne Netz, testbar)

def _rx(a):
    c, s = math.cos(math.radians(a)), math.sin(math.radians(a))
    return [[1, 0, 0], [0, c, -s], [0, s, c]]


def _ry(a):
    c, s = math.cos(math.radians(a)), math.sin(math.radians(a))
    return [[c, 0, s], [0, 1, 0], [-s, 0, c]]


def _rz(a):
    c, s = math.cos(math.radians(a)), math.sin(math.radians(a))
    return [[c, -s, 0], [s, c, 0], [0, 0, 1]]


def _mm(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def auswerten(gelenke, kartesisch):
    """Vergleicht die von der Steuerung gemeldete Lage (X Y Z mm, A B C Grad) mit der eigenen Vorwaertsrechnung.
    Die Orientierung haengt nicht von der Werkzeuglaenge ab; die Werkzeugverschiebung folgt aus der Differenz."""
    p, r = kin.fk(gelenke, 0.0)
    flansch = [[r[i * 3 + j] for j in range(3)] for i in range(3)]
    a, b, c = kartesisch[3:6]
    irc = _mm(_mm(_rz(a), _ry(b)), _rx(c))   # Rz(A) Ry(B) Rx(C), gemessen am 26.10.2026 mit iRC V15
    fehler = max(abs(irc[i][j] - flansch[i][j]) for i in range(3) for j in range(3))
    d = [kartesisch[i] - p[i] * 1000.0 for i in range(3)]
    werkzeug = [sum(flansch[k][i] * d[k] for k in range(3)) for i in range(3)]   # im Flanschsystem
    return {"orientierung_passt": fehler < 0.01, "orientierung_fehler": round(fehler, 5),
            "werkzeug_mm": [round(v, 2) for v in werkzeug]}


# ---------------------------------------------------------------- Ein- und Ausgabe

class Sitzung:
    def __init__(self, ordner=None):
        self.stempel = time.strftime("%Y-%m-%d_%H%M")
        self.ordner = ordner or os.path.join(ORDNER, self.stempel)
        os.makedirs(self.ordner, exist_ok=True)
        self.einst_datei = os.path.join(self.ordner, "einstellungen.json")
        self.einst = {}
        if os.path.exists(self.einst_datei):
            with open(self.einst_datei, encoding="utf-8") as f:
                self.einst = json.load(f)

    def merken(self, schluessel, wert):
        self.einst[schluessel] = wert
        self.json("einstellungen", self.einst, fest=True)

    def ip(self, schluessel, titel):
        alt = self.einst.get(schluessel, "")
        while True:
            wert = input("IP-Adresse %s%s: " % (titel, " [%s]" % alt if alt else "")).strip() or alt
            if re.fullmatch(r"[0-9A-Za-z.:-]{1,64}", wert or ""):
                self.merken(schluessel, wert)
                return wert
            print("  Bitte eine IP-Adresse oder einen Rechnernamen eingeben.")

    def datei(self, name, endung):
        """Freier Dateiname: name.endung, bei Wiederholung name_2.endung usw."""
        pfad, n = os.path.join(self.ordner, "%s.%s" % (name, endung)), 1
        while os.path.exists(pfad):
            n += 1
            pfad = os.path.join(self.ordner, "%s_%d.%s" % (name, n, endung))
        return pfad

    def json(self, name, daten, fest=False):
        pfad = os.path.join(self.ordner, name + ".json") if fest else self.datei(name, "json")
        with open(pfad, "w", encoding="utf-8") as f:
            json.dump(daten, f, ensure_ascii=False, indent=2, default=str)
        return pfad

    def text(self, name, text):
        pfad = self.datei(name, "txt")
        with open(pfad, "w", encoding="utf-8") as f:
            f.write(text)
        return pfad


class Mitschrift(io.TextIOBase):
    """Gibt alles aus und schreibt es zugleich mit (fuer die Ausgabe der Probetests)."""

    def __init__(self, ziel):
        self.ziel, self.puffer = ziel, []

    def write(self, s):
        self.ziel.write(s)
        self.puffer.append(s)
        return len(s)

    def flush(self):
        self.ziel.flush()


def ja(frage):
    return input(frage + " (j/n) ").strip().lower() in ("j", "ja", "y")


def _befehl(*args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        return "(%s: %s)" % (type(e).__name__, e)


def _port_offen(ip, port, zeit=2.0):
    try:
        with socket.create_connection((ip, port), timeout=zeit):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------- Schritte

def schritt_container(s):
    daten = {
        "zeit": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "version": _befehl("git", "-C", BASIS, "log", "-1", "--format=%h %cd %s", "--date=iso"),
        "geaendert": _befehl("git", "-C", BASIS, "status", "--short"),
        "dienste": {d: _befehl("systemctl", "is-active", d) for d in DIENSTE},
        "plaetze": _befehl("systemctl", "list-units", "rebel-sim@*", "--no-legend", "--all"),
        "robotermodell": os.path.isdir("/var/lib/rebel-sim/irc"),
        "python": sys.version.split()[0],
        "webots": _befehl("sh", "-c", "dpkg-query -W -f='${Version}' webots 2>/dev/null"),
        "speicher": _befehl("df", "-h", "/", "/var/lib/rebel-sim"),
        "arbeitsspeicher": _befehl("free", "-m"),
        "laufzeit": _befehl("uptime"),
        "netz": _befehl("ip", "-br", "addr"),
        "routen": _befehl("ip", "route"),
        "erreichbar": {},
    }
    print("Version %s" % daten["version"])
    for d, z in daten["dienste"].items():
        print("  %-22s %s" % (d, z))
    for schluessel, titel, ports in GERAETE:
        if not ja("%s pruefen?" % titel):
            continue
        ip = s.ip(schluessel, titel)
        ping = subprocess.run(["ping", "-c", "2", "-W", "1", ip], capture_output=True, text=True)
        offen = {str(p): _port_offen(ip, p) for p in ports}
        daten["erreichbar"][schluessel] = {"ip": ip, "ping": ping.returncode == 0,
                                           "ping_ausgabe": ping.stdout.strip().splitlines()[-1:] or [], "ports": offen}
        print("  %s %s: ping %s, Ports %s" % (titel, ip, "ok" if ping.returncode == 0 else "KEINE ANTWORT",
                                             ", ".join("%s %s" % (p, "offen" if o else "zu") for p, o in offen.items())))
    # nur die Simulationsplaetze, nicht der Lehrerbereich (dort stehen Anmeldeversuche)
    s.text("journal_plaetze", _befehl("journalctl", "-u", "rebel-sim@*", "--since", "-1d", "--no-pager", "-n", "400"))
    print("Gespeichert: %s" % s.json("container", daten))


def _probetest(s, name, argv):
    import probetest_sps
    mit = Mitschrift(sys.stdout)
    bericht = s.datei(name, "json")
    with contextlib.redirect_stdout(mit):
        try:
            probetest_sps.main(argv + ["--bericht", bericht])
        except SystemExit:
            pass
        except Exception as e:   # noqa: BLE001 - der Messkoffer soll weiterlaufen
            print("[FEHL] %s: %s" % (type(e).__name__, e))
    s.text(name + "_ausgabe", "".join(mit.puffer))


def schritt_sps(s, schreiben):
    ip = s.ip("sps", "SPS")
    benutzer = input("Benutzer fuer OPC UA [%s] (leer lassen = %s, '-' = anonym): "
                     % (s.einst.get("sps_benutzer", "admin"), s.einst.get("sps_benutzer", "admin"))).strip()
    benutzer = benutzer or s.einst.get("sps_benutzer", "admin")
    s.merken("sps_benutzer", benutzer)
    argv = ["opcua", "--sps", ip] + (["--anonym"] if benutzer == "-" else ["--benutzer", benutzer])
    if s.einst.get("sps_instanz"):
        argv += ["--programm-instanz", s.einst["sps_instanz"]]
    if schreiben:
        print("ROB_OUT wird 20 s lang auf das Testmuster 0b10100101 gesetzt und danach zurueckgestellt.")
        if not ja("Weiter?"):
            return
        argv += ["--schreiben", "--halten", "20"]
    _probetest(s, "sps_schreiben" if schreiben else "sps_lesen", argv)
    if not schreiben and ja("Liegen die ROB_*-Variablen in einer Programminstanz (z.B. MainInstance)?"):
        s.merken("sps_instanz", input("Name der Programminstanz: ").strip())
        print("Gemerkt, bitte Schritt 2 wiederholen.")


def schritt_app(s, schluessel):
    titel = dict((k, t) for k, t, _p in GERAETE)[schluessel]
    ip = s.ip(schluessel, titel)
    argv = ["irc", "--ziel", ip]
    if schluessel == "pc":
        if ja("Schreibtest am Schueler-PC (GSig 1 an/aus, Variable sps_wert=5)?"):
            argv += ["--schreiben", "--gsig", "1", "--variable", "sps_wert=5"]
    else:
        _probetest(s, "app_rebel_lesen", argv)
        if not ja("Jetzt am echten ReBeL ein globales Signal (GSig 1) kurz an und wieder aus schalten?"):
            return
        argv += ["--schreiben", "--gsig", "1"]
    _probetest(s, "app_%s" % schluessel, argv)


def schritt_cri(s):
    from cri_client import CriClient
    print("Gegen welche Steuerung?  1 = echter ReBeL (Port 3920)   2 = iRC am Schueler-PC (Port 3921)")
    wahl = input("> ").strip()
    schluessel, ports = ("rebel", [3920, 3921]) if wahl != "2" else ("pc", [3921, 3920])
    ip = s.ip(schluessel, dict((k, t) for k, t, _p in GERAETE)[schluessel])
    cri = CriClient(ip, ports, passive=True, log=lambda *a: None).start()
    ende = time.time() + 8
    while cri.state is None and time.time() < ende:
        time.sleep(0.2)
    if cri.state is None:
        print("Keine CRI-Daten von %s (Ports %s). In iRC bzw. am ReBeL die CRI-Schnittstelle pruefen." % (ip, ports))
        s.json("cri_%s" % schluessel, {"ip": ip, "ports": ports, "fehler": "keine CRI-Daten"})
        cri.stop()
        return
    print("Verbunden mit %s:%s. Der Messkoffer hoert nur mit und bewegt nichts." % (ip, cri.connected_port))
    print("Jede Stellung per Joint-Jog anfahren, dann Enter. 's' = ueberspringen (Aufbau im Weg), 'q' = Ende.")
    messungen = []
    for titel, soll in STELLUNGEN:
        print("\n%s: A1..A6 = %s" % (titel, "  ".join("%g" % v for v in soll)))
        while True:
            antwort = input("  Enter = aufnehmen, s = ueberspringen, q = Ende > ").strip().lower()
            if antwort in ("s", "q"):
                break
            st = cri.state
            ist = st.joints("current")
            abw = max(abs(a - b) for a, b in zip(ist, soll))
            if abw > ABWEICHUNG_GRAD:
                print("  Ist %s weicht um %.1f Grad ab." % (" ".join("%.1f" % v for v in ist), abw))
                if not ja("  Trotzdem aufnehmen?"):
                    continue
            erg = auswerten(ist, st.cart)
            messungen.append({"titel": titel, "soll": soll, "gelenke": ist, "gelenke_soll": st.joints("setpoint"),
                              "kartesisch": st.cart, "roh": st.raw, "auswertung": erg})
            print("  X/Y/Z %s  A/B/C %s" % (" ".join("%.1f" % v for v in st.cart[:3]), " ".join("%.2f" % v for v in st.cart[3:])))
            print("  A/B/C-Reihenfolge Rz(A) Ry(B) Rx(C): %s, Werkzeug %s mm"
                  % ("passt" if erg["orientierung_passt"] else "PASST NICHT (Fehler %.4f)" % erg["orientierung_fehler"],
                     " / ".join("%.1f" % v for v in erg["werkzeug_mm"])))
            break
        if antwort == "q":
            break
    cri.stop()
    pfad = s.json("cri_%s" % schluessel, {"ip": ip, "port": cri.connected_port, "messungen": messungen})
    print("\n%d Stellungen aufgenommen, gespeichert: %s" % (len(messungen), pfad))


def schritt_notiz(s):
    print("Notiz eingeben (z.B. Backenrichtung bei A6 = 0, Beobachtungen). Ende mit einer leeren Zeile.")
    zeilen = []
    while True:
        z = input()
        if not z:
            break
        zeilen.append(z)
    if zeilen:
        print("Gespeichert: %s" % s.text("notiz", time.strftime("%H:%M ") + "\n".join(zeilen) + "\n"))


def schritt_fertig(s):
    """Ordner zu messung_<stempel>.zip packen (neben dem Ordner) und den Inhalt zeigen."""
    ziel = os.path.join(os.path.dirname(s.ordner), "messung_%s.zip" % os.path.basename(s.ordner))
    with zipfile.ZipFile(ziel, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(os.listdir(s.ordner)):
            z.write(os.path.join(s.ordner, name), name)
    print("Inhalt von %s:" % ziel)
    with zipfile.ZipFile(ziel) as z:
        for e in z.infolist():
            print("  %8d  %s" % (e.file_size, e.filename))
    _besitzer(ziel)
    print("Download: Lehrerbereich -> Messungen (oder per scp aus %s)" % os.path.dirname(ziel))
    return ziel


def _besitzer(*pfade):
    """Dateien dem Dienstkonto geben, damit der Lehrerbereich sie anbieten kann."""
    try:
        import pwd
        n = pwd.getpwnam("rebelsim")
        for p in pfade:
            os.chown(p, n.pw_uid, n.pw_gid)
    except (ImportError, KeyError, PermissionError):
        pass


MENUE = [("1", "Container pruefen", schritt_container),
         ("2", "SPS lesen (OPC UA)", lambda s: schritt_sps(s, False)),
         ("3", "SPS schreiben (ROB_OUT-Testmuster)", lambda s: schritt_sps(s, True)),
         ("4", "App-Schnittstelle Schueler-PC", lambda s: schritt_app(s, "pc")),
         ("5", "App-Schnittstelle echter ReBeL", lambda s: schritt_app(s, "rebel")),
         ("6", "CRI-Aufzeichnung (Stellungen nahe Home)", schritt_cri),
         ("7", "Notiz", schritt_notiz),
         ("8", "Fertig: ZIP schreiben", schritt_fertig)]


def main():
    if os.geteuid() != 0:
        print("Bitte als root starten: rebel-sim-messung")
        return 1
    os.makedirs(ORDNER, exist_ok=True)
    _besitzer(ORDNER)
    alte = sorted(d for d in os.listdir(ORDNER) if os.path.isdir(os.path.join(ORDNER, d)))
    ordner = None
    if alte and ja("Letzte Messung %s fortsetzen?" % alte[-1]):
        ordner = os.path.join(ORDNER, alte[-1])
    s = Sitzung(ordner)
    _besitzer(s.ordner)
    print("Messung in %s" % s.ordner)
    while True:
        print()
        for nr, titel, _f in MENUE:
            print("  %s  %s" % (nr, titel))
        print("  0  Beenden (ohne ZIP, spaeter fortsetzbar)")
        wahl = input("> ").strip()
        if wahl == "0":
            return 0
        schritt = next((f for nr, _t, f in MENUE if nr == wahl), None)
        if schritt is None:
            continue
        print()
        try:
            schritt(s)
        except KeyboardInterrupt:
            print("\nabgebrochen")
        except EOFError:
            return 0
        for name in os.listdir(s.ordner):
            _besitzer(os.path.join(s.ordner, name))
        if wahl == "8":
            return 0


if __name__ == "__main__":
    sys.exit(main())
