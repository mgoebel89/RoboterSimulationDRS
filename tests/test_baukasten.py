"""Tests fuer den Weltbaukasten: Verbindungsrechnung (tools/baukasten.py, ueberall) und
Teile-Pipeline (tools/teile.py, nur wo gmsh installiert ist, also im Container)."""

import json
import math
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import baukasten as bk  # noqa: E402

try:
    import gmsh  # noqa: F401
    HAT_GMSH = True
except ImportError:
    HAT_GMSH = False


def wuerfel(kante=20.0, bohrung=None):
    """Merkmale eines Wuerfels mit Ecke im CAD-Ursprung (wie teil.json), optional mit Bohrung in z."""
    k = kante
    h = k / 2
    flaechen = [
        {"id": 1, "normale": [0, 0, -1], "mitte": [h, h, 0]}, {"id": 2, "normale": [0, 0, 1], "mitte": [h, h, k]},
        {"id": 3, "normale": [-1, 0, 0], "mitte": [0, h, h]}, {"id": 4, "normale": [1, 0, 0], "mitte": [k, h, h]},
        {"id": 5, "normale": [0, -1, 0], "mitte": [h, 0, h]}, {"id": 6, "normale": [0, 1, 0], "mitte": [h, k, h]},
    ]
    kanten = [{"id": 11, "a": [0, 0, 0], "b": [k, 0, 0], "flaechen": [1, 5]},
              {"id": 12, "a": [0, 0, 0], "b": [0, k, 0], "flaechen": [1, 3]}]
    ecken = [{"id": 21, "punkt": [0, 0, 0], "flaechen": [{"flaeche": 1, "x": [1, 0, 0], "y": [0, 1, 0]}]}]
    bohrungen = []
    if bohrung:
        bohrungen.append({"id": 31, "innen": True, "durchmesser": bohrung, "achse": [0, 0, 1],
                          "enden": [[h, h, 0], [h, h, k]]})
    return {"box": [0, 0, 0, k, k, k], "volumen_mm3": k ** 3, "flaechen": flaechen, "kanten": kanten,
            "ecken": ecken, "bohrungen": bohrungen}


STIFT = {"box": [-4, -4, 0, 4, 4, 30], "volumen_mm3": 1500, "flaechen": [], "kanten": [], "ecken": [],
         "bohrungen": [{"id": 41, "innen": False, "durchmesser": 8, "achse": [0, 0, 1], "enden": [[0, 0, 0], [0, 0, 30]]}]}
TEILE = {"wuerfel": wuerfel(), "vorrichtung": wuerfel(40, bohrung=8), "stift": STIFT}


def huelle(lage, merkmale):
    """Huellquader eines Teils in Weltkoordinaten (mm)."""
    b = merkmale["box"]
    ecken = [lage.punkt([x, y, z]) for x in (b[0], b[3]) for y in (b[1], b[4]) for z in (b[2], b[5])]
    return [round(min(p[i] for p in ecken), 6) for i in range(3)] + [round(max(p[i] for p in ecken), 6) for i in range(3)]


def entwurf(*teile):
    return {"titel": "Test", "teile": list(teile)}


def teil(ident, name, punkt, an, an_punkt, versatz=(0, 0, 0), drehung=0, art="aufbau"):
    return {"id": ident, "teil": name, "art": art,
            "verbindung": {"punkt": punkt, "an": an, "an_punkt": an_punkt, "versatz_mm": list(versatz),
                           "drehung_grad": drehung}}


UNTEN = {"art": "flaeche", "id": 1}
TISCH = {"art": "flaeche", "id": "oben"}


