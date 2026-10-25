"""Ein-/Ausgaenge der Simulation: Taster aus dem Browser -> digitale Eingaenge in iRC,
Ausgaenge aus iRC -> Leuchtmelder.

Eingaenge setzt nur die App-Schnittstelle von iRC (gRPC, Port 5000) ohne der Oberflaeche die
Steuerung wegzunehmen. Dafuer muss auf dem PC die App "WebotsSim" (deploy/irc_app/WebotsSim.zip)
installiert und freigeschaltet sein. Per CRI ginge es nur als aktiver Client, dann waere die
iRC-Oberflaeche gesperrt.

Die eine Nachricht (IOStateRequest aus robotcontrolapp.proto, MIT-Lizenz Commonplace Robotics)
wird hier von Hand kodiert; so braucht es nur grpcio und keine protobuf-Versionen.

Konfiguration je Welt in worlds/<welt>.json:
  "ea": {"taster":   [{"name": "Start", "din": 21, "art": "schliesser"|"oeffner", "modus": "tastend"|"rastend"}],
         "leuchten": [{"name": "Greifer zu", "dout": 32, "farbe": "gruen", "umkehren": false}]}
Nummern wie in der iRC-Anzeige; din_versatz/dout_versatz rechnen auf CRI-/App-Nummern um.
"""

import json
import os
import threading
import time

MIN_PULS_S = 0.3        # so lange bleibt ein kurz angetippter Taster mindestens gedrueckt (iRC-Programme pruefen zyklisch)
WIEDERHOLEN_S = 2.0     # Zustand regelmaessig neu senden (iRC neu gestartet, App neu freigeschaltet)
METHODE = "/robotcontrolapp.RobotControlApp/SetIOState"


# --- Protobuf von Hand (nur IOStateRequest) ---

def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _feld(nr, art, inhalt):
    """art 0 = Zahl (varint), 2 = Laenge + Bytes."""
    kopf = _varint(nr << 3 | art)
    return kopf + (_varint(inhalt) if art == 0 else _varint(len(inhalt)) + inhalt)


def io_anfrage(app, eingaenge):
    """IOStateRequest {app_name = 1; repeated DIn DIns = 2} mit DIn {id = 1; state = 3 (LOW 0, HIGH 1)}."""
    daten = _feld(1, 2, app.encode("utf-8"))
    for nr in sorted(eingaenge):
        din = _feld(1, 0, nr) + _feld(3, 0, 1 if eingaenge[nr] else 0)
        daten += _feld(2, 2, din)
    return daten


def fehlertext(fehler, app="WebotsSim"):
    """Kurze, verstaendliche Meldung zu einem Fehler der App-Schnittstelle (grpc.RpcError oder Netzfehler)."""
    text = str(getattr(fehler, "details", lambda: fehler)() or fehler)
    klein = text.lower()
    if "not installed or enabled" in klein:
        return "App %s in iRC nicht installiert oder nicht freigeschaltet" % app
    if "simulation" in klein:
        return "iRC ist nicht im Simulationsmodus"
    if any(w in klein for w in ("failed to connect", "connection refused", "unavailable", "deadline", "connect")):
        return "iRC am PC nicht erreichbar (läuft iRC? App-Schnittstelle Port 5000)"
    return "iRC-App meldet: %s" % text[:60]


# --- Konfiguration ---

def lade_ea(welt_pfad):
    """"ea" aus der JSON neben der Welt; fehlerhafte Eintraege werden uebergangen."""
    try:
        with open(os.path.splitext(welt_pfad)[0] + ".json", encoding="utf-8") as f:
            ea = json.load(f).get("ea") or {}
    except (OSError, ValueError, AttributeError):
        return {"taster": [], "leuchten": [], "sensoren": [], "naeherung": []}
    taster = [t for t in ea.get("taster", []) if isinstance(t, dict) and isinstance(t.get("din"), int)]
    leuchten = [l for l in ea.get("leuchten", []) if isinstance(l, dict) and isinstance(l.get("dout"), int)]
    sensoren = [s for s in ea.get("sensoren", []) if isinstance(s, dict) and isinstance(s.get("knoten"), str)
                and (isinstance(s.get("din"), int) or s.get("typ") == "farbe")]
    naeherung = [n for n in ea.get("naeherung", []) if isinstance(n, dict) and isinstance(n.get("knoten"), str)
                 and isinstance(n.get("din"), int)]
    return {"taster": taster[:16], "leuchten": leuchten[:16], "sensoren": sensoren[:32], "naeherung": naeherung[:32]}


