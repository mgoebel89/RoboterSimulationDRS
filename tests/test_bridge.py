"""Tests ohne Webots: CRI-Protokoll, Demo-Server, Kinematik, erzeugtes Modell.

  python -m unittest discover -s tests -v
"""

import json
import math
import os
import re
import socket
import sys
import tempfile
import threading
import time
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "controllers", "irc_bridge"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

# Webots-Modul fuer den Import des Controllers ersetzen
sys.modules.setdefault("controller", types.SimpleNamespace(Supervisor=object))

import cri_client  # noqa: E402
import cri_demo_server  # noqa: E402
import ea  # noqa: E402
import irc_bridge  # noqa: E402
import rebel_kinematik as kin  # noqa: E402

DOKU_STATUS = (
    "CRISTART 1234 STATUS MODE joint "
    "POSJOINTSETPOINT 1.00 2.00 3.00 4.00 5.00 6.00 7 8 9 10 11 12 13 14 15.00 16.00 "
    "POSJOINTCURRENT 1.50 2.50 3.50 4.50 5.50 6.50 0 0 0 0 0 0 0 0 0 0 "
    "POSCARTROBOT 10.0 20.0 30.0 0.00 90.00 0.00 POSCARTPLATFORM 10.0 20.0 180.00 "
    "OVERRIDE 80.0 DIN 0 DOUT 40000000 ESTOP 3 SUPPLY 23000 CURRENTALL 2600 "
    "CURRENTJOINTS 150 200 140 160 0 0 0 0 0 0 0 0 0 0 0 0 "
    "ERROR no_error 8 8 8 8 8 8 0 0 0 0 0 0 0 0 0 0 KINSTATE 3 OPMODE -1 CARTSPEED 123.4 CRIEND"
)


class TestProtokoll(unittest.TestCase):
    def test_status_aus_doku(self):
        tokens = DOKU_STATUS.replace("CRIEND", "").split()
        st = cri_client.RobotState(cri_client.parse_status(tokens[3:]), DOKU_STATUS)
        self.assertEqual(st.mode, "joint")
        self.assertEqual(st.joints("setpoint"), [1, 2, 3, 4, 5, 6])
        self.assertEqual(st.joints(), [1.5, 2.5, 3.5, 4.5, 5.5, 6.5])
        self.assertEqual(st.cart, [10, 20, 30, 0, 90, 0])
        self.assertTrue(st.dout_bit(30))          # iRC-Anzeige DOut31
        self.assertFalse(st.dout_bit(31))
        self.assertEqual(st.kinstate, "3")

    def test_bitmasken_wie_irc_v15(self):
        # gemessen an der iRC-V15-Steuerung: CMD DOUT n true -> STATUS DOUT
        for nr, roh in ((0, "1"), (5, "20"), (20, "100000"), (21, "200000"),
                        (30, "40000000"), (31, "80000000")):
            self.assertEqual(cri_client.parse_int_mask(roh), 1 << nr, roh)
        self.assertEqual(cri_client.parse_int_mask(None), 0)


class TestKinematik(unittest.TestCase):
    def test_ik_fk_rundlauf(self):
        for x, y, z in [(0.35, 0.0, 0.02), (0.175, 0.3031, 0.04), (0.3, -0.2, 0.15)]:
            q = kin.ik_nach_unten(x, y, z, kin.TCP_EGP25)
            p, r = kin.fk(q, kin.TCP_EGP25)
            for a, b in zip(p, (x, y, z)):
                self.assertAlmostEqual(a, b, places=6)
            self.assertAlmostEqual(r[8], -1.0, places=6)   # Werkzeug zeigt nach unten

    def test_demo_bahn_in_irc_grenzen(self):
        grenzen = [(-179, 179), (-80, 140), (-80, 140), (-179, 179), (-95, 95), (-179, 179)]
        for greifer in ("egp25", "egp25_robi2", "vakuum"):
            for q, _dauer, _zu in cri_demo_server.ablauf(greifer):
                for v, (lo, hi) in zip(q, grenzen):
                    self.assertTrue(lo <= v <= hi, "%s: %s ausserhalb %s" % (greifer, q, (lo, hi)))

    def test_achswinkel_umrechnung(self):
        for q in ([10, 20, 30, 40, 50, 60], [0, 0, 0, 0, 0, 0], [180, 0, 0, 0, 0, 0], [0, 90, 0, 0, 0, 0]):
            _p, m = kin.fk(q)
            ax = irc_bridge.m_to_axis_angle(m)
            x, y, z, a = ax
            c, s, t = math.cos(a), math.sin(a), 1 - math.cos(a)
            back = [t * x * x + c, t * x * y - s * z, t * x * z + s * y,
                    t * x * y + s * z, t * y * y + c, t * y * z - s * x,
                    t * x * z - s * y, t * y * z + s * x, t * z * z + c]
            for u, v in zip(back, m):
                self.assertAlmostEqual(u, v, places=5)


