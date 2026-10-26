"""Tests fuer den Export der Greif- und Ablagepunkte als iRC-Programm (tools/punkte.py)."""

import io
import math
import os
import random
import sys
import unittest
import xml.etree.ElementTree as ET
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

import baukasten as bk  # noqa: E402
import punkte  # noqa: E402
import rebel_kinematik as kin  # noqa: E402
from test_baukasten import TEILE, UNTEN, TISCH, teil, wuerfel  # noqa: E402

EGP40 = punkte.greifer_daten("typ=egp40 tcp=0.10300 spalt=0.01400 hub=0.00600 backe_y=0.00800 "
                             "backe_z0=-0.00665 backe_z1=0.00565")
# liegende Rolle d = 16 mm entlang x mit einer ebenen Anflachung oben (Anfahrseite von oben, quer zur Achse)
ROLLE = {"box": [0, -8, -8, 30, 8, 8], "volumen_mm3": 6000,
         "flaechen": [{"id": 9, "normale": [0, 0, 1], "mitte": [15, 0, 8]}, {"id": 8, "normale": [0, 0, -1], "mitte": [15, 0, -8]}],
         "bohrungen": [{"id": 41, "innen": False, "durchmesser": 16, "enden": [[0, 0, 0], [30, 0, 0]]}],
         "kanten": [], "ecken": []}
# Scheibe d = 24 mm, 10 mm hoch, stehend (Stirnseite oben)
SCHEIBE = {"box": [-12, -12, 0, 12, 12, 10], "volumen_mm3": 4500,
           "flaechen": [{"id": 1, "normale": [0, 0, -1], "mitte": [0, 0, 0]}, {"id": 2, "normale": [0, 0, 1], "mitte": [0, 0, 10]}],
           "bohrungen": [{"id": 5, "innen": False, "durchmesser": 24, "enden": [[0, 0, 0], [0, 0, 10]]}],
           "kanten": [], "ecken": []}
ALLE = dict(TEILE, rolle=ROLLE, scheibe=SCHEIBE)


def werkstueck(ident, name, griff, versatz=(0, 0, 0), punkt=UNTEN):
    t = teil(ident, name, punkt, "tisch", TISCH, versatz=versatz, art="werkstueck")
    t["griff"] = griff
    return t


def ablage(ident, von, name, an, an_punkt, versatz=(0, 0, 0), punkt=UNTEN):
    t = teil(ident, name, punkt, an, an_punkt, versatz=versatz, art="ablage")
    t["von"] = von
    return t


class TestGeometrie(unittest.TestCase):
    def test_abc_hin_und_zurueck(self):
        zufall = random.Random(3)
        for _ in range(300):
            a, b, c = zufall.uniform(-179, 179), zufall.uniform(-89, 89), zufall.uniform(-179, 179)
            self.assertEqual([round(v, 6) for v in punkte.abc_aus_matrix(punkte.matrix_aus_abc(a, b, c))],
                             [round(v, 6) for v in (a, b, c)])
        r = punkte.matrix_aus_abc(180, 0, 0)   # Werkzeug zeigt nach unten
        self.assertEqual([round(r[i][2], 6) for i in range(3)], [0, 0, -1])

    def test_rueckwaerts_trifft_jede_stellung(self):
        zufall = random.Random(5)
        g = bk.IRC_ACHSGRENZEN
        for _ in range(500):
            q = [zufall.uniform(lo, hi) for lo, hi in g]
            p, r = kin.fk(q, 0.103)
            lage = bk.Lage([[r[0], r[1], r[2]], [r[3], r[4], r[5]], [r[6], r[7], r[8]]], [v * 1000 for v in p])
            qs = punkte.rueckwaerts(lage, 103, g)
            p2, r2 = kin.fk(qs, 0.103)
            self.assertLess(math.dist(p, p2), 1e-7)
            self.assertLess(max(abs(a - b) for a, b in zip(r, r2)), 1e-7)
        self.assertIsNone(punkte.rueckwaerts(bk.Lage(None, [2000, 0, 100]), 103, g))   # zu weit weg
        enge = [list(v) for v in g]
        enge[0] = [-10, 10]                                                              # Achse 1 eingeschraenkt
        self.assertIsNone(punkte.rueckwaerts(bk.Lage(punkte.matrix_aus_abc(180, 0, 0), [0, 350, 50]), 103, enge))

    def test_greiferdaten(self):
        self.assertEqual((EGP40["tcp_mm"], EGP40["offen_mm"], EGP40["zu_mm"]), (103, 28, 16))
        self.assertIsNone(punkte.greifer_daten("typ=vakuum tcp=0.07"))
        proto = "GREIFER = { 'egp40': 'typ=egp40 tcp=0.10300 spalt=0.01400 hub=0.00600', 'vakuum': 'typ=vakuum tcp=0.07' }"
        self.assertEqual(list(punkte.greifer_aus_proto(proto)), ["egp40"])