class Taster:
    """Zustand der Taster und Schalter: gedrueckt (vom Browser) -> Signal am Eingang."""

    def __init__(self, liste, din_versatz=-1):
        self.liste = liste
        self.versatz = din_versatz
        self.gedrueckt = [False] * len(liste)
        self.seit = [0.0] * len(liste)
        self.loslassen_um = [None] * len(liste)

    def ereignis(self, index, an, jetzt):
        """Knopf im Browser gedrueckt (an=True) oder losgelassen. Rastend: jedes Druecken schaltet um."""
        if not 0 <= index < len(self.liste):
            return
        if self.liste[index].get("modus") == "rastend":
            if an:
                self.gedrueckt[index] = not self.gedrueckt[index]
            return
        if an:
            self.gedrueckt[index], self.seit[index], self.loslassen_um[index] = True, jetzt, None
        elif self.gedrueckt[index]:
            ende = self.seit[index] + MIN_PULS_S
            if jetzt >= ende:
                self.gedrueckt[index] = False
            else:
                self.loslassen_um[index] = ende   # zu kurz getippt: iRC soll den Puls trotzdem sehen

    def weiter(self, jetzt):
        for i, um in enumerate(self.loslassen_um):
            if um is not None and jetzt >= um:
                self.gedrueckt[i], self.loslassen_um[i] = False, None

    def signale(self):
        """{CRI-Nummer: Pegel}; Oeffner liefern ungedrueckt 1. Mehrere Taster am selben Eingang: ODER."""
        sig = {}
        for t, gedrueckt in zip(self.liste, self.gedrueckt):
            nr = t["din"] + self.versatz
            if 0 <= nr < 64:
                sig[nr] = sig.get(nr, False) or (gedrueckt != (t.get("art") == "oeffner"))
        return sig


FREI, BELEGT = [0.55, 0.6, 0.7], [0.15, 0.8, 0.3]   # Farbe der Sensorflaeche in der 3D-Ansicht


def _in_sensor(p_s, r_s, punkt):
    """Punkt (Welt) ins System der Sensorflaeche: R^T (p - o); r als 9 Werte zeilenweise wie Webots."""
    d = [punkt[i] - p_s[i] for i in range(3)]
    return [r_s[0 + k] * d[0] + r_s[3 + k] * d[1] + r_s[6 + k] * d[2] for k in range(3)]


def belegt(p_s, r_s, groesse, p_t, r_t, size_t, rand=0.0005):
    """Ueberschneidet der Huellquader des Werkstuecks (Mitte p_t, Drehung r_t, Kanten size_t) den Quader
    der Sensorflaeche (Ursprung Mitte unten p_s, Drehung r_s, groesse)? rand: so viel muss er hineinragen."""
    ecken = []
    for sx in (-0.5, 0.5):
        for sy in (-0.5, 0.5):
            for sz in (-0.5, 0.5):
                lokal = [sx * size_t[0], sy * size_t[1], sz * size_t[2]]
                welt = [p_t[i] + sum(r_t[i * 3 + j] * lokal[j] for j in range(3)) for i in range(3)]
                ecken.append(_in_sensor(p_s, r_s, welt))
    grenzen = [(-groesse[0] / 2, groesse[0] / 2), (-groesse[1] / 2, groesse[1] / 2), (0.0, groesse[2])]
    for k, (lo, hi) in enumerate(grenzen):
        if max(e[k] for e in ecken) <= lo + rand or min(e[k] for e in ecken) >= hi - rand:
            return False
    return True


# Farbsensor: Werkstueckfarbe -> naechste dieser Farben (Namen wie FARBNAMEN in tools/baukasten.py),
# zu weit weg von allen (z.B. grau, lila) = keine Farbe erkannt
FARBEN = {"rot": (0.85, 0.12, 0.1), "gruen": (0.15, 0.7, 0.2), "blau": (0.1, 0.4, 0.9),
          "gelb": (0.95, 0.85, 0.1), "weiss": (0.95, 0.95, 0.95), "schwarz": (0.08, 0.08, 0.08)}