class _Feld:
    def __init__(self, node, name):
        self.node, self.name = node, name

    def getSFVec3f(self):
        return list(self.node.pos) if self.name == "translation" else self.node.werte[self.name]

    def getSFRotation(self):
        return list(self.node.rot_aa or [0, 0, 1, 0])

    def getSFString(self):
        return self.node.werte[self.name]

    def getSFFloat(self):
        return self.node.werte[self.name]

    def getMFString(self, i):
        return self.node.werte[self.name][i]

    def setSFVec3f(self, v):
        self.node.pos = list(v)

    def setSFRotation(self, r):
        self.node.rot_aa = list(r)

    def getSFColor(self):
        return self.node.werte[self.name]


class _Teil:
    def __init__(self, name, pos, size, **felder):
        self.pos, self.rot_aa = list(pos), None
        self.werte = dict({"name": name, "size": list(size), "form": "quader"}, **felder)

    def getPosition(self):
        return self.pos

    def getOrientation(self):
        return [1, 0, 0, 0, 1, 0, 0, 0, 1]

    def getField(self, name):
        return _Feld(self, name)

    def resetPhysics(self):
        pass

    def getId(self):
        return id(self)


class _Tcp:
    def setze(self, q):
        self.p, self.r = kin.fk(q, kin.TCP_EGP25)

    def getPosition(self):
        return self.p

    def getOrientation(self):
        return self.r


class _Geraet:
    def __init__(self, name):
        self.name, self.pos = name, None

    def getName(self):
        return self.name

    def setPosition(self, p):
        self.pos = p


class _Robot:
    # so schreibt erzeuge_rebel_modell.py die Kenndaten des EGP 25 ins Modell
    DATEN = ("typ=egp25 tcp=0.08500 spalt=0.00550 hub=0.00540 backe_y=0.00500 "
             "backe_z0=-0.00430 backe_z1=0.00390")

    def __init__(self, geraete=("A1", "finger_links", "finger_rechts"), daten=DATEN):
        self.geraete = {n: _Geraet(n) for n in geraete}
        self.daten = daten

    def getCustomData(self):
        return self.daten

    def getWorldPath(self):
        return os.path.join(ROOT, "worlds", "gibtsnicht.wbt")

    def getNumberOfDevices(self):
        return len(self.geraete)

    def getDeviceByIndex(self, i):
        return list(self.geraete.values())[i]

    def getDevice(self, name):
        return self.geraete[name]


AUF, ZU = 1 << 30, 1 << 31   # iRC-Anzeige DOut31 (oeffnen), DOut32 (schliessen)


