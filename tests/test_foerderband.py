"""Tests ohne Webots: Foerderband (Stufe FB1) - Foerdern, Stau, Anschlag, Taster, Fehler vor+zurueck.

  python -m unittest tests.test_foerderband -v
"""

import json
import math
import os
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "controllers", "irc_bridge"))
sys.modules.setdefault("controller", types.SimpleNamespace(Supervisor=object))

import crash  # noqa: E402
import ea  # noqa: E402
import foerderband as fb  # noqa: E402

EINS = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
DREH_90 = [0.0, -1.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]   # um z: Band-x zeigt in Welt-y


class Feld:
    def __init__(self, wert):
        self.wert = wert

    def getSFFloat(self):
        return self.wert

    getSFString = getSFVec3f = getSFColor = getSFRotation = getSFFloat

    def setSFVec3f(self, v):
        self.wert = list(v)

    setSFColor = setSFRotation = setSFVec3f


class Knoten:
    zaehler = 0

    def __init__(self, felder, pos=(0.0, 0.0, 0.0), dreh=EINS, typ="Werkstueck"):
        Knoten.zaehler += 1
        self.id, self.typ, self.dreh = Knoten.zaehler, typ, list(dreh)
        self.felder = {k: Feld(v) for k, v in felder.items()}
        self.felder.setdefault("translation", Feld(list(pos)))
        self.zurueckgesetzt = self.geschwindigkeit = None

    def getField(self, name):
        return self.felder.get(name)

    def getPosition(self):
        return list(self.felder["translation"].wert)

    def getOrientation(self):
        return list(self.dreh)

    def getId(self):
        return self.id

    def getTypeName(self):
        return self.typ

    def resetPhysics(self):
        self.zurueckgesetzt = True

    def setVelocity(self, v):
        self.geschwindigkeit = list(v)


def band_knoten(pos=(0.0, 0.0, 0.0), dreh=EINS, links="anschlag", rechts="anschlag"):
    return Knoten({"name": "band1", "laenge": 0.6, "breite": 0.08, "hoehe": 0.07, "ende_links": links,
                   "ende_rechts": rechts, "taster_links": 0.025, "taster_rechts": 0.025, "taster_hoehe": 0.008,
                   "led_links": [0, 0, 0], "led_rechts": [0, 0, 0], "lampe": [0, 0, 0]}, pos, dreh, "Foerderband")


def teil(pos, size=(0.02, 0.02, 0.02), name="teil"):
    return Knoten({"size": list(size), "name": name, "rotation": [0.0, 0.0, 1.0, 0.0]}, pos)


CFG = {"name": "Band 1", "knoten": "BAND_0", "dout_vor": 21, "dout_zurueck": 22, "geschwindigkeit_mm_s": 60,
       "taster": [{"din": 25, "art": "schliesser"}, {"din": 26, "art": "oeffner"}]}
VOR, ZURUECK = 1 << 20, 1 << 21   # iRC-Anzeige DOut21/22 = Bit 20/21


