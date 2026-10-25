"""Tests fuer die Kollisionspruefung (controllers/irc_bridge/kollision.py, Stufe K1)."""

import math
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "controllers", "irc_bridge"))

import kollision as k  # noqa: E402

EINHEIT = [1, 0, 0, 0, 1, 0, 0, 0, 1]


def wuerfel_netz(kante, mitte=(0.0, 0.0, 0.0)):
    h = kante / 2
    punkte = [[mitte[0] + sx * h, mitte[1] + sy * h, mitte[2] + sz * h] for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]
    flaechen = [(0, 1, 3, 2), (4, 5, 7, 6), (0, 1, 5, 4), (2, 3, 7, 6), (0, 2, 6, 4), (1, 3, 7, 5)]
    return punkte, [d for a, b, c, e in flaechen for d in ((a, b, c), (a, c, e))]


def zylinder_netz(r, h, n=48):
    """Geschlossener Zylinder um die z-Achse, Mitte im Ursprung (wie ein CAD-Werkstueck)."""
    punkte = [[r * math.cos(2 * math.pi * i / n), r * math.sin(2 * math.pi * i / n), z] for z in (-h / 2, h / 2) for i in range(n)]
    punkte += [[0.0, 0.0, -h / 2], [0.0, 0.0, h / 2]]
    unten, oben = 2 * n, 2 * n + 1
    dreiecke = []
    for i in range(n):
        j = (i + 1) % n
        dreiecke += [(i, j, n + j), (i, n + j, n + i), (unten, j, i), (oben, n + i, n + j)]
    return punkte, dreiecke


def teil(name, kante, pos, art="aufbau"):
    """Umgebungsteil: Wuerfel der Kantenlaenge kante (m) mit Mitte im Teilsystem, an Weltlage pos."""
    return k.Umgebungsteil(name, art, k.Punktwolke(k.abtasten(*wuerfel_netz(kante))), (list(pos), EINHEIT), name)


class TestHuellen(unittest.TestCase):
    def test_kugelraster_enthaelt_alle_punkte(self):
        punkte = k.abtasten(*wuerfel_netz(0.1), 0.005)
        kugeln = k.kugelraster(punkte, 0.025)
        self.assertGreater(len(kugeln), 8)
        for p in punkte:
            self.assertTrue(any(math.dist(p, q[:3]) <= q[3] + 1e-9 for q in kugeln))
        self.assertLess(max(q[3] for q in kugeln), 0.025 * math.sqrt(3) / 2 + 0.001)   # hoechstens halbe Wuerfeldiagonale

    def test_quader_um_und_abtasten(self):
        q = k.quader_um([[0, 0, 0], [0.02, 0.01, 0.04]])
        self.assertEqual(q, {"mitte": [0.01, 0.005, 0.02], "halb": [0.01, 0.005, 0.02]})
        punkte = k.abtasten([[0, 0, 0], [0.01, 0, 0], [0, 0.01, 0]], [(0, 1, 2)], 0.002)
        self.assertIn((0.0, 0.0, 0.0), punkte)
        self.assertEqual(len(punkte), 45)   # laengste Kante 14,1 mm -> 8 Schritte -> 9*10/2 Punkte

    def test_punktwolke_in_box(self):
        w = k.Punktwolke(k.abtasten(*wuerfel_netz(0.1)))
        innen = list(w.in_box([0.045, -0.01, -0.01], [0.06, 0.01, 0.01]))
        self.assertTrue(innen and all(abs(p[0] - 0.05) < 1e-9 for p in innen))   # nur die Seitenflaeche x = +50 mm
        self.assertEqual(list(w.in_box([0.2, 0.2, 0.2], [0.3, 0.3, 0.3])), [])
        self.assertAlmostEqual(w.radius, math.sqrt(3) * 0.05)

    def test_gedrehter_quader(self):
        # um z um 90 Grad gedreht: die lange Seite (x lokal) liegt in Welt-y
        rot = [0, -1, 0, 1, 0, 0, 0, 0, 1]
        q = k.Quader("q", ([1.0, 0.0, 0.0], rot), [0.0, 0.0, 0.0], [0.05, 0.01, 0.01])
        self.assertTrue(q.enthaelt([1.0, 0.04, 0.0], 0.0))
        self.assertFalse(q.enthaelt([1.04, 0.0, 0.0], 0.0))
        self.assertTrue(q.enthaelt([1.0105, 0.0, 0.0], 0.001))   # mit Sicherheitsabstand
        self.assertAlmostEqual(q.tiefster(), -0.01)


