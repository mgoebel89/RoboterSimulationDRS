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