FARB_ABSTAND = 0.35


def farbe_erkennen(rgb):
    """Name der naechsten Farbe aus FARBEN oder None."""
    name, abstand = min(((n, sum((a - b) ** 2 for a, b in zip(rgb, wert)) ** 0.5) for n, wert in FARBEN.items()),
                        key=lambda e: e[1])
    return name if abstand <= FARB_ABSTAND else None


class Sensoren:
    """Sensorflaechen der Welt (DEF SENSOR_n, Typ Sensorfeld): belegt -> Eingang, Farbe in der Ansicht.
    Farbsensoren (typ "farbe") melden die Farbe des Werkstuecks, das der Mitte am naechsten liegt:
    je Farbe ein Eingang (ausgabe "je_farbe") oder die Code-Zahl ueber Bits ab "din" (ausgabe "bits")."""

    def __init__(self, liste, robot, din_versatz=-1, log=print):
        self.liste, self.versatz = [], din_versatz
        self.knoten = []
        for s in liste:
            knoten = robot.getFromDef(s["knoten"])
            if knoten is None:
                log("E/A: Sensorflaeche %s fehlt in der Welt" % s["knoten"])
                continue
            self.liste.append(s)
            self.knoten.append(knoten)
        # je Sensor: True/False (belegt) bzw. Farbname oder None (Farbsensor)
        self.belegt = [None if s.get("typ") == "farbe" else False for s in self.liste]
        self._anzeige = [FREI] * len(self.liste)   # zuletzt gesetzte Farbe des Feldes (nur bei Aenderung setzen)

    def pruefen(self, teile):
        for i, (s, k) in enumerate(zip(self.liste, self.knoten)):
            p_s, r_s = k.getPosition(), k.getOrientation()
            groesse = k.getField("groesse").getSFVec3f()
            drauf = [t for t in teile if belegt(p_s, r_s, groesse, t.getPosition(), t.getOrientation(),
                                                 t.getField("size").getSFVec3f())]
            if s.get("typ") == "farbe":
                mitte = [0.0, 0.0, groesse[2] / 2]
                naechstes = min(drauf, default=None, key=lambda t: sum(
                    (a - b) ** 2 for a, b in zip(_in_sensor(p_s, r_s, t.getPosition()), mitte)))
                rgb = None if naechstes is None else naechstes.getField("farbe").getSFColor()
                jetzt, anzeige = (None, FREI) if rgb is None else (farbe_erkennen(rgb), list(rgb))
            else:
                jetzt = bool(drauf)
                anzeige = BELEGT if jetzt else FREI
            self.belegt[i] = jetzt
            if anzeige != self._anzeige[i]:   # Farbsensor: Feld in der Farbe des Teils, sonst grau/gruen
                self._anzeige[i] = anzeige
                k.getField("farbe").setSFColor(anzeige)

    def signale(self):
        sig = {}

        def setze(din, pegel):
            nr = din + self.versatz
            if 0 <= nr < 64:
                sig[nr] = sig.get(nr, False) or pegel

        for s, wert in zip(self.liste, self.belegt):
            if s.get("typ") != "farbe":
                setze(s["din"], wert != (s.get("art") == "oeffner"))
            elif s.get("ausgabe") == "bits":
                code = next((f["code"] for f in s.get("farben", []) if f["farbe"] == wert), 0)
                for bit in range(s.get("bits", 1)):
                    setze(s["din"] + bit, bool(code >> bit & 1))
            else:
                for f in s.get("farben", []):
                    setze(f["din"], wert == f["farbe"])
        return sig


# --- Naeherungssensoren (Stufe G2) ---
# Schaltabstand = Sn x Faktor des Materials (Reduktionsfaktoren wie in Datenblaettern, gerundet). Induktiv: nur Metall;
# kapazitiv: alles, Kunststoff und Holz schwaecher. Material steht im Feld "material" des Werkstuecks (Stufe G1).
FAKTOR = {"induktiv": {"stahl": 1.0, "edelstahl": 0.7, "messing": 0.5, "alu": 0.4, "pla": 0.0, "holz": 0.0},
          "kapazitiv": {"stahl": 1.0, "edelstahl": 1.0, "messing": 1.0, "alu": 1.0, "pla": 0.5, "holz": 0.4}}