def drehmatrix(achse, w):
    x, y, z = achse
    n = math.sqrt(x * x + y * y + z * z)
    x, y, z = x / n, y / n, z / n
    c, s, t = math.cos(w), math.sin(w), 1 - math.cos(w)
    return [t * x * x + c, t * x * y - s * z, t * x * z + s * y, t * x * y + s * z, t * y * y + c, t * y * z - s * x,
            t * x * z - s * y, t * y * z + s * x, t * z * z + c]


class TestImSystem(unittest.TestCase):
    def test_gleich_wie_in_weltkoordinaten(self):
        import random
        zufall = random.Random(7)
        for _ in range(30):
            teil = ([zufall.uniform(-1, 1) for _ in range(3)], drehmatrix([zufall.uniform(-1, 1) for _ in range(3)], zufall.uniform(0, 6)))
            koerper = [k.Quader("q", ([zufall.uniform(-1, 1) for _ in range(3)],
                                      drehmatrix([zufall.uniform(-1, 1) for _ in range(3)], zufall.uniform(0, 6))),
                                [zufall.uniform(-.05, .05) for _ in range(3)], [zufall.uniform(.01, .2) for _ in range(3)]),
                       k.Kugel("k", [zufall.uniform(-1, 1) for _ in range(3)], zufall.uniform(.01, .3))]
            for kk in koerper:
                lo, hi, enthaelt = kk.im_system(teil[0], teil[1], 0.004)
                for _ in range(200):
                    p_welt = [kk.mitte_welt[i] if isinstance(kk, k.Quader) else kk.mitte[i] for i in range(3)]
                    p_welt = [v + zufall.uniform(-.3, .3) for v in p_welt]
                    p_lokal = k._mtv(teil[1], k._sub(p_welt, teil[0]))
                    self.assertEqual(enthaelt(p_lokal), kk.enthaelt(p_welt, 0.004))
                    if enthaelt(p_lokal):
                        self.assertTrue(all(lo[i] - 1e-9 <= p_lokal[i] <= hi[i] + 1e-9 for i in range(3)))


