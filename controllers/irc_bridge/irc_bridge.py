"""Webots-Controller: koppelt den simulierten ReBeL an igus Robot Control (iRC).

iRC rechnet und steuert, Webots zeigt nur den Aufbau auf dem Tisch:
  - Gelenkwinkel A1..A6 kommen per CRI aus iRC und werden 1:1 nachgefahren.
  - Die digitalen Ausgaenge aus iRC schalten den Greifer (siehe config.json).
    Schunk EGP 25 wie am echten ReBeL mit zwei Kanaelen: DOut31 oeffnet,
    DOut32 schliesst, beide aus = stromlos (Backen bleiben stehen).
  - Greift der Greifer, wird das Werkstueck zwischen den Backen bzw. am
    Sauger festgehalten, bis der Greifer wieder oeffnet.

Einstellungen stehen in config.json neben dieser Datei. Fuer den Betrieb mit
mehreren Plaetzen (eine Webots-Instanz je Schueler-PC) gehen diese
Umgebungsvariablen vor:
  IRC_HOST    Rechner mit iRC, z.B. R212-PC07 oder 192.168.2.142
  IRC_PORTS   Ports der iRC-Steuerung, z.B. "3921,3920"
  PLATZ       Platzname, z.B. 07 -> platz-07.log / platz-07.json
  LOG_DIR     Ordner fuer die Logdatei (Standard: neben dieser Datei)
  STATUS_DIR  Ordner fuer die Statusdatei (Standard: wie LOG_DIR)
"""

import json
import math
import os
import time

from controller import Supervisor

from cri_client import CriClient
import ea

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "ports": [3921, 3920],
    "platz": "",
    "log_ordner": "",
    "status_ordner": "",
    "nur_mithoeren": True,
    "gelenkwerte": "current",
    "achsrichtung": [1, 1, 1, 1, 1, 1],
    "achsversatz_grad": [0, 0, 0, 0, 0, 0],
    "greifer_dout_oeffnen": 31,
    "greifer_dout_schliessen": 32,
    "greifer_dout_vakuum": 32,
    "dout_versatz": -1,
    "din_versatz": -1,          # Taster: Nummer wie in der iRC-Anzeige -> CRI-/App-Nummer
    "app_port": 5000,           # App-Schnittstelle von iRC (gRPC), dort setzt die App WebotsSim Eingaenge
    "app_name": "WebotsSim",
    "greifer_invertiert": False,
    "abweichung_warnen_ab_mm": 5.0,
}


_print = print
LOGDATEI = None   # wird nur im Webots-Betrieb gesetzt, nicht in den Tests


def print(*args):   # noqa: A001 - Meldungen auch in irc_bridge.log schreiben
    text = " ".join(str(a) for a in args)
    _print(text)
    if LOGDATEI is None:
        return
    try:
        with open(LOGDATEI, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
    except OSError:
        pass


def load_config(environ=None, path=None):
    environ = os.environ if environ is None else environ
    path = os.path.join(HERE, "config.json") if path is None else path
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg.update({k: v for k, v in json.load(f).items() if not k.startswith("_")})
    # Umgebungsvariablen gehen vor: eine Webots-Instanz je Platz
    if environ.get("IRC_HOST"):
        cfg["host"] = environ["IRC_HOST"].strip()
    if environ.get("IRC_PORTS"):
        try:
            cfg["ports"] = [int(p) for p in environ["IRC_PORTS"].replace(";", ",").split(",") if p.strip()]
        except ValueError:
            print("IRC_PORTS ungueltig (%r), nehme %s" % (environ["IRC_PORTS"], cfg["ports"]))
    for var, key in (("PLATZ", "platz"), ("LOG_DIR", "log_ordner"), ("STATUS_DIR", "status_ordner")):
        if environ.get(var):
            cfg[key] = environ[var].strip()
    return cfg


def _dateiname(cfg, endung):
    return ("platz-%s.%s" % (cfg["platz"], endung)) if cfg["platz"] else ("irc_bridge.%s" % endung)


def log_pfad(cfg):
    return os.path.join(cfg["log_ordner"] or HERE, _dateiname(cfg, "log"))


def status_pfad(cfg):
    return os.path.join(cfg["status_ordner"] or cfg["log_ordner"] or HERE, _dateiname(cfg, "json"))


def befehl_pfad(cfg):
    """Befehle vom Webdienst (z.B. Teile zuruecksetzen) liegen neben der Statusdatei."""
    return os.path.join(cfg["status_ordner"] or cfg["log_ordner"] or HERE, _dateiname(cfg, "befehl"))


def hole_befehle(pfad):
    """Befehle (eine Zeile je Befehl, der Webdienst haengt an) lesen und entfernen. Erst umbenennen,
    damit eine gerade angehaengte Zeile nicht verloren geht; jeder Befehl wird nur einmal ausgefuehrt."""
    lesen = pfad + ".lesen"
    try:
        os.replace(pfad, lesen)
    except OSError:
        return []
    try:
        with open(lesen, encoding="utf-8") as f:
            zeilen = [z.strip() for z in f if z.strip()]
        os.remove(lesen)
    except OSError:
        return []
    return zeilen


def schreibe_status(pfad, daten):
    """Statusdatei fuer die Uebersichtsseite; erst in eine Hilfsdatei, dann
    ersetzen, damit nie eine halbe Datei gelesen wird."""
    tmp = pfad + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(daten, f, ensure_ascii=False, indent=1)
        os.replace(tmp, pfad)
    except OSError:
        pass


# --- kleine Matrix-Helfer (3x3, zeilenweise wie Webots getOrientation) ---

def m_t(a):
    return [a[0], a[3], a[6], a[1], a[4], a[7], a[2], a[5], a[8]]


def m_mul(a, b):
    return [sum(a[r * 3 + k] * b[k * 3 + c] for k in range(3)) for r in range(3) for c in range(3)]


def m_vec(a, v):
    return [a[r * 3] * v[0] + a[r * 3 + 1] * v[1] + a[r * 3 + 2] * v[2] for r in range(3)]


def achse_winkel_zu_matrix(aa):
    """Webots-Rotation (Achse x y z, Winkel) -> 3x3 zeilenweise wie getOrientation."""
    x, y, z, w = aa
    laenge = math.sqrt(x * x + y * y + z * z) or 1.0
    x, y, z = x / laenge, y / laenge, z / laenge
    c, s, t = math.cos(w), math.sin(w), 1 - math.cos(w)
    return [t * x * x + c, t * x * y - s * z, t * x * z + s * y,
            t * x * y + s * z, t * y * y + c, t * y * z - s * x,
            t * x * z - s * y, t * y * z + s * x, t * z * z + c]


def m_to_axis_angle(m):
    cos_a = max(-1.0, min(1.0, (m[0] + m[4] + m[8] - 1.0) / 2.0))
    angle = math.acos(cos_a)
    if angle < 1e-6:
        return [0.0, 0.0, 1.0, 0.0]
    if math.pi - angle < 1e-4:
        diag = [m[0], m[4], m[8]]
        i = diag.index(max(diag))
        axis = [0.0, 0.0, 0.0]
        axis[i] = math.sqrt(max(0.0, (diag[i] + 1.0) / 2.0))
        j, k = (i + 1) % 3, (i + 2) % 3
        axis[j] = m[i * 3 + j] / (2.0 * axis[i])
        axis[k] = m[i * 3 + k] / (2.0 * axis[i])
        return axis + [angle]
    s = 2.0 * math.sin(angle)
    return [(m[7] - m[5]) / s, (m[2] - m[6]) / s, (m[3] - m[1]) / s, angle]


def sub(a, b):
    return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]