HYSTERESE = 1.1           # einmal geschaltet, faellt der Sensor erst bei 10 % mehr Abstand ab
LED_AN, LED_AUS = [1.0, 0.82, 0.1], [0.35, 0.3, 0.1]


def faktor(typ, material):
    return FAKTOR.get(typ, {}).get(material or "pla", 0.0)


def _mv9(r, v):   # 3x3 (9 Zahlen, zeilenweise wie Webots) mal Vektor
    return [r[0] * v[0] + r[1] * v[1] + r[2] * v[2], r[3] * v[0] + r[4] * v[1] + r[5] * v[2],
            r[6] * v[0] + r[7] * v[1] + r[8] * v[2]]


def _mtv9(r, v):  # transponiert mal Vektor
    return [r[0] * v[0] + r[3] * v[1] + r[6] * v[2], r[1] * v[0] + r[4] * v[1] + r[7] * v[2],
            r[2] * v[0] + r[5] * v[1] + r[8] * v[2]]


def abstand_vor_sensor(sensor, durchmesser, laenge, reichweite, teil, wolke):
    """Kleinster Abstand (m) einer Teil-Oberflaeche vor der aktiven Flaeche, im aktiven Durchmesser und hoechstens
    `reichweite` weit; None, wenn nichts davor liegt. sensor/teil: (Position, Drehung als 9 Zahlen) in der Welt,
    wolke: Punktwolke der Teil-Oberflaeche im Teilsystem (kollision.Punktwolke)."""
    (o_s, r_s), (o_t, r_t) = sensor, teil
    r = durchmesser / 2.0
    # Erfassungsbereich (Zylinder vor der Flaeche, als Quader) ins Teilsystem, dort nur Punkte im Kasten holen
    ecken = []
    for x in (-r, r):
        for y in (-r, r):
            for z in (laenge - 0.002, laenge + reichweite):
                w = _mv9(r_s, [x, y, z])
                ecken.append(_mtv9(r_t, [o_s[i] + w[i] - o_t[i] for i in range(3)]))
    lo = [min(e[i] for e in ecken) for i in range(3)]
    hi = [max(e[i] for e in ecken) for i in range(3)]
    bester = None
    for q in wolke.in_box(lo, hi):
        w = _mv9(r_t, q)
        s = _mtv9(r_s, [o_t[i] + w[i] - o_s[i] for i in range(3)])   # Punkt im Sensorsystem
        vor = s[2] - laenge
        if -0.002 <= vor <= reichweite and s[0] * s[0] + s[1] * s[1] <= r * r and (bester is None or vor < bester):
            bester = vor
    return None if bester is None else max(0.0, bester)


class Naeherungssensoren:
    """Naeherungssensoren der Welt (DEF NAEHERUNG_n, Typ Naeherungssensor): Werkstueck davor -> Eingang, LED."""

    def __init__(self, liste, robot, din_versatz=-1, log=print, wolke_fn=None):
        self.liste, self.knoten, self.versatz, self.wolke_fn = [], [], din_versatz, wolke_fn
        for n in liste:
            knoten = robot.getFromDef(n["knoten"])
            if knoten is None:
                log("E/A: Naeherungssensor %s fehlt in der Welt" % n["knoten"])
                continue
            self.liste.append(n)
            self.knoten.append(knoten)
        self.an = [False] * len(self.liste)
        self.abstand_mm = [None] * len(self.liste)

    @staticmethod
    def material(node):
        feld = node.getField("material")
        return feld.getSFString() if feld is not None else "pla"

    def pruefen(self, teile):
        for i, (n, k) in enumerate(zip(self.liste, self.knoten)):
            d, laenge = n.get("durchmesser_mm", 12.0) / 1000.0, n.get("laenge_mm", 50.0) / 1000.0
            lage_s = (k.getPosition(), k.getOrientation())
            flaeche = [lage_s[0][j] + _mv9(lage_s[1], [0.0, 0.0, laenge])[j] for j in range(3)]
            bester = None
            for t in teile:
                f = faktor(n.get("typ"), self.material(t))
                if f <= 0:
                    continue
                reichweite = n["sn_mm"] / 1000.0 * f * (HYSTERESE if self.an[i] else 1.0)
                wolke = self.wolke_fn(t)
                pos = t.getPosition()
                if sum((pos[j] - flaeche[j]) ** 2 for j in range(3)) ** 0.5 > reichweite + wolke.radius + d:
                    continue   # zu weit weg, ohne Punkte anzusehen
                a = abstand_vor_sensor(lage_s, d, laenge, reichweite, (pos, t.getOrientation()), wolke)
                if a is not None and (bester is None or a < bester):
                    bester = a
            an = bester is not None
            self.abstand_mm[i] = None if bester is None else round(bester * 1000.0, 1)
            if an != self.an[i]:
                self.an[i] = an
                k.getField("led").setSFColor(LED_AN if an else LED_AUS)

    def signale(self):
        sig = {}
        for n, an in zip(self.liste, self.an):
            nr = n["din"] + self.versatz
            if 0 <= nr < 64:
                sig[nr] = sig.get(nr, False) or (an != (n.get("art") == "oeffner"))
        return sig