class TestGreifer(unittest.TestCase):
    def setUp(self):
        self.robot, self.tcp = _Robot(), _Tcp()
        self.greifer = irc_bridge.Greifer(self.robot, dict(irc_bridge.DEFAULT_CONFIG), self.tcp)
        self.stift = _Teil("stift", (0.35, 0.0, 0.01), (0.008, 0.008, 0.02))
        self.anderes = _Teil("weit_weg", (0.0, 0.4, 0.01), (0.008, 0.008, 0.02))

    def test_egp25_zwei_kanaele(self):
        self.assertEqual(self.greifer.typ, "egp25")
        self.assertEqual((self.greifer.bit_auf, self.greifer.bit_zu), (30, 31))
        self.assertAlmostEqual(self.greifer.masse["spalt"], 0.0055)
        self.assertIn("oeffnen DOut31", self.greifer.beschreibung())

    def test_befehl_wie_schunk_egp(self):
        b = self.greifer.befehl
        self.assertIs(b(ZU), True)
        self.assertIs(b(AUF), False)
        self.assertIsNone(b(0))                 # stromlos: Backen bleiben stehen
        self.assertIsNone(b(AUF | ZU))          # beide ein: Fehler, nichts tun
        self.assertIn("gleichzeitig", self.greifer.meldung)
        self.assertIs(b(ZU | (1 << 5)), True)   # andere Ausgaenge stoeren nicht

    def test_greifen_tragen_loslassen(self):
        self.tcp.setze(kin.ik_nach_unten(0.351, 0.0, 0.0155, kin.TCP_EGP25))
        self.greifer.schalte(True, [self.anderes, self.stift])
        self.assertIs(self.greifer.teil, self.stift)
        hub = self.robot.geraete["finger_links"].pos
        self.assertAlmostEqual(0.0055 - hub, 0.004, delta=0.0005)   # Backe liegt am Stift an

        self.tcp.setze(kin.ik_nach_unten(0.175, 0.3031, 0.12, kin.TCP_EGP25))
        self.greifer.mitfuehren()
        # zentrisch gegriffen: Stift sitzt mittig unter dem TCP, 5,5 mm tiefer
        for a, b in zip(self.stift.pos, (0.175, 0.3031, 0.12 - 0.0055)):
            self.assertAlmostEqual(a, b, places=4)

        self.greifer.schalte(False, [self.stift])
        self.assertIsNone(self.greifer.teil)
        self.assertEqual(self.robot.geraete["finger_links"].pos, 0.0)

    def test_daneben_gegriffen(self):
        self.tcp.setze(kin.ik_nach_unten(0.35, 0.08, 0.0155, kin.TCP_EGP25))
        self.greifer.schalte(True, [self.stift])
        self.assertIsNone(self.greifer.teil)
        self.assertIn("kein Werkstueck", self.greifer.meldung)
        self.assertAlmostEqual(self.robot.geraete["finger_links"].pos, 0.0054)   # ganz zu

    def test_zu_breit_oder_zu_hoch(self):
        wuerfel = _Teil("wuerfel", (0.35, 0.0, 0.02), (0.04, 0.04, 0.04))
        self.tcp.setze(kin.ik_nach_unten(0.35, 0.0, 0.02, kin.TCP_EGP25))
        self.greifer.schalte(True, [wuerfel])
        self.assertIsNone(self.greifer.teil, "40-mm-Wuerfel passt nicht zwischen die Backen")
        self.greifer.schalte(False, [])
        self.tcp.setze(kin.ik_nach_unten(0.35, 0.0, 0.035, kin.TCP_EGP25))   # Backen ueber dem Stift
        self.greifer.schalte(True, [self.stift])
        self.assertIsNone(self.greifer.teil)

    def test_einteach_hilfe_fuehrt_zur_greifstellung(self):
        basis = [1, 0, 0, 0, 1, 0, 0, 0, 1]
        # 30 mm ueber der Greifhoehe und 3 mm daneben: in iRC-Koordinaten noch X -3, Z -30 mm
        self.tcp.setze(kin.ik_nach_unten(0.353, 0.0, 0.0462, kin.TCP_EGP25))
        h = self.greifer.einteach_hilfe([self.anderes, self.stift], basis)
        self.assertEqual(h["teil"], "stift")
        self.assertAlmostEqual(h["dx"], -3.0, delta=0.2)
        self.assertAlmostEqual(h["dy"], 0.0, delta=0.2)
        self.assertAlmostEqual(h["dz"], -30.0, delta=0.2)
        self.assertFalse(h["greifbar"])
        self.assertIn("noch X -3", h["text"])
        # genau in der Greifstellung: Oberkante 0,5 mm unter dem Greiferkoerper -> TCP 16,2 mm
        self.tcp.setze(kin.ik_nach_unten(0.35, 0.0, 0.0162, kin.TCP_EGP25))
        h = self.greifer.einteach_hilfe([self.stift], basis)
        self.assertTrue(h["greifbar"])
        self.assertEqual(h["text"], "stift: Greifstellung erreicht")
        self.greifer.schalte(True, [self.stift])
        self.assertIs(self.greifer.teil, self.stift)
        self.assertIsNone(self.greifer.einteach_hilfe([self.stift], basis))   # gegriffen: keine Hilfe

    def test_einteach_hilfe_weit_weg_und_zu_breit(self):
        basis = [1, 0, 0, 0, 1, 0, 0, 0, 1]
        self.tcp.setze(kin.ik_nach_unten(0.2, -0.2, 0.15, kin.TCP_EGP25))
        self.assertIn("naechstes Teil: stift", self.greifer.einteach_hilfe([self.stift], basis)["text"])
        leiste = _Teil("leiste", (0.35, 0.0, 0.01), (0.04, 0.008, 0.02))   # quer zu den Backen zu breit
        self.tcp.setze(kin.ik_nach_unten(0.35, 0.0, 0.0162, kin.TCP_EGP25))
        h = self.greifer.einteach_hilfe([leiste], basis)
        self.assertIn("40.0 mm breit, Backen oeffnen nur 11.0 mm", h["text"])
        self.assertIn("um 90 Grad drehen", h["text"])
        wuerfel = _Teil("target", (0.35, 0.0, 0.02), (0.04, 0.04, 0.04))   # in jeder Richtung zu breit
        self.tcp.setze(kin.ik_nach_unten(0.35, 0.0, 0.037, kin.TCP_EGP25))
        h = self.greifer.einteach_hilfe([wuerfel], basis)
        self.assertIn("zu gross fuer diesen Greifer", h["text"])
        self.assertNotIn("drehen", h["text"])

    def test_robi2_backen_greifen_an_der_spitze(self):
        # Kenndaten, wie sie der Generator aus cad/backen/robi2 berechnet
        robot = _Robot(daten="typ=egp25_robi2 tcp=0.13070 spalt=0.00550 hub=0.00540 backe_y=0.01300 "
                             "backe_z0=-0.05000 backe_z1=0.00000")
        tcp = _Tcp()
        tcp.setze = lambda q: setattr(tcp, "p", kin.fk(q, kin.TCP_EGP25_ROBI2)[0]) or setattr(
            tcp, "r", kin.fk(q, kin.TCP_EGP25_ROBI2)[1])
        g = irc_bridge.Greifer(robot, dict(irc_bridge.DEFAULT_CONFIG), tcp)
        basis = [1, 0, 0, 0, 1, 0, 0, 0, 1]
        # Greifstellung: Backenspitze 0,5 mm ueber dem Tisch, nicht darunter
        tcp.setze(kin.ik_nach_unten(0.35, 0.0, 0.0005, kin.TCP_EGP25_ROBI2))
        self.assertEqual(g.einteach_hilfe([self.stift], basis)["text"], "stift: Greifstellung erreicht")
        tcp.setze(kin.ik_nach_unten(0.35, 0.0, 0.001, kin.TCP_EGP25_ROBI2))   # wie der Demo-Server
        g.schalte(True, [self.stift])
        self.assertIs(g.teil, self.stift)

    def test_cad_werkstueck_an_der_schmalen_stelle_greifen(self):
        """Wie das Target des Nutzers: Scheibe 40 x 40 x 30 mm mit Zapfen 10 x 10 x 10 mm obendrauf.
        Der Huellquader ist 40 mm breit, der Zapfen passt aber zwischen die Backen (11 mm)."""
        import tempfile

        def quader(x0, y0, z0, x1, y1, z1, basis):
            v = [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
            f = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1), (2, 3, 7), (2, 7, 6),
                 (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]
            return v, [tuple(i + basis + 1 for i in d) for d in f]
        v1, f1 = quader(-20, -20, 0, 20, 20, 30, 0)
        v2, f2 = quader(-5, -5, 30, 5, 5, 40, 8)
        with tempfile.TemporaryDirectory() as tmp:
            obj = os.path.join(tmp, "target.obj")
            with open(obj, "w") as f:
                f.write("".join("v %g %g %g\n" % p for p in v1 + v2) + "".join("f %d %d %d\n" % d for d in f1 + f2))
            irc_bridge.NETZE.clear()
            target = _Teil("target", (0.35, 0.0, 0.02), (0.04, 0.04, 0.04), form="modell", modell=[obj],
                           skalierung=0.001, modellversatz=[0.0, 0.0, -0.02])
            basis = [1, 0, 0, 0, 1, 0, 0, 0, 1]
            # Backen am Zapfen (Oberkante z = 40 mm): Hilfe fuehrt hin, Greifen klappt
            self.tcp.setze(kin.ik_nach_unten(0.352, 0.0, 0.050, kin.TCP_EGP25))
            h = self.greifer.einteach_hilfe([target], basis)
            self.assertIn("noch X -2", h["text"])
            # 2 mm daneben stoesst eine Backe auf den Zapfen, 0,5 mm daneben passt es
            self.tcp.setze(kin.ik_nach_unten(0.352, 0.0, 0.0362, kin.TCP_EGP25))
            self.assertFalse(self.greifer.einteach_hilfe([target], basis)["greifbar"])
            self.tcp.setze(kin.ik_nach_unten(0.3505, 0.0, 0.0362, kin.TCP_EGP25))
            self.assertTrue(self.greifer.einteach_hilfe([target], basis)["greifbar"])
            self.greifer.schalte(True, [target])
            self.assertIs(self.greifer.teil, target)
            self.assertAlmostEqual(self.robot.geraete["finger_links"].pos, 0.0055 - 0.005 - 0.0002, places=6)
            # mittig gegriffen: der Zapfen rutscht unter den TCP
            self.greifer.mitfuehren()
            self.assertAlmostEqual(target.pos[0], 0.3505, places=4)
            self.greifer.schalte(False, [target])
            # Backen tiefer an der Scheibe (40 mm breit): kein Griff, Hinweis auf die Breite
            self.tcp.setze(kin.ik_nach_unten(0.35, 0.0, 0.020, kin.TCP_EGP25))
            self.greifer.schalte(True, [target])
            self.assertIsNone(self.greifer.teil)
            self.greifer.schalte(False, [])
            self.assertIn("an den Backen 40.0 mm breit", self.greifer.einteach_hilfe([target], basis)["text"])
        irc_bridge.NETZE.clear()

    def test_sauger_ein_kanal(self):
        g = irc_bridge.Greifer(_Robot(("A1", "vakuum_led"), "typ=vakuum"), dict(irc_bridge.DEFAULT_CONFIG), self.tcp)
        self.assertEqual((g.typ, g.bit_auf, g.bit_zu), ("vakuum", None, 31))
        self.assertIs(g.befehl(ZU), True)
        self.assertIs(g.befehl(0), False)


