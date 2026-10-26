"""Messkoffer (tools/messkoffer.py): Auswertung der CRI-Messung und das Paket, ohne Netz."""

import json
import os
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import messkoffer  # noqa: E402
import rebel_kinematik as kin  # noqa: E402


class TestMesskoffer(unittest.TestCase):
    def test_auswertung_wie_irc(self):
        # gemessen am Laptop-iRC (V15) mit EGP 40 und Standardbacken, TCP 143.68 mm
        e = messkoffer.auswerten([20, 30, 35, 25, 40, 20], [584.6, 291.6, 532.5, 138.45, 45.69, 107.0])
        self.assertTrue(e["orientierung_passt"])
        self.assertAlmostEqual(e["werkzeug_mm"][2], 143.68, delta=0.1)
        self.assertLess(max(abs(v) for v in e["werkzeug_mm"][:2]), 0.1)
        # andere Reihenfolge (A und C vertauscht) wuerde erkannt
        self.assertFalse(messkoffer.auswerten([20, 30, 35, 25, 40, 20], [584.6, 291.6, 532.5, 107.0, 45.69, 138.45])
                         ["orientierung_passt"])

    def test_stellungen_bleiben_ueber_dem_tisch(self):
        for titel, q in messkoffer.STELLUNGEN:
            self.assertEqual((q[1], q[2]), (0, 90), titel)   # Arm wie in Home
            self.assertLessEqual(abs(q[4] - 90), 30, titel)   # Greifer kippt hoechstens 30 Grad
            p, _r = kin.fk(q, 0.15)
            self.assertGreater(p[2], 0.15, titel)               # TCP weit ueber dem Tisch

    def test_paket_ohne_fremde_dateien(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = messkoffer.Sitzung(os.path.join(tmp, "2026-10-27_0800"))
            s.merken("sps", "192.168.1.20")
            s.json("cri_rebel", {"messungen": []})
            s.json("cri_rebel", {"messungen": [1]})   # Wiederholung: eigene Datei
            s.text("notiz", "Backen quer")
            ziel = messkoffer.schritt_fertig(s)
            self.assertEqual(os.path.basename(ziel), "messung_2026-10-27_0800.zip")
            with zipfile.ZipFile(ziel) as z:
                self.assertEqual(sorted(z.namelist()), ["cri_rebel.json", "cri_rebel_2.json", "einstellungen.json", "notiz.txt"])
                self.assertEqual(json.loads(z.read("einstellungen.json")), {"sps": "192.168.1.20"})


if __name__ == "__main__":
    unittest.main()
