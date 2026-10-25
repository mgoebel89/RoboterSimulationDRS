"""Weltbaukasten: Teile ueber Verbindungspunkte platzieren (wie Mate Connectors in Onshape).

Ein Entwurf (worlds/<welt>.baukasten.json) beschreibt eine Welt:
  {"titel": "...", "besitzer": "lehrer:gob", "greifer": "egp40",
   "teile": [{"id": "magazin", "teil": "magazin_v2", "art": "aufbau" | "werkstueck",
              "farbe": [r, g, b], "masse_kg": 0.05,
              "verbindung": {"punkt": <Punkt am Teil>, "an": "tisch" | "roboterfuss" | <id>,
                             "an_punkt": <Punkt am Gegenstueck>,
                             "versatz_mm": [x, y, z], "drehung_grad": 0, "umdrehen": false}}]}

Punkte: {"art": "flaeche", "id": 12}, {"art": "bohrung", "id": 5, "ende": 0},
        {"art": "kante", "id": 7, "flaeche": 12}, {"art": "ecke", "id": 3, "flaeche": 12}
Am Tisch: Flaeche "oben", Kanten "x+", "x-", "y+", "y-" und Ecken wie "x+y-" (jeweils mit Flaeche "oben").
Am Roboterfuss: Flaeche "ursprung" (iRC-Basiskoordinaten).

Jeder Punkt ist ein Koordinatensystem: z zeigt aus der Flaeche heraus (bei Bohrungen entlang der
Achse aus der Oeffnung), bei Kanten und Ecken zeigen x/y entlang der Kanten in die Flaeche hinein.
Eine Verbindung legt die beiden Punkte aufeinander, die z-Achsen gegeneinander (Flaeche auf Flaeche),
und verschiebt bzw. dreht dann im Punkt des Gegenstuecks: versatz x/y in der Flaeche (bei Kanten und
Ecken = Abstand von der Kante), z = Abstand von der Flaeche, drehung um z. "umdrehen": z gleichsinnig.

Alle Masse in mm wie in iRC; Ursprung = Roboterfuss, Tischplatte bei z = 0.
Nur Python-Standardbibliothek, laeuft auch ohne gmsh.
"""

import json
import math
import os
import re

# Tisch je Entwurf einstellbar ("tisch": {...}); Standard wie worlds/rebel_tisch.wbt:
# 1000 x 800 mm, Roboterfuss 250 mm vor der hinteren Kante und mittig.
# Der Roboterfuss bleibt der Ursprung (wie in iRC); eingestellt wird, wo der Tisch um ihn herum liegt.
TISCH_STANDARD = {
    "laenge_mm": 1000.0,             # in Blickrichtung des Roboters (bei Drehung 0 entlang x)
    "breite_mm": 800.0,
    "roboter_von_hinten_mm": 250.0,  # Abstand Roboterfuss (Mitte) von der hinteren Tischkante
    "roboter_von_links_mm": 400.0,   # ... von der linken Tischkante (links = +y bei Drehung 0)
    "drehung_grad": 0.0,             # Roboter auf dem Tisch gedreht (gegen den Uhrzeigersinn, von oben)
    "hoehe_mm": 0.0,                 # Tischplatte relativ zum Roboterfuss, z.B. -100 bei 100-mm-Sockel
}
TISCH_GRENZEN = {"laenge_mm": (200, 4000), "breite_mm": (200, 4000), "roboter_von_hinten_mm": (-2000, 6000),
                 "roboter_von_links_mm": (-2000, 6000), "drehung_grad": (-360, 360), "hoehe_mm": (-1000, 500)}

# Profiltisch ("tisch": {"art": "profil", ...}): Bosch-Rexroth-Profile Nut 10, 800 x 800 x 40 mm.
# Aussen 40er-Rahmen zwischen vier 40er-Beinen (oben Abdeckkappen), innen 6 Profile 120 x 40 von links
# nach rechts. Alle Nuten der Oberseite liegen im 40er-Raster: Nut 1 im hinteren Rahmen (20 mm von der
# hinteren Kante) bis Nut 20 im vorderen Rahmen; dazu je eine Nut im linken und rechten Rahmen.
# Der Roboter steht auf der Basisplatte (Teil "basisplatte", Onshape): Platte 2 mm, Anschlagkante 8 mm hoch
# in der Ecke hinten links, deren Aussenseiten 90 mm von der Fussmitte. Gemessen wird von dort zur hinteren
# und linken Tischkante. Die Bohrungen (Lochkreis 160 mm) liegen 40/80 mm vor und hinter der Mitte,
# deshalb muss die Fussmitte auf einer Nut stehen: hinten nur im 40er-Raster, links stufenlos.
PROFIL = {"groesse_mm": 800.0, "rahmen_mm": 40.0, "raster_mm": 40.0, "nut_mm": 10.0, "dicke_mm": 40.0,
          "kappe_mm": 4.0, "nuten": 20}
PROFIL_STANDARD = {"anschlag_hinten_mm": 50.0, "anschlag_links_mm": 310.0, "drehung_grad": 0.0}
BASISPLATTE = "basisplatte"
PLATTE = {"dicke_mm": 2.0, "anschlag_mm": 90.0, "bohrung_quer_mm": 69.28, "bohrung_r_mm": 3.25}
# Namen aus der ersten Fassung (Tisch fest, Roboter nicht gedreht) gelten weiter
ALTE_NAMEN = {"x+": "vorne", "x-": "hinten", "y+": "links", "y-": "rechts",
              "x+y-": "vorne-rechts", "x+y+": "vorne-links", "x-y-": "hinten-rechts", "x-y+": "hinten-links"}
ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,40}")
MAX_TEILE = 100


class BaukastenFehler(Exception):
    """Fehler mit einer Meldung, die so im Baukasten stehen kann."""


# --- kleine Vektor- und Matrixhelfer (Matrizen zeilenweise als Listen von Listen) ---

def _sub(a, b):
    return [a[i] - b[i] for i in range(3)]


def _add(a, b):
    return [a[i] + b[i] for i in range(3)]


def _mul(a, s):
    return [v * s for v in a]


def _dot(a, b):
    return sum(a[i] * b[i] for i in range(3))


