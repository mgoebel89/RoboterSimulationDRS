"""Foerderbaender (Stufe FB1): zwei Ausgaenge aus iRC fahren das Band vor bzw. zurueck, je Ende ein
Reflexlichttaster meldet ein Werkstueck an einen Eingang in iRC.

Werkstuecke auf dem Gurt werden nicht ueber Reibung bewegt (in Webots ruckelt das und haengt vom Zeitschritt ab),
sondern in jedem Schritt um Tempo x Zeit in Bandrichtung verschoben. Sie stauen sich hintereinander und bleiben am
Anschlag stehen; an einem offenen Ende rutschen sie herunter, sobald ihre Mitte ueber das Ende hinaus ist.
"vor" und "zurueck" gleichzeitig an ist ein Fehler wie eine Kollision (Programm anhalten, quittieren).

Konfiguration je Welt in worlds/<welt>.json (Nummern wie in der iRC-Anzeige):
  "ea": {"baender": [{"name": "Band 1", "knoten": "BAND_0", "dout_vor": 21, "dout_zurueck": 22,
                      "geschwindigkeit_mm_s": 60,
                      "taster": [{"din": 25, "art": "schliesser"}, {"din": 26, "art": "oeffner"}]}]}
taster: [links, rechts], ein Eintrag darf null sein (Taster ohne Eingang). Masse, Bandenden und Lage der Taster
stehen im Knoten (Proto Foerderband).
Ohne Webots testbar: die Rechnung steckt in den Funktionen ohne Knoten (auf_gurt, foerdern, taster_belegt).
"""

GESCHWINDIGKEIT_MM_S = 60.0
MAX_BAENDER = 2
FEHLER_NACH_S = 0.05    # so lange muessen beide Ausgaenge an sein (iRC schaltet nacheinander, kurze Ueberlappung)
FANG_Z = 0.004          # so nah muss die Unterseite eines Teils an der Gurtoberkante sein (liegt auf dem Gurt)
LUECKE = 0.0003         # Abstand gestauter Teile zueinander und zum Anschlag
LED_AN, LED_AUS = [1.0, 0.82, 0.1], [0.35, 0.3, 0.1]
LAMPE = {"steht": [0.45, 0.47, 0.5], "laeuft": [0.15, 0.8, 0.3], "fehler": [0.9, 0.1, 0.1]}


# --- Rechnung im Bandsystem (x entlang des Bands, z nach oben, Ursprung Mitte der Aufstellflaeche) ---

def _mtv(r, v):
    """Transponierte 3x3 (9 Zahlen, zeilenweise wie Webots) mal Vektor."""
    return [r[0] * v[0] + r[3] * v[1] + r[6] * v[2], r[1] * v[0] + r[4] * v[1] + r[7] * v[2],
            r[2] * v[0] + r[5] * v[1] + r[8] * v[2]]


def _mv(r, v):
    return [r[0] * v[0] + r[1] * v[1] + r[2] * v[2], r[3] * v[0] + r[4] * v[1] + r[5] * v[2],
            r[6] * v[0] + r[7] * v[1] + r[8] * v[2]]


def ins_band(band_p, band_r, punkt):
    return _mtv(band_r, [punkt[i] - band_p[i] for i in range(3)])


def aus_band(band_p, band_r, lokal):
    w = _mv(band_r, lokal)
    return [band_p[i] + w[i] for i in range(3)]


def halbe_im_band(band_r, teil_r, size):
    """Halbe Ausdehnung des Huellquaders eines Teils (Drehung teil_r, Kanten size) entlang der Bandachsen."""
    rel = [sum(band_r[k * 3 + i] * teil_r[k * 3 + j] for k in range(3)) for i in range(3) for j in range(3)]
    return [0.5 * sum(abs(rel[i * 3 + j]) * size[j] for j in range(3)) for i in range(3)]