class TestPruefung(unittest.TestCase):
    def test_tisch_aufbau_und_abstand(self):
        magazin = teil("magazin", 0.1, (0.3, 0.0, 0.05))   # Oberseite bei z = 100 mm
        frei = k.Kugel("Glied 5", [0.3, 0.0, 0.13], 0.02)    # 10 mm Luft
        self.assertEqual(k.pruefe([frei], [magazin], 0.0, 0.002, 0.002), [])
        self.assertEqual([t["b"] for t in k.pruefe([frei], [magazin], 0.0, 0.012, 0.002)], ["magazin"])   # 12 mm Abstand
        nah = k.Kugel("Glied 5", [0.3, 0.0, 0.119], 0.02)
        t = k.pruefe([nah], [magazin], 0.0, 0.0, 0.002)
        self.assertEqual((t[0]["a"], t[0]["b"]), ("Glied 5", "magazin"))
        self.assertAlmostEqual(t[0]["punkt"][2], 0.1, places=6)
        tief = k.Kugel("Glied 3", [0.6, 0.0, 0.015], 0.02)
        self.assertEqual(k.pruefe([tief], [magazin], 0.0, 0.0, 0.0)[0]["b"], "Tisch")
        self.assertEqual(k.pruefe([tief], [magazin], 0.0, 0.0, 0.0, tisch_frei={"Glied 3"}), [])
        self.assertEqual(k.pruefe([tief], [magazin], None, 0.0, 0.0), [])   # ohne Tisch

    def test_gegriffenes_teil_mit_ablagetoleranz(self):
        magazin = teil("magazin", 0.1, (0.3, 0.0, 0.05))
        def stift(z):
            return k.Quader("stift (gegriffen)", ([0.3, 0.0, z], EINHEIT), [0.0, 0.0, 0.0], [0.01, 0.01, 0.015])
        self.assertEqual(k.pruefe([stift(0.1149)], [magazin], 0.0, 0.002, 0.002), [])      # liegt auf, 0,1 mm eingedrueckt
        self.assertEqual(k.pruefe([stift(0.1135)], [magazin], 0.0, 0.002, 0.002), [])      # 1,5 mm tief: innerhalb 2 mm
        self.assertEqual(k.pruefe([stift(0.111)], [magazin], 0.0, 0.002, 0.002)[0]["b"], "magazin")   # 4 mm zu tief
        self.assertEqual(k.pruefe([stift(0.1135)], [magazin], 0.0, 0.002, 0.001)[0]["b"], "magazin")  # Toleranz 1 mm

    def test_rundes_teil_in_passender_tasche(self):
        """Scheibe Ø40 x 10 mm in einer Tasche Ø41: der Huellquader ragt mit den Ecken in die Wand, die echte
        Form nicht (Fehlalarm beim Greifen aus dem Magazin, 25.10.2026)."""
        scheibe = zylinder_netz(0.02, 0.01)
        form = k.Saeulen(*scheibe)
        self.assertTrue(form.innen([0.0, 0.0, 0.0]))
        self.assertTrue(form.innen([0.019, 0.0, 0.0]))
        self.assertFalse(form.innen([0.0148, 0.0148, 0.0]))         # Ecke des Quaders: nicht in der Scheibe
        self.assertFalse(form.innen([0.0, 0.0, 0.0049], 0.002))     # zu nah an der Oberseite
        self.assertTrue(form.innen([0.0, 0.0, 0.002], 0.002))
        # Tasche: Wand (Zylindermantel r = 20,5 mm) und Boden bei z = 0, als Punktwolke
        wand = [(0.0205 * math.cos(w / 120 * 2 * math.pi), 0.0205 * math.sin(w / 120 * 2 * math.pi), z / 1000)
                for w in range(120) for z in range(0, 11)]
        boden = [(x / 1000, y / 1000, 0.0) for x in range(-20, 21) for y in range(-20, 21) if x * x + y * y <= 400]
        tasche = k.Umgebungsteil("magazin", "aufbau", k.Punktwolke(wand + boden), ([0.35, -0.03, 0.0], EINHEIT), "magazin")
        lage = ([0.35, -0.03, 0.0051], EINHEIT)                      # liegt 0,1 mm ueber dem Boden
        quader = k.Quader("Werkstueck_1 (gegriffen)", lage, [0.0, 0.0, 0.0], [0.02, 0.02, 0.005])
        self.assertEqual(k.pruefe([quader], [tasche], 0.0, 0.002, 0.002)[0]["b"], "magazin")   # alter Fehlalarm
        echt = k.Formkoerper("Werkstueck_1 (gegriffen)", lage, form, k.eckpunkte(scheibe[0]))
        self.assertEqual(k.pruefe([echt], [tasche], 0.0, 0.002, 0.002), [])
        # quer verschoben: 3 mm in die Wand -> Kollision; 3 mm in den Boden gedrueckt -> Kollision
        quer = k.Formkoerper("Werkstueck_1 (gegriffen)", ([0.3535, -0.03, 0.0051], EINHEIT), form, k.eckpunkte(scheibe[0]))
        self.assertEqual(k.pruefe([quer], [tasche], 0.0, 0.002, 0.002)[0]["b"], "magazin")
        tief = k.Formkoerper("Werkstueck_1 (gegriffen)", ([0.35, -0.03, 0.002], EINHEIT), form, k.eckpunkte(scheibe[0]))
        self.assertEqual({t["b"] for t in k.pruefe([tief], [tasche], None, 0.002, 0.002)}, {"magazin"})
        # Tisch: tiefster Netzpunkt zaehlt, auch gedreht (Scheibe hochkant: Rand 20 mm unter der Mitte)
        hochkant = [1, 0, 0, 0, 0, -1, 0, 1, 0]
        self.assertEqual(k.pruefe([k.Formkoerper("s (gegriffen)", ([0.5, 0.0, 0.0205], hochkant), form,
                                                 k.eckpunkte(scheibe[0]))], [], 0.0, 0.002, 0.002), [])
        self.assertEqual(k.pruefe([k.Formkoerper("s (gegriffen)", ([0.5, 0.0, 0.017], hochkant), form,
                                                 k.eckpunkte(scheibe[0]))], [], 0.0, 0.002, 0.002)[0]["b"], "Tisch")

    def test_ausnahmen_und_werkstuecke(self):
        stift = teil("stift", 0.02, (0.3, 0.0, 0.01), art="werkstueck")
        backe = k.Quader("Backe links", ([0.3, -0.009, 0.01], EINHEIT), [0.0, 0.0, 0.0], [0.002, 0.004, 0.004])
        self.assertEqual(k.pruefe([backe], [stift], None, 0.002, 0.002)[0]["b"], "stift")
        self.assertEqual(k.pruefe([backe], [stift], None, 0.002, 0.002, ausnahmen={("Backe links", "stift")}), [])

    def test_einstellungen(self):
        self.assertEqual(k.einstellungen(None), k.STANDARD)
        e = k.einstellungen({"an": False, "reaktion": "melden", "abstand_mm": "5", "ablage_mm": 99})
        self.assertEqual(e, {"an": False, "reaktion": "melden", "abstand_mm": 5.0, "ablage_mm": 20.0})
        self.assertEqual(k.einstellungen({"reaktion": "explodieren", "abstand_mm": "x"})["reaktion"], "stopp")