class TestRechnung(unittest.TestCase):
    def test_foerdern_frei(self):
        self.assertEqual(fb.foerdern([(0.0, 0.0, 0.01, 0.01)], 1, 0.005, 0.6, True), [0.005])
        self.assertEqual(fb.foerdern([(0.0, 0.0, 0.01, 0.01)], -1, 0.005, 0.6, True), [-0.005])

    def test_anschlag_haelt_an(self):
        neu = fb.foerdern([(0.285, 0.0, 0.01, 0.01)], 1, 0.05, 0.6, True)
        self.assertAlmostEqual(neu[0], 0.3 - fb.LUECKE - 0.01)
        # ohne Anschlag faehrt es ueber das Ende hinaus
        self.assertAlmostEqual(fb.foerdern([(0.285, 0.0, 0.01, 0.01)], 1, 0.05, 0.6, False)[0], 0.335)

    def test_stau_hintereinander(self):
        teile = [(0.2, 0.0, 0.01, 0.01), (0.289, 0.0, 0.01, 0.01), (0.0, 0.0, 0.01, 0.01)]   # Reihenfolge egal
        neu = fb.foerdern(teile, 1, 0.1, 0.6, True)
        vorn = 0.3 - fb.LUECKE - 0.01
        self.assertAlmostEqual(neu[1], vorn)
        self.assertAlmostEqual(neu[0], vorn - 0.02 - fb.LUECKE)
        self.assertAlmostEqual(neu[2], 0.1)   # hat noch Platz

    def test_nebeneinander_blockiert_nicht(self):
        neu = fb.foerdern([(0.289, 0.02, 0.01, 0.01), (0.28, -0.02, 0.01, 0.01)], 1, 0.1, 0.6, True)
        self.assertAlmostEqual(neu[1], 0.3 - fb.LUECKE - 0.01)

    def test_nie_gegen_die_laufrichtung(self):
        # steckt schon im Anschlag (z.B. so abgelegt): bleibt, wird nicht zurueckgeschoben
        self.assertEqual(fb.foerdern([(0.295, 0.0, 0.01, 0.01)], 1, 0.01, 0.6, True), [0.295])
        self.assertEqual(fb.foerdern([(-0.295, 0.0, 0.01, 0.01)], -1, 0.01, 0.6, True), [-0.295])

    def test_auf_gurt(self):
        halb = [0.01, 0.01, 0.01]
        self.assertTrue(fb.auf_gurt([0.0, 0.0, 0.08], halb, 0.6, 0.08, 0.07))
        self.assertFalse(fb.auf_gurt([0.0, 0.0, 0.09], halb, 0.6, 0.08, 0.07))     # schwebt (gegriffen)
        self.assertFalse(fb.auf_gurt([0.31, 0.0, 0.08], halb, 0.6, 0.08, 0.07))    # Mitte ueber dem Ende
        self.assertFalse(fb.auf_gurt([0.0, 0.05, 0.08], halb, 0.6, 0.08, 0.07))    # neben dem Gurt

    def test_taster(self):
        halb = [0.01, 0.01, 0.01]
        self.assertTrue(fb.taster_belegt([0.27, 0.0, 0.08], halb, 0.275, 0.078, 0.08))
        self.assertFalse(fb.taster_belegt([0.24, 0.0, 0.08], halb, 0.275, 0.078, 0.08))
        flach = [0.01, 0.01, 0.002]   # 4 mm hoch: unter dem Strahl (8 mm ueber dem Gurt)
        self.assertFalse(fb.taster_belegt([0.275, 0.0, 0.072], flach, 0.275, 0.078, 0.08))

    def test_halbe_im_gedrehten_band(self):
        halb = fb.halbe_im_band(DREH_90, EINS, [0.04, 0.02, 0.01])
        self.assertEqual([round(v, 6) for v in halb], [0.01, 0.02, 0.005])

    def test_steuerung(self):
        s = fb.Steuerung(20, 21)
        self.assertEqual(s.richtung(VOR, 0.0), 1)
        self.assertEqual(s.richtung(ZURUECK, 0.1), -1)
        self.assertEqual(s.richtung(0, 0.2), 0)
        self.assertEqual(s.richtung(None, 0.3), 0)
        self.assertEqual(s.richtung(VOR | ZURUECK, 1.0), 0)
        self.assertFalse(s.fehler)                        # kurze Ueberlappung beim Umschalten
        self.assertEqual(s.richtung(VOR | ZURUECK, 1.0 + fb.FEHLER_NACH_S), 0)
        self.assertTrue(s.fehler)
        self.assertEqual(s.richtung(VOR, 1.5), 1)
        self.assertFalse(s.fehler)

    def test_lade(self):
        gut = dict(CFG)
        liste = [gut, {"knoten": "X"}, "quatsch", dict(gut, knoten="B"), dict(gut, knoten="C")]
        self.assertEqual([b["knoten"] for b in fb.lade(liste)], ["BAND_0", "B"])
        self.assertEqual(fb.lade(None), [])