class TestVerbindungen(unittest.TestCase):
    def loese(self, *teile):
        return bk.loese(entwurf(*teile), TEILE.__getitem__)

    def test_flaeche_auf_tisch_mitte_und_versatz(self):
        lagen = self.loese(teil("a", "wuerfel", UNTEN, "tisch", TISCH),
                           teil("b", "wuerfel", UNTEN, "tisch", TISCH, versatz=(100, 50, 0)))
        self.assertEqual(huelle(lagen["a"], TEILE["wuerfel"]), [240, -10, 0, 260, 10, 20])   # Tischmitte x = 250
        self.assertEqual(huelle(lagen["b"], TEILE["wuerfel"]), [340, 40, 0, 360, 60, 20])

    def test_drehung_und_abstand(self):
        lagen = self.loese(teil("a", "wuerfel", UNTEN, "tisch", TISCH, versatz=(0, 0, 5), drehung=90))
        self.assertEqual(huelle(lagen["a"], TEILE["wuerfel"]), [240, -10, 5, 260, 10, 25])   # 5 mm ueber dem Tisch
        x_teil = bk._mv(lagen["a"].r, [1, 0, 0])
        self.assertAlmostEqual(x_teil[1], 1.0)   # CAD-x zeigt nach 90 Grad in Welt-y

    def test_ecke_mit_abstaenden_zu_den_tischkanten(self):
        lagen = self.loese(teil("a", "wuerfel", {"art": "ecke", "id": 21}, "tisch",
                                {"art": "ecke", "id": "x+y-"}, versatz=(30, 40, 0)))
        # Tischecke x = 750, y = -400; x des Punkts laeuft an der Kante y- entlang ins Innere (+y),
        # y des Punkts an der Kante x+ entlang ins Innere (-x)
        self.assertEqual(huelle(lagen["a"], TEILE["wuerfel"]), [690, -370, 0, 710, -350, 20])

    def test_kante_buendig_mit_abstand(self):
        lagen = self.loese(teil("a", "wuerfel", {"art": "kante", "id": 11, "flaeche": 1}, "tisch",
                                {"art": "kante", "id": "x+", "flaeche": "oben"}, versatz=(0, 25, 0)))
        # 25 mm von der Kante x+ (x = 750) nach innen, mittig auf der Kante, auf dem Tisch
        self.assertEqual(huelle(lagen["a"], TEILE["wuerfel"]), [705, -10, 0, 725, 10, 20])

    def test_stift_in_bohrung_der_vorrichtung(self):
        lagen = self.loese(
            teil("v", "vorrichtung", UNTEN, "tisch", TISCH, versatz=(100, 0, 0)),
            teil("s", "stift", {"art": "bohrung", "id": 41, "ende": 0}, "v",
                 {"art": "bohrung", "id": 31, "ende": 1}, versatz=(0, 0, -15), art="werkstueck"))
        self.assertEqual(huelle(lagen["v"], TEILE["vorrichtung"]), [330, -20, 0, 370, 20, 40])
        # Stift steckt 15 mm tief in der Bohrung oben in der Vorrichtung (Oberkante z = 40)
        self.assertEqual(huelle(lagen["s"], STIFT), [346, -4, 25, 354, 4, 55])

    def test_kette_zieht_mit(self):
        e = entwurf(teil("v", "vorrichtung", UNTEN, "tisch", TISCH),
                    teil("s", "stift", {"art": "bohrung", "id": 41, "ende": 0}, "v", {"art": "bohrung", "id": 31, "ende": 1}))
        vorher = bk.loese(e, TEILE.__getitem__)["s"].o
        e["teile"][0]["verbindung"]["versatz_mm"] = [0, 100, 0]
        nachher = bk.loese(e, TEILE.__getitem__)["s"].o
        self.assertAlmostEqual(nachher[1] - vorher[1], 100)

    def test_umdrehen(self):
        v = teil("a", "wuerfel", {"art": "flaeche", "id": 2}, "tisch", TISCH)
        v["verbindung"]["umdrehen"] = True   # Oberseite zeigt mit dem Tisch nach oben: Teil haengt unter der Platte
        self.assertEqual(huelle(self.loese(v)["a"], TEILE["wuerfel"]), [240, -10, -20, 260, 10, 0])

    def test_fehler(self):
        faelle = [
            entwurf(teil("a", "wuerfel", UNTEN, "b", UNTEN), teil("b", "wuerfel", UNTEN, "a", UNTEN)),   # Schleife
            entwurf(teil("a", "wuerfel", UNTEN, "gibtsnicht", UNTEN)),
            entwurf(teil("a", "wuerfel", {"art": "flaeche", "id": 99}, "tisch", TISCH)),
            entwurf(teil("a", "wuerfel", UNTEN, "tisch", {"art": "ecke", "id": "z+"})),
            entwurf(teil("a", "wuerfel", UNTEN, "tisch", TISCH), teil("a", "wuerfel", UNTEN, "tisch", TISCH)),
            entwurf(teil("1x", "wuerfel", UNTEN, "tisch", TISCH)),
            entwurf(teil("tisch", "wuerfel", UNTEN, "tisch", TISCH)),
            entwurf(teil("a", "wuerfel", UNTEN, "tisch", TISCH, versatz=(1, 2))),
            entwurf(dict(teil("a", "wuerfel", UNTEN, "tisch", TISCH), art="schraube")),
        ]
        for e in faelle:
            with self.assertRaises(bk.BaukastenFehler, msg=json.dumps(e)):
                bk.loese(e, TEILE.__getitem__)

    def test_achse_winkel(self):
        for grad in (0, 30, 90, 180, 270):
            r = bk._mm(bk._rot_z(grad), bk._rot_180([1, 1, 0]))
            ax, ay, az, w = bk.achse_winkel(r)
            import math
            c, s, t = math.cos(w), math.sin(w), 1 - math.cos(w)
            zurueck = [[t * ax * ax + c, t * ax * ay - s * az, t * ax * az + s * ay],
                       [t * ax * ay + s * az, t * ay * ay + c, t * ay * az - s * ax],
                       [t * ax * az - s * ay, t * ay * az + s * ax, t * az * az + c]]
            for zr, rr in zip(zurueck, r):
                for a, b in zip(zr, rr):
                    self.assertAlmostEqual(a, b, places=6)


