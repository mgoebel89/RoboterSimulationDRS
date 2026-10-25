"""Kollisionspruefung (Stufe K1): Roboter, Greifer und gegriffenes Teil gegen Tisch, Aufbauten und Werkstuecke.

Robotergelenke sind Kugelketten (aus den Kollisionsnetzen von iRC, tools/erzeuge_rebel_modell.py schreibt sie nach
protos/meshes/huellen.json), Greifer, Backen und gegriffenes Teil Quader. Die Umgebung wird als dichte Punktwolke auf
den echten Netzen abgetastet (einmal je Teil, raeumlich einsortiert); Kollision heisst: ein Punkt liegt in einem um
den Sicherheitsabstand vergroesserten Huellkoerper. Der Tisch ist eine Ebene. Nur Python-Standardbibliothek, ohne
Webots testbar. Alle Laengen in m.
"""

import math

ABTASTUNG = 0.002     # Abstand der Punkte auf den Netzen der Umgebung
ZELLE = 0.02          # Kantenlaenge der Rasterzellen zum Einsortieren der Punkte
STANDARD = {"an": True, "reaktion": "stopp", "abstand_mm": 2.0, "ablage_mm": 2.0}


def _sub(a, b):
    return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _mv(m, v):   # Matrix (Liste von 9 oder 3x3) mal Vektor
    if len(m) == 9:
        return [m[0] * v[0] + m[1] * v[1] + m[2] * v[2], m[3] * v[0] + m[4] * v[1] + m[5] * v[2],
                m[6] * v[0] + m[7] * v[1] + m[8] * v[2]]
    return [_dot(m[0], v), _dot(m[1], v), _dot(m[2], v)]


def _mtv(m, v):   # transponierte Matrix mal Vektor (Welt -> lokal)
    if len(m) == 9:
        return [m[0] * v[0] + m[3] * v[1] + m[6] * v[2], m[1] * v[0] + m[4] * v[1] + m[7] * v[2],
                m[2] * v[0] + m[5] * v[1] + m[8] * v[2]]
    return [m[0][0] * v[0] + m[1][0] * v[1] + m[2][0] * v[2], m[0][1] * v[0] + m[1][1] * v[1] + m[2][1] * v[2],
            m[0][2] * v[0] + m[1][2] * v[1] + m[2][2] * v[2]]


def _welt(lage, p):
    pos, rot = lage
    q = _mv(rot, p)
    return [q[0] + pos[0], q[1] + pos[1], q[2] + pos[2]]


# ---------------------------------------------------------------- Huellen erzeugen (Generator)

def hauptachse(punkte):
    """Richtung der groessten Ausdehnung (Hauptkomponente, Potenzmethode)."""
    n = len(punkte)
    c = [sum(p[i] for p in punkte) / n for i in range(3)]
    k = [[sum((p[i] - c[i]) * (p[j] - c[j]) for p in punkte) / n for j in range(3)] for i in range(3)]
    a = [1.0, 0.7, 0.3]
    for _ in range(60):
        b = [_dot(k[i], a) for i in range(3)]
        laenge = math.sqrt(_dot(b, b))
        if laenge < 1e-15:
            return c, [0.0, 0.0, 1.0]
        a = [v / laenge for v in b]
    return c, a


def kugelkette(punkte, scheibe=0.03):
    """Kugeln [x, y, z, r], die alle Punkte enthalten: Scheiben quer zur Hauptachse, je Scheibe eine Kugel um den
    Schwerpunkt der Scheibe mit dem Abstand zum entferntesten Punkt als Radius."""
    if not punkte:
        return []
    c, a = hauptachse(punkte)
    t = [_dot(_sub(p, c), a) for p in punkte]
    t0, t1 = min(t), max(t)
    anzahl = max(1, int(math.ceil((t1 - t0) / scheibe - 1e-9)))
    breite = (t1 - t0) / anzahl if anzahl else 1.0
    kugeln = []
    for k in range(anzahl):
        lo, hi = t0 + k * breite - 1e-9, t0 + (k + 1) * breite + 1e-9
        teil = [p for p, ti in zip(punkte, t) if lo <= ti <= hi]
        if not teil:
            continue
        m = [sum(p[i] for p in teil) / len(teil) for i in range(3)]
        r = max(math.sqrt(_dot(_sub(p, m), _sub(p, m))) for p in teil)
        kugeln.append([round(m[0], 5), round(m[1], 5), round(m[2], 5), round(r + 0.0005, 5)])
    return kugeln