class TestK3(unittest.TestCase):
    def test_paare_der_selbstkollision(self):
        self.assertTrue(k.selbst_erlaubt(3, 0))       # Ellbogen gegen Fuss
        self.assertTrue(k.selbst_erlaubt(6, 1))
        self.assertFalse(k.selbst_erlaubt(2, 1))      # Nachbarn
        self.assertFalse(k.selbst_erlaubt(1, 0))
        self.assertFalse(k.selbst_erlaubt(5, 3))      # kommen sich innerhalb der Achsgrenzen nie nahe
        self.assertTrue(k.selbst_erlaubt(None, 4))    # Greifer/gegriffenes Teil gegen den Unterarm
        self.assertFalse(k.selbst_erlaubt(None, 5))

    def test_bewegt(self):
        self.assertFalse(k.bewegt([0.0, 0.0, 0.0, 0, 0, 0]))
        self.assertFalse(k.bewegt([0.005, 0.0, -0.2, 0, 0, 0]))   # zittert bzw. faellt wenige mm (Ablegen)
        self.assertTrue(k.bewegt([0.0, 0.0, -0.6, 0, 0, 0]))      # aus gut 1 cm gefallen
        self.assertTrue(k.bewegt([0.05, 0.02, 0.0, 0, 0, 0]))     # geschoben

    def test_fallendes_teil_trifft_anderes(self):
        unten = teil("unten", 0.02, (0.3, 0.0, 0.01), art="werkstueck")
        oben = lambda z: k.Quader("oben", ([0.3, 0.0, z], EINHEIT), [0, 0, 0], [0.01, 0.01, 0.01])   # noqa: E731
        self.assertEqual(k.pruefe([oben(0.0305)], [unten], None, 0.001, 0.0)[0]["b"], "unten")   # liegt auf
        self.assertEqual(k.pruefe([oben(0.035)], [unten], None, 0.001, 0.0), [])                 # noch in der Luft


MESHES = os.path.join(ROOT, "protos", "meshes")