class TestTisch(unittest.TestCase):
    def loese(self, tisch, *teile):
        e = entwurf(*teile)
        e["tisch"] = tisch
        return bk.loese(e, TEILE.__getitem__)

    def test_standard_wie_bisher(self):
        m = bk.tisch_merkmale(bk.tisch_werte({}))
        ecke = {e["id"]: e["punkt"] for e in m["ecken"]}
        self.assertEqual(ecke["vorne-rechts"], [750, -400, 0])
        self.assertEqual(ecke["x+y-"], [750, -400, 0])   # alter Name gilt weiter
        self.assertEqual(ecke["hinten-links"], [-250, 400, 0])

    def test_eigene_groesse_und_roboterlage(self):
        tisch = {"laenge_mm": 1200, "breite_mm": 600, "roboter_von_hinten_mm": 100, "roboter_von_links_mm": 300}
        lagen = self.loese(tisch, teil("a", "wuerfel", {"art": "ecke", "id": 21}, "tisch",
                                       {"art": "ecke", "id": "hinten-links"}, versatz=(10, 20, 0)))
        # Ecke hinten-links bei x = -100, y = 300; 10 mm von der linken, 20 mm von der hinteren Kante
        self.assertEqual(huelle(lagen["a"], TEILE["wuerfel"]), [-80, 270, 0, -60, 290, 20])

    def test_gedrehter_roboter_und_sockel(self):
        tisch = {"drehung_grad": 90, "hoehe_mm": -100}
        m = bk.tisch_merkmale(bk.tisch_werte({"tisch": tisch}))
        ecke = {e["id"]: [round(v, 6) for v in e["punkt"]] for e in m["ecken"]}
        # Roboter 90 Grad gedreht: die Tischecke vorne-rechts (750/-400 im Tischsystem) liegt bei (-400, -750)
        self.assertEqual(ecke["vorne-rechts"], [-400, -750, -100])
        lagen = self.loese(tisch, teil("a", "wuerfel", UNTEN, "tisch", TISCH))
        self.assertEqual(huelle(lagen["a"], TEILE["wuerfel"]), [-10, -260, -100, 10, -240, -80])

    def test_ungueltige_werte(self):
        for tisch in ({"laenge_mm": 50}, {"hoehe_mm": "tief"}, {"drehung_grad": True}, "breit"):
            with self.assertRaises(bk.BaukastenFehler, msg=tisch):
                bk.tisch_werte({"tisch": tisch})

    def test_welt_mit_tisch(self):
        e = entwurf(teil("a", "wuerfel", UNTEN, "tisch", TISCH))
        e["tisch"] = {"laenge_mm": 1200, "breite_mm": 600, "drehung_grad": 90, "hoehe_mm": -100}
        text = bk.welt_text(e, bk.loese(e, TEILE.__getitem__), TEILE.__getitem__)
        self.assertIn("geometry Box { size 1.2000 0.6000 0.03 }", text)
        self.assertIn("rotation 0 0 1 -1.570796", text)
        self.assertIn("DEF SOCKEL Solid", text)
        self.assertIn("translation 0 0 -0.8500", text)   # Boden 750 mm unter der Tischplatte
        ohne = text.replace("%<", "").replace(">%", "")
        self.assertEqual(ohne.count("{"), ohne.count("}"))
        e["tisch"] = {}
        self.assertNotIn("SOCKEL", bk.welt_text(e, bk.loese(e, TEILE.__getitem__), TEILE.__getitem__))


PLATTE_BOHRUNGEN = [[80 * math.cos(math.radians(w)), 80 * math.sin(math.radians(w)), 2] for w in range(30, 360, 60)]   # Bohrung an der Tischseite der Platte
TEILE_P = dict(TEILE, basisplatte={"box": [-90, -90, -8, 90, 90, 2], "flaechen": [], "kanten": [], "ecken": [], "bohrungen": []})