def add(a, b):
    return [a[0] + b[0], a[1] + b[1], a[2] + b[2]]


def lies_greiferdaten(text):
    """Kenndaten aus Robot.customData, z.B. 'typ=egp25 tcp=0.085 spalt=0.0055'."""
    daten = {}
    for eintrag in (text or "").split():
        if "=" in eintrag:
            k, v = eintrag.split("=", 1)
            try:
                daten[k] = float(v)
            except ValueError:
                daten[k] = v
    return daten


NETZE = {}   # eingelesene Netze der CAD-Werkstuecke, je (url, skalierung, versatz)


def lies_netz(pfad, skal, versatz):
    """OBJ eines Werkstuecks: (Eckpunkte im Teilsystem in m, Dreiecke als Indextripel).
    Wie im Werkstueck-Proto: Punkt * skalierung + modellversatz."""
    punkte, dreiecke = [], []
    try:
        with open(pfad, encoding="utf-8", errors="replace") as f:
            for zeile in f:
                if zeile.startswith("v "):
                    x, y, z = (float(v) for v in zeile.split()[1:4])
                    punkte.append([x * skal + versatz[0], y * skal + versatz[1], z * skal + versatz[2]])
                elif zeile.startswith("f "):
                    ecken = [int(c.split("/")[0]) for c in zeile.split()[1:]]
                    ecken = [i - 1 if i > 0 else len(punkte) + i for i in ecken]
                    for k in range(1, len(ecken) - 1):
                        dreiecke.append((ecken[0], ecken[k], ecken[k + 1]))
    except OSError:
        print("Netz nicht lesbar: %s - greife mit dem Huellquader" % pfad)
        return None
    return punkte, dreiecke


def _abschneiden(poly, achse, wert, groesser):
    """Vieleck an der Ebene koordinate[achse] = wert abschneiden (Sutherland-Hodgman);
    behalten wird die Seite >= wert (groesser) bzw. <= wert."""
    def innen(q):
        return q[achse] >= wert if groesser else q[achse] <= wert
    neu = []
    for i, a in enumerate(poly):
        b = poly[(i + 1) % len(poly)]
        if innen(a):
            neu.append(a)
        if innen(a) != innen(b):
            t = (wert - a[achse]) / (b[achse] - a[achse])
            neu.append([a[k] + (b[k] - a[k]) * t for k in range(3)])
    return neu