class TestTeileZuruecksetzen(unittest.TestCase):
    def test_befehlsdatei_wird_einmal_gelesen(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = dict(irc_bridge.DEFAULT_CONFIG, platz="07", status_ordner=tmp)
            pfad = irc_bridge.befehl_pfad(cfg)
            self.assertEqual(pfad, os.path.join(tmp, "platz-07.befehl"))
            self.assertEqual(irc_bridge.hole_befehle(pfad), [])
            with open(pfad, "a", encoding="utf-8") as f:
                f.write("taster 0 1\ntaster 0 0\n")
            self.assertEqual(irc_bridge.hole_befehle(pfad), ["taster 0 1", "taster 0 0"])
            self.assertEqual(irc_bridge.hole_befehle(pfad), [])
            self.assertEqual(os.listdir(tmp), [])

    def test_teile_auf_startlage_und_loslassen(self):
        greifer = irc_bridge.Greifer(_Robot(), dict(irc_bridge.DEFAULT_CONFIG), _Tcp())
        stift, wuerfel = _Teil("stift", (0.35, 0.0, 0.01), (0.008, 0.008, 0.02)), _Teil("w", (0.2, 0.1, 0.02), (0.04,) * 3)
        startlagen = {}
        irc_bridge.merke_startlagen([stift], startlagen)
        irc_bridge.merke_startlagen([stift, wuerfel], startlagen)   # spaeter dazugekommen
        stift.pos, stift.rot_aa = [0.1, 0.2, 0.3], [1, 0, 0, 1.0]
        wuerfel.pos = [0.0, 0.0, 0.5]
        greifer.teil, greifer.teil_name, greifer.zu = stift, "stift", True
        irc_bridge.teile_zuruecksetzen([stift, wuerfel], startlagen, greifer)
        self.assertEqual((stift.pos, stift.rot_aa), ([0.35, 0.0, 0.01], [0, 0, 1, 0]))
        self.assertEqual(wuerfel.pos, [0.2, 0.1, 0.02])
        self.assertIsNone(greifer.teil)
        self.assertTrue(greifer.zu)   # die Backen bleiben, wie iRC sie geschaltet hat
        self.assertIn("Startlage", greifer.meldung)


class TestEinUndAusgaenge(unittest.TestCase):
    TASTER = [{"name": "Start", "din": 21, "art": "schliesser", "modus": "tastend"},
              {"name": "Not-Halt", "din": 22, "art": "oeffner", "modus": "tastend"},
              {"name": "Automatik", "din": 23, "art": "schliesser", "modus": "rastend"}]

    def test_kodierung_wie_protobuf(self):
        # Vergleichswerte aus robotcontrolapp_pb2 (offizielle proto-Datei), siehe STAND_UND_KONZEPT.md Stufe F
        self.assertEqual(ea.io_anfrage("WebotsSim", {20: True}).hex(), "0a095765626f747353696d120408141801")
        self.assertEqual(ea.io_anfrage("WebotsSim", {63: True, 5: True, 0: False}).hex(),
                         "0a095765626f747353696d1204080018001204080518011204083f1801")

    def test_taster_tastend_oeffner_rastend(self):
        t = ea.Taster(self.TASTER)
        self.assertEqual(t.signale(), {20: False, 21: True, 22: False})   # Oeffner ungedrueckt = 1
        t.ereignis(0, True, 10.0)
        t.ereignis(1, True, 10.0)
        self.assertEqual(t.signale(), {20: True, 21: False, 22: False})
        t.ereignis(0, False, 10.05)              # zu kurz getippt: bleibt bis 10.3 gedrueckt
        t.weiter(10.25)
        self.assertTrue(t.gedrueckt[0])
        t.weiter(10.31)
        self.assertFalse(t.gedrueckt[0])
        t.ereignis(1, False, 11.0)
        self.assertTrue(t.signale()[21])
        t.ereignis(2, True, 12.0)
        t.ereignis(2, False, 12.1)               # rastend: Loslassen aendert nichts
        self.assertTrue(t.signale()[22])
        t.ereignis(2, True, 13.0)
        self.assertFalse(t.signale()[22])
        t.ereignis(9, True, 13.0)                # unbekannter Taster: nichts passiert

    def test_leuchten(self):
        leuchten = [{"dout": 32}, {"dout": 31, "umkehren": True}, {"dout": 99}]
        self.assertEqual(ea.leuchten_zustand(leuchten, 1 << 31), [True, True, False])
        self.assertEqual(ea.leuchten_zustand(leuchten, 1 << 30), [False, False, False])
        self.assertEqual(ea.leuchten_zustand(leuchten, None), [False, True, False])

    def test_app_verbindung_und_meldungen(self):
        gesendet = []

        class Fehler(Exception):
            def details(self):
                return "app is not installed or enabled"

        antworten = [Fehler(), None]

        def senden(daten):
            gesendet.append(daten)
            antwort = antworten.pop(0) if antworten else None
            if antwort:
                raise antwort

        v = ea.AppVerbindung("127.0.0.1", kanal_fn=lambda: senden, log=lambda t: None).start()
        v.setze({20: True})
        for _ in range(50):
            if "nicht installiert" in v.meldung:
                break
            time.sleep(0.02)
        self.assertIn("nicht installiert", v.meldung)
        v.setze({20: False})
        for _ in range(50):
            if v.ok:
                break
            time.sleep(0.02)
        self.assertTrue(v.ok)
        self.assertEqual(gesendet[-1], ea.io_anfrage("WebotsSim", {20: False}))
        v.stopp(alles_aus=False)

    def test_konfiguration_aus_der_welt(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "w.json"), "w", encoding="utf-8") as f:
                json.dump({"titel": "x", "ea": {"taster": self.TASTER + [{"din": "21"}], "leuchten": [{"dout": 32}]}}, f)
            cfg = ea.lade_ea(os.path.join(tmp, "w.wbt"))
            self.assertEqual((len(cfg["taster"]), len(cfg["leuchten"])), (3, 1))
            self.assertEqual(ea.lade_ea(os.path.join(tmp, "fehlt.wbt")), {"taster": [], "leuchten": []})