class TestProfiltisch(unittest.TestCase):
    def werte(self, **tisch):
        return bk.tisch_werte({"tisch": dict({"art": "profil"}, **tisch)})

    def test_werte_aus_den_anschlaegen(self):
        t = self.werte(anschlag_hinten_mm=50, anschlag_links_mm=200)
        self.assertEqual((t["roboter_von_hinten_mm"], t["roboter_von_links_mm"]), (140, 290))
        self.assertEqual((t["laenge_mm"], t["breite_mm"], t["hoehe_mm"], t["nut"]), (800, 800, -2, 4))
        # Eingaben fuer den ebenen Tisch zaehlen beim Profiltisch nicht
        self.assertEqual(self.werte(laenge_mm=1500, hoehe_mm=-100)["laenge_mm"], 800)
        self.assertEqual(bk.tisch_werte({})["art"], "eben")

    def test_raster_und_grenzen(self):
        erlaubt = bk.profil_anschlaege_hinten()
        self.assertEqual((erlaubt[0], erlaubt[-1]), ({"anschlag_mm": 10, "nut": 3}, {"anschlag_mm": 610, "nut": 18}))
        self.assertEqual(self.werte(anschlag_hinten_mm=610, anschlag_links_mm=595)["nut"], 18)
        self.werte(anschlag_hinten_mm=10, anschlag_links_mm=40)   # knapp neben der Beinkappe
        for falsch in ({"anschlag_hinten_mm": 60}, {"anschlag_hinten_mm": 650}, {"anschlag_links_mm": 24},
                       {"anschlag_links_mm": 596}, {"anschlag_hinten_mm": "50"}, {"drehung_grad": 400},
                       {"anschlag_hinten_mm": 10, "anschlag_links_mm": 25}):
            with self.assertRaises(bk.BaukastenFehler, msg=falsch):
                self.werte(**falsch)
        with self.assertRaises(bk.BaukastenFehler):
            bk.tisch_werte({"tisch": {"art": "rund"}})

    def test_bohrungen_der_platte_liegen_ueber_nuten(self):
        for drehung in (0, 90, -30):
            t = self.werte(anschlag_hinten_mm=90, anschlag_links_mm=130, drehung_grad=drehung)
            nuten = [k for k in bk.tisch_merkmale(t)["kanten"] if k["id"].startswith("nut-")]
            self.assertEqual(len(nuten), 22)
            for p in PLATTE_BOHRUNGEN:
                w = bk.platte_lage(t).punkt(p)
                self.assertAlmostEqual(w[2], -2.0)
                naechste = min(nuten, key=lambda k: abstand_strecke(w, k["a"], k["b"]))
                self.assertLess(abstand_strecke(w, naechste["a"], naechste["b"]), 1e-6, (drehung, p))
        # Anschlagkante (Ecke -x/+y der Platte, 8 mm nach unten im CAD) zeigt nach oben in die Ecke hinten links
        ecke = bk.platte_lage(self.werte()).punkt([-90, 90, -8])
        self.assertEqual([round(v, 6) for v in ecke], [-90, 90, 8])

    def test_nuten_lage(self):
        t = self.werte(anschlag_hinten_mm=50, anschlag_links_mm=200)   # Fuss 140 von hinten, 290 von links
        k = {k["id"]: k for k in bk.tisch_merkmale(t)["kanten"]}
        self.assertEqual(k["nut-1"]["a"], [-120, 250, -2])
        self.assertEqual(k["nut-4"]["a"][0], 0)    # die Fussmitte steht auf Nut 4
        self.assertEqual(k["nut-20"]["b"], [640, -470, -2])
        self.assertEqual((k["nut-links"]["a"], k["nut-links"]["b"]), ([-100, 270, -2], [620, 270, -2]))
        self.assertEqual(k["nut-5"]["fang_mm"], 5)

    def test_vorrichtung_mit_bohrung_in_der_nut(self):
        e = entwurf(teil("v", "vorrichtung", {"art": "bohrung", "id": 31, "ende": 0}, "tisch",
                         {"art": "kante", "id": "nut-5", "flaeche": "oben"}))
        e["tisch"] = {"art": "profil", "anschlag_hinten_mm": 50, "anschlag_links_mm": 200}
        lagen = bk.loese(e, TEILE.__getitem__)
        # Nut 5 bei x = 40, Nutmitte bei y = 290 - 400; Tischplatte 2 mm unter dem Fuss
        self.assertEqual(huelle(lagen["v"], TEILE["vorrichtung"]), [20, -130, -2, 60, -90, 38])

    def test_welt(self):
        e = entwurf(teil("a", "wuerfel", UNTEN, "tisch", TISCH))
        e["tisch"] = {"art": "profil", "anschlag_hinten_mm": 50, "anschlag_links_mm": 200}
        text = bk.welt_text(e, bk.loese(e, TEILE_P.__getitem__), TEILE_P.__getitem__)
        self.assertIn('DEF BASISPLATTE Aufbau', text)
        self.assertIn('modell [ "../teile/basisplatte/netz.obj" ]', text)
        self.assertIn("translation 0 0 -0.7520", text)   # Boden 750 mm unter der Tischplatte
        self.assertNotIn("SOCKEL", text)
        self.assertEqual(text.count("USE NUTGRUND") + 1, 22)
        self.assertEqual(text.count("KAPPE"), 4)
        ohne = text.replace("%<", "").replace(">%", "")
        self.assertEqual(ohne.count("{"), ohne.count("}"))
        self.assertEqual(ohne.count("["), ohne.count("]"))
        with self.assertRaises(bk.BaukastenFehler):
            bk.welt_text(e, bk.loese(e, TEILE.__getitem__), TEILE.__getitem__)


class TestEinUndAusgaenge(unittest.TestCase):
    def test_ea_werte(self):
        e = {"ea": {"taster": [{"name": "  Start  ", "din": 21, "art": "oeffner", "modus": "rastend"}, {"din": 5}],
                    "leuchten": [{"name": "x" * 40, "dout": 32, "farbe": "lila", "umkehren": 1}]}}
        w = bk.ea_werte(e)
        self.assertEqual(w["taster"][0], {"name": "Start", "din": 21, "art": "oeffner", "modus": "rastend"})
        self.assertEqual(w["taster"][1], {"name": "DIN5", "din": 5, "art": "schliesser", "modus": "tastend"})
        self.assertEqual(w["leuchten"][0], {"name": "x" * 24, "dout": 32, "umkehren": True, "farbe": "gruen"})
        self.assertEqual(bk.ea_werte({}), {"taster": [], "leuchten": [], "sensoren": []})
        for falsch in ({"taster": [{"din": 0}]}, {"taster": [{"din": 65}]}, {"leuchten": [{"dout": "3"}]},
                       {"taster": [{"din": True}]}, {"taster": [{"din": 1}] * 9}, {"taster": "x"}, "x"):
            with self.assertRaises(bk.BaukastenFehler, msg=falsch):
                bk.ea_werte({"ea": falsch})