class Greifer:
    """Backengreifer (Schunk EGP, zwei Kanaele) oder Sauggreifer (ein Kanal),
    je nachdem welche Geraete das Modell hat."""

    # Masse des EGP 25 ohne Finger, falls das Modell keine Kenndaten mitbringt
    BACKEN = {"spalt": 0.0055, "hub": 0.0054, "backe_y": 0.005, "backe_z0": -0.0043, "backe_z1": 0.0039}

    def __init__(self, robot, cfg, tcp_node):
        self.robot = robot
        self.tcp = tcp_node
        names = [robot.getDeviceByIndex(i).getName() for i in range(robot.getNumberOfDevices())]
        daten = lies_greiferdaten(robot.getCustomData())
        versatz = cfg["dout_versatz"]
        self.fingers = []
        self.led = None
        self.dout_auf = self.dout_zu = None   # wie in iRC angezeigt
        if "finger_links" in names:
            self.typ = str(daten.get("typ", "egp25"))
            self.fingers = [robot.getDevice("finger_links"), robot.getDevice("finger_rechts")]
            self.masse = {k: float(daten.get(k, v)) for k, v in self.BACKEN.items()}
            self.dout_auf, self.dout_zu = cfg["greifer_dout_oeffnen"], cfg["greifer_dout_schliessen"]
        elif "vakuum_led" in names:
            self.typ = "vakuum"
            self.led = robot.getDevice("vakuum_led")
            self.dout_zu = cfg["greifer_dout_vakuum"]
        else:
            self.typ = "kein"
        self.bit_auf = None if self.dout_auf is None else self.dout_auf + versatz
        self.bit_zu = None if self.dout_zu is None else self.dout_zu + versatz
        self.invertiert = cfg["greifer_invertiert"]
        self.kanaele = None
        self.zu = False
        self.teil = None
        self.teil_name = ""
        self.halte_pos = None
        self.halte_rot = None
        self.meldung = ""
        self.auflagen = lambda: []   # Netze der Aufbauten (setzt main), fuer _auf_auflage
        self.abgelegt = {}           # Node-Id -> (Node, Lage, Drehung): auf Aufbauten abgelegt, festgehalten
        self.tischhoehe = lambda: None   # Oberkante des Tischs in m (setzt main)
        self.ablage = "auflage"          # oder "liegen_lassen" (Einstellung der Welt, siehe _auf_auflage)

    def beschreibung(self):
        if self.bit_auf is not None:
            return "%s, oeffnen DOut%d (Bit %d), schliessen DOut%d (Bit %d)" % (
                self.typ, self.dout_auf, self.bit_auf, self.dout_zu, self.bit_zu)
        if self.bit_zu is not None:
            return "%s an DOut%d (Bit %d)" % (self.typ, self.dout_zu, self.bit_zu)
        return self.typ

    def befehl(self, dout):
        """Was die Ausgaenge (Bitmaske aus iRC) verlangen: True = schliessen,
        False = oeffnen, None = nichts aendern."""
        if self.bit_zu is None:
            return None
        zu = bool((dout >> self.bit_zu) & 1)
        if self.bit_auf is None:   # Sauger: ein Ausgang
            return zu != self.invertiert
        auf = bool((dout >> self.bit_auf) & 1)
        if (auf, zu) != self.kanaele:
            if self.kanaele is not None and auf == zu:
                print("Greifer: DOut%d und DOut%d beide %s - %s" % (
                    self.dout_auf, self.dout_zu, "EIN" if auf else "AUS",
                    "Fehler, Greifer schaltet ab" if auf else "stromlos, Backen bleiben stehen"))
            if auf and zu:
                self.meldung = "Fehler: oeffnen und schliessen gleichzeitig EIN"
            self.kanaele = (auf, zu)
        if auf == zu:
            return None
        return zu

    def schalte(self, zu, teile):
        if zu == self.zu:
            return
        self.zu = zu
        if zu:
            self._greifen(teile)
        else:
            self._loslassen()

    def _tcp_lage(self):
        return self.tcp.getPosition(), self.tcp.getOrientation()

    def _halbe_ausdehnung(self, r_rel, size):
        return [0.5 * sum(abs(r_rel[k * 3 + j]) * size[j] for j in range(3)) for k in range(3)]

    def _im_werkzeug(self, node, p, rt):
        """Lage eines Teils im Werkzeugsystem: Mittelpunkt, Drehung, halbe Ausdehnung."""
        lokal = m_vec(rt, sub(node.getPosition(), p))
        r_rel = m_mul(rt, node.getOrientation())
        return lokal, r_rel, self._halbe_ausdehnung(r_rel, node.getField("size").getSFVec3f())

    # --- Querschnitt auf Hoehe der Backen ---

    def _netz(self, node):
        """Dreiecke eines CAD-Werkstuecks (form "modell") im Teilsystem (m), sonst None."""
        try:
            if node.getField("form").getSFString() != "modell":
                return None
            url = node.getField("modell").getMFString(0)
            skal = node.getField("skalierung").getSFFloat()
            versatz = node.getField("modellversatz").getSFVec3f()
        except (AttributeError, IndexError, TypeError):
            return None
        schluessel = (url, skal, tuple(versatz))
        if schluessel not in NETZE:
            NETZE[schluessel] = lies_netz(os.path.join(os.path.dirname(self.robot.getWorldPath()), url), skal, versatz)
        return NETZE[schluessel]

    def _querschnitt(self, node, lokal, r_rel, e):
        """Ausdehnung des Teils in Greifrichtung (x im Werkzeug) innerhalb des Bereichs der
        Backen (Hoehe backe_z0..z1, Breite +-backe_y): (xmin, xmax) in m oder None, wenn dort
        nichts vom Teil ist. CAD-Werkstuecke mit ihrer echten Form, sonst der Huellquader."""
        m = self.masse
        z0, z1, yb = m["backe_z0"], m["backe_z1"], m["backe_y"]
        netz = self._netz(node)
        if netz is None:
            if lokal[2] - e[2] < z1 and lokal[2] + e[2] > z0 and abs(lokal[1]) < e[1] + yb:
                return lokal[0] - e[0], lokal[0] + e[0]
            return None
        punkte = [add(m_vec(r_rel, v), lokal) for v in netz[0]]
        xmin = xmax = None
        for a, b, c in netz[1]:
            poly = [punkte[a], punkte[b], punkte[c]]
            for achse, wert, groesser in ((2, z0, True), (2, z1, False), (1, -yb, True), (1, yb, False)):
                poly = _abschneiden(poly, achse, wert, groesser)
                if not poly:
                    break
            for q in poly:
                xmin = q[0] if xmin is None else min(xmin, q[0])
                xmax = q[0] if xmax is None else max(xmax, q[0])
        return None if xmin is None else (xmin, xmax)

    def _passt(self, node, lokal, r_rel, e):
        if self.fingers:
            # der Querschnitt auf Hoehe der Backen passt zwischen die offenen Backen
            q = self._querschnitt(node, lokal, r_rel, e)
            spalt = self.masse["spalt"] + 0.0002
            return q is not None and q[0] >= -spalt and q[1] <= spalt
        if self.typ == "vakuum":
            abstand = lokal[2] - e[2]
            return -0.005 < abstand < 0.015 and abs(lokal[0]) < e[0] and abs(lokal[1]) < e[1]
        return False

    def _soll_tiefe(self, e):
        """Wo der Teilemittelpunkt beim Greifen auf der Werkzeugachse liegen soll (ab TCP)."""
        if self.fingers:
            m = self.masse
            # Oberkante knapp unter dem Greiferkoerper; ist das Teil kuerzer als die
            # Backen, stattdessen Backenspitzen 0,5 mm ueber der Unterkante (z zeigt nach unten)
            oberkante = max(m["backe_z0"] + 0.0005, m["backe_z1"] - 2 * e[2] + 0.0005)
            return oberkante + e[2]
        return e[2]   # Sauger liegt auf der Oberseite auf

    def einteach_hilfe(self, teile, basis_rot, weit_ab=0.15):
        """Fuer das naechste Werkstueck: wie weit der TCP noch von der Greifstellung
        entfernt ist, in Basiskoordinaten wie in iRC (mm). None ohne Greifer/Teile
        oder solange ein Teil gegriffen ist."""
        if not teile or self.typ == "kein" or self.teil is not None:
            return None
        p, r = self._tcp_lage()
        rt = m_t(r)
        naechstes = None
        for node in teile:
            lokal, r_rel, e = self._im_werkzeug(node, p, rt)
            d = math.sqrt(sum(c * c for c in lokal))
            if naechstes is None or d < naechstes[0]:
                naechstes = (d, node, lokal, r_rel, e)
        d, node, lokal, r_rel, e = naechstes
        name = node.getField("name").getSFString()
        q = self._querschnitt(node, lokal, r_rel, e) if self.fingers and d <= weit_ab else None
        # Soll-TCP: Teil (bzw. sein Querschnitt an den Backen) mittig, auf der richtigen Tiefe
        soll = sub(node.getPosition(), m_vec(r, [0.0, 0.0, self._soll_tiefe(e)]))
        if q is not None:
            soll = add(soll, m_vec(r, [(q[0] + q[1]) / 2 - lokal[0], 0.0, 0.0]))
        delta = [v * 1000.0 for v in m_vec(m_t(basis_rot), sub(soll, p))]
        greifbar = self._passt(node, lokal, r_rel, e)
        hilfe = {"teil": name, "dx": round(delta[0], 1), "dy": round(delta[1], 1), "dz": round(delta[2], 1),
                 "greifbar": greifbar}
        breite = None if not self.fingers else (q[1] - q[0] if q is not None else 2 * e[0])
        if d > weit_ab:
            hilfe["text"] = "naechstes Teil: %s, %.0f mm entfernt" % (name, d * 1000)
        elif breite is not None and breite > 2 * self.masse["spalt"] + 0.0004 and (q is not None or self._netz(node) is None):
            # in Richtung der Backen gemessen, haengt also auch von der Drehung (A6) ab
            offen = 2000 * self.masse["spalt"]
            hilfe["text"] = "%s: %s %.1f mm breit, Backen oeffnen nur %.1f mm" % (
                name, "an den Backen" if q is not None and self._netz(node) is not None else "", 1000 * breite, offen)
            hilfe["text"] = hilfe["text"].replace("  ", " ")
            # schmalste Seite des Teils (quer zur Werkzeugachse): passt es ueberhaupt?
            groesse = node.getField("size").getSFVec3f()
            schmal = sorted(abs(v) for v in groesse)[:2]
            if self._netz(node) is not None:
                hilfe["text"] += " - an schmalerer Stelle greifen oder Greifer drehen (A6)"
            elif e[1] <= self.masse["spalt"]:
                hilfe["text"] += " - Greifer um 90 Grad drehen (A6)"
            elif min(schmal) / 2 <= self.masse["spalt"]:
                hilfe["text"] += " - Greifer drehen (A6), bis die Backen parallel zum Teil stehen"
            else:
                hilfe["text"] += " - zu gross fuer diesen Greifer (andere Backen oder Sauger)"
        elif max(abs(v) for v in delta) < 1.0:
            hilfe["text"] = "%s: Greifstellung erreicht" % name
        else:
            # round(v) + 0: aus -0.4 wird 0 statt "-0"
            hilfe["text"] = "%s: noch X %+d  Y %+d  Z %+d mm%s" % (
                (name,) + tuple(int(round(v)) + 0 for v in delta) + (" - greifbar" if greifbar else "",))
        return hilfe

    def _greifen(self, teile):
        p, r = self._tcp_lage()
        rt = m_t(r)
        bestes = None
        for node in teile:
            lokal, r_rel, e = self._im_werkzeug(node, p, rt)
            d = math.sqrt(sum(c * c for c in lokal))
            if d < 0.3 and self._passt(node, lokal, r_rel, e):
                if bestes is None or d < bestes[0]:
                    bestes = (d, node, lokal, r_rel, e)
        if bestes is None:
            self.meldung = "Greifer zu - kein Werkstueck in Reichweite"
            naechstes = None
            for node in teile:
                lokal = m_vec(rt, sub(node.getPosition(), p))
                d = math.sqrt(sum(c * c for c in lokal))
                if naechstes is None or d < naechstes[0]:
                    naechstes = (d, node.getField("name").getSFString(), lokal)
            if naechstes is not None:
                d, name, lokal = naechstes
                print("%s (naechstes Teil '%s': %.0f mm entfernt, im Werkzeug x=%.0f y=%.0f z=%.0f mm)"
                      % (self.meldung, name, d * 1000, lokal[0] * 1000, lokal[1] * 1000, lokal[2] * 1000))
            else:
                print(self.meldung)
            for f in self.fingers:
                f.setPosition(self.masse["hub"])
            if self.led:
                self.led.set(1)
            return
        _d, node, lokal, r_rel, e = bestes
        self.abgelegt.pop(node.getId(), None)   # war auf einem Aufbau festgehalten
        self.teil = node
        self.teil_name = node.getField("name").getSFString()
        self.halte_pos = lokal
        self.halte_rot = r_rel
        self.meldung = "gegriffen: %s" % self.teil_name
        print(self.meldung)
        if self.fingers:
            # zentrischer Greifer: das Teil rutscht so, dass sein Querschnitt an den Backen mittig sitzt
            xmin, xmax = self._querschnitt(node, lokal, r_rel, e)
            self.halte_pos = [lokal[0] - (xmin + xmax) / 2, lokal[1], lokal[2]]
            hub = max(0.0, min(self.masse["hub"], self.masse["spalt"] - (xmax - xmin) / 2 - 0.0002))
            for f in self.fingers:
                f.setPosition(hub)
        if self.led:
            self.led.set(1)

    def _festlegen(self, node, hub):
        feld = node.getField("translation")
        t = feld.getSFVec3f()
        lage = [t[0], t[1], t[2] + hub]
        feld.setSFVec3f(lage)
        self.abgelegt[node.getId()] = (node, lage, node.getField("rotation").getSFRotation())

    def _auf_auflage(self, node):
        """Wird ein Teil ueber einem Aufbau (Magazin, Vorrichtung) losgelassen, kommt es direkt auf dessen Auflage
        und bleibt dort liegen (festhalten), bis es wieder gegriffen wird. Grund: Das Werkstueck kollidiert als
        Quader, der Aufbau als Netz; faellt ein Quader in eine Tasche, wirft ihn die Netz-Kollision weg (durch
        duenne Boeden nach unten, zur Seite). Gezaehlt wird nur der Kern der Grundflaeche (60 %), damit
        Taschenwaende am Rand nicht tragen. Ueber dem Tisch bleibt die Physik zustaendig.
        Ablage "liegen_lassen" (Einstellung der Welt, nachsichtig): Steckt das Teil beim Loslassen zu tief im
        Aufbau oder im Tisch, bleibt es genau dort (festgehalten), wo der Greifer es loslaesst.
        Rueckgabe (Verschiebung nach oben in m oder None ohne Auflage darunter, steckt-Tiefe in m oder 0)."""
        p, r = node.getPosition(), node.getOrientation()
        size = node.getField("size").getSFVec3f()
        halb = [0.5 * sum(abs(r[k * 3 + j]) * size[j] for j in range(3)) for k in range(3)]
        netze = self.auflagen()
        auflage = auflage_hoehe(netze, p, [0.6 * halb[0], 0.6 * halb[1]], p[2]) if netze else None
        unten = p[2] - halb[2]
        if self.ablage == "liegen_lassen":
            tisch = self.tischhoehe()
            stuetze = max((h for h in (auflage, tisch) if h is not None), default=None)
            if stuetze is not None and unten < stuetze - 0.0005:
                self._festlegen(node, 0.0)
                return 0.0, stuetze - unten
        if auflage is None:
            return None, 0.0
        hub = auflage + 0.0002 - unten
        self._festlegen(node, hub)
        return hub, 0.0

    def startlagen_festhalten(self, teile, startlagen):
        """Teile, deren Startlage auf einem Aufbau liegt (z.B. in einer Magazintasche), von Anfang an festhalten.
        Sonst sinken sie schon vor dem ersten Griff ein (der Quader rutscht durch den Netz-Boden, beim Target im
        Magazin 5 mm), werden eingesunken gegriffen und gelten beim Ablegen am selben Punkt als zu tief.
        Erst nach dem ersten Simulationsschritt aufrufen (dann kennt Webots die Lage der Aufbauten)."""
        netze = self.auflagen()
        anzahl = 0
        for node in teile if netze else []:
            lage = startlagen.get(node.getId())
            if lage is None or node is self.teil or node.getId() in self.abgelegt:
                continue
            p, drehung = list(lage[0]), list(lage[1])
            r = achse_winkel_zu_matrix(drehung)
            size = node.getField("size").getSFVec3f()
            halb = [0.5 * sum(abs(r[k * 3 + j]) * size[j] for j in range(3)) for k in range(3)]
            auflage = auflage_hoehe(netze, p, [0.6 * halb[0], 0.6 * halb[1]], p[2])
            if auflage is not None and abs(p[2] - halb[2] - auflage) <= 0.002:
                self.abgelegt[node.getId()] = (node, p, drehung)
                anzahl += 1
        return anzahl

    def festhalten(self):
        """Auf Aufbauten abgelegte Teile ruhig halten (in jedem Schritt; siehe _auf_auflage)."""
        for node, lage, drehung in self.abgelegt.values():
            if max(abs(a - b) for a, b in zip(node.getPosition(), lage)) > 0.0001:
                node.getField("translation").setSFVec3f(list(lage))
                node.getField("rotation").setSFRotation(list(drehung))
            node.resetPhysics()

    def _loslassen(self):
        steckt = 0.0
        if self.teil is not None:
            hub, steckt = self._auf_auflage(self.teil)
            self.teil.resetPhysics()
            x, y, z = self.teil.getPosition()
            if steckt:
                zusatz = (" - steckt %.1f mm zu tief, bleibt dort liegen (Ablage nachsichtig; am echten Roboter: "
                          "Ablegepunkt in iRC hoeher setzen)" % (steckt * 1000))
            elif hub is None:
                zusatz = ""
            elif hub > 0.001:
                zusatz = " - lag %.1f mm zu tief im Aufbau, auf die Auflage gehoben (Ablegepunkt in iRC hoeher setzen)" % (hub * 1000)
            else:
                zusatz = " - liegt auf dem Aufbau" + (" (%.1f mm abgesenkt)" % (-hub * 1000) if hub < -0.001 else "")
            print("losgelassen: %s bei x=%.0f y=%.0f z=%.0f mm%s" % (
                self.teil_name, x * 1000, y * 1000, (z + (hub or 0.0)) * 1000, zusatz))
        # zu tief abgelegt: im 3D-Bild stehen lassen, bis wieder gegriffen wird (am echten Roboter ein Crash)
        if self.teil is not None and steckt:
            self.meldung = "offen - %s %.1f mm zu tief abgelegt (bleibt liegen)" % (self.teil_name, steckt * 1000)
        elif self.teil is not None and hub is not None and hub > 0.001:
            self.meldung = "offen - %s lag %.1f mm zu tief im Aufbau: Ablegepunkt in iRC hoeher setzen" % (
                self.teil_name, hub * 1000)
        else:
            self.meldung = "Greifer offen"
        self.teil = None
        for f in self.fingers:
            f.setPosition(0.0)
        if self.led:
            self.led.set(0)

    def fallen_lassen(self, meldung):
        """Gegriffenes Teil freigeben, ohne die Backen zu bewegen (die steuert weiter iRC).
        Auch festgehaltene Teile sind danach wieder frei (z.B. vor "Teile zurueck")."""
        self.abgelegt = {}
        self.teil = None
        self.teil_name = ""
        self.meldung = meldung

    def mitfuehren(self):
        if self.teil is None:
            return
        p, r = self._tcp_lage()
        self.teil.getField("translation").setSFVec3f(add(p, m_vec(r, self.halte_pos)))
        self.teil.getField("rotation").setSFRotation(m_to_axis_angle(m_mul(r, self.halte_rot)))
        self.teil.resetPhysics()