def _kreuz(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def _norm(a):
    laenge = math.sqrt(_dot(a, a))
    if laenge < 1e-12:
        raise BaukastenFehler("Richtung der Laenge 0")
    return [v / laenge for v in a]


def _spalten(x, y, z):
    return [[x[0], y[0], z[0]], [x[1], y[1], z[1]], [x[2], y[2], z[2]]]


def _mm(a, b):
    return [[sum(a[r][k] * b[k][c] for k in range(3)) for c in range(3)] for r in range(3)]


def _mv(a, v):
    return [sum(a[r][k] * v[k] for k in range(3)) for r in range(3)]


def _t(a):
    return [[a[c][r] for c in range(3)] for r in range(3)]


def _rot_z(grad):
    c, s = math.cos(math.radians(grad)), math.sin(math.radians(grad))
    return [[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]]


def _rot_180(achse):
    """Halbe Drehung um eine Achse: 2 a a^T - I."""
    a = _norm(achse)
    return [[2 * a[r] * a[c] - (1.0 if r == c else 0.0) for c in range(3)] for r in range(3)]


class Lage:
    """Starre Lage: Drehung R (Spalten = Achsen) und Ursprung o, in mm."""

    def __init__(self, r=None, o=None):
        self.r = r or [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        self.o = list(o or [0.0, 0.0, 0.0])

    def mal(self, andere):
        return Lage(_mm(self.r, andere.r), _add(_mv(self.r, andere.o), self.o))

    def invers(self):
        rt = _t(self.r)
        return Lage(rt, _mul(_mv(rt, self.o), -1.0))

    def punkt(self, p):
        return _add(_mv(self.r, p), self.o)


def achse_winkel(r):
    """Drehmatrix -> [ax, ay, az, Winkel] fuer Webots."""
    m = [r[0][0], r[0][1], r[0][2], r[1][0], r[1][1], r[1][2], r[2][0], r[2][1], r[2][2]]
    w = math.acos(max(-1.0, min(1.0, (m[0] + m[4] + m[8] - 1.0) / 2.0)))
    if w < 1e-9:
        return [0.0, 0.0, 1.0, 0.0]
    if math.pi - w < 1e-6:
        d = [m[0], m[4], m[8]]
        i = d.index(max(d))
        a = [0.0, 0.0, 0.0]
        a[i] = math.sqrt(max(0.0, (d[i] + 1.0) / 2.0))
        j, k = (i + 1) % 3, (i + 2) % 3
        a[j] = m[i * 3 + j] / (2.0 * a[i])
        a[k] = m[i * 3 + k] / (2.0 * a[i])
        return a + [w]
    s = 2.0 * math.sin(w)
    return [(m[7] - m[5]) / s, (m[2] - m[6]) / s, (m[3] - m[1]) / s, w]


# --- Verbindungspunkte ---

def _x_in_ebene(z):
    """Feste x-Richtung in der Ebene senkrecht zu z: CAD-x projiziert (sonst CAD-y)."""
    for ref in ([1.0, 0.0, 0.0], [0.0, 1.0, 0.0]):
        x = _sub(ref, _mul(z, _dot(ref, z)))
        if _dot(x, x) > 1e-6:
            return _norm(x)
    raise BaukastenFehler("keine x-Richtung")


def _rahmen(z, x, o):
    z = _norm(z)
    x = _norm(_sub(x, _mul(z, _dot(x, z))))
    return Lage(_spalten(x, _kreuz(z, x), z), o)


def _finde(liste, ident, was):
    for eintrag in liste:
        if str(eintrag["id"]) == str(ident):
            return eintrag
    raise BaukastenFehler("%s %s gibt es an diesem Teil nicht" % (was, ident))


def punkt_lage(merkmale, punkt):
    """Koordinatensystem eines Verbindungspunkts im Teil (mm)."""
    art = punkt.get("art")
    if art == "flaeche":
        f = _finde(merkmale["flaechen"], punkt.get("id"), "Flaeche")
        z = f["normale"]
        return _rahmen(z, _x_in_ebene(_norm(z)), f["mitte"])
    if art == "bohrung":
        b = _finde(merkmale["bohrungen"], punkt.get("id"), "Bohrung")
        ende = int(punkt.get("ende", 0))
        if ende not in (0, 1):
            raise BaukastenFehler("Bohrung: ende 0 oder 1")
        p, q = b["enden"][ende], b["enden"][1 - ende]
        z = _norm(_sub(p, q))   # entlang der Achse aus dieser Oeffnung heraus
        return _rahmen(z, _x_in_ebene(z), p)
    if art == "kante":
        k = _finde(merkmale["kanten"], punkt.get("id"), "Kante")
        f = _finde(merkmale["flaechen"], punkt.get("flaeche", k["flaechen"][0]), "Flaeche")
        if f["id"] not in k["flaechen"]:
            raise BaukastenFehler("Kante %s liegt nicht an Flaeche %s" % (k["id"], f["id"]))
        z = _norm(f["normale"])
        mitte = _mul(_add(k["a"], k["b"]), 0.5)
        x = _norm(_sub(k["b"], k["a"]))
        y = _kreuz(z, x)
        if _dot(y, _sub(f["mitte"], mitte)) < 0:   # y soll in die Flaeche hinein zeigen
            x = _mul(x, -1.0)
        return _rahmen(z, x, mitte)
    if art == "ecke":
        e = _finde(merkmale["ecken"], punkt.get("id"), "Ecke")
        seiten = e["flaechen"]
        s = seiten[0] if "flaeche" not in punkt else next(
            (s for s in seiten if str(s["flaeche"]) == str(punkt["flaeche"])), None)
        if s is None:
            raise BaukastenFehler("Ecke %s liegt nicht an Flaeche %s" % (e["id"], punkt["flaeche"]))
        z = _norm(_finde(merkmale["flaechen"], s["flaeche"], "Flaeche")["normale"])
        x, y = _norm(s["x"]), _norm(s["y"])
        if _dot(_kreuz(x, y), z) < 0:   # rechtshaendig: x und y tauschen
            x = y
        return _rahmen(z, x, e["punkt"])
    raise BaukastenFehler("Unbekannte Art von Verbindungspunkt: %r" % art)


def tisch_werte(entwurf=None):
    """Tischeinstellungen eines Entwurfs, geprueft und mit Standardwerten ergaenzt."""
    werte = dict(TISCH_STANDARD)
    eigene = (entwurf or {}).get("tisch") or {}
    if not isinstance(eigene, dict):
        raise BaukastenFehler("'tisch' muss ein Objekt sein.")
    art = eigene.get("art", "eben")
    if art == "profil":
        return _profil_werte(eigene)
    if art != "eben":
        raise BaukastenFehler("Tisch: Art ist 'eben' oder 'profil'.")
    for k, v in eigene.items():
        if k not in TISCH_STANDARD:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise BaukastenFehler("Tisch: %s braucht eine Zahl." % k)
        lo, hi = TISCH_GRENZEN[k]
        if not lo <= v <= hi:
            raise BaukastenFehler("Tisch: %s muss zwischen %g und %g liegen." % (k, lo, hi))
        werte[k] = float(v)
    werte["art"] = "eben"
    return werte


EA_FARBEN = ("gruen", "gelb", "rot", "blau", "weiss")
EA_MAX = 8   # je Seite


def ea_werte(entwurf=None):
    """Taster (links, digitale Eingaenge) und Leuchtmelder (rechts, Ausgaenge) eines Entwurfs, geprueft.
    Nummern wie in der iRC-Anzeige (1..64)."""
    ea = (entwurf or {}).get("ea") or {}
    if not isinstance(ea, dict):
        raise BaukastenFehler("'ea' muss ein Objekt sein.")
    ergebnis = {"taster": [], "leuchten": [], "sensoren": []}
    for s in ea.get("sensoren") or []:   # schreibt baukasten_speichern aus den Sensorflaechen (ea_sensoren)
        if not isinstance(s, dict) or not re.fullmatch(r"SENSOR_\d{1,3}", str(s.get("knoten", ""))):
            continue
        try:
            ausgabe = sensor_ausgabe(s, s["knoten"])
        except BaukastenFehler:
            continue   # kaputte Eintraege uebergehen, die Seite soll trotzdem laufen
        ergebnis["sensoren"].append(dict(ausgabe, name=str(s.get("name") or s["knoten"])[:24], knoten=s["knoten"]))
    for art, nr_feld in (("taster", "din"), ("leuchten", "dout")):
        liste = ea.get(art) or []
        if not isinstance(liste, list) or len(liste) > EA_MAX:
            raise BaukastenFehler("Hoechstens %d %s." % (EA_MAX, "Taster" if art == "taster" else "Leuchtmelder"))
        for i, e in enumerate(liste, 1):
            if not isinstance(e, dict):
                raise BaukastenFehler("%s %d ist kein Objekt." % (art, i))
            nr = e.get(nr_feld)
            if isinstance(nr, bool) or not isinstance(nr, int) or not 1 <= nr <= 64:
                raise BaukastenFehler("%s %d: %s braucht eine Nummer von 1 bis 64."
                                      % ("Taster" if art == "taster" else "Leuchtmelder", i, nr_feld.upper()))
            name = str(e.get("name") or "").strip()[:24] or "%s%d" % (nr_feld.upper(), nr)
            if art == "taster":
                ergebnis[art].append({"name": name, "din": nr,
                                      "art": "oeffner" if e.get("art") == "oeffner" else "schliesser",
                                      "modus": "rastend" if e.get("modus") == "rastend" else "tastend"})
            else:
                ergebnis[art].append({"name": name, "dout": nr, "umkehren": bool(e.get("umkehren")),
                                      "farbe": e.get("farbe") if e.get("farbe") in EA_FARBEN else "gruen"})
    return ergebnis


def profil_anschlaege_hinten():
    """Erlaubte Abstaende Anschlagkante -> hintere Kante: Fussmitte auf Nut 3 .. 18 (Nut 1/2 bzw. 19/20
    sind fuer die Platte zu nah an der Kante), also 10, 50, ... 610 mm."""
    g, r, a = PROFIL["groesse_mm"], PROFIL["raster_mm"], PLATTE["anschlag_mm"]
    werte = []
    for k in range(PROFIL["nuten"]):
        mitte = r / 2 + k * r
        if mitte - a >= 0 and mitte + a <= g:
            werte.append({"anschlag_mm": mitte - a, "nut": k + 1})
    return werte


def _profil_werte(eigene):
    werte = dict(PROFIL_STANDARD)
    for k in PROFIL_STANDARD:
        v = eigene.get(k, werte[k])
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise BaukastenFehler("Tisch: %s braucht eine Zahl." % k)
        werte[k] = float(v)
    g, a = PROFIL["groesse_mm"], PLATTE["anschlag_mm"]
    erlaubt = profil_anschlaege_hinten()
    nut = next((e["nut"] for e in erlaubt if abs(e["anschlag_mm"] - werte["anschlag_hinten_mm"]) < 0.01), None)
    if nut is None:
        raise BaukastenFehler("Profiltisch: Abstand Anschlag - hintere Kante nur im Nutraster: %s mm."
                              % ", ".join("%g" % e["anschlag_mm"] for e in erlaubt))
    # Seitlich gleitet die Platte in den Nuten; die aeusseren Bohrungen muessen noch ueber den Nuten
    # der Querprofile liegen (zwischen linkem und rechtem Rahmen)
    rand = PROFIL["rahmen_mm"] + PROFIL["nut_mm"] / 2
    lo = math.ceil(rand + PLATTE["bohrung_quer_mm"] - a)
    hi = math.floor(g - rand - PLATTE["bohrung_quer_mm"] - a)
    if not lo <= werte["anschlag_links_mm"] <= hi:
        raise BaukastenFehler("Profiltisch: Abstand Anschlag - linke Kante zwischen %d und %d mm, sonst liegen "
                              "Bohrungen der Basisplatte ueber dem Rahmen." % (lo, hi))
    if not -360 <= werte["drehung_grad"] <= 360:
        raise BaukastenFehler("Tisch: drehung_grad muss zwischen -360 und 360 liegen.")
    if werte["anschlag_hinten_mm"] < PROFIL["rahmen_mm"] and werte["anschlag_links_mm"] < PROFIL["rahmen_mm"]:
        raise BaukastenFehler("Profiltisch: Die Anschlagkante stiesse an die Abdeckkappe des Tischbeins hinten links.")
    werte.update({"art": "profil", "nut": nut, "laenge_mm": g, "breite_mm": g, "hoehe_mm": -PLATTE["dicke_mm"],
                  "roboter_von_hinten_mm": werte["anschlag_hinten_mm"] + a,
                  "roboter_von_links_mm": werte["anschlag_links_mm"] + a})
    return werte


def profil_nuten(t):
    """Nuten der Oberseite im Tischsystem (Ursprung Roboterfuss, x nach vorne, ohne Drehung):
    {name: ((x, y) Anfang, (x, y) Ende)} entlang der Nutmitte."""
    x0, y1 = -t["roboter_von_hinten_mm"], t["roboter_von_links_mm"]
    g, r, rahmen = PROFIL["groesse_mm"], PROFIL["raster_mm"], PROFIL["rahmen_mm"]
    nuten = {}
    for k in range(PROFIL["nuten"]):
        x = x0 + r / 2 + k * r
        nuten["nut-%d" % (k + 1)] = ((x, y1 - rahmen), (x, y1 - g + rahmen))
    nuten["nut-links"] = ((x0 + rahmen, y1 - rahmen / 2), (x0 + g - rahmen, y1 - rahmen / 2))
    nuten["nut-rechts"] = ((x0 + rahmen, y1 - g + rahmen / 2), (x0 + g - rahmen, y1 - g + rahmen / 2))
    return nuten


def tisch_ecken_lokal(t):
    """Ecken der Platte im Tischsystem (Ursprung Roboterfuss, x nach vorne, ohne Drehung), mm."""
    x0, x1 = -t["roboter_von_hinten_mm"], t["laenge_mm"] - t["roboter_von_hinten_mm"]
    y1, y0 = t["roboter_von_links_mm"], t["roboter_von_links_mm"] - t["breite_mm"]
    return {"hinten-rechts": (x0, y0), "vorne-rechts": (x1, y0), "vorne-links": (x1, y1), "hinten-links": (x0, y1)}


def tisch_merkmale(t=None):
    """Tischplatte und Roboterfuss als Merkmale wie bei einem Teil (Weltkoordinaten = iRC, mm)."""
    t = t or dict(TISCH_STANDARD)
    r = _rot_z(-t["drehung_grad"])   # Roboter um +d gedreht = Tisch um -d um den Roboter
    h = t["hoehe_mm"]

    def welt(x, y):
        return _add(_mv(r, [x, y, 0.0]), [0.0, 0.0, h])

    lokal = tisch_ecken_lokal(t)
    ecken_p = {n: welt(*p) for n, p in lokal.items()}
    mitte = welt(sum(p[0] for p in lokal.values()) / 4, sum(p[1] for p in lokal.values()) / 4)
    kanten = {"vorne": ("vorne-rechts", "vorne-links"), "hinten": ("hinten-rechts", "hinten-links"),
              "links": ("hinten-links", "vorne-links"), "rechts": ("hinten-rechts", "vorne-rechts")}
    ecken = []
    for name, p in ecken_p.items():
        nach_x = _mv(r, [1.0 if name.startswith("hinten") else -1.0, 0.0, 0.0])   # von der Querkante weg ins Innere
        nach_y = _mv(r, [0.0, 1.0 if name.endswith("rechts") else -1.0, 0.0])
        ecken.append({"id": name, "punkt": p, "flaechen": [{"flaeche": "oben", "x": nach_x, "y": nach_y}]})
    kanten_liste = [{"id": k, "a": ecken_p[a], "b": ecken_p[b], "flaechen": ["oben"]} for k, (a, b) in kanten.items()]
    if t.get("art") == "profil":   # Nutmitten als Kanten (Fangradius im Baukasten: nur in der Nut)
        kanten_liste += [{"id": n, "a": welt(*a), "b": welt(*b), "flaechen": ["oben"], "fang_mm": PROFIL["nut_mm"] / 2}
                         for n, (a, b) in profil_nuten(t).items()]
    neue_ecken = list(ecken)
    for alt, neu in ALTE_NAMEN.items():   # alte Namen als Doppel
        if neu in kanten:
            kanten_liste.append(dict(next(k for k in kanten_liste if k["id"] == neu), id=alt))
        else:
            ecken.append(dict(next(e for e in neue_ecken if e["id"] == neu), id=alt))
    return {
        "flaechen": [{"id": "oben", "normale": [0.0, 0.0, 1.0], "mitte": mitte},
                     {"id": "ursprung", "normale": [0.0, 0.0, 1.0], "mitte": [0.0, 0.0, 0.0]}],
        "kanten": kanten_liste,
        "ecken": ecken,
        "bohrungen": [],
    }


# --- Sensorflaechen (Stufe F2): Quader ohne Physik, meldet "belegt" an einen Eingang ---

SENSOR_STANDARD = {"groesse_mm": [40.0, 40.0, 20.0], "din": 24, "art": "schliesser", "typ": "belegt"}


def sensor_werte(t):
    """Einstellungen einer Sensorflaeche (Eintrag mit "art": "sensor"), geprueft."""
    s = t.get("sensor") or {}
    if not isinstance(s, dict):
        raise BaukastenFehler("%s: 'sensor' muss ein Objekt sein." % t.get("id"))
    groesse = s.get("groesse_mm", SENSOR_STANDARD["groesse_mm"])
    if (not isinstance(groesse, list) or len(groesse) != 3
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and 1 <= v <= 1000 for v in groesse)):
        raise BaukastenFehler("%s: Groesse braucht drei Zahlen von 1 bis 1000 mm." % t.get("id"))
    werte = {"name": str(s.get("name") or t.get("id"))[:24], "groesse_mm": [float(v) for v in groesse]}
    werte.update(sensor_ausgabe(s, t.get("id")))
    return werte


# Farbsensor (Stufe F3): erkennt die Farbe des Werkstuecks auf der Flaeche (naechste Farbe dieser Liste,
# Werte wie im Controller ea.FARBEN) und meldet sie je Farbe an einen Eingang oder als Zahl ueber Bits.
FARBNAMEN = ("rot", "gruen", "blau", "gelb", "weiss", "schwarz")


def _din(wert, wer, was="DIn"):
    if isinstance(wert, bool) or not isinstance(wert, int) or not 1 <= wert <= 64:
        raise BaukastenFehler("%s: %s braucht eine Nummer von 1 bis 64." % (wer, was))
    return wert


def sensor_ausgabe(s, wer):
    """Typ und Ausgaenge einer Sensorflaeche: "belegt" (din, art) oder "farbe" mit ausgabe "je_farbe"
    (farben: [{farbe, din}]) bzw. "bits" (din = erstes Bit, bits = Anzahl, farben: [{farbe, code}])."""
    if s.get("typ", "belegt") != "farbe":
        return {"typ": "belegt", "din": _din(s.get("din", SENSOR_STANDARD["din"]), wer),
                "art": "oeffner" if s.get("art") == "oeffner" else "schliesser"}
    ausgabe = "bits" if s.get("ausgabe") == "bits" else "je_farbe"
    farben, gesehen = [], set()
    roh = s.get("farben") or []
    if not isinstance(roh, list):
        raise BaukastenFehler("%s: 'farben' muss eine Liste sein." % wer)
    werte = {"typ": "farbe", "ausgabe": ausgabe}
    if ausgabe == "bits":
        din, bits = _din(s.get("din", SENSOR_STANDARD["din"]), wer, "Erstes Bit (DIn)"), s.get("bits", 2)
        if isinstance(bits, bool) or not isinstance(bits, int) or not 1 <= bits <= 4 or din + bits - 1 > 64:
            raise BaukastenFehler("%s: 1 bis 4 Bits, das letzte hoechstens DIn64." % wer)
        werte.update(din=din, bits=bits)
    for f in roh:
        if not isinstance(f, dict) or f.get("farbe") not in FARBNAMEN or f["farbe"] in gesehen:
            raise BaukastenFehler("%s: Farbe %r ist unbekannt oder doppelt." % (wer, (f or {}).get("farbe")))
        gesehen.add(f["farbe"])
        if ausgabe == "bits":
            code = f.get("code")
            if isinstance(code, bool) or not isinstance(code, int) or not 1 <= code < 2 ** werte["bits"]:
                raise BaukastenFehler("%s: Code fuer %s von 1 bis %d (0 = keine Farbe)." % (wer, f["farbe"], 2 ** werte["bits"] - 1))
            farben.append({"farbe": f["farbe"], "code": code})
        else:
            farben.append({"farbe": f["farbe"], "din": _din(f.get("din"), wer, "DIn fuer %s" % f["farbe"])})
    if not farben:
        raise BaukastenFehler("%s: Der Farbsensor braucht mindestens eine Farbe." % wer)
    if ausgabe == "bits" and len({f["code"] for f in farben}) != len(farben):
        raise BaukastenFehler("%s: Jeder Code nur einmal." % wer)
    werte["farben"] = farben
    return werte


def quader_merkmale(lx, ly, lz):
    """Merkmale eines Quaders mit Ursprung in der Mitte der Unterseite (wie teil.json)."""
    hx, hy = lx / 2.0, ly / 2.0
    seiten = {"vorne": [1.0, 0.0, 0.0], "hinten": [-1.0, 0.0, 0.0], "links": [0.0, 1.0, 0.0], "rechts": [0.0, -1.0, 0.0]}
    flaechen = [{"id": "unten", "normale": [0.0, 0.0, -1.0], "mitte": [0.0, 0.0, 0.0]},
                {"id": "oben", "normale": [0.0, 0.0, 1.0], "mitte": [0.0, 0.0, lz]}]
    for name, n in seiten.items():
        flaechen.append({"id": name, "normale": n, "mitte": [n[0] * hx, n[1] * hy, lz / 2.0]})
    ecken_p = {"hinten-rechts": [-hx, -hy, 0.0], "vorne-rechts": [hx, -hy, 0.0],
               "vorne-links": [hx, hy, 0.0], "hinten-links": [-hx, hy, 0.0]}
    kanten = [{"id": s, "a": ecken_p[a], "b": ecken_p[b], "flaechen": ["unten", s]}
              for s, (a, b) in {"vorne": ("vorne-rechts", "vorne-links"), "hinten": ("hinten-rechts", "hinten-links"),
                                "links": ("hinten-links", "vorne-links"), "rechts": ("hinten-rechts", "vorne-rechts")}.items()]
    ecken = [{"id": n, "punkt": p, "flaechen": [{"flaeche": "unten", "x": [-1.0 if p[0] > 0 else 1.0, 0.0, 0.0],
                                                  "y": [0.0, -1.0 if p[1] > 0 else 1.0, 0.0]}]}
             for n, p in ecken_p.items()]
    return {"box": [-hx, -hy, 0.0, hx, hy, lz], "volumen_mm3": lx * ly * lz, "flaechen": flaechen,
            "kanten": kanten, "ecken": ecken, "bohrungen": []}


# Kollisionspruefung je Welt (Stufen K1/K2), Werte wie kollision.einstellungen im Controller
KOLLISION_STANDARD = {"an": True, "reaktion": "stopp", "abstand_mm": 2.0, "ablage_mm": 2.0}


def kollision_werte(entwurf=None):
    """Einstellungen "kollision" des Entwurfs, geprueft: an (bool), reaktion (melden|stopp), abstand_mm und
    ablage_mm (0 bis 20)."""
    e = dict(KOLLISION_STANDARD)
    k = (entwurf or {}).get("kollision") or {}
    if not isinstance(k, dict):
        raise BaukastenFehler("'kollision' muss ein Objekt sein.")
    if "an" in k:
        e["an"] = bool(k["an"])
    if k.get("reaktion", e["reaktion"]) not in ("melden", "stopp"):
        raise BaukastenFehler("Kollision: Reaktion ist 'melden' oder 'stopp'.")
    e["reaktion"] = k.get("reaktion", e["reaktion"])
    for feld, text in (("abstand_mm", "Sicherheitsabstand"), ("ablage_mm", "Ablagetoleranz")):
        if feld in k:
            wert = k[feld]
            if isinstance(wert, bool) or not isinstance(wert, (int, float)) or not 0 <= wert <= 20:
                raise BaukastenFehler("Kollision: %s von 0 bis 20 mm." % text)
            e[feld] = float(wert)
    return e


# --- Wegbegrenzungen (Stufe K4): Arbeitsraum, Sperrzonen, Achsgrenzen ---
# Gelten fuer Greifer, Backen und gegriffenes Teil (Achsgrenzen fuer die Gelenke). Reaktion je Grenze:
# "warnung" (nur Hinweis) oder "stopp" (wie eine Kollision: Programm anhalten, quittieren).

IRC_ACHSGRENZEN = [[-179.0, 179.0], [-80.0, 140.0], [-80.0, 140.0], [-179.0, 179.0], [-95.0, 95.0], [-179.0, 179.0]]   # ReBeL-6DOF-03
SPERRZONE_STANDARD = {"groesse_mm": [100.0, 100.0, 100.0], "reaktion": "stopp"}
GRENZEN_STANDARD = {"arbeitsraum": {"an": False, "min_mm": [-200.0, -500.0, 0.0], "max_mm": [700.0, 500.0, 700.0],
                                    "reaktion": "stopp"},
                    "achsen": None, "achsen_reaktion": "stopp"}
REAKTIONEN = ("warnung", "stopp")


def sperrzone_werte(t):
    """Einstellungen einer Sperrzone (Eintrag mit "art": "sperrzone"), geprueft."""
    z = t.get("sperrzone") or {}
    if not isinstance(z, dict):
        raise BaukastenFehler("%s: 'sperrzone' muss ein Objekt sein." % t.get("id"))
    groesse = z.get("groesse_mm", SPERRZONE_STANDARD["groesse_mm"])
    if (not isinstance(groesse, list) or len(groesse) != 3
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and 1 <= v <= 3000 for v in groesse)):
        raise BaukastenFehler("%s: Groesse der Sperrzone braucht drei Zahlen von 1 bis 3000 mm." % t.get("id"))
    reaktion = z.get("reaktion", SPERRZONE_STANDARD["reaktion"])
    if reaktion not in REAKTIONEN:
        raise BaukastenFehler("%s: Reaktion ist 'warnung' oder 'stopp'." % t.get("id"))
    return {"name": str(z.get("name") or t.get("id"))[:24], "groesse_mm": [float(v) for v in groesse], "reaktion": reaktion}