class TestGriff(unittest.TestCase):
    def rechne(self, *teile, magazine=None, greifer=EGP40):
        e = {"titel": "T", "teile": list(teile), "magazine": magazine or []}
        return punkte.berechne(e, ALLE.__getitem__, greifer)

    def punkt(self, erg, name):
        return next(p for p in erg["punkte"] if p["name"] == name)

    def test_zwei_flaechen_von_oben(self):
        # Wuerfel 20 mm in der Tischmitte (x 240..260), Greifflaechen links/rechts (x), Anfahrt von oben
        erg = self.rechne(werkstueck("w1", "wuerfel", {"flaechen": [3, 4], "anfahrt": 2}))
        p, vor = self.punkt(erg, "w1_greifen"), self.punkt(erg, "w1_greifen_vor")
        # Backen ganz am Teil: TCP 6,65 mm unter der Oberkante (halbe Backenhoehe des EGP 40)
        self.assertEqual([round(v, 2) for v in p["lage"].o], [250, 0, 13.35])
        self.assertEqual([round(p["lage"].r[i][2], 6) for i in range(3)], [0, 0, -1])        # Werkzeug nach unten
        self.assertEqual(abs(round(p["lage"].r[0][0], 6)), 1)                                # Backen schliessen in x
        self.assertEqual([round(v, 2) for v in vor["lage"].o], [250, 0, 53.35])             # 40 mm darueber
        self.assertEqual(p["warnungen"], [])
        self.assertIsNotNone(p["gelenke"])
        self.assertLess(abs(p["gelenke"][5]), 90)                                            # A6 moeglichst wenig

    def test_breite_tiefe_und_fehler(self):
        w = {"flaechen": [3, 4], "anfahrt": 2, "tiefe_mm": 25}
        erg = self.rechne(werkstueck("w1", "wuerfel", w), greifer=dict(EGP40, offen_mm=18, zu_mm=12))
        texte = " ".join(self.punkt(erg, "w1_greifen")["warnungen"])
        self.assertIn("oeffnet nur 18.0 mm", texte)
        self.assertIn("ragen", texte)                                                        # 25 mm tief bei 20 mm Teil
        for falsch, text in (({"flaechen": [3, 5], "anfahrt": 2}, "parallel"),
                             ({"flaechen": [3, 4], "anfahrt": 4}, "quer"),
                             ({"zylinder": 41, "anfahrt": 2}, "Aussenflaeche")):
            erg = self.rechne(werkstueck("w1", "vorrichtung", falsch))
            self.assertIn(text, erg["meldungen"][0])
        with self.assertRaises(bk.BaukastenFehler):
            self.rechne(werkstueck("w1", "wuerfel", {"flaechen": [3, 4], "anfahrt": 2}), greifer=None)

    def test_rolle_von_der_seite_und_scheibe_von_oben(self):
        erg = self.rechne(werkstueck("r", "rolle", {"zylinder": 41, "anfahrt": 9}, punkt={"art": "flaeche", "id": 8}),
                          werkstueck("s", "scheibe", {"zylinder": 5, "anfahrt": 2}, versatz=(0, 150, 0), punkt={"art": "flaeche", "id": 1}),
                          werkstueck("s2", "scheibe", {"zylinder": 5, "anfahrt": 2, "drehung_grad": 90}, versatz=(0, -150, 0),
                                     punkt={"art": "flaeche", "id": 1}))
        r = self.punkt(erg, "r_greifen")
        # Rolle liegt mit der unteren Anflachung auf dem Tisch: Achse 8 mm hoch, TCP auf der Achse, Backen quer (y)
        self.assertEqual([round(v, 2) for v in r["lage"].o], [250 - 15 + 15, 0, 8])
        self.assertEqual(abs(round(r["lage"].r[1][0], 6)), 1)
        s, s2 = self.punkt(erg, "s_greifen"), self.punkt(erg, "s2_greifen")
        # 10 mm hoch: TCP 3,35 mm unter der Oberkante, die Backenspitze (5,65 mm tiefer) 1 mm ueber dem Boden
        self.assertEqual([round(v, 2) for v in s["lage"].o], [250, 150, 6.65])
        self.assertAlmostEqual(s["gelenke"][5], 0, places=3)                                 # Backen entlang Fuss - Teil
        self.assertAlmostEqual(abs(s2["gelenke"][5]), 90, places=3)                          # um 90 Grad gedreht

    def test_ablage_und_magazin(self):
        teile = [teil("v", "vorrichtung", UNTEN, "tisch", TISCH, versatz=(100, 0, 0)),
                 werkstueck("w", "wuerfel", {"flaechen": [3, 4], "anfahrt": 2, "name": "Klotz"}, versatz=(0, 150, 0))]
        teile += [ablage("a%d" % i, "w", "wuerfel", "v", {"art": "flaeche", "id": 2}, versatz=v)
                  for i, v in enumerate(((-10, -10, 0), (-10, 10, 0), (10, -10, 0), (10, 10, 0)))]
        teile[-1]["ablage"] = {"name": "Extra", "vor_mm": 25}
        erg = self.rechne(*teile, magazine=[{"name": "Mag", "a": "a0", "b": "a1", "c": "a2", "spalten": 2, "zeilen": 3}])
        namen = [p["name"] for p in erg["punkte"]]
        self.assertEqual(namen, ["Klotz_greifen_vor", "Klotz_greifen", "Extra_ablegen_vor", "Extra_ablegen",
                                 "Mag_A_vor", "Mag_A", "Mag_B_vor", "Mag_B", "Mag_C_vor", "Mag_C"])
        extra, extra_vor = self.punkt(erg, "Extra_ablegen"), self.punkt(erg, "Extra_ablegen_vor")
        self.assertAlmostEqual(extra_vor["lage"].o[2] - extra["lage"].o[2], 25)            # eigener Abstand
        # Wuerfel auf der Vorrichtung (Oberkante 40): Greifpunkt 20 + 40 - 6,65
        self.assertAlmostEqual(extra["lage"].o[2], 53.35)
        m = erg["magazine"][0]
        self.assertEqual((m["von"], len(m["taschen"])), ("w", 6))
        mitte = next(l for i, j, l in m["taschen"] if (i, j) == (1, 1))                     # halber Weg nach C, ganz nach B
        a, b, c = (self.punkt(erg, "Mag_" + k)["lage"] for k in "ABC")
        self.assertEqual([round(v, 6) for v in mitte.o], [round(a.o[k] + (b.o[k] - a.o[k]) + (c.o[k] - a.o[k]) / 2, 6) for k in range(3)])

    def test_pruefung_der_angaben(self):
        w = werkstueck("w", "wuerfel", {"flaechen": [3, 4], "anfahrt": 2})
        a = ablage("a", "w", "wuerfel", "tisch", TISCH)
        for teile, magazine, text in (
                ([dict(w, griff={"flaechen": [3], "anfahrt": 2})], [], "zwei verschiedene"),
                ([dict(w, griff={"flaechen": [3, 4]})], [], "Anfahrseite"),
                ([w, dict(a, von="gibtsnicht")], [], "keinem Werkstueck"),
                ([w, dict(a, teil="vorrichtung")], [], "anderes Teil"),
                ([w, a], [{"name": "M", "a": "a", "b": "w", "spalten": 2, "zeilen": 1}], "keine Ablage"),
                ([w, a], [{"name": "M", "a": "a", "b": "a", "spalten": 2, "zeilen": 1}], "verschiedene"),
                ([w, a], [{"name": "M", "a": "a", "spalten": 1, "zeilen": 1}], "zwei Plaetze"),
                ([w, dict(a, ablage={"name": "w"})], [], "doppelt")):   # w_greifen / w_ablegen kollidieren nicht ...
            if text == "doppelt":
                teile = [w, dict(a, ablage={"name": "x"}), dict(a, id="b", ablage={"name": "x"})]
            with self.assertRaises(bk.BaukastenFehler, msg=text) as f:
                self.rechne(*teile, magazine=magazine)
            self.assertIn(text, str(f.exception))
        self.assertEqual(bk.variablen_name("3-er Magazin"), "P_3_er_Magazin")