def kugelraster(punkte, zelle):
    """Kugeln [x, y, z, r], die alle Punkte enthalten: Punkte in Wuerfel der Kantenlaenge `zelle` einsortiert,
    je Wuerfel eine Kugel um den Schwerpunkt mit dem Abstand zum entferntesten Punkt. Liegt hoechstens etwa eine
    halbe Wuerfeldiagonale ueber der Oberflaeche (bei 15 mm also rund 13 mm)."""
    zellen = {}
    for q in punkte:
        zellen.setdefault(tuple(int(math.floor(q[i] / zelle)) for i in range(3)), []).append(q)
    kugeln = []
    for zahl, teil in zellen.items():
        # Mitte: Schwerpunkt der Punkte oder Wuerfelmitte, je nachdem, was die kleinere Kugel ergibt
        beste = None
        for m in ([sum(q[i] for q in teil) / len(teil) for i in range(3)], [(zahl[i] + 0.5) * zelle for i in range(3)]):
            r = max(math.sqrt(_dot(_sub(q, m), _sub(q, m))) for q in teil)
            if beste is None or r < beste[1]:
                beste = (m, r)
        m, r = beste
        kugeln.append([round(m[0], 5), round(m[1], 5), round(m[2], 5), round(r + 0.0005, 5)])
    return kugeln


def quader_um(punkte):
    """Achsparalleler Huellquader {mitte, halb} im System der Punkte."""
    lo = [min(p[i] for p in punkte) for i in range(3)]
    hi = [max(p[i] for p in punkte) for i in range(3)]
    return {"mitte": [round((lo[i] + hi[i]) / 2, 5) for i in range(3)], "halb": [round((hi[i] - lo[i]) / 2, 5) for i in range(3)]}


# ---------------------------------------------------------------- Umgebung abtasten

def abtasten(punkte, dreiecke, abstand=ABTASTUNG):
    """Punkte auf allen Dreiecken im Raster `abstand` (baryzentrisch), Ecken eingeschlossen."""
    erg = []
    for ia, ib, ic in dreiecke:
        a, b, c = punkte[ia], punkte[ib], punkte[ic]
        kante = max(math.dist(a, b), math.dist(b, c), math.dist(c, a))
        n = max(1, int(math.ceil(kante / abstand)))
        for i in range(n + 1):
            for j in range(n + 1 - i):
                u, v = i / n, j / n
                w = 1.0 - u - v
                erg.append((a[0] * w + b[0] * u + c[0] * v, a[1] * w + b[1] * u + c[1] * v, a[2] * w + b[2] * u + c[2] * v))
    return erg


def quader_punkte(halb, abstand=ABTASTUNG * 2):
    """Oberflaechenpunkte eines Quaders um den Ursprung (fuer Werkstuecke ohne Netz)."""
    ecken = [[sx * halb[0], sy * halb[1], sz * halb[2]] for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]
    flaechen = [(0, 1, 3, 2), (4, 5, 7, 6), (0, 1, 5, 4), (2, 3, 7, 6), (0, 2, 6, 4), (1, 3, 7, 5)]
    dreiecke = [d for a, b, c, e in flaechen for d in ((a, b, c), (a, c, e))]
    return abtasten(ecken, dreiecke, abstand)


class Punktwolke:
    """Punkte eines Teils im eigenen System, in Rasterzellen einsortiert; Abfrage mit einem Quader im selben System."""

    def __init__(self, punkte, zelle=ZELLE):
        self.zelle = zelle
        self.zellen = {}
        for p in punkte:
            self.zellen.setdefault(tuple(int(math.floor(p[i] / zelle)) for i in range(3)), []).append(p)
        self.anzahl = len(punkte)
        if punkte:
            self.lo = [min(p[i] for p in punkte) for i in range(3)]
            self.hi = [max(p[i] for p in punkte) for i in range(3)]
        else:
            self.lo = self.hi = [0.0, 0.0, 0.0]
        self.radius = max((math.sqrt(_dot(p, p)) for p in punkte), default=0.0)

    def in_box(self, lo, hi):
        """Alle Punkte im achsparallelen Kasten lo..hi."""
        if any(lo[i] > self.hi[i] or hi[i] < self.lo[i] for i in range(3)):
            return
        z0 = [int(math.floor(lo[i] / self.zelle)) for i in range(3)]
        z1 = [int(math.floor(hi[i] / self.zelle)) for i in range(3)]
        if (z1[0] - z0[0] + 1) * (z1[1] - z0[1] + 1) * (z1[2] - z0[2] + 1) > 4000:   # riesiger Kasten: alles pruefen
            for liste in self.zellen.values():
                for p in liste:
                    if all(lo[i] <= p[i] <= hi[i] for i in range(3)):
                        yield p
            return
        for x in range(z0[0], z1[0] + 1):
            for y in range(z0[1], z1[1] + 1):
                for z in range(z0[2], z1[2] + 1):
                    for p in self.zellen.get((x, y, z), ()):
                        if lo[0] <= p[0] <= hi[0] and lo[1] <= p[1] <= hi[1] and lo[2] <= p[2] <= hi[2]:
                            yield p


# ---------------------------------------------------------------- Huellkoerper des Roboters in Weltlage