def auf_gurt(lokal, halb, laenge, breite, hoehe):
    """Liegt das Teil (Mitte lokal, halbe Ausdehnung halb, beides im Bandsystem) auf dem Gurt? Die Mitte muss ueber
    dem Gurt sein (ist sie ueber das Ende hinaus, kippt es herunter) und die Unterseite auf der Gurtoberkante."""
    return (abs(lokal[0]) <= laenge / 2 and abs(lokal[1]) <= breite / 2
            and abs(lokal[2] - halb[2] - hoehe) <= FANG_Z)


def foerdern(teile, richtung, weg, laenge, anschlag_vorne):
    """Neue x-Lage der Teile auf dem Gurt. teile: [(x, y, halb_x, halb_y)] im Bandsystem, richtung +1/-1, weg in m.
    Das vorderste Teil hoechstens bis an den Anschlag (falls vorn einer ist), jedes weitere hoechstens bis an das Teil
    vor ihm (nur wenn sie sich quer zum Band ueberdecken). Kein Teil wird gegen die Laufrichtung geschoben.
    Rueckgabe in der Reihenfolge der Eingabe."""
    d = 1 if richtung > 0 else -1
    reihe = sorted(range(len(teile)), key=lambda i: -d * teile[i][0])   # vorderstes zuerst
    neu = [t[0] for t in teile]
    fertig = []
    for i in reihe:
        x, y, hx, hy = teile[i]
        ziel = x + d * weg
        if anschlag_vorne:
            grenze = d * (laenge / 2 - LUECKE) - d * hx
            ziel = min(ziel, grenze) if d > 0 else max(ziel, grenze)
        for j in fertig:   # Teile davor: nicht hineinschieben
            xj, yj, hxj, hyj = neu[j], teile[j][1], teile[j][2], teile[j][3]
            if abs(y - yj) >= hy + hyj or d * (teile[j][0] - x) < 0:
                continue
            grenze = xj - d * (hxj + hx + LUECKE)
            ziel = min(ziel, grenze) if d > 0 else max(ziel, grenze)
        neu[i] = max(ziel, x) if d > 0 else min(ziel, x)
        fertig.append(i)
    return neu


def taster_belegt(lokal, halb, x_taster, z_strahl, breite):
    """Schneidet der Huellquader des Teils den Strahl des Tasters (quer ueber den Gurt bei x_taster, Hoehe z_strahl)?"""
    return (abs(lokal[0] - x_taster) <= halb[0] and abs(lokal[2] - z_strahl) <= halb[2]
            and abs(lokal[1]) - halb[1] < breite / 2)


class Steuerung:
    """Richtung aus den Ausgaengen; beide an (laenger als FEHLER_NACH_S) = Fehler, Band steht."""

    def __init__(self, bit_vor, bit_zurueck):
        self.bit_vor, self.bit_zurueck = bit_vor, bit_zurueck
        self.beide_seit = None
        self.fehler = False

    def richtung(self, dout, jetzt):
        vor = dout is not None and 0 <= self.bit_vor < 64 and bool(dout >> self.bit_vor & 1)
        zur = dout is not None and 0 <= self.bit_zurueck < 64 and bool(dout >> self.bit_zurueck & 1)
        if vor and zur:
            if self.beide_seit is None:
                self.beide_seit = jetzt
            self.fehler = self.fehler or jetzt - self.beide_seit >= FEHLER_NACH_S
            return 0
        self.beide_seit, self.fehler = None, False
        return 1 if vor else -1 if zur else 0


def lade(liste):
    """Gueltige Eintraege aus "ea" -> "baender" (hoechstens MAX_BAENDER)."""
    gut = []
    for b in liste or []:
        if (isinstance(b, dict) and isinstance(b.get("knoten"), str) and isinstance(b.get("dout_vor"), int)
                and isinstance(b.get("dout_zurueck"), int)):
            gut.append(b)
    return gut[:MAX_BAENDER]


# --- in Webots ---