def werkstuecke(robot):
    kinder = robot.getRoot().getField("children")
    return [kinder.getMFNode(i) for i in range(kinder.getCount())
            if kinder.getMFNode(i).getTypeName() == "Werkstueck"]


# --- Auflage beim Loslassen: Teile, die zu tief in einem Aufbau abgelegt werden ---
# Legt iRC ein Teil z.B. 5 mm unter dem Boden einer Magazintasche ab, steckt es beim Loslassen im
# Aufbau. Die Physik schiebt es dann durch duenne Boeden nach UNTEN durch (Netz-Kollision), und beim
# naechsten Griff liegt es zu tief. Deshalb wird es vorher auf die Auflage darunter gehoben.

AUFBAU_NETZE = {}   # Aufbauten stehen fest: Dreiecke in Weltkoordinaten nur einmal je Node rechnen


def aufbauten(robot):
    kinder = robot.getRoot().getField("children")
    return [kinder.getMFNode(i) for i in range(kinder.getCount())
            if kinder.getMFNode(i).getTypeName() == "Aufbau"]


def tisch_oberkante(robot):
    """Oberkante des Tischs (DEF TISCH, Kollision Box oder Pose mit Box) in m, sonst None."""
    node = robot.getFromDef("TISCH")
    if node is None:
        return None
    try:
        form = node.getField("boundingObject").getSFNode()
        dz = 0.0
        if form is not None and form.getTypeName() == "Pose":
            dz = form.getField("translation").getSFVec3f()[2]
            form = form.getField("children").getMFNode(0)
        if form is not None and form.getTypeName() == "Box":
            return node.getPosition()[2] + dz + form.getField("size").getSFVec3f()[2] / 2
    except (AttributeError, IndexError, TypeError):
        pass
    return None