class TestSensorflaechen(unittest.TestCase):
    def sensor(self, an_punkt, versatz=(0, 0, 0), **sensor):
        return {"id": "platz1", "art": "sensor", "sensor": dict({"name": "Platz 1", "din": 25, "groesse_mm": [40, 30, 10]}, **sensor),
                "verbindung": {"punkt": {"art": "flaeche", "id": "unten"}, "an": "tisch", "an_punkt": an_punkt,
                               "versatz_mm": list(versatz), "drehung_grad": 0}}

    def test_quader_merkmale(self):
        m = bk.quader_merkmale(40, 30, 10)
        self.assertEqual(m["box"], [-20, -15, 0, 20, 15, 10])
        lage = bk.punkt_lage(m, {"art": "ecke", "id": "hinten-links", "flaeche": "unten"})
        self.assertEqual([round(v, 6) for v in lage.o], [-20, 15, 0])
        self.assertEqual([round(v, 6) for v in bk._t(lage.r)[2]], [0, 0, -1])   # z zeigt aus der Unterseite

    def test_sensor_auf_dem_tisch_und_in_der_welt(self):
        e = entwurf(teil("v", "vorrichtung", UNTEN, "tisch", TISCH, versatz=(100, 0, 0)),
                    self.sensor(TISCH, versatz=(300, 50, 0)))
        lagen = bk.loese(e, TEILE.__getitem__)
        self.assertEqual(huelle(lagen["platz1"], bk.quader_merkmale(40, 30, 10)), [530, 35, 0, 570, 65, 10])
        text = bk.welt_text(e, lagen, TEILE.__getitem__)
        self.assertIn('EXTERNPROTO "../protos/Sensorfeld.proto"', text)
        self.assertIn('DEF SENSOR_0 Sensorfeld {\n  translation 0.550000 0.050000 0.000000', text)
        self.assertIn("groesse 0.040000 0.030000 0.010000", text)
        self.assertEqual(bk.ea_sensoren(e), [{"name": "Platz 1", "groesse_mm": [40.0, 30.0, 10.0], "din": 25,
                                              "art": "schliesser", "typ": "belegt", "knoten": "SENSOR_0"}])
        # Sensor als Gegenstueck: ein Teil auf die Sensorflaeche stellen
        e["teile"].append(teil("w", "wuerfel", UNTEN, "platz1", {"art": "flaeche", "id": "oben"}, art="werkstueck"))
        self.assertEqual(huelle(bk.loese(e, TEILE.__getitem__)["w"], TEILE["wuerfel"]), [540, 40, 10, 560, 60, 30])

    def test_sensor_an_einer_nut(self):
        e = entwurf(self.sensor({"art": "kante", "id": "nut-5", "flaeche": "oben"}))
        e["tisch"] = {"art": "profil", "anschlag_hinten_mm": 50, "anschlag_links_mm": 200}
        self.assertEqual(huelle(bk.loese(e, TEILE.__getitem__)["platz1"], bk.quader_merkmale(40, 30, 10))[2], -2)

    def test_farbsensor(self):
        je = self.sensor(TISCH, typ="farbe", farben=[{"farbe": "rot", "din": 25}, {"farbe": "blau", "din": 26}])
        bits = self.sensor(TISCH, typ="farbe", ausgabe="bits", din=30, bits=2,
                           farben=[{"farbe": "rot", "code": 1}, {"farbe": "gruen", "code": 2}, {"farbe": "blau", "code": 3}])
        bits["id"] = "platz2"
        e = entwurf(je, bits)
        bk.loese(e, TEILE.__getitem__)
        s1, s2 = bk.ea_sensoren(e)
        self.assertEqual((s1["typ"], s1["ausgabe"], s1["farben"]), ("farbe", "je_farbe", je["sensor"]["farben"]))
        self.assertNotIn("din", s1)
        self.assertEqual((s2["ausgabe"], s2["din"], s2["bits"], s2["knoten"]), ("bits", 30, 2, "SENSOR_1"))
        self.assertEqual(bk.ea_werte({"ea": {"sensoren": [s1, s2]}})["sensoren"][1]["farben"][2], {"farbe": "blau", "code": 3})
        for falsch in ({"farben": []}, {"farben": [{"farbe": "lila", "din": 25}]},
                       {"farben": [{"farbe": "rot", "din": 25}, {"farbe": "rot", "din": 26}]},
                       {"farben": [{"farbe": "rot"}]},
                       {"ausgabe": "bits", "bits": 2, "farben": [{"farbe": "rot", "code": 4}]},
                       {"ausgabe": "bits", "bits": 2, "farben": [{"farbe": "rot", "code": 1}, {"farbe": "blau", "code": 1}]},
                       {"ausgabe": "bits", "bits": 5, "farben": [{"farbe": "rot", "code": 1}]},
                       {"ausgabe": "bits", "din": 64, "bits": 2, "farben": [{"farbe": "rot", "code": 1}]}):
            with self.assertRaises(bk.BaukastenFehler, msg=falsch):
                bk.loese(entwurf(self.sensor(TISCH, typ="farbe", **falsch)), TEILE.__getitem__)

    def test_ungueltige_sensoren(self):
        for falsch in ({"din": 0}, {"din": "3"}, {"groesse_mm": [40, 30]}, {"groesse_mm": [0, 30, 10]}):
            with self.assertRaises(bk.BaukastenFehler, msg=falsch):
                bk.loese(entwurf(self.sensor(TISCH, **falsch)), TEILE.__getitem__)
        w = bk.ea_werte({"ea": {"sensoren": [{"din": 25, "knoten": "SENSOR_0", "name": "A"},
                                             {"din": 25, "knoten": "kaputt"}, {"din": 99, "knoten": "SENSOR_1"}]}})
        self.assertEqual(w["sensoren"], [{"name": "A", "din": 25, "art": "schliesser", "typ": "belegt", "knoten": "SENSOR_0"}])