def _drei_zahlen(werte, was):
    if (not isinstance(werte, list) or len(werte) != 3
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and -5000 <= v <= 5000 for v in werte)):
        raise BaukastenFehler("%s braucht drei Zahlen (mm)." % was)
    return [float(v) for v in werte]


def grenzen_werte(entwurf=None):
    """Arbeitsraum und Achsgrenzen des Entwurfs ("grenzen"), geprueft und mit Standardwerten.
    arbeitsraum: an, min_mm/max_mm (Quader in iRC-Koordinaten), reaktion; achsen: None (= iRC-Grenzen, keine
    eigene Pruefung) oder 6 x [min, max] in Grad; achsen_reaktion."""
    g = (entwurf or {}).get("grenzen") or {}
    if not isinstance(g, dict):
        raise BaukastenFehler("'grenzen' muss ein Objekt sein.")
    a_roh = g.get("arbeitsraum") or {}
    if not isinstance(a_roh, dict):
        raise BaukastenFehler("Arbeitsraum: muss ein Objekt sein.")
    a = dict(GRENZEN_STANDARD["arbeitsraum"])
    a["an"] = bool(a_roh.get("an", a["an"]))
    a["min_mm"] = _drei_zahlen(a_roh.get("min_mm", a["min_mm"]), "Arbeitsraum von")
    a["max_mm"] = _drei_zahlen(a_roh.get("max_mm", a["max_mm"]), "Arbeitsraum bis")
    if any(a["max_mm"][i] - a["min_mm"][i] < 10 for i in range(3)):
        raise BaukastenFehler("Arbeitsraum: 'bis' muss je Achse mindestens 10 mm groesser sein als 'von'.")
    if a_roh.get("reaktion", a["reaktion"]) not in REAKTIONEN:
        raise BaukastenFehler("Arbeitsraum: Reaktion ist 'warnung' oder 'stopp'.")
    a["reaktion"] = a_roh.get("reaktion", a["reaktion"])
    achsen = g.get("achsen")
    if achsen is not None:
        if not isinstance(achsen, list) or len(achsen) != 6:
            raise BaukastenFehler("Achsgrenzen: sechs Paare (A1 bis A6).")
        geprueft = []
        for i, (paar, irc) in enumerate(zip(achsen, IRC_ACHSGRENZEN)):
            if (not isinstance(paar, list) or len(paar) != 2
                    or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in paar)):
                raise BaukastenFehler("Achsgrenze A%d: zwei Zahlen (min, max) in Grad." % (i + 1))
            lo, hi = float(paar[0]), float(paar[1])
            if not irc[0] <= lo < hi <= irc[1]:
                raise BaukastenFehler("Achsgrenze A%d: %g bis %g Grad, innerhalb der iRC-Grenzen %g bis %g."
                                      % (i + 1, lo, hi, irc[0], irc[1]))
            geprueft.append([lo, hi])
        achsen = None if geprueft == IRC_ACHSGRENZEN else geprueft   # wie iRC: nichts eigenes zu pruefen
    reaktion = g.get("achsen_reaktion", GRENZEN_STANDARD["achsen_reaktion"])
    if reaktion not in REAKTIONEN:
        raise BaukastenFehler("Achsgrenzen: Reaktion ist 'warnung' oder 'stopp'.")
    return {"arbeitsraum": a, "achsen": achsen, "achsen_reaktion": reaktion}