@unittest.skipUnless(os.path.exists(os.path.join(MESHES, "rebel_j1_coll.obj")), "Modell noch nicht erzeugt")
class TestSelbstkollisionModell(unittest.TestCase):
    """Wie der Controller: Kugeln der Glieder gegen die Oberflaeche von Fuss und Gliedern 1-4 (echte Netze)."""

    @classmethod
    def setUpClass(cls):
        import json
        with open(os.path.join(MESHES, "huellen.json"), encoding="utf-8") as f:
            cls.kugeln = {int(n): v for n, v in json.load(f)["glieder"].items()}
        cls.wolken = {}
        for n in k.SELBST_FLAECHEN:
            dateien = (["rebel_j%d_coll.obj" % n] if n else
                       sorted(d for d in os.listdir(MESHES) if d.startswith("rebel_j0_") and d.endswith(".obj")))
            punkte = []
            for d in dateien:
                v, f = [], []
                with open(os.path.join(MESHES, d), encoding="utf-8") as datei:
                    for z in datei:
                        if z.startswith("v "):
                            v.append([float(x) for x in z.split()[1:4]])
                        elif z.startswith("f "):
                            e = [int(c.split("/")[0]) - 1 for c in z.split()[1:]]
                            f += [(e[0], e[j], e[j + 1]) for j in range(1, len(e) - 1)]
                punkte += k.abtasten(v, f, 0.005)
            cls.wolken[n] = k.Punktwolke(punkte, duenn=0.004)

    @staticmethod
    def lagen(q_grad):
        """Lage der Glieder wie im Proto (Versatz zum Vorgaenger, dann Drehung um z,y,y,z,y,z)."""
        versatz = [0.1, 0.1522, 0.24152, 0.1222, 0.1778, 0.129]
        r, p = [[1, 0, 0], [0, 1, 0], [0, 0, 1]], [0.0, 0.0, 0.0]
        erg = {0: (list(p), [x for zeile in r for x in zeile])}
        for i, (achse, w) in enumerate(zip("zyyzyz", q_grad)):
            p = [p[j] + r[j][2] * versatz[i] for j in range(3)]
            c, s = math.cos(math.radians(w)), math.sin(math.radians(w))
            d = [[c, -s, 0], [s, c, 0], [0, 0, 1]] if achse == "z" else [[c, 0, s], [0, 1, 0], [-s, 0, c]]
            r = [[sum(r[a][m] * d[m][b] for m in range(3)) for b in range(3)] for a in range(3)]
            erg[i + 1] = (list(p), [x for zeile in r for x in zeile])
        return erg

    def treffer(self, q):
        lagen = self.lagen(q)
        koerper = [k.Kugel("Glied %d" % n, k._welt(lagen[n], kk[:3]), kk[3]) for n in range(1, 7) for kk in self.kugeln[n]]
        umgebung = [k.Umgebungsteil("Glied %d" % n, "roboter", self.wolken[n], lagen[n], "roboter%d" % n) for n in self.wolken]
        ausnahmen = {("Glied %d" % g, "roboter%d" % n) for g in range(1, 7) for n in self.wolken if not k.selbst_erlaubt(g, n)}
        rand_paare = {("Glied %d" % g, "roboter%d" % n): -k.SELBST_TIEFE for g in range(1, 7) for n in self.wolken}
        return k.pruefe(koerper, umgebung, None, 0.002, 0.002, ausnahmen=ausnahmen, rand_paare=rand_paare)

    def test_arbeitsstellungen_ohne_fehlalarm(self):
        for q in ([0, 0, 0, 0, 0, 0], [0, -20, 110, 0, 90, 0], [0, 60, 80, 0, 40, 0], [90, 30, 100, 0, 50, 0],
                  [0, -80, 140, 0, 95, 0], [0, 0, 140, 0, 95, 0]):
            self.assertEqual(self.treffer(q), [], q)

    def test_arm_ueber_den_fuss_gefaltet(self):
        t = self.treffer([0, 140, 140, 0, 0, 0])
        self.assertTrue(t)
        self.assertIn(t[0]["b"], ("Glied 0", "Glied 1"))


@unittest.skipUnless(os.path.exists(os.path.join(ROOT, "protos", "meshes", "huellen.json")), "Modell noch nicht erzeugt")
class TestHuellenDatei(unittest.TestCase):
    def test_glieder_und_greifer(self):
        import json
        with open(os.path.join(ROOT, "protos", "meshes", "huellen.json"), encoding="utf-8") as f:
            h = json.load(f)
        self.assertEqual(sorted(h["glieder"]), ["1", "2", "3", "4", "5", "6"])
        self.assertTrue(all(len(v) > 0 for v in h["glieder"].values()))
        g = h["greifer"]["egp40"]
        self.assertEqual(sorted(g), ["grund", "links", "rechts"])
        self.assertLess(g["links"]["mitte"][0], 0)   # linke Backe bei -x (offen: Traeger innen bei -14 mm)
        self.assertAlmostEqual(g["links"]["mitte"][0] + g["links"]["halb"][0], -0.014, places=4)
        with open(os.path.join(ROOT, "protos", "IgusRebel6DOF.proto"), encoding="utf-8") as f:
            proto = f.read()
        for name in ("DEF GLIED1 Solid", "DEF GLIED6 Solid", "DEF FINGER_LINKS Solid", "DEF FINGER_RECHTS Solid"):
            self.assertIn(name, proto)


if __name__ == "__main__":
    unittest.main()
