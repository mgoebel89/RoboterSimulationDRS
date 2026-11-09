"""Foerderbaender (Stufe FB1/FB2): zwei Ausgaenge aus iRC fahren das Band vor bzw. zurueck, je Ende ein
Reflexlichttaster meldet ein Werkstueck an einen Eingang in iRC.

Werkstuecke auf dem Gurt werden nicht ueber Reibung bewegt (in Webots ruckelt das und haengt vom Zeitschritt ab),
sondern in jedem Schritt um Tempo x Zeit in Bandrichtung verschoben. Sie stauen sich hintereinander und bleiben am
Anschlag stehen; an einem offenen Ende rutschen sie herunter, sobald ihre Mitte ueber das Ende hinaus ist.
"vor" und "zurueck" gleichzeitig an ist ein Fehler wie eine Kollision (Programm anhalten, quittieren).
Lagerbox (FB2): Teile, die hineinfallen, werden gezaehlt und auf die unsichtbare Vorratsablage 20 m unter dem Band
gelegt (geparkt). "Teil nachlegen" (Knopf im Browser) setzt ein geparktes Vorratsteil an den Einlauf (linkes Ende),
die Sorte zufaellig aus der Liste. Werkstuecke werden nie neu erzeugt (ob Webots neue Knoten sauber in den Stream
bringt, ist unsicher), der Vorrat steht in der Welt und wird beim Start geparkt.

Konfiguration je Welt in worlds/<welt>.json (Nummern wie in der iRC-Anzeige):
  "ea": {"baender": [{"name": "Band 1", "knoten": "BAND_0", "dout_vor": 21, "dout_zurueck": 22,
                      "geschwindigkeit_mm_s": 60,
                      "taster": [{"din": 25, "art": "schliesser"}, {"din": 26, "art": "oeffner"}],
                      "nachlegen": {"vorrat": [{"sorte": "rot", "knoten": ["VORRAT_0_0", "VORRAT_0_1"]},
                                               {"sorte": "blau", "knoten": ["VORRAT_0_2"]}]}}]}
taster: [links, rechts], ein Eintrag darf null sein (Taster ohne Eingang). nachlegen ist freiwillig; die Knoten
sind Werkstuecke mit DEF-Namen. Masse, Bandenden und Lage der Taster
stehen im Knoten (Proto Foerderband).
Ohne Webots testbar: die Rechnung steckt in den Funktionen ohne Knoten (auf_gurt, foerdern, taster_belegt).
"""

import math
import random

GESCHWINDIGKEIT_MM_S = 60.0
MAX_BAENDER = 2
FEHLER_NACH_S = 0.05    # so lange muessen beide Ausgaenge an sein (iRC schaltet nacheinander, kurze Ueberlappung)
FANG_Z = 0.004          # so nah muss die Unterseite eines Teils an der Gurtoberkante sein (liegt auf dem Gurt)
LUECKE = 0.0003         # Abstand gestauter Teile zueinander und zum Anschlag
LED_AN, LED_AUS = [1.0, 0.82, 0.1], [0.35, 0.3, 0.1]
PARK_Z = -20.0          # Vorratsablage im Bandsystem (wie PARK_Z im Proto)
PARK_RASTER = 0.08      # Abstand der Parkplaetze (mindestens; groessere Teile bekommen mehr)
MELDUNG_S = 5.0         # so lange steht eine Meldung ("Einlauf belegt") in der Anzeige
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


def achse_winkel_matrix(aa):
    """Drehung als Achse + Winkel (Webots-Feld rotation) -> 3x3 (9 Zahlen zeilenweise)."""
    x, y, z, w = aa
    n = math.sqrt(x * x + y * y + z * z) or 1.0
    x, y, z = x / n, y / n, z / n
    c, s, t = math.cos(w), math.sin(w), 1 - math.cos(w)
    return [t * x * x + c, t * x * y - s * z, t * x * z + s * y,
            t * x * y + s * z, t * y * y + c, t * y * z - s * x,
            t * x * z - s * y, t * y * z + s * x, t * z * z + c]


def lagerbox_mass(laenge, hoehe):
    """Innenraum der Lagerbox am rechten Ende im Bandsystem (links gespiegelt), wie im Proto:
    (Mitte x, halbe Laenge, halbe Breite, Hoehe des Rands)."""
    return laenge / 2 + 0.063, 0.057, 0.067, max(0.03, hoehe - 0.025)


def in_lagerbox(lokal, seite, laenge, hoehe):
    """Liegt die Mitte eines Teils (Bandsystem) in der Lagerbox der Seite -1 (links) bzw. +1 (rechts), unter dem Rand?"""
    xm, hx, hy, rand = lagerbox_mass(laenge, hoehe)
    return abs(lokal[0] - seite * xm) <= hx and abs(lokal[1]) <= hy and lokal[2] < rand