def grenzen_fuer_welt(entwurf, lagen):
    """"grenzen" fuer worlds/<welt>.json (liest der Controller): Arbeitsraum (nur wenn an), Sperrzonen mit Mitte,
    Drehung (Zeilen) und halber Groesse in iRC-Koordinaten (mm), Achsgrenzen."""
    g = grenzen_werte(entwurf)
    zonen = []
    for t in entwurf.get("teile", []):
        if t.get("art") != "sperrzone" or t["id"] not in lagen:
            continue
        z = sperrzone_werte(t)
        lage = lagen[t["id"]]
        lx, ly, lz = z["groesse_mm"]
        zonen.append({"name": z["name"], "reaktion": z["reaktion"],
                      "mitte_mm": [round(v, 2) for v in lage.punkt([0.0, 0.0, lz / 2.0])],
                      "drehung": [[round(v, 9) for v in zeile] for zeile in lage.r],
                      "halb_mm": [lx / 2.0, ly / 2.0, lz / 2.0]})
    return {"arbeitsraum": g["arbeitsraum"] if g["arbeitsraum"]["an"] else None, "sperrzonen": zonen,
            "achsen": g["achsen"], "achsen_reaktion": g["achsen_reaktion"]}


def ea_sensoren(entwurf):
    """Sensorflaechen fuer worlds/<welt>.json ("ea" -> "sensoren"); "knoten" = DEF-Name in der Welt."""
    return [dict(sensor_werte(t), knoten="SENSOR_%d" % i)
            for i, t in enumerate(t for t in entwurf.get("teile", []) if t.get("art") == "sensor")]