class _Sensorknoten:
    def __init__(self, pos, groesse, rot=(1, 0, 0, 0, 1, 0, 0, 0, 1)):
        self.pos, self.rot, self.werte = list(pos), list(rot), {"groesse": list(groesse), "farbe": None}

    def getPosition(self):
        return self.pos

    def getOrientation(self):
        return self.rot

    def getField(self, name):
        knoten = self

        class Feld:
            def getSFVec3f(self):
                return knoten.werte[name]

            def setSFColor(self, c):
                knoten.werte[name] = c
        return Feld()


class TestSensorflaechen(unittest.TestCase):
    EINHEIT = [1, 0, 0, 0, 1, 0, 0, 0, 1]

    def test_belegt_geometrie(self):
        b = ea.belegt
        zone = ([0.5, 0.1, 0.0], self.EINHEIT, [0.04, 0.04, 0.02])
        self.assertTrue(b(*zone, [0.5, 0.1, 0.01], self.EINHEIT, [0.008, 0.008, 0.02]))     # Stift steht drauf
        self.assertTrue(b(*zone, [0.53, 0.1, 0.01], self.EINHEIT, [0.04, 0.04, 0.02]))      # ragt 10 mm hinein
        self.assertFalse(b(*zone, [0.56, 0.1, 0.01], self.EINHEIT, [0.04, 0.04, 0.02]))     # liegt nur daneben
        self.assertFalse(b(*zone, [0.5, 0.1, 0.035], self.EINHEIT, [0.008, 0.008, 0.02]))   # schwebt darueber
        # Sensorflaeche um 90 Grad gedreht (Laenge 100 mm entlang y): Teil 40 mm neben der Mitte in y
        gedreht = [0, -1, 0, 1, 0, 0, 0, 0, 1]
        self.assertTrue(b([0.5, 0.1, 0.0], gedreht, [0.1, 0.02, 0.02], [0.5, 0.14, 0.01], self.EINHEIT, [0.01] * 3))
        self.assertFalse(b([0.5, 0.1, 0.0], self.EINHEIT, [0.1, 0.02, 0.02], [0.5, 0.14, 0.01], self.EINHEIT, [0.01] * 3))

    def test_sensoren_melden_und_faerben(self):
        knoten = {"SENSOR_0": _Sensorknoten([0.5, 0.1, 0.0], [0.04, 0.04, 0.02]),
                  "SENSOR_1": _Sensorknoten([0.3, -0.2, 0.0], [0.04, 0.04, 0.02])}
        robot = types.SimpleNamespace(getFromDef=knoten.get)
        meldungen = []
        f = ea.Sensoren([{"din": 25, "knoten": "SENSOR_0"}, {"din": 26, "art": "oeffner", "knoten": "SENSOR_1"},
                         {"din": 27, "knoten": "SENSOR_9"}], robot, log=meldungen.append)
        self.assertEqual(len(f.liste), 2)
        self.assertIn("SENSOR_9", meldungen[0])
        self.assertEqual(f.signale(), {24: False, 25: True})
        stift = _Teil("stift", (0.5, 0.1, 0.01), (0.008, 0.008, 0.02))
        f.pruefen([stift])
        self.assertEqual((f.belegt, f.signale()), ([True, False], {24: True, 25: True}))
        self.assertEqual(knoten["SENSOR_0"].werte["farbe"], ea.BELEGT)
        stift.pos = [0.3, -0.2, 0.01]
        f.pruefen([stift])
        self.assertEqual((f.belegt, f.signale()), ([False, True], {24: False, 25: False}))
        self.assertEqual(knoten["SENSOR_0"].werte["farbe"], ea.FREI)

    def test_farbe_erkennen(self):
        self.assertEqual(ea.farbe_erkennen([0.1, 0.4, 0.9]), "blau")      # Standardfarbe der Werkstuecke
        self.assertEqual(ea.farbe_erkennen([0.9, 0.2, 0.15]), "rot")
        self.assertEqual(ea.farbe_erkennen([1.0, 1.0, 1.0]), "weiss")
        self.assertIsNone(ea.farbe_erkennen([0.5, 0.5, 0.5]))            # grau: keine der Farben
        self.assertIsNone(ea.farbe_erkennen([0.6, 0.2, 0.7]))            # lila

    def test_farbsensor_je_farbe_und_bits(self):
        knoten = {"SENSOR_0": _Sensorknoten([0.5, 0.1, 0.0], [0.04, 0.04, 0.02]),
                  "SENSOR_1": _Sensorknoten([0.3, -0.2, 0.0], [0.04, 0.04, 0.02])}
        f = ea.Sensoren([{"typ": "farbe", "ausgabe": "je_farbe", "knoten": "SENSOR_0",
                          "farben": [{"farbe": "rot", "din": 25}, {"farbe": "blau", "din": 26}]},
                         {"typ": "farbe", "ausgabe": "bits", "din": 30, "bits": 2, "knoten": "SENSOR_1",
                          "farben": [{"farbe": "rot", "code": 1}, {"farbe": "gruen", "code": 2}, {"farbe": "blau", "code": 3}]}],
                        types.SimpleNamespace(getFromDef=knoten.get), log=lambda t: None)
        self.assertEqual(f.signale(), {24: False, 25: False, 29: False, 30: False})
        rot = _Teil("rot", (0.5, 0.1, 0.01), (0.02, 0.02, 0.02), farbe=[0.9, 0.1, 0.1])
        gruen = _Teil("gruen", (0.3, -0.2, 0.01), (0.02, 0.02, 0.02), farbe=[0.2, 0.7, 0.25])
        f.pruefen([rot, gruen])
        self.assertEqual(f.belegt, ["rot", "gruen"])
        self.assertEqual(f.signale(), {24: True, 25: False, 29: False, 30: True})   # gruen = Code 2 = Bit 1
        self.assertEqual(knoten["SENSOR_1"].werte["farbe"], [0.2, 0.7, 0.25])      # Feld zeigt die Teilefarbe
        # zwei Teile auf der Flaeche: das der Mitte naechste zaehlt
        blau = _Teil("blau", (0.3, -0.21, 0.01), (0.02, 0.02, 0.02), farbe=[0.1, 0.4, 0.9])
        gruen.pos = [0.315, -0.2, 0.01]
        f.pruefen([rot, gruen, blau])
        self.assertEqual(f.belegt[1], "blau")
        self.assertEqual(f.signale()[29], True)
        self.assertEqual(f.signale()[30], True)                                     # Code 3
        grau = _Teil("grau", (0.5, 0.1, 0.01), (0.02, 0.02, 0.02), farbe=[0.5, 0.5, 0.5])
        f.pruefen([grau])
        self.assertEqual(f.belegt, [None, None])
        self.assertEqual(set(f.signale().values()), {False})