# Schunk EGP 25 wie in teile/schunk_egp25 (Schunk-CAD, mm): M4-Gewinde quer durch die Backentraeger
SCHUNK = {"box": [-13.5, 0, -9, 13.5, 80.9, 9], "flaechen": [], "kanten": [], "ecken": [], "bohrungen": [
    {"id": 454, "innen": True, "durchmesser": 3.24, "enden": [[-9.5, 76.9, 0], [-5.5, 76.9, 0]]},
    {"id": 331, "innen": True, "durchmesser": 3.24, "enden": [[9.5, 76.9, 0], [5.5, 76.9, 0]]}]}
# Schunk EGP 40 wie in teile/schunk_egp40: je Traeger zwei M3 in Greifrichtung (Traeger x 8..14 geschlossen)
SCHUNK40 = {"box": [-20, 0, -13, 20.8, 100.65, 13], "flaechen": [], "kanten": [], "ecken": [], "bohrungen": [
    {"id": 620, "innen": True, "durchmesser": 2.5, "enden": [[-8.0, 97.65, -5.0], [-14.0, 97.65, -5.0]]},
    {"id": 511, "innen": True, "durchmesser": 2.5, "enden": [[8.0, 97.65, 5.0], [14.0, 97.65, 5.0]]}]}
TEILE_B = dict(TEILE, schunk_egp25=SCHUNK, schunk_egp40=SCHUNK40)


class TestBackensatz(unittest.TestCase):
    def entwurf(self, bohrung, ende=0, versatz=(0, 0, 0)):
        return {"titel": "Test-Backen", "art": "backensatz", "teile": [
            {"id": "backe", "teil": "vorrichtung", "art": "aufbau", "verbindung": {
                "punkt": {"art": "bohrung", "id": 31, "ende": 0}, "an": "greifer",
                "an_punkt": {"art": "bohrung", "id": bohrung, "ende": ende}, "versatz_mm": list(versatz)}}]}

    def satz(self, e):
        return bk.backensatz_aus_entwurf("test", e, bk.loese(e, TEILE_B.__getitem__), TEILE_B.__getitem__)

    def test_greifer_im_werkzeugsystem(self):
        # M4 des linken Traegers: Schunk (-9.5, 76.9, 0) -> Werkzeug (-9.5, 0, 84.9), wie im iRC-Modell
        self.assertEqual([round(v, 6) for v in bk.GREIFER_LAGE.punkt([-9.5, 76.9, 0])], [-9.5, 0, 84.9])

    def test_backe_am_linken_traeger(self):
        e = self.entwurf(454)
        lage = bk.loese(e, TEILE_B.__getitem__)["backe"]
        # Bohrungsende der Backe (Mitte der Unterseite) liegt auf dem aeusseren Gewindeende, Achse entlang x
        self.assertEqual([round(v, 6) for v in lage.punkt([20, 20, 0])], [-9.5, 0, 84.9])
        self.assertEqual([round(v, 6) for v in lage.punkt([20, 20, 40])], [-49.5, 0, 84.9])
        s = self.satz(e)
        self.assertEqual((s["name"], s["basis"], s["teil"], s["tcp"], s["titel"]), ("test", "egp25", "vorrichtung", "spitze", "Test-Backen"))
        self.assertEqual([round(v, 4) for v in bk.Lage(s["matrix"], s["verschiebung_mm"]).punkt([20, 20, 40])], [-49.5, 0, 84.9])

    def test_egp40_tcp_und_hub(self):
        e = dict(self.entwurf(620, ende=1), basis="egp40", tcp={"bezug": "mitte", "mm": -2.5}, hub_mm="5,5".replace(",", "."))
        lage = bk.loese(e, TEILE_B.__getitem__)["backe"]
        # Bohrungsende der Backe auf dem aeusseren Ende des M3 am linken Traeger: Schunk (-14, 97.65, -5) -> (-14, 5, 105.65)
        self.assertEqual([round(v, 6) for v in lage.punkt([20, 20, 0])], [-14.0, 5.0, 105.65])
        s = self.satz(e)
        self.assertEqual((s["basis"], s["tcp"], s["tcp_versatz_mm"], s["hub_mm"]), ("egp40", "mitte", -2.5, 5.5))
        self.assertEqual(bk.tcp_werte({"tcp": {"bezug": "flansch", "mm": 131.24}}), {"tcp": 131.24, "tcp_versatz_mm": 0})
        self.assertEqual(bk.tcp_werte({}), {"tcp": "spitze", "tcp_versatz_mm": 0})
        for falsch in ({"bezug": "irgendwo"}, {"bezug": "flansch", "mm": 0}, {"bezug": "spitze", "mm": "viel"},
                       {"bezug": "mitte", "mm": 500}):
            with self.assertRaises(bk.BaukastenFehler, msg=falsch):
                bk.tcp_werte({"tcp": falsch})
        with self.assertRaises(bk.BaukastenFehler):
            self.satz(dict(e, hub_mm=0))
        with self.assertRaises(bk.BaukastenFehler):
            bk.loese(dict(e, basis="egp64"), TEILE_B.__getitem__)

    def test_backe_am_rechten_traeger_wird_zur_linken(self):
        links, rechts = self.satz(self.entwurf(454)), self.satz(self.entwurf(331))
        for a, b in zip(sum(links["matrix"], []) + links["verschiebung_mm"], sum(rechts["matrix"], []) + rechts["verschiebung_mm"]):
            self.assertAlmostEqual(a, b, places=6)

    def test_fehler(self):
        e = self.entwurf(454)
        for name in ("", "Robi", "1abc", "a" * 21, "mit-strich"):
            with self.assertRaises(bk.BaukastenFehler, msg=name):
                bk.backensatz_aus_entwurf(name, e, bk.loese(e, TEILE_B.__getitem__), TEILE_B.__getitem__)
        zwei = self.entwurf(454)
        zwei["teile"].append(dict(zwei["teile"][0], id="backe2"))
        with self.assertRaises(bk.BaukastenFehler):
            self.satz(zwei)
        am_tisch = self.entwurf(454)
        am_tisch["teile"][0]["verbindung"].update(an="tisch", an_punkt={"art": "flaeche", "id": "oben"})
        with self.assertRaises(bk.BaukastenFehler):          # im Backensatz gibt es keinen Tisch
            bk.loese(am_tisch, TEILE_B.__getitem__)
        welt = self.entwurf(454)
        del welt["art"]
        with self.assertRaises(bk.BaukastenFehler):          # in einer Welt gibt es keinen Greifer als Bezug
            bk.loese(welt, TEILE_B.__getitem__)