def welt_ablage(welt_pfad):
    """Einstellung "ablage" der Welt (worlds/<welt>.json): "auflage" (realistisch) oder "liegen_lassen"."""
    try:
        with open(os.path.splitext(welt_pfad)[0] + ".json", encoding="utf-8") as f:
            wert = json.load(f).get("ablage")
    except (OSError, ValueError, AttributeError):
        return "auflage"
    return wert if wert in ("auflage", "liegen_lassen") else "auflage"


def aufbau_welt_netz(robot, node):
    """(Punkte in Welt, Dreiecke, Huellquader [xmin, ymin, xmax, ymax]) eines Aufbaus oder None."""
    if node.getId() not in AUFBAU_NETZE:
        netz = None
        try:
            url = node.getField("modell").getMFString(0)
            skal = node.getField("skalierung").getSFFloat()
            roh = lies_netz(os.path.join(os.path.dirname(robot.getWorldPath()), url), skal, [0.0, 0.0, 0.0])
        except (AttributeError, IndexError, TypeError):
            roh = None
        if roh is not None and roh[0]:
            p, r = node.getPosition(), node.getOrientation()
            punkte = [add(p, m_vec(r, v)) for v in roh[0]]
            box = [min(q[0] for q in punkte), min(q[1] for q in punkte), max(q[0] for q in punkte), max(q[1] for q in punkte)]
            netz = (punkte, roh[1], box)
        AUFBAU_NETZE[node.getId()] = netz
    return AUFBAU_NETZE[node.getId()]