class TestKonfigurationJePlatz(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_datei = os.path.join(self.tmp.name, "config.json")
        with open(self.cfg_datei, "w", encoding="utf-8") as f:
            f.write('{"_hilfe": "x", "host": "127.0.0.1", "ports": [3921, 3920], "greifer_dout_schliessen": 33}')

    def tearDown(self):
        self.tmp.cleanup()

    def test_ohne_umgebungsvariablen_gilt_config(self):
        cfg = irc_bridge.load_config({}, self.cfg_datei)
        self.assertEqual((cfg["host"], cfg["ports"], cfg["platz"]), ("127.0.0.1", [3921, 3920], ""))
        self.assertNotIn("_hilfe", cfg)
        self.assertEqual(os.path.basename(irc_bridge.log_pfad(cfg)), "irc_bridge.log")

    def test_umgebungsvariablen_gehen_vor(self):
        env = {"IRC_HOST": " R212-PC07 ", "IRC_PORTS": "3921;3920", "PLATZ": "07",
               "LOG_DIR": os.path.join(self.tmp.name, "log"), "STATUS_DIR": os.path.join(self.tmp.name, "st")}
        cfg = irc_bridge.load_config(env, self.cfg_datei)
        self.assertEqual(cfg["host"], "R212-PC07")
        self.assertEqual(cfg["ports"], [3921, 3920])
        self.assertEqual(irc_bridge.log_pfad(cfg), os.path.join(self.tmp.name, "log", "platz-07.log"))
        self.assertEqual(irc_bridge.status_pfad(cfg), os.path.join(self.tmp.name, "st", "platz-07.json"))
        self.assertEqual(cfg["greifer_dout_schliessen"], 33)   # Rest kommt weiter aus config.json

    def test_status_ohne_eigenen_ordner_liegt_beim_log(self):
        cfg = irc_bridge.load_config({"PLATZ": "3", "LOG_DIR": self.tmp.name}, self.cfg_datei)
        self.assertEqual(irc_bridge.status_pfad(cfg), os.path.join(self.tmp.name, "platz-3.json"))

    def test_ungueltige_ports_behalten_config(self):
        cfg = irc_bridge.load_config({"IRC_PORTS": "drei"}, self.cfg_datei)
        self.assertEqual(cfg["ports"], [3921, 3920])

    def test_statusdatei_vollstaendig_und_ohne_reste(self):
        import json
        pfad = os.path.join(self.tmp.name, "platz-07.json")
        for i in range(3):
            irc_bridge.schreibe_status(pfad, {"platz": "07", "verbunden": True, "runde": i})
        with open(pfad, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["runde"], 2)
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["config.json", "platz-07.json"])