def abstand_strecke(p, a, b):
    ab = [b[i] - a[i] for i in range(3)]
    t = max(0.0, min(1.0, sum((p[i] - a[i]) * ab[i] for i in range(3)) / sum(v * v for v in ab)))
    return math.dist(p, [a[i] + ab[i] * t for i in range(3)])


def umnummeriert(m, plus=100):
    """Dieselben Merkmale mit neuen Nummern, wie nach erneutem Einlesen in gmsh."""
    n = json.loads(json.dumps(m))
    for liste in ("flaechen", "bohrungen", "kanten", "ecken"):
        for e in n[liste]:
            e["id"] += plus
    for k in n["kanten"]:
        k["flaechen"] = [f + plus for f in k["flaechen"]]
    for e in n["ecken"]:
        for s in e["flaechen"]:
            s["flaeche"] += plus
    return n


class TestTeilErsetzen(unittest.TestCase):
    def entwurf(self):
        return entwurf(teil("v", "vorrichtung", UNTEN, "tisch", TISCH, versatz=(100, 0, 0)),
                       teil("s", "stift", {"art": "bohrung", "id": 41, "ende": 0}, "v",
                            {"art": "bohrung", "id": 31, "ende": 1}, art="werkstueck"),
                       teil("w", "wuerfel", {"art": "kante", "id": 11}, "v", {"art": "ecke", "id": 21}))

    def test_neue_nummern_gleiche_lage(self):
        alt = TEILE["vorrichtung"]
        neu = umnummeriert(alt)
        e = self.entwurf()
        e2, offen, verschoben = bk.entwurf_umstellen(e, "vorrichtung", alt, neu)
        self.assertEqual((offen, verschoben), ([], []))
        v = {t["id"]: t["verbindung"] for t in e2["teile"]}
        self.assertEqual(v["v"]["punkt"], {"art": "flaeche", "id": 101})
        self.assertEqual(v["s"]["an_punkt"], {"art": "bohrung", "id": 131, "ende": 1})
        self.assertEqual(v["w"]["an_punkt"], {"art": "ecke", "id": 121, "flaeche": 101})
        self.assertEqual(v["s"]["punkt"], e["teile"][1]["verbindung"]["punkt"])   # anderes Teil: unveraendert
        self.assertEqual(e["teile"][0]["verbindung"]["punkt"], UNTEN)            # Original bleibt
        vorher = bk.loese(e, TEILE.__getitem__)
        nachher = bk.loese(e2, dict(TEILE, vorrichtung=neu).__getitem__)
        for ident in vorher:
            self.assertEqual([round(x, 6) for x in vorher[ident].o], [round(x, 6) for x in nachher[ident].o], ident)

    def test_geaendertes_teil(self):
        # groesser: Unterseite in derselben Ebene (Mitte wandert), Bohrung an anderer Stelle
        alt, neu = TEILE["vorrichtung"], umnummeriert(wuerfel(60, bohrung=8))
        e2, offen, verschoben = bk.entwurf_umstellen(self.entwurf(), "vorrichtung", alt, neu)
        self.assertEqual(offen, ["s an v Bohrung 31"])
        self.assertEqual(verschoben, [("v Fläche 1", 14.14)])   # Ecke 21 im Ursprung bleibt, wo sie war
        v = {t["id"]: t["verbindung"] for t in e2["teile"]}
        self.assertIsNone(v["s"]["an_punkt"])
        self.assertEqual(v["w"]["an_punkt"], {"art": "ecke", "id": 121, "flaeche": 101})   # Ecke im Ursprung bleibt
        # gedreht (Unterseite zeigt nach oben): nichts passt
        gekippt = umnummeriert(alt)
        for f in gekippt["flaechen"]:
            f["normale"] = [-x for x in f["normale"]]
        self.assertEqual(bk.punkt_wiederfinden(alt, gekippt, UNTEN), (None, None))
        # die andere Oeffnung derselben Bohrung zaehlt nicht
        p, d = bk.punkt_wiederfinden(alt, umnummeriert(alt), {"art": "bohrung", "id": 31})
        self.assertEqual((p, d), ({"art": "bohrung", "id": 131, "ende": 0}, 0.0))

    def test_verweise_im_backensatz(self):
        e = {"art": "backensatz", "basis": "egp40", "teile": [
            teil("backe", "robi2_backe", {"art": "bohrung", "id": 7}, "greifer", {"art": "bohrung", "id": 620})]}
        self.assertEqual([f for _t, f in bk.teil_verweise(e, "schunk_egp40")], ["an_punkt"])
        self.assertEqual([f for _t, f in bk.teil_verweise(e, "robi2_backe")], ["punkt"])
        self.assertEqual(bk.teil_verweise(e, "schunk_egp25"), [])


