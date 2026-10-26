"""Kollisionspruefung (Stufe K1): Roboter, Greifer und gegriffenes Teil gegen Tisch, Aufbauten und Werkstuecke.

Robotergelenke sind Kugelketten (aus den Kollisionsnetzen von iRC, tools/erzeuge_rebel_modell.py schreibt sie nach
protos/meshes/huellen.json), Greifer, Backen und gegriffenes Teil Quader. Die Umgebung wird als dichte Punktwolke auf
den echten Netzen abgetastet (einmal je Teil, raeumlich einsortiert); Kollision heisst: ein Punkt liegt in einem um
den Sicherheitsabstand vergroesserten Huellkoerper. Der Tisch ist eine Ebene. Nur Python-Standardbibliothek, ohne
Webots testbar. Alle Laengen in m.
"""

import math

ABTASTUNG = 0.003     # Abstand der Punkte auf den Netzen der Umgebung
ZELLE = 0.02          # Kantenlaenge der Rasterzellen zum Einsortieren der Punkte
STANDARD = {"an": True, "reaktion": "stopp", "abstand_mm": 2.0, "ablage_mm": 2.0}


def _sub(a, b):
    return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]


def _add(a, b):
    return [a[0] + b[0], a[1] + b[1], a[2] + b[2]]


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


def zylinder_punkte(r, z0, z1, abstand=ABTASTUNG):
    """Oberflaechenpunkte eines Zylinders entlang z (Mantel und beide Deckel), Radius r, von z0 bis z1."""
    erg = []
    um = max(8, int(math.ceil(2 * math.pi * r / abstand)))
    hoehe = max(1, int(math.ceil((z1 - z0) / abstand)))
    for i in range(um):
        c, s = math.cos(2 * math.pi * i / um), math.sin(2 * math.pi * i / um)
        erg += [(r * c, r * s, z0 + (z1 - z0) * j / hoehe) for j in range(hoehe + 1)]
        for k in range(1, int(math.ceil(r / abstand))):   # Deckel: Ringe nach innen
            erg += [(r * c * k / math.ceil(r / abstand), r * s * k / math.ceil(r / abstand), z) for z in (z0, z1)]
    return erg + [(0.0, 0.0, z0), (0.0, 0.0, z1)]


def naeherungssensor_punkte(durchmesser, laenge):
    """Naeherungssensor wie in protos/Naeherungssensor.proto: Gewindehuelse von z = 0 bis zur aktiven Flaeche
    (z = laenge) und die Kontermutter (Eckmass 1,45 x Radius, 4 mm) bei 55 % der Laenge."""
    r, mitte = durchmesser / 2, laenge * 0.55
    return zylinder_punkte(r, 0.0, laenge) + zylinder_punkte(r * 1.45, mitte - 0.002, mitte + 0.002)


class Punktwolke:
    """Punkte eines Teils im eigenen System, in Rasterzellen einsortiert; Abfrage mit einem Quader im selben System."""

    def __init__(self, punkte, zelle=ZELLE, duenn=ABTASTUNG / 2):
        # fein vernetzte Stellen (Rundungen) liefern sehr viele Punkte: hoechstens einer je Wuerfel `duenn`
        if duenn:
            punkte = list({tuple(int(math.floor(p[i] / duenn)) for i in range(3)): p for p in punkte}.values())
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

    def im_system(self, pos, rot, rand):
        """(lo, hi, enthaelt) fuer Punkte im System (pos, rot) eines Umgebungsteils: eine Umrechnung je Teil
        statt einer je Punkt."""
        c = _mtv(rot, _sub(self.mitte, pos))
        r = self.r + rand
        r2 = r * r

        def enthaelt(p):
            dx, dy, dz = p[0] - c[0], p[1] - c[1], p[2] - c[2]
            return dx * dx + dy * dy + dz * dz <= r2
        return [c[0] - r, c[1] - r, c[2] - r], [c[0] + r, c[1] + r, c[2] + r], enthaelt


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

    def im_system(self, pos, rot, rand):
        # Drehung Quader -> Teil und Quadermitte im Teilsystem
        rq = [[self.rot[i * 3 + j] for j in range(3)] for i in range(3)] if len(self.rot) == 9 else self.rot
        rt = [[rot[i * 3 + j] for j in range(3)] for i in range(3)] if len(rot) == 9 else rot
        rel = [[sum(rt[k][i] * rq[k][j] for k in range(3)) for j in range(3)] for i in range(3)]   # rt^T * rq
        c = _mtv(rot, _sub(self.mitte_welt, pos))
        h = [self.halb[i] + rand for i in range(3)]
        ecken = [[c[i] + sum(rel[i][j] * (sx, sy, sz)[j] * h[j] for j in range(3)) for i in range(3)]
                 for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]

        def enthaelt(p):
            d = (p[0] - c[0], p[1] - c[1], p[2] - c[2])
            for j in range(3):   # Koordinate j im Quadersystem = Spalte j von rel mal d
                if abs(rel[0][j] * d[0] + rel[1][j] * d[1] + rel[2][j] * d[2]) > h[j]:
                    return False
            return True
        return [min(q[i] for q in ecken) for i in range(3)], [max(q[i] for q in ecken) for i in range(3)], enthaelt