# --- Backensaetze (Phase 4): eigene Backe am Backentraeger eines Schunk EGP ---
# Der Greifer aus der Teilebibliothek (Schunk-CAD, Traeger geschlossen wie im iRC-Modell) steht im Werkzeugsystem
# von iRC: Ursprung Flansch, z vom Flansch weg, Backen bewegen sich entlang x. Schunk-x bleibt x,
# Schunk-y wird z (+8 mm Adapterplatte), Schunk-z wird -y. Bei EGP 25 und EGP 40 gleich.
GREIFER_TEILE = {"egp40": "schunk_egp40", "egp25": "schunk_egp25"}
GREIFER_TEIL = GREIFER_TEILE["egp25"]   # Entwuerfe ohne "basis" stammen aus der Zeit vor dem EGP 40
GREIFER_LAGE = Lage([[1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]], [0.0, 0.0, 8.0])
BACKENSATZ_NAME = re.compile(r"[a-z][a-z0-9_]{0,19}")


def greifer_basis(entwurf):
    basis = entwurf.get("basis") or "egp25"
    if basis not in GREIFER_TEILE:
        raise BaukastenFehler("Greifer %r gibt es nicht (EGP 40 oder EGP 25)." % basis)
    return basis


def tcp_werte(entwurf):
    """TCP eines Backensatzes: Bezug "spitze" oder "mitte" der Backen plus Versatz (mm, + = vom Flansch weg),
    oder "flansch" mit festem Abstand. Rueckgabe: Felder fuer backensatz.json."""
    t = entwurf.get("tcp") or {}
    art = t.get("bezug", "spitze")
    try:
        wert = float(t.get("mm") or 0)
    except (TypeError, ValueError):
        raise BaukastenFehler("TCP: bitte eine Zahl in mm angeben.")
    if art in ("spitze", "mitte"):
        if not -200 <= wert <= 200:
            raise BaukastenFehler("TCP-Versatz: -200 bis 200 mm.")
        return {"tcp": art, "tcp_versatz_mm": round(wert, 2)}
    if art == "flansch":
        if not 0 < wert <= 400:
            raise BaukastenFehler("TCP ab Flansch: 0 bis 400 mm.")
        return {"tcp": round(wert, 2), "tcp_versatz_mm": 0}
    raise BaukastenFehler("TCP-Bezug ist 'spitze', 'mitte' oder 'flansch'.")


def backensatz_aus_entwurf(name, entwurf, lagen, merkmale_von):
    """backensatz.json fuer tools/erzeuge_rebel_modell.py: die LINKE Backe (x < 0) im Werkzeugsystem.
    Haengt sie am rechten Traeger, wird sie um die Werkzeugachse gedreht (die rechte ist dieselbe Backe)."""
    if not BACKENSATZ_NAME.fullmatch(name or ""):
        raise BaukastenFehler("Name des Greifers: Buchstabe zuerst, dann a-z, 0-9 und _, hoechstens 20 Zeichen.")
    basis = greifer_basis(entwurf)
    tcp = tcp_werte(entwurf)
    hub = entwurf.get("hub_mm")
    if hub not in (None, ""):
        try:
            hub = round(float(hub), 2)
        except (TypeError, ValueError):
            raise BaukastenFehler("Hub je Backe: bitte eine Zahl in mm.")
        if not 0 < hub <= 30:
            raise BaukastenFehler("Hub je Backe: 0 bis 30 mm.")
    backen = [t for t in entwurf.get("teile", []) if t.get("art", "aufbau") == "aufbau"]
    if len(backen) != 1 or len(entwurf["teile"]) != 1:
        raise BaukastenFehler("Ein eigener Greifer hat genau eine Backe (die zweite ist dieselbe, gedreht).")
    backe = backen[0]
    if (backe.get("verbindung") or {}).get("an") != "greifer":
        raise BaukastenFehler("Die Backe muss mit dem Greifer verbunden sein.")
    lage = lagen[backe["id"]]
    box = merkmale_von(backe["teil"])["box"]
    mitte = lage.punkt([(box[0] + box[3]) / 2, (box[1] + box[4]) / 2, (box[2] + box[5]) / 2])
    if mitte[0] > 0:
        lage = Lage(_rot_z(180.0)).mal(lage)
    satz = {"name": name, "titel": str(entwurf.get("titel") or name)[:40], "basis": basis, "teil": backe["teil"],
            "matrix": [[round(v, 9) for v in zeile] for zeile in lage.r],
            "verschiebung_mm": [round(v, 4) for v in lage.o]}
    satz.update(tcp)
    if hub not in (None, ""):
        satz["hub_mm"] = hub
    return satz