class TestBandInWebots(unittest.TestCase):
    def setUp(self):
        self.meldungen = []

    def band(self, knoten=None, cfg=CFG):
        return fb.Band(cfg, knoten or band_knoten(), -1, -1, log=self.meldungen.append)

    def test_foerdert_teile_auf_dem_gurt(self):
        b = self.band()
        a, schwebt, daneben = teil([0.0, 0.0, 0.08]), teil([0.1, 0.0, 0.2]), teil([0.0, 0.2, 0.01])
        for _ in range(10):
            b.schritt([a, schwebt, daneben], 0.016, VOR, 0.0, set())
        self.assertAlmostEqual(a.getPosition()[0], 10 * 0.016 * 0.06)
        self.assertTrue(a.zurueckgesetzt)
        self.assertEqual(schwebt.getPosition(), [0.1, 0.0, 0.2])
        self.assertEqual(daneben.getPosition(), [0.0, 0.2, 0.01])
        self.assertEqual(b.knoten.getField("lampe").wert, fb.LAMPE["laeuft"])

    def test_bewegte_teile_und_log(self):
        knoten = band_knoten()
        alle = fb.Baender.__new__(fb.Baender)
        alle.liste = [fb.Band(CFG, knoten, -1, -1, log=self.meldungen.append)]
        a, liegt = teil([0.0, 0.0, 0.08]), teil([0.0, 0.3, 0.01])
        alle.schritt([a, liegt], 0.016, VOR, 10.0)
        self.assertEqual(alle.kuerzlich_bewegt(11.0), {a.getId()})
        self.assertEqual(alle.kuerzlich_bewegt(12.0), set())
        # Umschalten mit kurzer Ueberlappung: kein "steht" dazwischen im Protokoll
        alle.schritt([a], 0.016, VOR | ZURUECK, 12.0)
        alle.schritt([a], 0.016, ZURUECK, 12.02)
        self.assertEqual(self.meldungen, ["Foerderband Band 1: faehrt vor", "Foerderband Band 1: faehrt zurueck"])

    def test_ausgenommen_gegriffen(self):
        b = self.band()
        a = teil([0.0, 0.0, 0.08])
        b.schritt([a], 0.016, VOR, 0.0, {a.getId()})
        self.assertEqual(a.getPosition(), [0.0, 0.0, 0.08])

    def test_gedrehtes_band_foerdert_in_welt_y(self):
        b = self.band(band_knoten(pos=(0.35, 0.0, 0.0), dreh=DREH_90))
        a = teil([0.35, 0.0, 0.08])
        b.schritt([a], 0.5, ZURUECK, 0.0, set())
        p = a.getPosition()
        self.assertAlmostEqual(p[0], 0.35)
        self.assertAlmostEqual(p[1], -0.03)

    def test_offenes_ende_gibt_schwung(self):
        b = self.band(band_knoten(rechts="offen"))
        a = teil([0.299, 0.0, 0.08])
        b.schritt([a], 0.1, VOR, 0.0, set())
        self.assertGreater(a.getPosition()[0], 0.3)
        self.assertAlmostEqual(a.geschwindigkeit[0], 0.06)
        self.assertIn("Foerderband Band 1: teil faellt am rechten Ende herunter", self.meldungen)
        # danach liegt die Mitte nicht mehr ueber dem Gurt: das Band schiebt nicht weiter, die Physik uebernimmt
        x = a.getPosition()[0]
        b.schritt([a], 0.1, VOR, 0.0, set())
        self.assertEqual(a.getPosition()[0], x)

    def test_taster_und_signale(self):
        b = self.band()
        a = teil([0.27, 0.0, 0.08])
        b.taster_pruefen([a])
        self.assertEqual(b.taster, [False, True])
        # links Schliesser frei -> 0, rechts Oeffner belegt -> 0; CRI-Nummer = Anzeige - 1
        self.assertEqual(b.signale(), {24: False, 25: False})
        b.taster_pruefen([])
        self.assertEqual(b.signale(), {24: False, 25: True})
        self.assertEqual(self.meldungen, ["Foerderband Band 1: Taster rechts belegt", "Foerderband Band 1: Taster rechts frei"])
        self.assertEqual(b.knoten.getField("led_rechts").wert, fb.LED_AUS)

    def test_taster_ohne_eingang(self):
        b = self.band(cfg=dict(CFG, taster=[None, {"din": 26}]))
        b.taster_pruefen([teil([-0.27, 0.0, 0.08])])
        self.assertEqual(b.taster, [True, False])
        self.assertEqual(b.signale(), {25: False})

    def test_fehler_als_treffer(self):
        b = self.band()
        a = teil([0.0, 0.0, 0.08])
        b.schritt([a], 0.016, VOR | ZURUECK, 0.0, set())
        b.schritt([a], 0.016, VOR | ZURUECK, 0.1, set())
        self.assertEqual(a.getPosition(), [0.0, 0.0, 0.08])   # Band steht
        t = b.treffer()
        self.assertEqual(len(t), 1)
        self.assertEqual(t[0]["art"], "band")
        self.assertIn("DOut21", t[0]["text"])
        self.assertEqual(b.knoten.getField("lampe").wert, fb.LAMPE["fehler"])
        self.assertIn("FEHLER", b.zustand_text())
        # Crash-Stopp wie bei einer Kollision, Status nennt die Art (fuer die Anzeige im Browser)
        z = crash.CrashZustand("stopp", log=lambda *a: None)
        self.assertTrue(z.kollision(t, 0.0))
        z.ergebnis("gestoppt", 0.0, "haupt")
        self.assertEqual(z.status(lambda p: p)["art"], "band")
        self.assertTrue(z.quittieren(t))
        b.schritt([a], 0.016, VOR, 0.2, set())   # einen Ausgang aus: Band laeuft wieder, kein Treffer mehr
        self.assertEqual(b.treffer(), [])
        for i in range(crash.FREI_NACH):
            z.kollision(b.treffer(), 0.3 + i)
        self.assertEqual(z.zustand, "frei")

    def test_baender_sucht_knoten(self):
        knoten = band_knoten()
        robot = types.SimpleNamespace(getFromDef=lambda name: {"BAND_0": knoten, "SENSOR_0": teil([0, 0, 0])}.get(name))
        meldungen = []
        alle = fb.Baender([CFG, dict(CFG, knoten="SENSOR_0")], robot, log=meldungen.append)   # kein Foerderband
        self.assertEqual(len(alle.liste), 1)
        self.assertEqual(len(fb.Baender([dict(CFG, knoten="FEHLT")], robot, log=meldungen.append).liste), 0)
        self.assertEqual(len(meldungen), 2)
        self.assertEqual(alle.status(), [{"name": "Band 1", "lauf": 0, "fehler": False, "taster": [False, False]}])
        self.assertEqual(alle.label(), "Foerderband Band 1: steht")