def parkplatz(k, raster=PARK_RASTER):
    """x, y (Bandsystem) des k-ten Platzes auf der Vorratsablage: Reihen zu 10."""
    return [-0.4 + raster * (k % 10), -0.4 + raster * (k // 10)]


def einlauf_frei(lagen, x0, x1, breite, hoehe):
    """lagen: [(Mitte, halbe Ausdehnung)] im Bandsystem. Frei, wenn kein Teil auf dem Gurt (oder knapp darueber) den
    Bereich x0..x1 beruehrt."""
    for lokal, halb in lagen:
        if (lokal[0] + halb[0] > x0 and lokal[0] - halb[0] < x1 and abs(lokal[1]) - halb[1] < breite / 2
                and lokal[2] + halb[2] > hoehe - 0.005 and lokal[2] - halb[2] < hoehe + 0.05):
            return False
    return True


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


def vorrat_liste(cfg):
    """[(Sorte, DEF-Name)] aus "nachlegen" -> "vorrat" eines Bands (fehlerhafte Eintraege uebergangen)."""
    nachlegen = cfg.get("nachlegen") if isinstance(cfg.get("nachlegen"), dict) else {}
    liste = []
    for e in nachlegen.get("vorrat") or []:
        if not isinstance(e, dict) or not isinstance(e.get("sorte"), str):
            continue
        liste += [(e["sorte"], k) for k in e.get("knoten") or [] if isinstance(k, str)]
    return liste[:200]


# --- in Webots ---

class Band:
    def __init__(self, cfg, knoten, din_versatz, dout_versatz, log=print):
        self.cfg, self.knoten, self.din_versatz, self.log = cfg, knoten, din_versatz, log
        self.name = cfg.get("name") or knoten.getField("name").getSFString()
        f = knoten.getField
        self.laenge, self.breite, self.hoehe = f("laenge").getSFFloat(), f("breite").getSFFloat(), f("hoehe").getSFFloat()
        self.ende = [f("ende_links").getSFString(), f("ende_rechts").getSFString()]
        self.anschlag = [e == "anschlag" for e in self.ende]
        self.box = [e == "lagerbox" for e in self.ende]
        self.im_box = [0, 0]
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
        self.vorrat = []        # [(Sorte, Node, Drehung aus der Welt)] fuer "Teil nachlegen"
        self.geparkt = set()    # Node-Ids auf der Vorratsablage (unsichtbar, fuer nichts sonst da)
        self.platz = {}         # Node-Id -> Nummer des Parkplatzes
        self.raster = PARK_RASTER
        self.meldung, self.meldung_bis = None, 0.0
        self.zufall = random.Random()

    def vorrat_setzen(self, eintraege):
        """eintraege: [(Sorte, Node)]. Parkplaetze werden in dieser Reihenfolge vergeben."""
        self.vorrat = [(sorte, node, list(node.getField("rotation").getSFRotation())) for sorte, node in eintraege]
        groesste = max((max(node.getField("size").getSFVec3f()) for _s, node, _r in self.vorrat), default=0.0)
        self.raster = max(PARK_RASTER, groesste + 0.02)
        for _s, node, _r in self.vorrat:
            self.platz.setdefault(node.getId(), len(self.platz))

    def parken(self, node, drehung=None):
        """Teil auf seinen Platz der Vorratsablage legen (unsichtbar, ruhig). Rueckgabe: die Lage."""
        k = self.platz.setdefault(node.getId(), len(self.platz))
        p, r = self.lage()
        x, y = parkplatz(k, self.raster)
        lage = aus_band(p, r, [x, y, PARK_Z + max(node.getField("size").getSFVec3f()) / 2 + 0.001])
        node.getField("translation").setSFVec3f(lage)
        if drehung is not None:
            node.getField("rotation").setSFRotation(list(drehung))
        node.resetPhysics()
        self.geparkt.add(node.getId())
        return lage

    def vorrat_parken(self):
        """Alle Vorratsteile parken, Boxen leeren (Start, "Teile zurueck"). Rueckgabe: neue Startlagen
        {Node-Id: (Lage, Drehung)}, damit "Teile zurueck" sie wieder dorthin legt."""
        self.geparkt, self.im_box = set(), [0, 0]
        return {node.getId(): (self.parken(node, drehung), list(drehung)) for _s, node, drehung in self.vorrat}

    def _melden(self, text, jetzt):
        self.meldung, self.meldung_bis = text, jetzt + MELDUNG_S
        self.log("Foerderband %s: Teil nachlegen: %s" % (self.name, text))
        return False

    def nachlegen(self, teile, jetzt):
        """Ein geparktes Vorratsteil (Sorte zufaellig unter den vorraetigen) an den Einlauf links setzen.
        teile: die sichtbaren Werkstuecke (fuer "Einlauf belegt"). True, wenn ein Teil kam."""
        if not self.vorrat:
            return self._melden("kein Vorrat eingestellt", jetzt)
        frei = [e for e in self.vorrat if e[1].getId() in self.geparkt]
        if not frei:
            return self._melden("Vorrat leer (alle Teile liegen schon irgendwo)", jetzt)
        sorte = self.zufall.choice(sorted({e[0] for e in frei}))
        _s, node, drehung = next(e for e in frei if e[0] == sorte)
        p, r = self.lage()
        halb = halbe_im_band(r, achse_winkel_matrix(drehung), node.getField("size").getSFVec3f())
        x = -self.laenge / 2 + halb[0] + (LUECKE if self.anschlag[0] else 0.005)
        if not einlauf_frei([self.im_band(t) for t in teile], -self.laenge / 2, x + halb[0] + 0.005,
                            self.breite, self.hoehe):
            return self._melden("Einlauf belegt", jetzt)
        node.getField("rotation").setSFRotation(list(drehung))
        node.getField("translation").setSFVec3f(aus_band(p, r, [x, 0.0, self.hoehe + halb[2] + 0.0005]))
        node.resetPhysics()
        self.geparkt.discard(node.getId())
        self.meldung = None
        self.log("Foerderband %s: Teil nachgelegt: %s (%s)" % (self.name, node.getField("name").getSFString(), sorte))
        return True

    def lagerboxen(self, teile, ausnehmen=()):
        """Teile in einer Lagerbox zaehlen und parken."""
        if not any(self.box):
            return
        p, r = self.lage()
        for node in teile:
            if node.getId() in ausnehmen or node.getId() in self.geparkt:
                continue
            lokal = ins_band(p, r, node.getPosition())
            for i, seite in ((0, -1), (1, 1)):
                if self.box[i] and in_lagerbox(lokal, seite, self.laenge, self.hoehe):
                    self.im_box[i] += 1
                    self.log("Foerderband %s: %s in der Lagerbox %s (%d)" % (
                        self.name, node.getField("name").getSFString(), ("links", "rechts")[i], self.im_box[i]))
                    self.parken(node)
                    break

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

    def status(self, jetzt=None):
        d = {"name": self.name, "lauf": self.lauf, "fehler": self.steuerung.fehler, "taster": list(self.taster)}
        if any(self.box):
            d["box"] = [n if b else None for n, b in zip(self.im_box, self.box)]
        if self.vorrat:
            d["vorrat"] = sum(1 for e in self.vorrat if e[1].getId() in self.geparkt)
        if self.meldung and (jetzt is None or jetzt < self.meldung_bis):
            d["meldung"] = self.meldung
        return d


class Baender:
    """Alle Foerderbaender der Welt (DEF aus "knoten", Typ Foerderband)."""

    def __init__(self, liste, robot, din_versatz=-1, dout_versatz=-1, log=print):
        self.liste = []
        for cfg in lade(liste):
            knoten = robot.getFromDef(cfg["knoten"])
            if knoten is None or knoten.getTypeName() != "Foerderband":
                log("Foerderband %s fehlt in der Welt" % cfg["knoten"])
                continue
            band = Band(cfg, knoten, din_versatz, dout_versatz, log=log)
            vorrat = []
            for sorte, name in vorrat_liste(cfg):
                node = robot.getFromDef(name)
                if node is None or node.getTypeName() != "Werkstueck":
                    log("Foerderband %s: Vorratsteil %s fehlt in der Welt" % (band.name, name))
                else:
                    vorrat.append((sorte, node))
            band.vorrat_setzen(vorrat)
            self.liste.append(band)

    def schritt(self, teile, dt, dout, jetzt, ausnehmen=()):
        for b in self.liste:
            b.schritt(teile, dt, dout, jetzt, ausnehmen)

    def taster_pruefen(self, teile):
        for b in self.liste:
            b.taster_pruefen(teile)

    def lagerboxen(self, teile, ausnehmen=()):
        for b in self.liste:
            b.lagerboxen(teile, ausnehmen)

    def vorrat_parken(self):
        lagen = {}
        for b in self.liste:
            lagen.update(b.vorrat_parken())
        return lagen

    def geparkt(self):
        return set().union(*(b.geparkt for b in self.liste)) if self.liste else set()

    def nachlegen(self, nr, teile, jetzt):
        if 0 <= nr < len(self.liste):
            return self.liste[nr].nachlegen(teile, jetzt)
        return False

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

    def status(self, jetzt=None):
        return [b.status(jetzt) for b in self.liste]

    def label(self):
        return "\n".join("Foerderband %s: %s%s" % (
            b.name, b.zustand_text(), "".join(", Box %s: %d" % (("links", "rechts")[i], b.im_box[i])
                                             for i in range(2) if b.box[i])) for b in self.liste)