def _umklappen(art_teil, art_ziel):
    """Halbe Drehung, die z umkehrt und das Wesentliche der Punkte zusammenhaelt."""
    if art_teil == art_ziel == "kante":
        return _rot_180([0.0, 1.0, 0.0])    # Innenseiten (y) bleiben auf derselben Seite
    if art_teil == art_ziel == "ecke":
        return _rot_180([1.0, 1.0, 0.0])    # beide Kanten bleiben innen
    return _rot_180([1.0, 0.0, 0.0])        # x bleibt: Drehung 0 = gleiche x-Richtung


# --- Entwurf loesen ---

def pruefe_entwurf(entwurf):
    teile = entwurf.get("teile")
    if not isinstance(teile, list) or len(teile) > MAX_TEILE:
        raise BaukastenFehler("Der Entwurf braucht eine Liste 'teile' (hoechstens %d)." % MAX_TEILE)
    ids = set()
    for t in teile:
        if not ID.fullmatch(str(t.get("id", ""))) or t["id"] in ("tisch", "roboterfuss"):
            raise BaukastenFehler("Ungueltiger Name %r (Buchstabe zuerst, dann a-z, 0-9, - und _)." % t.get("id"))
        if t["id"] in ids or t["id"] == "greifer":
            raise BaukastenFehler("Der Name %s kommt doppelt vor oder ist vergeben." % t["id"])
        ids.add(t["id"])
        if t.get("art", "aufbau") not in ("aufbau", "werkstueck", "sensor", "sperrzone"):
            raise BaukastenFehler("%s: Art ist 'aufbau', 'werkstueck', 'sensor' oder 'sperrzone'." % t["id"])
        if t.get("art") == "sensor":
            sensor_werte(t)
        if t.get("art") == "sperrzone":
            sperrzone_werte(t)
        v = t.get("verbindung")
        if not isinstance(v, dict):
            raise BaukastenFehler("%s hat keine Verbindung." % t["id"])
        versatz = v.get("versatz_mm", [0, 0, 0])
        if len(versatz) != 3 or not all(isinstance(x, (int, float)) for x in versatz):
            raise BaukastenFehler("%s: versatz_mm braucht drei Zahlen." % t["id"])
        if not isinstance(v.get("drehung_grad", 0), (int, float)):
            raise BaukastenFehler("%s: drehung_grad ist eine Zahl." % t["id"])


def loese(entwurf, merkmale_von):
    """Lagen aller Teile in Weltkoordinaten (mm). merkmale_von(teilname) -> teil.json.
    Rueckgabe: {id: Lage}. Fehler bei Schleifen, unbekannten Teilen oder Punkten."""
    pruefe_entwurf(entwurf)
    teile = {t["id"]: t for t in entwurf["teile"]}
    backensatz = entwurf.get("art") == "backensatz"   # Bezug ist dann der Greifer statt des Tischs
    tisch = None if backensatz else tisch_merkmale(tisch_werte(entwurf))

    def merkmale(t):
        if t.get("art") == "sensor":
            return quader_merkmale(*sensor_werte(t)["groesse_mm"])
        if t.get("art") == "sperrzone":
            return quader_merkmale(*sperrzone_werte(t)["groesse_mm"])
        return merkmale_von(t["teil"])
    lagen, in_arbeit = {}, set()

    def lage(ident):
        if ident in lagen:
            return lagen[ident]
        if ident in in_arbeit:
            raise BaukastenFehler("Die Verbindungen bilden eine Schleife (bei %s)." % ident)
        in_arbeit.add(ident)
        t = teile[ident]
        v = t["verbindung"]
        an = v.get("an")
        if an in ("tisch", "roboterfuss") and not backensatz:
            ziel_welt, ziel_merkmale = Lage(), tisch
        elif an == "greifer" and backensatz:
            ziel_welt, ziel_merkmale = GREIFER_LAGE, merkmale_von(GREIFER_TEILE[greifer_basis(entwurf)])
        elif an in teile:
            ziel_welt, ziel_merkmale = lage(an), merkmale(teile[an])
        else:
            raise BaukastenFehler("%s haengt an %r, das gibt es nicht." % (ident, an))
        an_punkt = v.get("an_punkt") or {}
        ziel = ziel_welt.mal(punkt_lage(ziel_merkmale, an_punkt))
        teil_punkt = punkt_lage(merkmale(t), v.get("punkt") or {})
        versatz = Lage(None, [float(x) for x in v.get("versatz_mm", [0, 0, 0])])
        drehung = Lage(_rot_z(float(v.get("drehung_grad", 0))))
        klapp = Lage() if v.get("umdrehen") else Lage(_umklappen((v.get("punkt") or {}).get("art"), an_punkt.get("art")))
        lagen[ident] = ziel.mal(versatz).mal(drehung).mal(klapp).mal(teil_punkt.invers())
        in_arbeit.discard(ident)
        return lagen[ident]

    for ident in teile:
        lage(ident)
    return lagen


# --- Welt schreiben ---

KOPF = """#VRML_SIM R2025a utf8
# Erzeugt vom Weltbaukasten (tools/baukasten.py) aus %(entwurf)s - Aenderungen bitte im Baukasten.
# Koordinaten: Weltursprung = Roboterfuss, Tischplatte bei z = 0, Position in m = iRC-Koordinate in mm / 1000.

EXTERNPROTO "../protos/IgusRebel6DOF.proto"
EXTERNPROTO "../protos/Werkstueck.proto"
EXTERNPROTO "../protos/Aufbau.proto"
EXTERNPROTO "../protos/Sensorfeld.proto"

WorldInfo {
  info [ %(info)s ]
  title %(titel)s
  basicTimeStep 16
  contactProperties [ ContactProperties { coulombFriction [ 0.8 ] } ]
}
Viewpoint {
  orientation %(blick_drehung)s
  position %(blick_ort)s
}
Background { skyColor [ 0.86 0.89 0.93 ] }
DirectionalLight { direction -0.4 0.5 -1 intensity 2.2 castShadows FALSE }
DirectionalLight { direction 0.6 -0.3 -0.5 intensity 0.8 }
DEF BODEN Solid {
  translation 0 0 %(boden_z).4f
  children [ Shape { appearance PBRAppearance { baseColor 0.55 0.56 0.58 roughness 1 metalness 0 } geometry Plane { size 8 8 } } ]
  name "boden"
  boundingObject Plane { size 8 8 }
}
DEF TISCH Solid {
  translation %(tisch_x).4f %(tisch_y).4f %(tisch_z).4f
  rotation 0 0 1 %(tisch_drehung).6f
  children [
%(tisch_kinder)s  ]
  name "tisch"
  boundingObject Pose { translation 0 0 -%(tisch_dicke_halb).4f children [ Box { size %(tisch_lx).4f %(tisch_ly).4f %(tisch_dicke).4f } ] }
}
%(sockel)sDEF REBEL IgusRebel6DOF {
  greifer %(greifer)s
}
"""

TISCH_EBEN = """    Pose { translation 0 0 -0.015 children [ Shape { appearance PBRAppearance { baseColor 0.82 0.80 0.76 roughness 0.8 metalness 0 } geometry Box { size %(tisch_lx).4f %(tisch_ly).4f 0.03 } } ] }
    Pose { translation %(bein_x).4f %(bein_y).4f -0.39 children [ DEF BEIN Shape { appearance DEF ALU PBRAppearance { baseColor 0.7 0.72 0.75 roughness 0.4 metalness 0.8 } geometry Box { size 0.05 0.05 0.72 } } ] }
    Pose { translation -%(bein_x).4f %(bein_y).4f -0.39 children [ USE BEIN ] }
    Pose { translation %(bein_x).4f -%(bein_y).4f -0.39 children [ USE BEIN ] }
    Pose { translation -%(bein_x).4f -%(bein_y).4f -0.39 children [ USE BEIN ] }
"""

PLATTE_AUFBAU = """DEF BASISPLATTE Aufbau {
  rotation %(drehung)s
  name "basisplatte"
  modell [ %(url)s ]
  skalierung 0.001
  farbe 0.32 0.34 0.37
}
"""

SOCKEL = """DEF SOCKEL Solid {
  translation 0 0 %(z).4f
  children [ Shape { appearance PBRAppearance { baseColor 0.35 0.36 0.38 roughness 0.6 metalness 0.3 } geometry Cylinder { radius 0.08 height %(h).4f } } ]
  name "sockel"
  boundingObject Cylinder { radius 0.08 height %(h).4f }
}
"""