def leuchten_zustand(leuchten, dout, dout_versatz=-1):
    """Leuchtet der Melder? Ausgang aus der DOUT-Bitmaske, umkehren = an bei Ausgang AUS."""
    zustand = []
    for l in leuchten:
        bit = l["dout"] + dout_versatz
        an = bool(dout is not None and 0 <= bit < 64 and (dout >> bit) & 1)
        zustand.append(an != bool(l.get("umkehren")))
    return zustand


class AppVerbindung:
    """Schickt den Soll-Zustand der Eingaenge im Hintergrund an iRC (blockiert die Simulation nie)."""

    def __init__(self, host, port=5000, app="WebotsSim", log=print, kanal_fn=None):
        self.ziel = "%s:%d" % (host, port)
        self.app = app
        self.log = log
        self.kanal_fn = kanal_fn          # fuer Tests: liefert eine Funktion senden(bytes)
        self.soll = None
        self.gesendet = None
        self.meldung = "verbinde mit iRC-App ..."
        self.ok = False
        self._neu = threading.Event()
        self._stopp = threading.Event()
        self._faden = threading.Thread(target=self._lauf, daemon=True)

    def start(self):
        self._faden.start()
        return self

    def setze(self, eingaenge):
        if eingaenge != self.soll:
            self.soll = dict(eingaenge)
            self._neu.set()

    def stopp(self, alles_aus=True):
        """Beim Ende: alle verwalteten Eingaenge zuruecknehmen (Versuch, ohne Garantie)."""
        if alles_aus and self.soll:
            self.setze({nr: False for nr in self.soll})
            self._neu.set()
            time.sleep(0.3)
        self._stopp.set()
        self._neu.set()

    def _sender(self):
        if self.kanal_fn:
            return self.kanal_fn()
        import grpc   # erst hier: Welten ohne Taster brauchen kein gRPC
        kanal = grpc.insecure_channel(self.ziel)
        aufruf = kanal.unary_unary(METHODE, request_serializer=lambda b: b, response_deserializer=lambda b: b)
        return lambda daten: aufruf(daten, timeout=3)

    def _lauf(self):
        senden, letzte_meldung = None, None
        while not self._stopp.is_set():
            self._neu.wait(WIEDERHOLEN_S)
            self._neu.clear()
            soll = self.soll
            if soll is None:
                continue
            try:
                if senden is None:
                    senden = self._sender()
                senden(io_anfrage(self.app, soll))
                self.gesendet, self.ok = dict(soll), True
                self.meldung = "Eingaenge mit iRC verbunden"
            except ImportError:
                self.ok, self.meldung = False, "gRPC fehlt im Container (python3-grpcio)"
            except Exception as e:   # grpc.RpcError und Netzfehler
                self.ok = False
                self.meldung = fehlertext(e, self.app)
            if self.meldung != letzte_meldung:
                self.log("E/A: %s" % self.meldung)
                letzte_meldung = self.meldung