class TestKonfiguration(unittest.TestCase):
    def test_lade_ea_mit_baendern(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "w.json"), "w", encoding="utf-8") as f:
                json.dump({"ea": {"baender": [CFG]}}, f)
            self.assertEqual(ea.lade_ea(os.path.join(tmp, "w.wbt"))["baender"], [CFG])

    def test_proto_felder_passen_zum_controller(self):
        with open(os.path.join(ROOT, "protos", "Foerderband.proto"), encoding="utf-8") as f:
            text = f.read()
        for feld in ("laenge", "breite", "hoehe", "ende_links", "ende_rechts", "taster_links", "taster_rechts",
                     "taster_hoehe", "led_links", "led_rechts", "lampe"):
            self.assertRegex(text, r"field\s+\w+(\{[^}]*\})?\s+%s\s" % feld)

    def test_main_bindet_baender_ein(self):
        with open(os.path.join(ROOT, "controllers", "irc_bridge", "irc_bridge.py"), encoding="utf-8") as f:
            text = f.read()
        for stelle in ("foerderband.Baender(", "baender.schritt(", "baender.taster_pruefen(", "baender.signale()",
                       "baender.treffer()", '"baender": baender.status(jetzt)',
                       "baender.kuerzlich_bewegt(jetzt)", "baender.lagerboxen(", "baender.vorrat_parken()",
                       'worte[0] == "band_nachlegen"', "baender.nachlegen("):
            self.assertIn(stelle, text)
        self.assertTrue(math.isclose(fb.GESCHWINDIGKEIT_MM_S, 60.0))

    def test_proto_lagerbox_wie_controller(self):
        with open(os.path.join(ROOT, "protos", "Foerderband.proto"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn('"lagerbox"} ende_links', text)
        self.assertIn("bx0 = L / 2 + 0.003, bxl = 0.12, byb = 0.14, hb = Math.max(0.03, H - 0.025)", text)
        self.assertIn("const PARK_Z = %d;" % fb.PARK_Z, text)
        xm, hx, hy, rand = fb.lagerbox_mass(0.6, 0.07)
        self.assertAlmostEqual(xm, 0.3 + 0.003 + 0.06)              # Mitte der Box (aussen 120 mm lang)
        self.assertAlmostEqual(hx, 0.06 - 0.003)                    # innen: minus Wand
        self.assertAlmostEqual(hy, 0.07 - 0.003)
        self.assertAlmostEqual(rand, 0.045)


class TestLagerboxUndVorrat(unittest.TestCase):
    def setUp(self):
        self.meldungen = []
        self.knoten = band_knoten(links="anschlag", rechts="lagerbox")
        self.band = fb.Band(dict(CFG), self.knoten, -1, -1, log=self.meldungen.append)
        self.vorrat = [("rot", teil([1.0, 1.0, 0.01], name="rot_0")), ("rot", teil([1.1, 1.0, 0.01], name="rot_1")),
                       ("blau", teil([1.2, 1.0, 0.01], size=(0.03, 0.02, 0.04), name="blau_0"))]
        self.band.vorrat_setzen(self.vorrat)
        self.band.zufall.seed(1)

    def sichtbar(self, extra=()):
        return [n for _s, n in self.vorrat if n.getId() not in self.band.geparkt] + list(extra)

    def test_rechnung(self):
        self.assertTrue(fb.in_lagerbox([0.363, 0.0, 0.02], 1, 0.6, 0.07))
        self.assertFalse(fb.in_lagerbox([0.363, 0.0, 0.05], 1, 0.6, 0.07))    # noch ueber dem Rand
        self.assertFalse(fb.in_lagerbox([0.363, 0.0, 0.02], -1, 0.6, 0.07))   # links ist keine
        self.assertTrue(fb.in_lagerbox([-0.363, 0.06, 0.0], -1, 0.6, 0.07))
        self.assertEqual(fb.parkplatz(0), [-0.4, -0.4])
        self.assertEqual([round(v, 6) for v in fb.parkplatz(12)], [-0.24, -0.32])
        self.assertEqual([round(v, 6) for v in fb.achse_winkel_matrix([0, 0, 1, math.pi / 2])], DREH_90)
        halb = [0.01, 0.01, 0.01]
        self.assertFalse(fb.einlauf_frei([([-0.28, 0.0, 0.08], halb)], -0.3, -0.27, 0.08, 0.07))
        self.assertTrue(fb.einlauf_frei([([-0.2, 0.0, 0.08], halb)], -0.3, -0.27, 0.08, 0.07))     # weiter vorn
        self.assertTrue(fb.einlauf_frei([([-0.28, 0.0, 0.2], halb)], -0.3, -0.27, 0.08, 0.07))     # gegriffen, hoch

    def test_vorrat_parken_und_nachlegen(self):
        start = self.band.vorrat_parken()
        self.assertEqual(len(start), 3)
        self.assertEqual(self.band.geparkt, {n.getId() for _s, n in self.vorrat})
        for _s, n in self.vorrat:   # unsichtbar auf der Ablage, jedes auf eigenem Platz
            self.assertLess(n.getPosition()[2], fb.PARK_Z + 0.05)
            self.assertEqual(start[n.getId()][0], n.getPosition())
        self.assertEqual(len({tuple(n.getPosition()) for _s, n in self.vorrat}), 3)
        self.assertEqual(self.band.status()["vorrat"], 3)
        self.assertTrue(self.band.nachlegen(self.sichtbar(), 0.0))
        neu = self.sichtbar()
        self.assertEqual(len(neu), 1)
        p = neu[0].getPosition()
        halb_x = 0.015 if neu[0].getField("name").getSFString() == "blau_0" else 0.01
        self.assertAlmostEqual(p[0], -0.3 + fb.LUECKE + halb_x)      # links am Anschlag
        self.assertAlmostEqual(p[1], 0.0)
        self.assertGreater(p[2], 0.07)
        self.assertTrue(fb.auf_gurt(p, [halb_x, 0.01, p[2] - 0.0705], 0.6, 0.08, 0.07))
        # gleich noch einmal: Einlauf belegt, Meldung fuer die Anzeige
        self.assertFalse(self.band.nachlegen(self.sichtbar(), 1.0))
        self.assertEqual(self.band.status(2.0)["meldung"], "Einlauf belegt")
        self.assertNotIn("meldung", self.band.status(1.0 + fb.MELDUNG_S + 0.1))
        neu[0].getField("translation").setSFVec3f([0.1, 0.0, 0.08])   # weitergefoerdert
        self.assertTrue(self.band.nachlegen(self.sichtbar(), 2.0))
        self.assertEqual(self.band.status()["vorrat"], 1)

    def test_beide_sorten_kommen(self):
        self.band.vorrat_parken()
        sorten = set()
        for i in range(3):
            self.assertTrue(self.band.nachlegen(self.sichtbar(), float(i)))
            for n in self.sichtbar():   # vom Einlauf wegraeumen
                n.getField("translation").setSFVec3f([0.5 + 0.1 * i, 0.5, 0.01]) if n.getPosition()[0] < -0.2 else None
            sorten |= {s for s, n in self.vorrat if n.getId() not in self.band.geparkt}
        self.assertEqual(sorten, {"rot", "blau"})
        self.assertFalse(self.band.nachlegen(self.sichtbar(), 5.0))
        self.assertIn("Vorrat leer", self.band.meldung)

    def test_lagerbox_zaehlt_und_parkt(self):
        self.band.vorrat_parken()
        self.band.nachlegen(self.sichtbar(), 0.0)
        drin = self.sichtbar()[0]
        drin.getField("translation").setSFVec3f([0.36, 0.01, 0.015])   # in die Box gefallen
        fremd = teil([0.36, -0.03, 0.03], name="fremd")                # kein Vorratsteil: auch zaehlen
        daneben = teil([0.5, 0.0, 0.01], name="daneben")
        self.band.lagerboxen([drin, fremd, daneben])
        self.assertEqual(self.band.im_box, [0, 2])
        self.assertIn(drin.getId(), self.band.geparkt)
        self.assertIn(fremd.getId(), self.band.geparkt)
        self.assertNotIn(daneben.getId(), self.band.geparkt)
        self.assertLess(fremd.getPosition()[2], -19)
        self.assertEqual(self.band.status()["box"], [None, 2])
        self.assertEqual(self.band.status()["vorrat"], 3)
        self.assertIn("Foerderband Band 1: fremd in der Lagerbox rechts (2)", self.meldungen)
        # gegriffen ueber der Box zaehlt nicht
        oben = teil([0.36, 0.0, 0.03], name="oben")
        self.band.lagerboxen([oben], {oben.getId()})
        self.assertEqual(self.band.im_box, [0, 2])
        # "Teile zurueck": Boxen leer, Vorrat wieder komplett geparkt
        self.band.vorrat_parken()
        self.assertEqual(self.band.im_box, [0, 0])
        self.assertNotIn(fremd.getId(), self.band.geparkt)   # fremde Teile legt "Teile zurueck" selbst zurueck
        self.assertIn("Box rechts: 0", fb.Baender.label(types.SimpleNamespace(liste=[self.band])))

    def test_baender_mit_vorrat_aus_der_welt(self):
        knoten = band_knoten()
        teile = {"V_0": teil([1, 1, 0], name="v0"), "V_1": teil([1, 1, 0], name="v1"), "SENSOR_0": band_knoten()}
        robot = types.SimpleNamespace(getFromDef=lambda name: dict(teile, BAND_0=knoten).get(name))
        cfg = dict(CFG, nachlegen={"vorrat": [{"sorte": "rot", "knoten": ["V_0", "FEHLT"]}, {"sorte": "gruen",
                                                                                           "knoten": ["V_1", "SENSOR_0"]},
                                              {"knoten": ["V_0"]}, "quatsch"]})
        meldungen = []
        alle = fb.Baender([cfg], robot, log=meldungen.append)
        self.assertEqual([(s, n.getField("name").getSFString()) for s, n, _r in alle.liste[0].vorrat],
                         [("rot", "v0"), ("gruen", "v1")])
        self.assertEqual(len(meldungen), 2)
        self.assertEqual(len(alle.vorrat_parken()), 2)
        self.assertEqual(alle.geparkt(), {teile["V_0"].getId(), teile["V_1"].getId()})
        self.assertTrue(alle.nachlegen(0, [], 0.0))
        self.assertFalse(alle.nachlegen(3, [], 0.0))
        self.assertEqual(fb.vorrat_liste({}), [])


class TestAnzeige(unittest.TestCase):
    def test_band_html_und_welt(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import uebersicht
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "w.wbt"), "w", encoding="utf-8") as f:
                f.write("#VRML_SIM R2025a utf8\n")
            with open(os.path.join(tmp, "w.json"), "w", encoding="utf-8") as f:
                json.dump({"ea": {"baender": [dict(CFG, taster=[None, {"din": 26, "art": "oeffner"}],
                                                   nachlegen={"vorrat": [{"sorte": "rot", "knoten": ["V"]}]}),
                                              {"knoten": "kaputt"}]}}, f)
            alt, uebersicht.WELTEN_DIR = uebersicht.WELTEN_DIR, tmp
            try:
                baender = uebersicht.baender_von_welt("w.wbt")
            finally:
                uebersicht.WELTEN_DIR = alt
        self.assertEqual(baender, [{"name": "Band 1", "dout_vor": 21, "dout_zurueck": 22,
                                    "taster": [None, {"din": 26, "art": "oeffner"}], "nachlegen": True}])
        schueler = uebersicht.band_html(baender, True)
        self.assertIn('class="band-nachlegen" data-band="0"', schueler)
        self.assertIn("vor DOut21 · zurück DOut22", schueler)
        self.assertIn("ohne Eingang", schueler)
        self.assertIn("DIn26 · Öffner", schueler)
        self.assertNotIn("band-nachlegen", uebersicht.band_html(baender, False))   # Lehreransicht: nur sehen
        self.assertEqual(uebersicht.band_html([], True), "")
        self.assertIn("/api/band_nachlegen", uebersicht.EA_JS)


if __name__ == "__main__":
    unittest.main()