AUSSEHEN = {"ALU": "baseColor 0.78 0.8 0.83 roughness 0.45 metalness 0.35",
            "NUTGRUND": "baseColor 0.2 0.21 0.23 roughness 0.8 metalness 0",
            "KAPPE": "baseColor 0.08 0.08 0.09 roughness 0.7 metalness 0"}


def _quader(x, y, z, lx, ly, lz, aussehen, definiert):
    """Pose mit Box in m aus Mitte und Groesse in mm; das Aussehen beim ersten Mal per DEF, danach USE."""
    if aussehen in definiert:
        a = "USE " + aussehen
    else:
        a = "DEF %s PBRAppearance { %s }" % (aussehen, AUSSEHEN[aussehen])
        definiert.add(aussehen)
    return "    Pose { translation %.4f %.4f %.4f children [ Shape { appearance %s geometry Box { size %.4f %.4f %.4f } } ] }\n" % (
        x / 1000, y / 1000, z / 1000, a, lx / 1000, ly / 1000, lz / 1000)


def _streifen(von, bis, nuten, breite):
    """Stege zwischen den Nuten: Abschnitte von..bis ohne die Nuten (Mitten, Breite)."""
    kanten = [von] + [k for n in nuten for k in (n - breite / 2, n + breite / 2)] + [bis]
    return [(kanten[i], kanten[i + 1]) for i in range(0, len(kanten), 2)]


def profiltisch_kinder(boden_mm=750.0):
    """Children des Profiltischs im Tischsystem (Mitte der Platte, Oberseite z = 0, x nach vorne, y nach links)."""
    g, rahmen, r, nb, d = (PROFIL[k] for k in ("groesse_mm", "rahmen_mm", "raster_mm", "nut_mm", "dicke_mm"))
    h, tief = g / 2, 12.0   # Nutgrund sichtbar 12 mm unter der Oberseite
    text, definiert = "", set()
    # Rahmen hinten/vorne und Querprofile: Nuten von links nach rechts im 40er-Raster
    quer = [-h + r / 2 + k * r for k in range(PROFIL["nuten"])]
    for a, b in _streifen(-h, h, quer, nb):
        text += _quader((a + b) / 2, 0, -d / 2, b - a, g - 2 * rahmen, d, "ALU", definiert)
    for x in quer:
        text += _quader(x, 0, -(d + tief) / 2, nb, g - 2 * rahmen, d - tief, "NUTGRUND", definiert)
    # Rahmen links/rechts: Nut von hinten nach vorne
    for seite in (1, -1):
        mitte = seite * (h - rahmen / 2)
        for a, b in _streifen(mitte - rahmen / 2, mitte + rahmen / 2, [mitte], nb):
            text += _quader(0, (a + b) / 2, -d / 2, g - 2 * rahmen, b - a, d, "ALU", definiert)
        text += _quader(0, mitte, -(d + tief) / 2, g - 2 * rahmen, nb, d - tief, "NUTGRUND", definiert)
    # Beine bis zum Boden, oben Abdeckkappen
    for sx in (1, -1):
        for sy in (1, -1):
            x, y = sx * (h - rahmen / 2), sy * (h - rahmen / 2)
            text += _quader(x, y, -boden_mm / 2, rahmen, rahmen, boden_mm, "ALU", definiert)
            text += _quader(x, y, PROFIL["kappe_mm"] / 2, rahmen, rahmen, PROFIL["kappe_mm"], "KAPPE", definiert)
    return text


def platte_lage(t):
    """Lage der Basisplatte (CAD wie aus Onshape: Mitte im Ursprung, Auflage des Roboters bei z = 0,
    Anschlagkante zeigt nach -z in der Ecke -x/+y). Umgedreht, Kante nach oben, Ecke hinten links."""
    umgedreht = _rot_180([1.0, -1.0, 0.0])   # (x, y, z) -> (-y, -x, -z), die Ecke -x/+y bleibt
    return Lage(_mm(_rot_z(-t["drehung_grad"]), umgedreht))


def _blick(ort, ziel):
    """Viewpoint-Drehung: Kamera bei ort schaut auf ziel (Webots: x = Blickrichtung, z = oben)."""
    x = _norm(_sub(ziel, ort))
    z = _norm(_sub([0.0, 0.0, 1.0], _mul(x, x[2])))
    return achse_winkel(_spalten(x, _kreuz(z, x), z))


def _vrml_text(s):
    return json.dumps(str(s), ensure_ascii=True)


def _zahlen(werte, stellen=6):
    return " ".join(("%." + str(stellen) + "f") % v for v in werte)


def welt_text(entwurf, lagen, merkmale_von, entwurf_name="Entwurf", teile_url="../teile"):
    """Webots-Welt aus Entwurf und geloesten Lagen."""
    t = tisch_werte(entwurf)
    lx, ly = t["laenge_mm"] / 1000, t["breite_mm"] / 1000
    mitte = tisch_merkmale(t)["flaechen"][0]["mitte"]
    h = t["hoehe_mm"] / 1000
    ziel = [mitte[0] / 1000, mitte[1] / 1000, h]
    ort = [ziel[0] + 1.1, ziel[1] - 1.15, h + 0.75]
    werte = {"tisch_lx": lx, "tisch_ly": ly, "bein_x": lx / 2 - 0.05, "bein_y": ly / 2 - 0.05}
    if t["art"] == "profil":
        try:
            merkmale_von(BASISPLATTE)
        except (BaukastenFehler, KeyError):
            raise BaukastenFehler("Fuer den Profiltisch fehlt das Teil %s in der Teilebibliothek." % BASISPLATTE)
        kinder, dicke = profiltisch_kinder(), PROFIL["dicke_mm"] / 1000
        sockel = PLATTE_AUFBAU % {"drehung": _zahlen(achse_winkel(platte_lage(t).r)),
                                  "url": _vrml_text("%s/%s/netz.obj" % (teile_url, BASISPLATTE))}
    else:
        kinder, dicke = TISCH_EBEN % werte, 0.03
        sockel = SOCKEL % {"z": h / 2, "h": -h} if h < -0.001 else ""
    text = KOPF % dict(werte, **{
        "entwurf": entwurf_name, "info": _vrml_text(entwurf.get("aufgabe", "")[:500]),
        "titel": _vrml_text(entwurf.get("titel", "Baukasten-Welt")), "greifer": _vrml_text(entwurf.get("greifer", "egp40")),
        "tisch_x": mitte[0] / 1000, "tisch_y": mitte[1] / 1000, "tisch_z": h,
        "tisch_drehung": math.radians(-t["drehung_grad"]), "boden_z": h - 0.75,
        "tisch_kinder": kinder, "tisch_dicke": dicke, "tisch_dicke_halb": dicke / 2,
        "blick_drehung": _zahlen(_blick(ort, ziel), 4), "blick_ort": _zahlen(ort, 3), "sockel": sockel,
    })
    sensor_nr = 0
    for t in entwurf["teile"]:
        lage = lagen[t["id"]]
        if t.get("art") == "sensor":   # DEF-Name wie in ea_sensoren: SENSOR_<Reihenfolge>
            s = sensor_werte(t)
            text += ("DEF SENSOR_%d Sensorfeld {\n  translation %s\n  rotation %s\n  name %s\n  groesse %s\n}\n" % (
                sensor_nr, _zahlen([v / 1000 for v in lage.o]), _zahlen(achse_winkel(lage.r)),
                _vrml_text("sensor_" + t["id"]), _zahlen([v / 1000 for v in s["groesse_mm"]])))
            sensor_nr += 1
            continue
        if t.get("art") == "sperrzone":   # nur in worlds/<welt>.json ("grenzen"), nicht sichtbar in Webots
            continue
        m = merkmale_von(t["teil"])
        url = "%s/%s/netz.obj" % (teile_url, t["teil"])
        farbe = _zahlen(t.get("farbe") or ([0.1, 0.4, 0.9] if t.get("art") == "werkstueck" else [0.7, 0.7, 0.72]), 3)
        if t.get("art") == "werkstueck":
            # Werkstueck: Ursprung = Mitte des Huellquaders (so rechnen Physik und Greifer)
            box = m["box"]
            mitte = [(box[i] + box[i + 3]) / 2 for i in range(3)]
            groesse = [(box[i + 3] - box[i]) / 1000 for i in range(3)]
            masse = t.get("masse_kg") or round(m.get("volumen_mm3", 1000.0) * 1.24e-6, 4)   # PLA, falls nichts angegeben
            text += ("Werkstueck {\n  translation %s\n  rotation %s\n  name %s\n  form \"modell\"\n  modell [ %s ]\n"
                     "  skalierung 0.001\n  modellversatz %s\n  size %s\n  farbe %s\n  masse %.4f\n}\n" % (
                         _zahlen([v / 1000 for v in lage.punkt(mitte)]), _zahlen(achse_winkel(lage.r)),
                         _vrml_text(t["id"]), _vrml_text(url), _zahlen([-v / 1000 for v in mitte]),
                         _zahlen(groesse), farbe, max(0.001, float(masse))))
        else:
            text += ("Aufbau {\n  translation %s\n  rotation %s\n  name %s\n  modell [ %s ]\n  skalierung 0.001\n"
                     "  farbe %s\n}\n" % (_zahlen([v / 1000 for v in lage.o]), _zahlen(achse_winkel(lage.r)),
                                          _vrml_text(t["id"]), _vrml_text(url), farbe))
    return text