def wuerfel_netz(kante):
    """Dreiecksnetz eines Wuerfels mit Ecke im Ursprung (wie test_baukasten.wuerfel), mm."""
    p = [[x, y, z] for x in (0, kante) for y in (0, kante) for z in (0, kante)]
    flaechen = [(0, 1, 3, 2), (4, 5, 7, 6), (0, 1, 5, 4), (2, 3, 7, 6), (0, 2, 6, 4), (1, 3, 7, 5)]
    return p, [d for a, b, c, e in flaechen for d in ((a, b, c), (a, c, e))]


# EGP 40 wie in protos/meshes/huellen.json: Grundkoerper und offene Backen im Flanschsystem (m)
HUELLE = {"grund": {"mitte": [0.0, 0.0, 0.04817], "halb": [0.0275, 0.0275, 0.04818]},
          "links": {"mitte": [-0.017, 0.0, 0.1025], "halb": [0.003, 0.008, 0.00615]},
          "rechts": {"mitte": [0.017, 0.0, 0.1025], "halb": [0.003, 0.008, 0.00615]}}


class TestKollision(unittest.TestCase):
    def test_greifer_gegen_nachbar_und_tisch(self):
        netze = {"wuerfel": wuerfel_netz(20), "vorrichtung": wuerfel_netz(40)}
        griff = {"flaechen": [3, 4], "anfahrt": 2}
        # w1 in der Tischmitte (x 240..260); w2 5 mm daneben in Schliessrichtung (x 265..285); w3 weit weg, zu tief gegriffen
        e = {"titel": "T", "teile": [werkstueck("w1", "wuerfel", griff), werkstueck("w2", "wuerfel", griff, versatz=(25, 0, 0)),
                                     werkstueck("w3", "wuerfel", dict(griff, tiefe_mm=19), versatz=(0, 200, 0))]}
        erg = punkte.berechne(e, ALLE.__getitem__, EGP40, netze.__getitem__, HUELLE)
        warn = {p["name"]: " ".join(p["warnungen"]) for p in erg["punkte"]}
        self.assertIn("stoesst an: w2", warn["w1_greifen"])
        self.assertIn("w1", warn["w2_greifen"])
        self.assertIn("Tisch", warn["w3_greifen"])
        self.assertNotIn("stoesst", warn["w3_greifen_vor"])
        self.assertNotIn("w3", warn["w1_greifen"])   # das eigene Teil zwischen den Backen zaehlt nicht
        # ohne Netze und Huelle keine Kollisionspruefung
        ohne = punkte.berechne(e, ALLE.__getitem__, EGP40)
        self.assertFalse(any("stoesst" in w for p in ohne["punkte"] for w in p["warnungen"]))

    def test_obj_lesen(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            pfad = os.path.join(tmp, "netz.obj")
            with open(pfad, "w") as f:
                f.write("# Test\nv 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nvn 0 0 1\nf 1//1 2//1 3//1 4//1\n")
            self.assertEqual(punkte.lies_obj(pfad), ([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], [(0, 1, 2), (0, 2, 3)]))


class TestProgramme(unittest.TestCase):
    def setUp(self):
        teile = [teil("v", "vorrichtung", UNTEN, "tisch", TISCH, versatz=(100, 0, 0)),
                 werkstueck("w", "wuerfel", {"flaechen": [3, 4], "anfahrt": 2}, versatz=(0, 150, 0))]
        teile += [ablage("a%d" % i, "w", "wuerfel", "v", {"art": "flaeche", "id": 2}, versatz=v)
                  for i, v in enumerate(((-10, -10, 0), (-10, 10, 0), (10, -10, 0)))]
        self.e = {"titel": "T", "teile": teile, "magazine": [{"name": "Mag", "a": "a0", "b": "a1", "c": "a2", "spalten": 4, "zeilen": 2}]}
        self.erg = punkte.berechne(self.e, ALLE.__getitem__, EGP40)

    def test_punkte_xml(self):
        wurzel = ET.fromstring(punkte.punkte_xml(self.erg, ["Kopf"]).encode())
        stores = wurzel.findall("Store")
        self.assertEqual([s.get("Name") for s in stores], [p["name"] for p in self.erg["punkte"]])
        s = stores[1]
        self.assertEqual((s.get("Nr"), s.get("Type")), ("2", "position"))
        werte = [float(v) for v in s.get("Value").split()]
        self.assertEqual(len(werte), 6)
        self.assertEqual([round(v, 2) for v in werte[:3]], [round(v, 2) for v in self.erg["punkte"][1]["lage"].o])
        self.assertEqual(len(s.get("ValueJoints").split()), 9)
        r = punkte.matrix_aus_abc(*werte[3:])   # A/B/C geben die Orientierung wieder
        self.assertLess(max(abs(r[i][k] - self.erg["punkte"][1]["lage"].r[i][k]) for i in range(3) for k in range(3)), 1e-3)

    def test_beispiel_xml(self):
        wurzel = ET.fromstring(punkte.beispiel_xml(self.erg, "t_punkte.xml", ["Kopf"]).encode())
        befehle = [k for k in wurzel if k.tag != "Header"]
        self.assertEqual((befehle[0].tag, befehle[0].get("File")), ("Sub", "t_punkte.xml"))
        self.assertEqual([int(b.get("Nr")) for b in befehle], list(range(1, len(befehle) + 1)))
        variablen = [b.get("VariableName") for b in befehle if b.tag in ("Joint", "Linear")]
        self.assertEqual(variablen[:4], ["w_greifen_vor", "w_greifen", "w_greifen_vor", "Mag_A_vor"])   # in Tasche A
        self.assertTrue(set(variablen) <= {p["name"] for p in self.erg["punkte"]} | {"Mag_pos"})
        matrix = wurzel.find("Matrix")
        self.assertEqual((matrix.get("CntX"), matrix.get("CntY"), matrix.get("VarPointA"), matrix.get("VarPointC")),
                         ("4", "2", "Mag_A_vor", "Mag_C_vor"))
        relativ = wurzel.findall("Relative")
        self.assertEqual([(r.get("MoType"), float(r.get("z"))) for r in relativ], [("CartTool", 40), ("CartTool", -40)])
        kanaele = [(o.get("Channel"), o.get("State")) for o in wurzel.findall("Output")][:4]
        self.assertEqual(kanaele, [("DOut31", "True"), ("DOut32", "False"), ("DOut31", "False"), ("DOut32", "True")])

    def test_paket(self):
        name, daten = punkte.paket(self.erg, "lager", "Lager", "Schunk EGP 40", EGP40, "26.10.2026")
        self.assertEqual(name, "iRC_Punkte_lager.zip")
        z = zipfile.ZipFile(io.BytesIO(daten))
        self.assertEqual(sorted(z.namelist()), ["LIESMICH.txt", "installieren.cmd", "lager_beispiel.xml", "lager_punkte.xml"])
        self.assertIn(b"V15\\Data\\Programs", z.read("installieren.cmd"))
        self.assertIn(b'File="lager_punkte.xml"', z.read("lager_beispiel.xml"))
        self.assertIn("TCP 103.0 mm", z.read("LIESMICH.txt").decode())

    def test_warnungen_im_bericht_und_in_der_datei(self):
        e = {"titel": "T", "teile": [werkstueck("weit", "wuerfel", {"flaechen": [3, 4], "anfahrt": 2}, versatz=(900, 0, 0))]}
        erg = punkte.berechne(e, ALLE.__getitem__, EGP40)
        b = punkte.bericht(erg)
        self.assertEqual(b["warnungen"], 2)
        self.assertIsNone(b["punkte"][0]["gelenke"])
        self.assertIn("ACHTUNG: nicht erreichbar", punkte.punkte_xml(erg, []))


if __name__ == "__main__":
    unittest.main()