class TestWelt(unittest.TestCase):
    def test_welt_text(self):
        e = entwurf(teil("v", "vorrichtung", UNTEN, "tisch", TISCH, versatz=(100, 0, 0)),
                    teil("s", "stift", {"art": "bohrung", "id": 41, "ende": 0}, "v",
                         {"art": "bohrung", "id": 31, "ende": 1}, art="werkstueck"))
        e["greifer"] = "egp25_robi2"
        lagen = bk.loese(e, TEILE.__getitem__)
        text = bk.welt_text(e, lagen, TEILE.__getitem__, "test.baukasten.json")
        self.assertIn('greifer "egp25_robi2"', text)
        self.assertIn('Aufbau {\n  translation 0.330000 -0.020000 0.000000', text)
        self.assertIn('modell [ "../teile/vorrichtung/netz.obj" ]', text)
        # Werkstueck: Ursprung = Mitte des Huellquaders, Netz um die Mitte zurueckversetzt
        self.assertIn("Werkstueck {\n  translation 0.350000 0.000000 0.055000", text)
        self.assertIn("modellversatz -0.000000 -0.000000 -0.015000", text)
        self.assertIn("size 0.008000 0.008000 0.030000", text)
        ohne = text.replace("%<", "").replace(">%", "")
        self.assertEqual(ohne.count("{"), ohne.count("}"))
        self.assertEqual(ohne.count("["), ohne.count("]"))


@unittest.skipUnless(HAT_GMSH, "gmsh nicht installiert (laeuft im Container)")
class TestTeilePipeline(unittest.TestCase):
    def test_klotz_mit_bohrung(self):
        import gmsh
        import teile
        with tempfile.TemporaryDirectory() as tmp:
            step = os.path.join(tmp, "klotz.step")
            gmsh.initialize()
            try:
                gmsh.option.setNumber("General.Terminal", 0)
                gmsh.model.add("klotz")
                kasten = gmsh.model.occ.addBox(0, 0, 0, 40, 30, 20)
                loch = gmsh.model.occ.addCylinder(20, 15, -1, 0, 0, 22, 4)
                gmsh.model.occ.cut([(3, kasten)], [(3, loch)])
                gmsh.model.occ.synchronize()
                gmsh.write(step)
            finally:
                gmsh.finalize()
            t = teile.einlesen(step, tmp, "klotz")
            self.assertEqual(t["groesse_mm"], [40.0, 30.0, 20.0])
            self.assertEqual(len(t["flaechen"]), 6)
            self.assertEqual(len(t["bohrungen"]), 1)
            b = t["bohrungen"][0]
            self.assertTrue(b["innen"])
            self.assertAlmostEqual(b["durchmesser"], 8, places=2)
            self.assertEqual(sorted(round(e[2], 3) for e in b["enden"]), [0, 20])
            self.assertEqual(len(t["kanten"]), 12)
            self.assertEqual(len(t["ecken"]), 8)
            self.assertTrue(all(len(e["flaechen"]) == 3 for e in t["ecken"]))
            unten = next(f for f in t["flaechen"] if f["mitte"][2] == 0)
            self.assertEqual(unten["normale"], [0, 0, -1])   # Normalen zeigen nach aussen
            for name in ("netz.obj", "netz.json", "quelle.step", "teil.json"):
                self.assertTrue(os.path.exists(os.path.join(tmp, "klotz", name)), name)
            # mit echten Merkmalen auf den Tisch stellen: Ecke 10/10 mm von der Tischecke x-y-
            lade = bk.teile_lader(tmp)
            ecke = next(e for e in t["ecken"] if e["punkt"] == [0, 0, 0])
            e = entwurf(teil("k", "klotz", {"art": "ecke", "id": ecke["id"], "flaeche": unten["id"]}, "tisch",
                             {"art": "ecke", "id": "x-y-"}, versatz=(10, 10, 0)))
            lage = bk.loese(e, lade)["k"]
            h = huelle(lage, t)
            self.assertEqual((h[2], h[5]), (0, 20))
            self.assertEqual(sorted([round(h[0] + 250, 6), round(h[1] + 400, 6)]), [10, 10])   # je 10 mm von beiden Kanten

    def test_falscher_name_und_keine_step_datei(self):
        import teile
        with tempfile.TemporaryDirectory() as tmp:
            datei = os.path.join(tmp, "x.step")
            with open(datei, "w") as f:
                f.write("kein step")
            for name in ("Gross", "../x", ""):
                with self.assertRaises(teile.TeilFehler):
                    teile.einlesen(datei, tmp, name)
            with self.assertRaises(teile.TeilFehler):
                teile.einlesen(datei, tmp, "x")


if __name__ == "__main__":
    unittest.main()