def zusammenfassung(entwurf, lagen):
    """Lage jedes Teils fuer die Anzeige: Ursprung in mm und Drehung."""
    return {t["id"]: {"ursprung_mm": [round(v, 2) for v in lagen[t["id"]].o],
                      "drehung": [round(v, 5) for v in achse_winkel(lagen[t["id"]].r)]}
            for t in entwurf["teile"]}


# --- Teil ersetzen: Verbindungspunkte im neu eingelesenen Modell wiederfinden ---
# gmsh vergibt die Nummern der Flaechen, Bohrungen, Kanten und Ecken bei jedem Einlesen neu. Gesucht wird
# deshalb ueber die Geometrie: dieselbe Ebene, Achse oder Gerade bzw. derselbe Punkt (bis 0,05 mm) und
# ein Koordinatensystem mit denselben Achsen; bei mehreren Treffern der naechste.

NAEHE_MM = 0.05
ART_NAMEN = {"flaeche": "Fläche", "bohrung": "Bohrung", "kante": "Kante", "ecke": "Ecke"}


def _abstand(a, b):
    return math.sqrt(_dot(_sub(a, b), _sub(a, b)))


def _parallel(a, b):
    return abs(_dot(_norm(a), _norm(b))) > 0.9999


def _von_gerade(p, a, richtung):
    """Abstand des Punkts p von der Geraden durch a in Richtung richtung."""
    d = _sub(p, a)
    r = _norm(richtung)
    return _abstand(d, _mul(r, _dot(d, r)))


def _gleiche_ebene(f, g):
    return (_dot(_norm(f["normale"]), _norm(g["normale"])) > 0.9999
            and abs(_dot(_norm(f["normale"]), _sub(g["mitte"], f["mitte"]))) < NAEHE_MM)


def _kandidaten(alt, neu, punkt):
    art = punkt.get("art")
    if art == "flaeche":
        f = _finde(alt["flaechen"], punkt.get("id"), "Flaeche")
        for g in neu.get("flaechen", []):
            if _gleiche_ebene(f, g):
                yield {"art": "flaeche", "id": g["id"]}
    elif art == "bohrung":
        b = _finde(alt["bohrungen"], punkt.get("id"), "Bohrung")
        achse = _sub(b["enden"][1], b["enden"][0])
        for c in neu.get("bohrungen", []):
            if (c.get("innen") == b.get("innen") and abs(float(c["durchmesser"]) - float(b["durchmesser"])) < 0.02
                    and _parallel(_sub(c["enden"][1], c["enden"][0]), achse)
                    and _von_gerade(c["enden"][0], b["enden"][0], achse) < NAEHE_MM):
                for ende in (0, 1):
                    yield {"art": "bohrung", "id": c["id"], "ende": ende}
    elif art in ("kante", "ecke"):
        liste = "kanten" if art == "kante" else "ecken"
        k = _finde(alt[liste], punkt.get("id"), ART_NAMEN[art])
        if art == "kante":
            f_id = punkt.get("flaeche", k["flaechen"][0])
        else:
            f_id = punkt.get("flaeche", k["flaechen"][0]["flaeche"])
        f = _finde(alt["flaechen"], f_id, "Flaeche")
        for c in neu.get(liste, []):
            if art == "kante":
                richtung = _sub(k["b"], k["a"])
                if not (_parallel(_sub(c["b"], c["a"]), richtung) and _von_gerade(c["a"], k["a"], richtung) < NAEHE_MM):
                    continue
                flaechen = c["flaechen"]
            else:
                if _abstand(c["punkt"], k["punkt"]) >= NAEHE_MM:
                    continue
                flaechen = [s["flaeche"] for s in c["flaechen"]]
            for g_id in flaechen:
                g = next((g for g in neu.get("flaechen", []) if str(g["id"]) == str(g_id)), None)
                if g is not None and _gleiche_ebene(f, g):
                    yield {"art": art, "id": c["id"], "flaeche": g_id}


def punkt_wiederfinden(alt, neu, punkt):
    """Verbindungspunkt aus dem alten Modell eines Teils im neuen suchen.
    Rueckgabe (neuer Punkt, Verschiebung in mm) oder (None, None)."""
    try:
        vorher = punkt_lage(alt, punkt)
    except (BaukastenFehler, KeyError, TypeError, ValueError):
        return None, None
    bestes = (None, None)
    for p in _kandidaten(alt, neu, punkt):
        try:
            nachher = punkt_lage(neu, p)
        except BaukastenFehler:
            continue
        if any(sum(vorher.r[z][s] * nachher.r[z][s] for z in range(3)) < 0.9999 for s in range(3)):
            continue   # anders ausgerichtet (z.B. die andere Oeffnung einer Bohrung)
        d = _abstand(vorher.o, nachher.o)
        if bestes[1] is None or d < bestes[1]:
            bestes = (p, d)
    return bestes


def teil_verweise(entwurf, name):
    """Verbindungspunkte eines Entwurfs, die auf dem Teil `name` liegen: [(teil im Entwurf, "punkt"|"an_punkt")]."""
    teile = {t.get("id"): t for t in entwurf.get("teile", [])}
    greifer = None
    if entwurf.get("art") == "backensatz":
        greifer = GREIFER_TEILE.get(entwurf.get("basis") or "egp25")
    ergebnis = []
    for t in entwurf.get("teile", []):
        v = t.get("verbindung") or {}
        if t.get("art") not in ("sensor", "sperrzone") and t.get("teil") == name and v.get("punkt"):
            ergebnis.append((t, "punkt"))
        ziel = teile.get(v.get("an"))
        if v.get("an_punkt") and ((ziel is not None and ziel.get("art") not in ("sensor", "sperrzone") and ziel.get("teil") == name)
                                  or (v.get("an") == "greifer" and greifer == name)):
            ergebnis.append((t, "an_punkt"))
    return ergebnis


def entwurf_umstellen(entwurf, name, alt, neu):
    """Kopie des Entwurfs mit den Verbindungspunkten am Teil `name` im neuen Modell `neu`.
    Rueckgabe (entwurf, offen, verschoben): offen = nicht wiedergefundene Punkte (im Entwurf geleert,
    der Baukasten zeigt das Teil dann als "offen"), verschoben = [(Text, mm)] fuer Punkte, die sich bewegt haben."""
    e = json.loads(json.dumps(entwurf))
    offen, verschoben = [], []
    for t, feld in teil_verweise(e, name):
        v = t["verbindung"]
        punkt = v[feld]
        text = "%s %s %s" % (t.get("id") if feld == "punkt" else "%s an %s" % (t.get("id"), v.get("an")),
                             ART_NAMEN.get(punkt.get("art"), punkt.get("art")), punkt.get("id"))
        neuer, d = punkt_wiederfinden(alt, neu, punkt)
        if neuer is None:
            v[feld] = None
            offen.append(text)
        else:
            v[feld] = neuer
            if d > NAEHE_MM:
                verschoben.append((text, round(d, 2)))
    return e, offen, verschoben


def teile_lader(teile_dir):
    """merkmale_von(name) mit Zwischenspeicher, liest teile/<name>/teil.json."""
    cache = {}

    def merkmale_von(name):
        if not re.fullmatch(r"[a-z0-9_-]{1,60}", str(name)):
            raise BaukastenFehler("Ungueltiger Teilename %r" % name)
        if name not in cache:
            try:
                with open(os.path.join(teile_dir, name, "teil.json"), encoding="utf-8") as f:
                    cache[name] = json.load(f)
            except (OSError, ValueError):
                raise BaukastenFehler("Das Teil %s gibt es nicht in der Teilebibliothek." % name)
        return cache[name]
    return merkmale_von