class Kugel:
    def __init__(self, name, mitte, r):
        self.name, self.mitte, self.r = name, mitte, r

    def kasten(self, rand):
        r = self.r + rand
        return [self.mitte[i] - r for i in range(3)], [self.mitte[i] + r for i in range(3)]

    def enthaelt(self, p, rand):
        d = _sub(p, self.mitte)
        return _dot(d, d) <= (self.r + rand) ** 2

    def tiefster(self):
        return self.mitte[2] - self.r


class Quader:
    """Quader mit Mitte und halben Kantenlaengen in einem System (pos, rot) der Welt."""

    def __init__(self, name, lage, mitte, halb):
        self.name, self.pos, self.rot = name, lage[0], lage[1]
        self.mitte_welt = _welt(lage, mitte)
        self.halb = halb

    def _lokal(self, p):
        return _mtv(self.rot, _sub(p, self.mitte_welt))

    def ecken(self):
        return [_welt((self.mitte_welt, self.rot), [sx * self.halb[0], sy * self.halb[1], sz * self.halb[2]])
                for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]

    def kasten(self, rand):
        e = self.ecken()
        return ([min(q[i] for q in e) - rand for i in range(3)], [max(q[i] for q in e) + rand for i in range(3)])

    def enthaelt(self, p, rand):
        q = self._lokal(p)
        return all(abs(q[i]) <= self.halb[i] + rand for i in range(3))

    def tiefster(self):
        return min(q[2] for q in self.ecken())


# ---------------------------------------------------------------- Pruefung

class Umgebungsteil:
    """Aufbau oder Werkstueck: Punktwolke im eigenen System und aktuelle Lage (pos, rot)."""

    def __init__(self, name, art, wolke, lage, kennung=None):
        self.name, self.art, self.wolke, self.lage, self.kennung = name, art, wolke, lage, kennung


def pruefe(koerper, umgebung, tisch_z, rand, ablage, ausnahmen=frozenset(), tisch_frei=frozenset(), hoechstens=3):
    """Kollisionen der Huellkoerper `koerper` (Kugel/Quader, Attribut name; gegriffenes Teil mit name endend auf
    '(gegriffen)') mit dem Tisch (Ebene tisch_z, None = kein Tisch) und den Umgebungsteilen.
    rand: Sicherheitsabstand; fuer das gegriffene Teil gilt stattdessen -ablage (es darf so tief eindruecken).
    ausnahmen: Paare (koerpername, kennung des Umgebungsteils), die nicht zaehlen; tisch_frei: Koerper ohne Tischpruefung.
    Rueckgabe: Liste {a, b, punkt} (hoechstens `hoechstens`)."""
    treffer = []
    for k in koerper:
        r = -ablage if k.name.endswith("(gegriffen)") else rand
        if tisch_z is not None and k.name not in tisch_frei and k.tiefster() < tisch_z + r:
            p = k.mitte if isinstance(k, Kugel) else k.mitte_welt
            treffer.append({"a": k.name, "b": "Tisch", "punkt": [p[0], p[1], tisch_z]})
            if len(treffer) >= hoechstens:
                return treffer
        lo, hi = k.kasten(r)
        for teil in umgebung:
            if (k.name, teil.kennung) in ausnahmen:
                continue
            pos, rot = teil.lage
            # grob: Abstand der Mitten gegen die Ausdehnungen
            m = [(lo[i] + hi[i]) / 2 for i in range(3)]
            h = max((hi[i] - lo[i]) / 2 for i in range(3)) * math.sqrt(3)
            if math.dist(m, pos) > h + teil.wolke.radius + 0.001:
                continue
            # Kasten des Koerpers ins System des Teils (achsparallel umschlossen)
            ecken = [_mtv(rot, _sub([x, y, z], pos)) for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]
            llo = [min(q[i] for q in ecken) for i in range(3)]
            lhi = [max(q[i] for q in ecken) for i in range(3)]
            for q in teil.wolke.in_box(llo, lhi):
                pw = _welt(teil.lage, q)
                if k.enthaelt(pw, r):
                    treffer.append({"a": k.name, "b": teil.name, "punkt": pw})
                    break
            if len(treffer) >= hoechstens:
                return treffer
    return treffer


def einstellungen(daten):
    """Kollisions-Einstellungen einer Welt (worlds/<welt>.json, "kollision"), geprueft und mit Standardwerten."""
    e = dict(STANDARD)
    if isinstance(daten, dict):
        if isinstance(daten.get("an"), bool):
            e["an"] = daten["an"]
        if daten.get("reaktion") in ("melden", "stopp"):
            e["reaktion"] = daten["reaktion"]
        for k, hoch in (("abstand_mm", 20.0), ("ablage_mm", 20.0)):
            try:
                e[k] = min(hoch, max(0.0, float(daten.get(k, e[k]))))
            except (TypeError, ValueError):
                pass
    return e
