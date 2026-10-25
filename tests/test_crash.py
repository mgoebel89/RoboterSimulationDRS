"""Tests fuer den Crash-Stopp (Stufe K2), ohne Webots und ohne iRC."""

import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "controllers", "irc_bridge"))

import crash  # noqa: E402
import ea  # noqa: E402


def bewegung(runstate=None, quelle=None, programm="", befehl=None):
    """MotionState wie iRC ihn schickt (Felder mit Standardwert fehlen)."""
    ipo = b""
    if runstate:
        ipo += ea._feld(1, 0, runstate)
    if programm:
        ipo += ea._feld(3, 2, programm.encode()) + ea._feld(4, 2, programm.encode())
    if befehl is not None:
        ipo += ea._feld(7, 0, befehl & ((1 << 64) - 1))
    daten = ea._feld(1, 0, quelle) if quelle else b""
    return daten + ea._feld(2, 2, ipo) + ea._feld(6, 0, 1)


TREFFER = {"a": "Greifer", "b": "magazin", "punkt": [0.35, 0.0, 0.02]}


class TestKodierung(unittest.TestCase):
    def test_anfragen(self):
        self.assertEqual(crash.bewegung_anfrage("WebotsSim"), b"\x0a\x09WebotsSim")
        # runstate NOT_RUNNING muss mitgeschickt werden (optional), sonst passiert nichts
        self.assertEqual(crash.stopp_anfrage("WebotsSim"), b"\x0a\x09WebotsSim\x10\x00")

    def test_bewegungszustand(self):
        z = crash.bewegungszustand(bewegung(crash.RUNNING, quelle=1, programm="magazin.xml", befehl=4))
        self.assertEqual((z["laeuft"], z["quelle"], z["programm"], z["befehl"]), (True, 1, "magazin.xml", 4))
        self.assertTrue(crash.bewegungszustand(bewegung(crash.PAUSED))["laeuft"])
        leer = crash.bewegungszustand(b"")
        self.assertEqual((leer["laeuft"], leer["programm"], leer["quelle"]), (False, "", 0))
        self.assertEqual(crash.bewegungszustand(bewegung(befehl=-1))["befehl"], -1)
        with self.assertRaises(ValueError):
            crash.bewegungszustand(b"\x12\x05ab")


class TestZustand(unittest.TestCase):
    def setUp(self):
        self.log = []
        self.z = crash.CrashZustand("stopp", log=self.log.append)

    def test_crash_quittieren_freifahren(self):
        z = self.z
        self.assertFalse(z.kollision([], 0.0))
        self.assertIsNone(z.status(lambda p: p))
        self.assertTrue(z.kollision([TREFFER], 1.0))
        self.assertEqual(z.zustand, "pruefe")
        self.assertFalse(z.kollision([TREFFER], 1.1))            # Anfrage laeuft schon
        z.ergebnis("gestoppt", 1.2, "magazin.xml")
        self.assertTrue(z.gesperrt)
        st = z.status(lambda p: [v * 1000 for v in p])
        self.assertEqual((st["zustand"], st["a"], st["b"], st["programm"]), ("crash", "Greifer", "magazin", "magazin.xml"))
        self.assertEqual(st["punkt_mm"], [350.0, 0.0, 20.0])
        self.assertFalse(z.kollision([], 2.0))                   # bleibt stehen, auch wenn die Beruehrung endet
        self.assertTrue(z.gesperrt)
        z.ergebnis("wieder_gestoppt", 2.5, "magazin.xml")
        self.assertIn("quittieren", z.status(lambda p: p)["meldung"])
        # quittieren: Beruehrung mit dem Magazin zaehlt beim Freifahren nicht, mit etwas anderem schon
        self.assertTrue(z.quittieren([TREFFER]))
        self.assertEqual(z.zustand, "freifahren")
        self.assertFalse(z.kollision([TREFFER, dict(TREFFER, a="Backe links")], 3.0))
        self.assertTrue(z.kollision([TREFFER, dict(TREFFER, b="stift")], 3.1))
        self.assertEqual(z.ausloeser["b"], "stift")
        z.ergebnis("kein_programm", 3.2)
        self.assertEqual(z.zustand, "frei")

    def test_frei_gefahren(self):
        z = self.z
        z.kollision([TREFFER], 1.0)
        z.ergebnis("gestoppt", 1.1)
        z.quittieren([TREFFER])
        for i in range(crash.FREI_NACH - 1):
            self.assertFalse(z.kollision([], 2.0 + i / 10))
        self.assertFalse(z.kollision([TREFFER], 2.5))             # Beruehrung an der Grenze flackert
        self.assertEqual(z.zustand, "freifahren")
        for i in range(crash.FREI_NACH):
            z.kollision([], 3.0 + i / 10)
        self.assertEqual(z.zustand, "frei")                       # wieder scharf
        self.assertTrue(z.kollision([TREFFER], 4.0))

    def test_quittieren_wenn_schon_frei(self):
        z = self.z
        z.kollision([TREFFER], 1.0)
        z.ergebnis("gestoppt", 1.1)
        self.assertTrue(z.quittieren([]))
        self.assertEqual(z.zustand, "freifahren")
        self.assertFalse(z.quittieren([]))                        # nichts mehr zu quittieren
        for i in range(crash.FREI_NACH):
            z.kollision([], 2.0 + i / 10)
        self.assertEqual(z.zustand, "frei")

    def test_jog_nur_melden(self):
        z = self.z
        z.kollision([TREFFER], 1.0)
        z.ergebnis("kein_programm", 1.05)
        self.assertEqual(z.zustand, "frei")
        self.assertIn("von Hand", z.status(lambda p: p)["meldung"])
        self.assertFalse(z.kollision([TREFFER], 1.5))             # nicht 10-mal je Sekunde nachfragen
        self.assertTrue(z.kollision([TREFFER], 2.1))

    def test_ohne_app(self):
        z = self.z
        z.kollision([TREFFER], 1.0)
        z.ergebnis("fehler", 1.1, text="App WebotsSim in iRC nicht installiert oder nicht freigeschaltet")
        self.assertEqual(z.zustand, "frei")
        self.assertIn("nicht angehalten", z.status(lambda p: p)["meldung"])
        self.assertFalse(z.kollision([TREFFER], 3.0))
        self.assertTrue(z.kollision([TREFFER], 6.2))

    def test_nur_melden(self):
        z = crash.CrashZustand("melden")
        self.assertFalse(z.kollision([TREFFER], 1.0))
        self.assertIsNone(z.status(lambda p: p))