class Saeulen:
    """Rauminhalt eines geschlossenen Netzes (Teilsystem) als senkrechte Saeulen: je Rasterpunkt (x, y) die
    z-Bereiche, die innen liegen (Strahl in z-Richtung, Schnittpunkte paarweise). Einmal je Teil gerechnet; danach
    ist "liegt dieser Punkt mindestens `tiefe` tief im Teil?" eine Nachschau statt einer Rechnung mit Dreiecken."""

    def __init__(self, punkte, dreiecke, schritt=0.0005, hoechstens=200):
        self.lo = [min(p[i] for p in punkte) for i in range(3)]
        self.hi = [max(p[i] for p in punkte) for i in range(3)]
        groesse = max(self.hi[0] - self.lo[0], self.hi[1] - self.lo[1])
        self.s = max(schritt, groesse / hoechstens)
        self.nx = int(math.ceil((self.hi[0] - self.lo[0]) / self.s)) + 1
        self.ny = int(math.ceil((self.hi[1] - self.lo[1]) / self.s)) + 1
        # Strahlen knapp neben dem Raster, damit sie nie genau eine Kante zweier Dreiecke treffen
        self.dx, self.dy = 1.37e-7, 2.91e-7
        schnitte = {}
        for ia, ib, ic in dreiecke:
            a, b, c = punkte[ia], punkte[ib], punkte[ic]
            det = (b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])
            if abs(det) < 1e-18:   # senkrecht stehend: trifft kein senkrechter Strahl
                continue
            i0 = max(0, int(math.ceil((min(a[0], b[0], c[0]) - self.lo[0] - self.dx) / self.s)))
            i1 = min(self.nx - 1, int(math.floor((max(a[0], b[0], c[0]) - self.lo[0] - self.dx) / self.s)))
            j0 = max(0, int(math.ceil((min(a[1], b[1], c[1]) - self.lo[1] - self.dy) / self.s)))
            j1 = min(self.ny - 1, int(math.floor((max(a[1], b[1], c[1]) - self.lo[1] - self.dy) / self.s)))
            for i in range(i0, i1 + 1):
                x = self.lo[0] + i * self.s + self.dx
                for j in range(j0, j1 + 1):
                    y = self.lo[1] + j * self.s + self.dy
                    u = ((x - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (y - a[1])) / det
                    v = ((b[0] - a[0]) * (y - a[1]) - (x - a[0]) * (b[1] - a[1])) / det
                    if u < 0 or v < 0 or u + v > 1:
                        continue
                    schnitte.setdefault((i, j), []).append(a[2] + u * (b[2] - a[2]) + v * (c[2] - a[2]))
        self.saeulen = {}
        for ij, zs in schnitte.items():
            zs.sort()
            self.saeulen[ij] = [(zs[k], zs[k + 1]) for k in range(0, len(zs) - 1, 2)]   # ungerade: Netz offen, Rest weg

    def _in_saeule(self, x, y, z, tiefe):
        i = int(round((x - self.lo[0] - self.dx) / self.s))
        j = int(round((y - self.lo[1] - self.dy) / self.s))
        return any(z0 + tiefe <= z <= z1 - tiefe for z0, z1 in self.saeulen.get((i, j), ()))

    def innen(self, q, tiefe=0.0):
        """Liegt q (Teilsystem) mindestens `tiefe` tief im Teil? Seitlich genaehert: auch die Saeulen um `tiefe`
        daneben muessen q enthalten."""
        if not self._in_saeule(q[0], q[1], q[2], tiefe):
            return False
        if tiefe <= 0:
            return True
        return all(self._in_saeule(q[0] + ex, q[1] + ey, q[2], 0.0)
                   for ex, ey in ((tiefe, 0.0), (-tiefe, 0.0), (0.0, tiefe), (0.0, -tiefe)))


class Formkoerper:
    """Gegriffenes Teil mit seiner echten Form (Saeulen); ein Quader um ein rundes Teil ragte mit den Ecken in
    passende Magazintaschen. rand < 0 heisst: erst ab so viel Eindringen ist es eine Kollision."""

    def __init__(self, name, lage, form, ecken):
        self.name, self.pos, self.rot, self.form = name, lage[0], lage[1], form
        mitte = [(form.lo[i] + form.hi[i]) / 2 for i in range(3)]
        self.mitte_welt = _welt(lage, mitte)
        self.halb = [(form.hi[i] - form.lo[i]) / 2 for i in range(3)]
        self.ecken_lokal = ecken   # einige Eckpunkte des Netzes fuer die Tischpruefung

    def kasten(self, rand):
        e = [_welt((self.mitte_welt, self.rot), [sx * self.halb[0], sy * self.halb[1], sz * self.halb[2]])
             for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]
        return ([min(q[i] for q in e) - rand for i in range(3)], [max(q[i] for q in e) + rand for i in range(3)])

    def tiefster(self):
        return min(_welt((self.pos, self.rot), p)[2] for p in self.ecken_lokal)

    def im_system(self, pos, rot, rand):
        lo, hi = self.kasten(max(rand, 0.0))
        e = [_mtv(rot, _sub([(lo, hi)[sx][0], (lo, hi)[sy][1], (lo, hi)[sz][2]], pos))
             for sx in (0, 1) for sy in (0, 1) for sz in (0, 1)]
        tiefe = -rand
        form, eigene_pos, eigene_rot = self.form, self.pos, self.rot

        def enthaelt(p):
            return form.innen(_mtv(eigene_rot, _sub(_welt((pos, rot), p), eigene_pos)), tiefe)
        return [min(q[i] for q in e) for i in range(3)], [max(q[i] for q in e) for i in range(3)], enthaelt


def eckpunkte(punkte, hoechstens=400):
    """Hoechstens so viele Netzpunkte, gleichmaessig ausgewaehlt, dazu die aeussersten je Achse (Tischpruefung)."""
    schritt = max(1, len(punkte) // hoechstens)
    aussen = [f(punkte, key=lambda p, i=i: p[i]) for i in range(3) for f in (min, max)]
    return list(punkte[::schritt]) + aussen


# ---------------------------------------------------------------- Pruefung

class Umgebungsteil:
    """Aufbau oder Werkstueck: Punktwolke im eigenen System und aktuelle Lage (pos, rot)."""

    def __init__(self, name, art, wolke, lage, kennung=None):
        self.name, self.art, self.wolke, self.lage, self.kennung = name, art, wolke, lage, kennung


def pruefe(koerper, umgebung, tisch_z, rand, ablage, ausnahmen=frozenset(), tisch_frei=frozenset(), hoechstens=3,
           rand_paare=None):
    """Kollisionen der Huellkoerper `koerper` (Kugel/Quader, Attribut name; gegriffenes Teil mit name endend auf
    '(gegriffen)') mit dem Tisch (Ebene tisch_z, None = kein Tisch) und den Umgebungsteilen.
    rand: Sicherheitsabstand; fuer das gegriffene Teil gilt stattdessen -ablage (es darf so tief eindruecken).
    ausnahmen: Paare (koerpername, kennung des Umgebungsteils), die nicht zaehlen; tisch_frei: Koerper ohne Tischpruefung;
    rand_paare: {(koerpername, kennung): rand} fuer einzelne Paare (z.B. Greifer und das Teil, das er gerade greift).
    Rueckgabe: Liste {a, b, punkt} (hoechstens `hoechstens`)."""
    treffer = []
    # Gruppen (alle Kugeln eines Glieds): eine Huellkugel je Gruppe, damit ganze Glieder fern eines Teils mit einer
    # einzigen Rechnung entfallen
    gruppen = {}
    for k in koerper:
        gruppen.setdefault(k.name, []).append(k)
    huelle = {}
    for name, liste in gruppen.items():
        mitten = [k.mitte if isinstance(k, Kugel) else k.mitte_welt for k in liste]
        m = [sum(p[i] for p in mitten) / len(mitten) for i in range(3)]
        huelle[name] = (m, max(math.dist(m, c) + (k.r if isinstance(k, Kugel) else math.sqrt(_dot(k.halb, k.halb)))
                               for c, k in zip(mitten, liste)))
    rand_max = max([rand] + list((rand_paare or {}).values()))
    in_reichweite = {}
    for name, (m, rg) in huelle.items():
        liste = []
        for teil in umgebung:
            pos, rot = teil.lage
            c = _mtv(rot, _sub(m, pos))
            w = teil.wolke
            if all(w.lo[i] - rg - rand_max <= c[i] <= w.hi[i] + rg + rand_max for i in range(3)):
                liste.append(teil)
        in_reichweite[name] = liste
    for k in koerper:
        r = -ablage if k.name.endswith("(gegriffen)") else rand
        if tisch_z is not None and k.name not in tisch_frei and k.tiefster() < tisch_z + r:
            p = k.mitte if isinstance(k, Kugel) else k.mitte_welt
            treffer.append({"a": k.name, "b": "Tisch", "punkt": [p[0], p[1], tisch_z]})
            if len(treffer) >= hoechstens:
                return treffer
        lo0, hi0 = k.kasten(r)
        for teil in in_reichweite[k.name]:
            if (k.name, teil.kennung) in ausnahmen:
                continue
            rt = (rand_paare or {}).get((k.name, teil.kennung), r)
            lo, hi = (lo0, hi0) if rt == r else k.kasten(rt)
            pos, rot = teil.lage
            # grob: Abstand der Mitten gegen die Ausdehnungen
            m = [(lo[i] + hi[i]) / 2 for i in range(3)]
            h = max((hi[i] - lo[i]) / 2 for i in range(3)) * math.sqrt(3)
            if math.dist(m, pos) > h + teil.wolke.radius + 0.001:
                continue
            llo, lhi, enthaelt = k.im_system(pos, rot, rt)
            for q in teil.wolke.in_box(llo, lhi):
                if enthaelt(q):
                    treffer.append({"a": k.name, "b": teil.name, "punkt": _welt(teil.lage, q)})
                    break
            if len(treffer) >= hoechstens:
                return treffer
    return treffer


# ---------------------------------------------------------------- Stufe K3: Selbstkollision, Werkstueck gegen Werkstueck

# Innerhalb der Achsgrenzen des ReBeL kommen sich die Glieder 2 bis 6 untereinander und Greifer/Ober-/Unterarm nie
# nahe (nachgerechnet 25.10.2026). Echte Selbstkollisionen gibt es nur, wenn der Arm ueber Fuss oder Drehteller
# abknickt, und mit Greifer bzw. gegriffenem Teil gegen den Arm. Geprueft werden die Kugeln eines Glieds gegen die
# echte Oberflaeche des anderen; die Kugeln sind bis gut 1 cm groesser als das Glied, daher erst ab SELBST_TIEFE.
SELBST_FLAECHEN = (0, 1, 2, 3, 4)   # Glieder, deren Oberflaeche geprueft wird (0 = Fuss)
SELBST_TIEFE = 0.010


def selbst_erlaubt(glied, flaeche):
    """Pruefen wir Glied `glied` (1..6, None = Greifer/Backen/gegriffenes Teil) gegen die Oberflaeche von
    Glied `flaeche`? Nachbarn und Paare, die sich nie nahe kommen, entfallen."""
    if glied is None:
        return flaeche in SELBST_FLAECHEN
    return flaeche in (0, 1) and glied >= flaeche + 2


FALLEN_MS = 0.45    # schneller nach unten = aus mehr als etwa 1 cm Hoehe gefallen
SCHIEBEN_MS = 0.03  # schneller waagerecht = wird geschoben (ruhende Teile zittern deutlich langsamer)


def bewegt(geschwindigkeit):
    """Faellt oder rutscht ein Werkstueck? geschwindigkeit: [vx, vy, vz, ...] in m/s (Webots getVelocity)."""
    vx, vy, vz = geschwindigkeit[:3]
    return vz < -FALLEN_MS or math.hypot(vx, vy) > SCHIEBEN_MS


# ---------------------------------------------------------------- Stufe K4: Arbeitsraum, Sperrzonen, Achsgrenzen

def _zeilen(rot):
    """Drehung als 3 Zeilen (Webots liefert 9 Zahlen zeilenweise)."""
    return [list(rot[0:3]), list(rot[3:6]), list(rot[6:9])] if len(rot) == 9 else [list(z) for z in rot]


def _mm_mal(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def obb_schneiden(c1, r1, h1, c2, r2, h2):
    """Ueberschneiden sich zwei gedrehte Quader (Mitte, Drehung als Zeilen, halbe Kanten)? Trennende Achsen (SAT)."""
    a = [[r1[0][i], r1[1][i], r1[2][i]] for i in range(3)]   # Achsen = Spalten der Drehung
    b = [[r2[0][i], r2[1][i], r2[2][i]] for i in range(3)]
    t = _sub(c2, c1)
    achsen = a + b + [[x[1] * y[2] - x[2] * y[1], x[2] * y[0] - x[0] * y[2], x[0] * y[1] - x[1] * y[0]] for x in a for y in b]
    for n in achsen:
        if _dot(n, n) < 1e-12:   # parallele Kanten
            continue
        ra = sum(h1[i] * abs(_dot(a[i], n)) for i in range(3))
        rb = sum(h2[i] * abs(_dot(b[i], n)) for i in range(3))
        if abs(_dot(t, n)) > ra + rb:
            return False
    return True


def grenzen_einstellungen(daten):
    """"grenzen" aus worlds/<welt>.json (schreibt tools/baukasten.grenzen_fuer_welt), fehlerhafte Teile entfallen."""
    e = {"arbeitsraum": None, "sperrzonen": [], "achsen": None, "achsen_reaktion": "stopp"}
    if not isinstance(daten, dict):
        return e
    reaktion = lambda w: w if w in ("warnung", "stopp") else "stopp"   # noqa: E731
    a = daten.get("arbeitsraum")
    try:
        if isinstance(a, dict):
            e["arbeitsraum"] = {"min_mm": [float(v) for v in a["min_mm"]][:3], "max_mm": [float(v) for v in a["max_mm"]][:3],
                                "reaktion": reaktion(a.get("reaktion"))}
    except (KeyError, TypeError, ValueError):
        pass
    for z in daten.get("sperrzonen") or []:
        try:
            e["sperrzonen"].append({"name": str(z.get("name") or "Sperrzone")[:24], "reaktion": reaktion(z.get("reaktion")),
                                    "mitte_mm": [float(v) for v in z["mitte_mm"]][:3],
                                    "drehung": [[float(v) for v in zeile][:3] for zeile in z["drehung"]][:3],
                                    "halb_mm": [float(v) for v in z["halb_mm"]][:3]})
        except (AttributeError, KeyError, TypeError, ValueError):
            pass
    achsen = daten.get("achsen")
    if isinstance(achsen, list) and len(achsen) == 6:
        try:
            e["achsen"] = [[float(p[0]), float(p[1])] for p in achsen]
        except (TypeError, ValueError, IndexError):
            pass
    e["achsen_reaktion"] = reaktion(daten.get("achsen_reaktion"))
    return e


def _ecken_welt(k):
    """Acht Ecken eines Quaders bzw. des Huellquaders eines Formkoerpers (Welt)."""
    return [_welt((k.mitte_welt, k.rot), [sx * k.halb[0], sy * k.halb[1], sz * k.halb[2]])
            for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]


class Grenzen:
    """Wegbegrenzungen einer Welt pruefen. Koordinaten der Grenzen: iRC-Basis (mm), Pruefung in der Welt (m)."""

    def __init__(self, daten):
        self.e = grenzen_einstellungen(daten)

    @property
    def an(self):
        return bool(self.e["arbeitsraum"] or self.e["sperrzonen"] or self.e["achsen"])

    @property
    def stoppt(self):
        """Haelt mindestens eine Grenze das Programm an?"""
        return any(r == "stopp" for r in ([self.e["arbeitsraum"]["reaktion"]] if self.e["arbeitsraum"] else [])
                   + [z["reaktion"] for z in self.e["sperrzonen"]] + ([self.e["achsen_reaktion"]] if self.e["achsen"] else []))

    def pruefe(self, hand, basis, gelenke_grad=None, tcp=None):
        """hand: Koerper (Quader/Formkoerper, Welt), basis: (Position, Drehung) des Roboterfusses in der Welt,
        gelenke_grad: A1..A6 aus iRC, tcp: Weltpunkt fuer die Anzeige. Rueckgabe: [{a, b, punkt, text, reaktion}]."""
        o, rb = basis[0], _zeilen(basis[1])
        rbt = [[rb[j][i] for j in range(3)] for i in range(3)]
        in_basis = lambda p: [v * 1000.0 for v in _mv(rbt, _sub(p, o))]   # noqa: E731
        treffer = []
        a = self.e["arbeitsraum"]
        if a:
            schlimmst = None   # (wie weit draussen in mm, Koerper, Ecke, Achse, Wert)
            for k in hand:
                for p in _ecken_welt(k):
                    q = in_basis(p)
                    for i in range(3):
                        raus = max(a["min_mm"][i] - q[i], q[i] - a["max_mm"][i])
                        if raus > 0 and (schlimmst is None or raus > schlimmst[0]):
                            schlimmst = (raus, k.name, p, i, q[i])
            if schlimmst:
                raus, name, p, i, wert = schlimmst
                grenze = a["min_mm"][i] if wert < a["min_mm"][i] else a["max_mm"][i]
                treffer.append({"a": name, "b": "Arbeitsraum", "punkt": p, "reaktion": a["reaktion"],
                                "text": "%s außerhalb des Arbeitsraums (%s = %.0f mm, Grenze %.0f mm)"
                                        % (name, "xyz"[i], wert, grenze)})
        for z in self.e["sperrzonen"]:
            c = _add(o, _mv(rb, [v / 1000.0 for v in z["mitte_mm"]]))
            r = _mm_mal(rb, z["drehung"])
            h = [v / 1000.0 for v in z["halb_mm"]]
            for k in hand:
                if obb_schneiden(k.mitte_welt, _zeilen(k.rot), k.halb, c, r, h):
                    treffer.append({"a": k.name, "b": "Sperrzone „%s“" % z["name"], "punkt": k.mitte_welt,
                                    "reaktion": z["reaktion"], "text": "%s in der Sperrzone „%s“" % (k.name, z["name"])})
                    break
        if self.e["achsen"] and gelenke_grad:
            for i, (q, (lo, hi)) in enumerate(zip(gelenke_grad, self.e["achsen"])):
                if q < lo - 0.05 or q > hi + 0.05:
                    treffer.append({"a": "A%d" % (i + 1), "b": "Achsgrenze A%d" % (i + 1), "punkt": tcp or o,
                                    "reaktion": self.e["achsen_reaktion"],
                                    "text": "A%d = %.0f° (erlaubt %.0f° bis %.0f°)" % (i + 1, q, lo, hi)})
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