class Band:
    def __init__(self, cfg, knoten, din_versatz, dout_versatz, log=print):
        self.cfg, self.knoten, self.din_versatz, self.log = cfg, knoten, din_versatz, log
        self.name = cfg.get("name") or knoten.getField("name").getSFString()
        f = knoten.getField
        self.laenge, self.breite, self.hoehe = f("laenge").getSFFloat(), f("breite").getSFFloat(), f("hoehe").getSFFloat()
        self.anschlag = [f("ende_links").getSFString() == "anschlag", f("ende_rechts").getSFString() == "anschlag"]
        self.x_taster = [-self.laenge / 2 + f("taster_links").getSFFloat(), self.laenge / 2 - f("taster_rechts").getSFFloat()]
        self.z_strahl = self.hoehe + f("taster_hoehe").getSFFloat()
        tempo = cfg.get("geschwindigkeit_mm_s", GESCHWINDIGKEIT_MM_S)
        self.tempo = (tempo if isinstance(tempo, (int, float)) and 0 < tempo <= 1000 else GESCHWINDIGKEIT_MM_S) / 1000.0
        self.steuerung = Steuerung(cfg["dout_vor"] + dout_versatz, cfg["dout_zurueck"] + dout_versatz)
        taster = cfg.get("taster") or []
        self.taster_cfg = [taster[i] if i < len(taster) and isinstance(taster[i], dict) else None for i in range(2)]
        self.lauf = 0
        self.taster = [False, False]
        self._led = [None, None]
        self._lampe = None
        self._lage = None
        self.bewegt = {}   # Node-Id -> Zeit: zuletzt vom Band verschoben (Werkstueck-Meldungen ausnehmen)

    def lage(self):
        """Lage des Bands (steht fest; erst nach dem ersten Schritt lesen, vorher kennt Webots sie nicht)."""
        if self._lage is None:
            self._lage = (self.knoten.getPosition(), self.knoten.getOrientation())
        return self._lage

    def im_band(self, node):
        p, r = self.lage()
        lokal = ins_band(p, r, node.getPosition())
        return lokal, halbe_im_band(r, node.getOrientation(), node.getField("size").getSFVec3f())

    def schritt(self, teile, dt, dout, jetzt, ausnehmen):
        alt = (self.lauf, self.steuerung.fehler)
        self.lauf = self.steuerung.richtung(dout, jetzt)
        umschalten = self.steuerung.beide_seit is not None and not self.steuerung.fehler   # gleich Fehler oder wieder gut
        if (self.lauf, self.steuerung.fehler) != alt and not umschalten:
            self.log("Foerderband %s: %s" % (self.name, self.zustand_text()))
        lampe = LAMPE["fehler" if self.steuerung.fehler else "laeuft" if self.lauf else "steht"]
        if lampe != self._lampe:
            self._lampe = lampe
            self.knoten.getField("lampe").setSFColor(lampe)
        if not self.lauf:
            return
        p, r = self.lage()
        drauf, lagen = [], []
        for node in teile:
            if node.getId() in ausnehmen:
                continue
            lokal, halb = self.im_band(node)
            if auf_gurt(lokal, halb, self.laenge, self.breite, self.hoehe):
                drauf.append((node, lokal))
                lagen.append((lokal[0], lokal[1], halb[0], halb[1]))
        if not drauf:
            return
        neu = foerdern(lagen, self.lauf, self.tempo * dt, self.laenge, self.anschlag[1 if self.lauf > 0 else 0])
        richtung_welt = _mv(r, [float(self.lauf), 0.0, 0.0])
        for (node, lokal), x in zip(drauf, neu):
            if abs(x - lokal[0]) < 1e-7:
                continue
            node.getField("translation").setSFVec3f(aus_band(p, r, [x, lokal[1], lokal[2]]))
            self.bewegt[node.getId()] = jetzt
            if abs(x) > self.laenge / 2:   # Mitte ueber das offene Ende: mit Schwung herunterrutschen lassen
                self.log("Foerderband %s: %s faellt am %s Ende herunter" % (
                    self.name, node.getField("name").getSFString(), "rechten" if x > 0 else "linken"))
                node.setVelocity([v * self.tempo for v in richtung_welt] + [0.0, 0.0, 0.0])
            else:
                node.resetPhysics()

    def taster_pruefen(self, teile):
        for i in range(2):
            an = False
            for node in teile:
                lokal, halb = self.im_band(node)
                if taster_belegt(lokal, halb, self.x_taster[i], self.z_strahl, self.breite):
                    an = True
                    break
            if an != self.taster[i]:
                self.log("Foerderband %s: Taster %s %s" % (self.name, ("links", "rechts")[i], "belegt" if an else "frei"))
            self.taster[i] = an
            led = LED_AN if an else LED_AUS
            if led != self._led[i]:
                self._led[i] = led
                self.knoten.getField("led_links" if i == 0 else "led_rechts").setSFColor(led)

    def signale(self):
        sig = {}
        for t, an in zip(self.taster_cfg, self.taster):
            if t is None or not isinstance(t.get("din"), int):
                continue
            nr = t["din"] + self.din_versatz
            if 0 <= nr < 64:
                sig[nr] = sig.get(nr, False) or (an != (t.get("art") == "oeffner"))
        return sig

    def treffer(self):
        """Fehler "vor und zurueck gleichzeitig" als Treffer fuer den Crash-Stopp (wie eine Kollision)."""
        if not self.steuerung.fehler:
            return []
        p, r = self.lage()
        return [{"a": "Förderband %s" % self.name, "b": "Ausgänge vor und zurück (%s)" % self.name, "art": "band",
                 "punkt": aus_band(p, r, [self.laenge / 2, 0.0, self.hoehe]),
                 "text": "%s: DOut%d (vor) und DOut%d (zurück) gleichzeitig an" % (
                     self.name, self.cfg["dout_vor"], self.cfg["dout_zurueck"])}]

    def zustand_text(self):
        if self.steuerung.fehler:
            return "FEHLER vor und zurueck gleichzeitig"
        return {1: "faehrt vor", -1: "faehrt zurueck", 0: "steht"}[self.lauf]

    def status(self):
        return {"name": self.name, "lauf": self.lauf, "fehler": self.steuerung.fehler, "taster": list(self.taster)}