class FalschesIrc:
    """Spielt die App-Schnittstelle nach: Programm laeuft oder nicht, merkt sich Stopp-Befehle."""

    def __init__(self, laeuft=True, fehler=None):
        self.laeuft, self.fehler, self.stopps = laeuft, fehler, 0

    def aufruf(self, methode, daten):
        if self.fehler:
            raise RuntimeError(self.fehler)
        if methode == crash.GET_MOTION:
            return bewegung(crash.RUNNING if self.laeuft else None, programm="test.xml")
        self.assertStopp(daten)
        self.laeuft = False
        self.stopps += 1
        return bewegung(programm="test.xml")

    def assertStopp(self, daten):
        if daten != crash.stopp_anfrage("WebotsSim"):
            raise AssertionError(daten)


def warte_auf(fn, zeit=3.0):
    ende = time.monotonic() + zeit
    while time.monotonic() < ende:
        wert = fn()
        if wert:
            return wert
        time.sleep(0.02)
    return None


class TestProgrammStopp(unittest.TestCase):
    def test_anhalten_und_sperre(self):
        irc = FalschesIrc(laeuft=True)
        s = crash.ProgrammStopp("x", kanal_fn=lambda: irc.aufruf).start()
        try:
            s.anhalten()
            self.assertEqual(warte_auf(s.ergebnisse), [("gestoppt", "test.xml", None)])
            self.assertEqual(irc.stopps, 1)
            s.sperren(True)
            irc.laeuft = True                                  # Schueler startet wieder, ohne zu quittieren
            self.assertEqual(warte_auf(s.ergebnisse), [("wieder_gestoppt", "test.xml", None)])
            self.assertEqual(irc.stopps, 2)
            s.sperren(False)
            time.sleep(0.05)
            irc.laeuft = True
            time.sleep(crash.SPERRE_TAKT_S + 0.2)
            self.assertEqual(s.ergebnisse(), [])                 # nicht mehr gesperrt: laeuft weiter
            self.assertEqual(irc.stopps, 2)
        finally:
            s.beenden()

    def test_kein_programm_und_fehler(self):
        irc = FalschesIrc(laeuft=False)
        s = crash.ProgrammStopp("x", kanal_fn=lambda: irc.aufruf).start()
        try:
            s.anhalten()
            self.assertEqual(warte_auf(s.ergebnisse), [("kein_programm", "test.xml", None)])
            self.assertEqual(irc.stopps, 0)
            irc.fehler = "Exception calling application: App not installed or enabled"
            s.anhalten()
            art, _, text = warte_auf(s.ergebnisse)[0]
            self.assertEqual((art, text), ("fehler", "App WebotsSim in iRC nicht installiert oder nicht freigeschaltet"))
        finally:
            s.beenden()


if __name__ == "__main__":
    unittest.main()