def auflage_hoehe(netze, mitte, halb, z_max):
    """Hoechste Stelle der Aufbauten unter dem Rechteck mitte +- halb (x, y in m), nur unterhalb von z_max.
    None, wenn dort kein Aufbau ist (dann traegt der Tisch, den die Physik selbst richtig behandelt)."""
    x0, x1, y0, y1 = mitte[0] - halb[0], mitte[0] + halb[0], mitte[1] - halb[1], mitte[1] + halb[1]
    hoechste = None
    for punkte, dreiecke, box in netze:
        if box[2] < x0 or box[0] > x1 or box[3] < y0 or box[1] > y1:
            continue
        for a, b, c in dreiecke:
            poly = [punkte[a], punkte[b], punkte[c]]
            if (max(q[0] for q in poly) < x0 or min(q[0] for q in poly) > x1 or max(q[1] for q in poly) < y0
                    or min(q[1] for q in poly) > y1 or min(q[2] for q in poly) > z_max):
                continue
            for achse, wert, groesser in ((0, x0, True), (0, x1, False), (1, y0, True), (1, y1, False), (2, z_max, False)):
                poly = _abschneiden(poly, achse, wert, groesser)
                if not poly:
                    break
            for q in poly:
                hoechste = q[2] if hoechste is None else max(hoechste, q[2])
    return hoechste


def merke_startlagen(teile, startlagen):
    """Startlage jedes Werkstuecks beim ersten Sehen merken (Schluessel: Node-Id)."""
    for node in teile:
        if node.getId() not in startlagen:
            startlagen[node.getId()] = (node.getField("translation").getSFVec3f(),
                                        node.getField("rotation").getSFRotation())


def teile_zuruecksetzen(teile, startlagen, greifer):
    """Alle Werkstuecke ohne Neustart der Simulation auf ihre Startlage, ein gegriffenes wird losgelassen."""
    greifer.fallen_lassen("Teile auf Startlage gesetzt")
    for node in teile:
        lage = startlagen.get(node.getId())
        if lage is None:
            continue
        node.getField("translation").setSFVec3f(list(lage[0]))
        node.getField("rotation").setSFRotation(list(lage[1]))
        node.resetPhysics()
    fest = greifer.startlagen_festhalten(teile, startlagen)
    print("Teile auf Startlage gesetzt (%d Werkstuecke%s)" % (len(teile), ", %d davon auf Aufbauten festgehalten" % fest
                                                              if fest else ""))