class Baender:
    """Alle Foerderbaender der Welt (DEF aus "knoten", Typ Foerderband)."""

    def __init__(self, liste, robot, din_versatz=-1, dout_versatz=-1, log=print):
        self.liste = []
        for cfg in lade(liste):
            knoten = robot.getFromDef(cfg["knoten"])
            if knoten is None or knoten.getTypeName() != "Foerderband":
                log("Foerderband %s fehlt in der Welt" % cfg["knoten"])
                continue
            self.liste.append(Band(cfg, knoten, din_versatz, dout_versatz, log=log))

    def schritt(self, teile, dt, dout, jetzt, ausnehmen=()):
        for b in self.liste:
            b.schritt(teile, dt, dout, jetzt, ausnehmen)

    def taster_pruefen(self, teile):
        for b in self.liste:
            b.taster_pruefen(teile)

    def signale(self):
        sig = {}
        for b in self.liste:
            for nr, pegel in b.signale().items():
                sig[nr] = sig.get(nr, False) or pegel
        return sig

    def treffer(self):
        return [t for b in self.liste for t in b.treffer()]

    def kuerzlich_bewegt(self, jetzt, sekunden=1.5):
        """Ids der Teile, die ein Band eben noch verschoben hat. Stossen sie aneinander (gestaut, gemeinsam vom
        Band gefallen), ist das keine Werkstueck-Meldung wert."""
        return {i for b in self.liste for i, t in b.bewegt.items() if jetzt - t <= sekunden}

    def status(self):
        return [b.status() for b in self.liste]

    def label(self):
        return "\n".join("Foerderband %s: %s" % (b.name, b.zustand_text()) for b in self.liste)