class TestVerbindung(unittest.TestCase):
    def test_client_gegen_demo_server(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        stop, bereit = threading.Event(), threading.Event()
        log = []
        t = threading.Thread(target=cri_demo_server.serve,
                             kwargs=dict(port=port, takt=0.02, log=log.append, stop=stop, bereit=bereit),
                             daemon=True)
        t.start()
        bereit.wait(2)
        cli = cri_client.CriClient("127.0.0.1", [port], log=log.append).start()
        try:
            ende = time.monotonic() + 15
            gesehen = set()
            while time.monotonic() < ende and len(gesehen) < 2:
                time.sleep(0.02)
                if cli.state is not None:
                    gesehen.add((cli.state.dout_bit(30), cli.state.dout_bit(31)))
            self.assertTrue(cli.connected)
            # EGP 25: abwechselnd nur oeffnen (DOut31) bzw. nur schliessen (DOut32)
            self.assertEqual(gesehen, {(True, False), (False, True)})
            # Takt nur grob pruefen, unter Last (iRC + Webots laufen) schwankt er
            self.assertTrue(0.005 < cli.status_period < 0.1, cli.status_period)
            # Verbindung bleibt dank ALIVEJOG laenger als 2 s bestehen
            self.assertNotIn("Demo: kein ALIVEJOG seit 2 s, trenne (wie iRC)", log)
        finally:
            cli.stop()
            stop.set()


class TestModell(unittest.TestCase):
    PROTO = os.path.join(ROOT, "protos", "IgusRebel6DOF.proto")

    @unittest.skipUnless(os.path.exists(PROTO), "Modell noch nicht erzeugt")
    def test_proto_vollstaendig(self):
        with open(self.PROTO, encoding="utf-8") as f:
            text = f.read()
        ohne_vorlage = re.sub(r"%<.*?>%", "", text)
        self.assertEqual(ohne_vorlage.count("{"), ohne_vorlage.count("}"))
        self.assertEqual(ohne_vorlage.count("["), ohne_vorlage.count("]"))
        for i in range(1, 7):
            self.assertIn('name "A%d"' % i, text)
        for url in re.findall(r'url "([^"]+)"', text):
            self.assertTrue(os.path.exists(os.path.join(ROOT, "protos", url)), url)
        for name in ("DEF FLANSCH", "DEF TCP", "finger_links", "vakuum_led", "customData", "egp25_grund.obj"):
            self.assertIn(name, text)
        # Kenndaten im Modell passen zu dem, was der Controller erwartet
        daten = re.search(r"'egp25': '([^']+)'", text).group(1)
        g = irc_bridge.lies_greiferdaten(daten)
        self.assertEqual(g["typ"], "egp25")
        self.assertAlmostEqual(g["tcp"], kin.TCP_EGP25)
        self.assertAlmostEqual(g["spalt"], irc_bridge.Greifer.BACKEN["spalt"])
        self.assertIn("translation 0 0 0.08500", text)

    def test_backensatz_lage(self):
        import erzeuge_rebel_modell as gen
        satz = next(s for s in gen.lade_backensaetze() if s["name"] == "robi2")
        links, rechts = gen.backe_platzieren(satz, scale=1.0)
        lv, rv = links[0], rechts[0]
        # duennes Ende zeigt vom Flansch weg, dickes Ende buendig mit dem Backentraeger (80,7 mm)
        self.assertAlmostEqual(min(p[2] for p in lv), 80.7, places=3)
        self.assertAlmostEqual(max(p[2] for p in lv), 130.7, places=3)
        # ebene Greifflaeche zur Mitte: links bei x = -5,5, rechts gespiegelt bei +5,5
        self.assertAlmostEqual(max(p[0] for p in lv), -5.5, places=3)
        self.assertAlmostEqual(min(p[0] for p in rv), 5.5, places=3)
        self.assertAlmostEqual(max(abs(p[1]) for p in lv), 13.0, places=3)
        for falsch in ({"x": "-z", "y": "y", "z": "-x"}, {"x": "z", "y": "z", "z": "-x"}):   # Spiegelung, doppelt
            with self.assertRaises(SystemExit):
                gen.backen_lage(dict(satz, achsen=falsch))

    @unittest.skipUnless(os.path.exists(PROTO), "Modell noch nicht erzeugt")
    def test_proto_mit_backensatz(self):
        with open(self.PROTO, encoding="utf-8") as f:
            text = f.read()
        self.assertIn('"egp25_robi2"', text)
        g = irc_bridge.lies_greiferdaten(re.search(r"'egp25_robi2': '([^']+)'", text).group(1))
        self.assertAlmostEqual(g["tcp"], kin.TCP_EGP25_ROBI2)
        self.assertAlmostEqual(g["spalt"], 0.0055)
        self.assertIn("egp25_robi2_backe_links.obj", text)

    def test_welt_klammern(self):
        for datei in ("vorlagen/welten/rebel_tisch.wbt", "vorlagen/welten/stift_robi2.wbt", "protos/Werkstueck.proto",
                      "protos/Aufbau.proto", "protos/Sensorfeld.proto"):
            with open(os.path.join(ROOT, datei), encoding="utf-8") as f:
                text = re.sub(r"%<.*?>%", "", f.read())
            self.assertEqual(text.count("{"), text.count("}"), datei)
            self.assertEqual(text.count("["), text.count("]"), datei)


if __name__ == "__main__":
    unittest.main()