def main():
    global LOGDATEI
    cfg = load_config()
    for ordner in (cfg["log_ordner"], cfg["status_ordner"]):
        if ordner:
            os.makedirs(ordner, exist_ok=True)
    LOGDATEI = log_pfad(cfg)
    statusdatei = status_pfad(cfg)
    platz_text = ("Platz %s - " % cfg["platz"]) if cfg["platz"] else ""
    print("Start: %siRC %s Ports %s" % (platz_text, cfg["host"], cfg["ports"]))
    robot = Supervisor()
    step = int(robot.getBasicTimeStep())
    me = robot.getSelf()

    motoren = [robot.getDevice("A%d" % i) for i in range(1, 7)]
    sensoren = [robot.getDevice("A%d_sensor" % i) for i in range(1, 7)]
    for s in sensoren:
        s.enable(step)

    flansch = me.getFromProtoDef("FLANSCH")
    tcp = me.getFromProtoDef("TCP")
    greifer = Greifer(robot, cfg, tcp)
    greifer.auflagen = lambda: [n for n in (aufbau_welt_netz(robot, a) for a in aufbauten(robot)) if n]
    tisch_oben = {}   # erst beim ersten Loslassen lesen (vor dem ersten Schritt kennt Webots die Lage noch nicht)

    def tischhoehe():
        if "z" not in tisch_oben:
            tisch_oben["z"] = tisch_oberkante(robot)
        return tisch_oben["z"]
    greifer.tischhoehe = tischhoehe
    greifer.ablage = welt_ablage(robot.getWorldPath())
    print("Greifer: %s%s" % (greifer.beschreibung(),
                             ", zu tief abgelegte Teile bleiben liegen" if greifer.ablage == "liegen_lassen" else ""))

    cri = CriClient(cfg["host"], cfg["ports"], passive=cfg["nur_mithoeren"], log=print).start()

    teile = werkstuecke(robot)
    startlagen = {}
    merke_startlagen(teile, startlagen)
    befehlsdatei = befehl_pfad(cfg)
    hole_befehle(befehlsdatei)   # alte Befehle von vor dem Start verwerfen

    # Taster, Leuchtmelder und Sensorflaechen der Welt (worlds/<welt>.json, "ea")
    ea_cfg = ea.lade_ea(robot.getWorldPath())
    taster = ea.Taster(ea_cfg["taster"], cfg["din_versatz"])
    sensorfelder = ea.Sensoren(ea_cfg["sensoren"], robot, cfg["din_versatz"], log=print)
    app = None
    if (ea_cfg["taster"] or sensorfelder.liste) and not os.environ.get("VORSCHAU_BILD"):
        app = ea.AppVerbindung(cfg["host"], cfg["app_port"], cfg["app_name"], log=print).start()
        print("E/A: %d Taster, %d Leuchtmelder, %d Sensorflaechen"
              % (len(ea_cfg["taster"]), len(ea_cfg["leuchten"]), len(sensorfelder.liste)))
    status_takt = 0.25 if ea_cfg["taster"] or ea_cfg["leuchten"] or sensorfelder.liste else 1.0
    sensor_zeit = 0.0
    teile_zeit = start_zeit = time.monotonic()
    label_zeit = hilfe_zeit = 0.0
    hilfe = None
    # Echtzeitfaktor: wie viel Simulationszeit pro Sekunde Wanduhr vergeht
    echtzeit = 1.0
    wand_letzt = time.monotonic()
    letzter = None
    soll = None
    greifer_soll = None   # wird erst ausgewertet, wenn der Roboter angekommen ist
    letzte_dout = None
    abweichung_mm = None
    abgleich_bezug = "Flansch"
    letzte_warnung = 0.0
    letzter_statusbericht = 0.0
    erster_schritt = True

    # tools/vorschau.sh: nach kurzer Zeit ein Bild der Szene speichern und Webots beenden
    vorschau = os.environ.get("VORSCHAU_BILD")

    while robot.step(step) != -1:
        if erster_schritt:   # jetzt kennt Webots die Lage der Aufbauten: Teile darauf ab Start festhalten
            erster_schritt = False
            fest = greifer.startlagen_festhalten(teile, startlagen)
            if fest:
                print("%d Werkstueck(e) liegen auf Aufbauten und werden dort festgehalten, bis sie gegriffen werden" % fest)
        if vorschau and robot.getTime() > 1.0:
            robot.exportImage(vorschau, 90)
            print("Vorschaubild gespeichert: %s" % vorschau)
            robot.simulationQuit(0)
            vorschau = None
        jetzt = time.monotonic()
        wand_dt = jetzt - wand_letzt
        wand_letzt = jetzt
        if wand_dt > 0:
            echtzeit = 0.9 * echtzeit + 0.1 * min(1.0, (step / 1000.0) / wand_dt)
        for befehl in hole_befehle(befehlsdatei):
            worte = befehl.split()
            if befehl == "teile_zuruecksetzen":
                teile_zuruecksetzen(teile, startlagen, greifer)
            elif len(worte) == 3 and worte[0] == "taster" and worte[1].isdigit():
                taster.ereignis(int(worte[1]), worte[2] == "1", jetzt)
        if sensorfelder.liste and jetzt - sensor_zeit >= 0.1:   # 10x pro Sekunde reicht fuer "belegt"
            sensor_zeit = jetzt
            sensorfelder.pruefen(teile)
        if app is not None:
            taster.weiter(jetzt)
            signale = taster.signale()
            for nr, pegel in sensorfelder.signale().items():   # Taster und Sensor am selben Eingang: ODER
                signale[nr] = signale.get(nr, False) or pegel
            app.setze(signale)
        if jetzt - teile_zeit > 2.0:   # neu eingefuegte Teile erkennen
            teile = werkstuecke(robot)
            merke_startlagen(teile, startlagen)
            teile_zeit = jetzt

        # Greifer erst schalten, wenn die Gelenke die gemeldete Stellung erreicht haben
        if greifer_soll is not None and soll is not None:
            if all(abs(q - s.getValue()) < 0.005 for s, q in zip(sensoren, soll)):
                greifer.schalte(greifer_soll, teile)
                greifer_soll = None

        st = cri.state
        if st is not None and st is not letzter:
            letzter = st
            soll = [cfg["achsrichtung"][i] * math.radians(w + cfg["achsversatz_grad"][i])
                    for i, w in enumerate(st.joints(cfg["gelenkwerte"]))]
            # bis zur naechsten iRC-Meldung vergeht so viel Simulationszeit;
            # laeuft Webots langsamer als Echtzeit, springt der Roboter direkt hin
            periode = max(cri.status_period * echtzeit, step / 1000.0)
            for m, s, q in zip(motoren, sensoren, soll):
                v = abs(q - s.getValue()) / periode * 1.1
                m.setVelocity(max(0.05, min(m.getMaxVelocity(), v)))
                m.setPosition(q)

            if st.dout != letzte_dout:
                if letzte_dout is not None:
                    geaendert = st.dout ^ letzte_dout
                    for bit in range(64):
                        if (geaendert >> bit) & 1:
                            print("DOut%d (Bit %d) -> %s" % (bit - cfg["dout_versatz"], bit,
                                                            "EIN" if st.dout_bit(bit) else "AUS"))
                letzte_dout = st.dout
            zu = greifer.befehl(st.dout)
            if zu is not None:
                if zu != greifer.zu:
                    greifer_soll = zu
                elif greifer_soll is not None and greifer_soll != zu:
                    greifer_soll = None   # kurzer Puls, schon wieder zurueck

            # Abgleich: kartesische Lage aus iRC gegen Webots, nur wenn der Roboter
            # die gemeldete Stellung schon erreicht hat. Mit Werkzeug meldet iRC
            # den TCP, ohne Werkzeug den Flansch; verglichen wird mit dem passenden.
            angekommen = all(abs(q - s.getValue()) < 0.005 for s, q in zip(sensoren, soll))
            if angekommen and any(st.cart[:3]):
                bp, br = me.getPosition(), me.getOrientation()
                kandidaten = []
                for bezug, node in (("Flansch", flansch), ("TCP", tcp)):
                    lokal = m_vec(m_t(br), sub(node.getPosition(), bp))
                    abw = math.sqrt(sum((lokal[i] * 1000.0 - st.cart[i]) ** 2 for i in range(3)))
                    kandidaten.append((abw, bezug, lokal))
                abweichung_mm, abgleich_bezug, lokal = min(kandidaten, key=lambda k: k[0])
                if abweichung_mm > cfg["abweichung_warnen_ab_mm"] and jetzt - letzte_warnung > 5.0:
                    letzte_warnung = jetzt
                    print("Abgleich: %s Webots %.1f %.1f %.1f mm, iRC %.1f %.1f %.1f mm (Abweichung %.1f mm)"
                          % (abgleich_bezug, lokal[0] * 1000, lokal[1] * 1000, lokal[2] * 1000,
                             st.cart[0], st.cart[1], st.cart[2], abweichung_mm))

        greifer.mitfuehren()
        greifer.festhalten()
        if jetzt - hilfe_zeit >= 0.25:   # 4x pro Sekunde reicht (Netzschnitt bei CAD-Werkstuecken)
            hilfe_zeit = jetzt
            hilfe = greifer.einteach_hilfe(teile, me.getOrientation())

        if jetzt - letzter_statusbericht >= status_takt:
            letzter_statusbericht = jetzt
            schreibe_status(statusdatei, {
                "platz": cfg["platz"],
                "irc_host": cfg["host"],
                "irc_port": cri.connected_port if cri.connected else None,
                "verbunden": cri.connected,
                "sekunden_seit_irc": None if letzter is None else round(jetzt - letzter.received, 1),
                "gelenke_grad": None if letzter is None else [round(v, 2) for v in letzter.joints(cfg["gelenkwerte"])],
                "greifer": greifer.typ,
                "greifer_zu": greifer.zu,
                "teil": greifer.teil_name if greifer.teil is not None else None,
                "meldung": greifer.meldung,
                "abweichung_mm": None if abweichung_mm is None else round(abweichung_mm, 2),
                "abgleich_bezug": abgleich_bezug,
                "echtzeit": round(echtzeit, 2),
                "laufzeit_s": round(jetzt - start_zeit),
                "hilfe": hilfe,
                "taster": list(taster.gedrueckt),
                "sensoren": list(sensorfelder.belegt),
                "leuchten": ea.leuchten_zustand(ea_cfg["leuchten"], None if letzter is None else letzter.dout,
                                                cfg["dout_versatz"]),
                "ea_meldung": None if app is None else app.meldung,
                "ea_ok": None if app is None else app.ok,
                # fuer die Kamera "Greifer" im Browser: TCP und Querrichtung zu den Backen (Welt)
                "tcp": [round(v, 4) for v in tcp.getPosition()],
                "tcp_y": [round(v, 3) for v in m_vec(tcp.getOrientation(), [0.0, 1.0, 0.0])],
                "zeit": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })

        if cri.connected:
            zeile1 = "%siRC verbunden (%s:%d)" % (platz_text, cfg["host"], cri.connected_port)
        else:
            zeile1 = "%swarte auf iRC (%s, Port %s) ..." % (platz_text, cfg["host"],
                                                           "/".join(map(str, cfg["ports"])))
        zeile2 = "Greifer %s: %s" % (greifer.typ, greifer.meldung or "offen")
        zeile3 = "" if abweichung_mm is None else "Abgleich %s: %.1f mm" % (abgleich_bezug, abweichung_mm)
        zeile4 = "" if echtzeit > 0.8 else "Simulation langsamer als Echtzeit (%.2fx)" % echtzeit
        # Beschriftungen (nicht im Vorschaubild). Webots schickt sie nur bei Aenderung an
        # die Browser: hoechstens 4x pro Sekunde (sonst kommt ein langsamer Browser nicht
        # mit), und alle 2 s mit wechselndem Leerzeichen, damit neue Zuschauer sie auch bekommen.
        if not vorschau and jetzt - label_zeit >= 0.25:
            label_zeit = jetzt
            auffrischen = " " if int(jetzt / 2) % 2 else ""
            robot.setLabel(0, "\n".join(z for z in (zeile1, zeile2, zeile3, zeile4) if z) + auffrischen,
                           0.01, 0.01, 0.07, 0x1A1A1A, 0.0, "Arial")
            # Einteach-Hilfe unten links: gruen, sobald der Griff klappen wuerde
            robot.setLabel(1, ("" if hilfe is None else hilfe["text"]) + auffrischen, 0.01, 0.86, 0.08,
                           0x1E8C4A if hilfe and hilfe["greifbar"] else 0xB34700, 0.0, "Arial")

    cri.stop()
    if app is not None:
        app.stopp()   # Taster loslassen, damit in iRC keine Eingaenge haengen bleiben


if __name__ == "__main__":
    main()
