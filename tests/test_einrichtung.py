"""Tests fuer die Einrichtung (tools/einrichtung.py): Code, erstes Konto, iRC-Daten, Verbindungstest, Exportskript.
Die Seiten und Schnittstellen dazu testet test_lehrer.py (TestLehrerSeiten)."""

import os
import socket
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import einrichtung  # noqa: E402
import lehrer  # noqa: E402

ROBOTER = "Data\\Robots\\REBEL\\REBEL-6DOF-03\\"
GREIFER = "Data\\Tools\\"


def zip_datei(pfad, eintraege):
    with zipfile.ZipFile(pfad, "w") as z:
        for name, inhalt in eintraege.items():
            z.writestr(name, inhalt)
    return pfad


class TestCode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.zustand = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_code_bleibt_bis_neu(self):
        code = einrichtung.code_holen(self.zustand)
        self.assertRegex(code, r"^\d{4}-\d{4}-\d{4}$")
        self.assertEqual(einrichtung.code_holen(self.zustand), code)
        neu = einrichtung.code_holen(self.zustand, neu=True)
        self.assertNotEqual(neu, code)   # 1 : 10^12, dass es zufaellig derselbe ist
        self.assertFalse(einrichtung.code_pruefen(self.zustand, code))
        self.assertTrue(einrichtung.code_pruefen(self.zustand, neu))
        self.assertTrue(einrichtung.code_pruefen(self.zustand, " " + neu.replace("-", " ") + " "))
        self.assertFalse(einrichtung.code_pruefen(self.zustand, ""))
        if os.name != "nt":   # nur der Dienst darf den Code lesen
            self.assertEqual(os.stat(os.path.join(self.zustand, "einrichtung.json")).st_mode & 0o777, 0o600)

    def test_ohne_code_kein_pruefen(self):
        self.assertFalse(einrichtung.code_pruefen(self.zustand, "0000-0000-0000"))

    def test_erstes_konto_loescht_code(self):
        code = einrichtung.code_holen(self.zustand)
        with self.assertRaises(einrichtung.EinrichtungFehler):
            einrichtung.erstes_konto(self.zustand, "1111-2222-3333", "goeb", "Herr Göbel", "geheim123")
        self.assertFalse(lehrer.hat_benutzer(self.zustand))
        with self.assertRaises(lehrer.LehrerFehler):   # zu kurzes Passwort: kein Konto, Code bleibt
            einrichtung.erstes_konto(self.zustand, code, "goeb", "", "kurz")
        b = einrichtung.erstes_konto(self.zustand, code, "Goeb", "Herr Göbel", "geheim123")
        self.assertEqual((b["name"], b["rolle"], b["muss_aendern"]), ("goeb", "admin", False))
        self.assertNotIn("code", einrichtung.lies(self.zustand))
        self.assertFalse(einrichtung.code_pruefen(self.zustand, code))
        self.assertIsNone(einrichtung.code_holen(self.zustand))   # mit Konto gibt es keinen Code mehr
        with self.assertRaises(einrichtung.EinrichtungFehler):
            einrichtung.erstes_konto(self.zustand, code, "zweiter", "", "geheim123")


class TestIrcDaten(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ziel = os.path.join(self.tmp.name, "irc")

    def tearDown(self):
        self.tmp.cleanup()

    def zip(self, eintraege, name="irc-daten.zip"):
        return zip_datei(os.path.join(self.tmp.name, name), eintraege)

    def test_zip_aus_dem_exportskript(self):
        z = self.zip({ROBOTER + "REBEL-6DOF-03.xml": "<Robot/>", ROBOTER + "Joint1.obj": "o",
                      GREIFER + "Schunk_EGP40_RebelMountingPlate.xml": "<Tool/>", GREIFER + "Schunk_EGP40_closed.obj": "o"})
        bericht = einrichtung.irc_daten_entpacken(z, self.ziel)
        self.assertEqual(bericht, {"dateien": 4, "greifer": ["Schunk_EGP40_RebelMountingPlate.xml"]})
        self.assertTrue(os.path.isfile(os.path.join(self.ziel, "Data", "Robots", "REBEL", "REBEL-6DOF-03", "Joint1.obj")))
        self.assertTrue(os.path.isfile(os.path.join(self.ziel, "Data", "Tools", "Schunk_EGP40_closed.obj")))

    def test_ganzer_data_ordner_und_handgepackt(self):
        # Data-Ordner von iRC (mit Unterordner und fremden Robotern): nur ReBeL und Schunk_EGP kommen mit
        z = self.zip({"Data/Robots/REBEL/REBEL-6DOF-03/REBEL-6DOF-03.xml": "r", "Data/Robots/REBEL/REBEL-6DOF-03/m/a.obj": "o",
                      "Data/Robots/DLE/DLE-RG-01/DLE-RG-01.xml": "x", "Data/Tools/Schunk_EGP25_RebelMountingPlate.xml": "t",
                      "Data/Tools/Festo_xyz.xml": "f", "Data/Programs/demo.xml": "p"}, "data.zip")
        self.assertEqual(einrichtung.irc_daten_entpacken(z, self.ziel)["dateien"], 3)
        self.assertTrue(os.path.isfile(os.path.join(self.ziel, "Data", "Robots", "REBEL", "REBEL-6DOF-03", "m", "a.obj")))
        self.assertFalse(os.path.exists(os.path.join(self.ziel, "Data", "Robots", "DLE")))
        self.assertFalse(os.path.exists(os.path.join(self.ziel, "Data", "Programs")))
        # von Hand: Ordner REBEL-6DOF-03 und die Schunk-Dateien nebeneinander
        ziel2 = os.path.join(self.tmp.name, "irc2")
        z = self.zip({"REBEL-6DOF-03/REBEL-6DOF-03.xml": "r", "Schunk_EGP40_RebelMountingPlate_Finger.xml": "t"}, "hand.zip")
        einrichtung.irc_daten_entpacken(z, ziel2)
        self.assertTrue(os.path.isfile(os.path.join(ziel2, "Data", "Robots", "REBEL", "REBEL-6DOF-03", "REBEL-6DOF-03.xml")))

    def test_fehler(self):
        with open(os.path.join(self.tmp.name, "kein.zip"), "wb") as f:
            f.write(b"PK nicht wirklich")
        for z, text in ((os.path.join(self.tmp.name, "kein.zip"), "keine ZIP"),
                        (self.zip({"Data/Tools/Schunk_EGP40_RebelMountingPlate.xml": "t"}, "a.zip"), "fehlt der Roboter"),
                        (self.zip({ROBOTER + "REBEL-6DOF-03.xml": "r"}, "b.zip"), "Schunk-Greifer")):
            with self.assertRaises(einrichtung.EinrichtungFehler) as e:
                einrichtung.irc_daten_entpacken(z, os.path.join(self.tmp.name, "x"))
            self.assertIn(text, str(e.exception))

    def test_pfade_bleiben_im_ziel(self):
        z = self.zip({"../../REBEL-6DOF-03/REBEL-6DOF-03.xml": "r", "REBEL-6DOF-03/../../../boese.txt": "b",
                      "C:\\Windows\\Schunk_EGP40_RebelMountingPlate.xml": "t"})
        einrichtung.irc_daten_entpacken(z, self.ziel)
        for wurzel, _ordner, dateien in os.walk(self.tmp.name):
            for d in dateien:
                pfad = os.path.join(wurzel, d)
                self.assertTrue(pfad.startswith(self.ziel) or pfad.endswith(".zip"), pfad)

    def test_einspielen_tauscht_und_stellt_bei_fehler_wieder_her(self):
        alt = zip_datei(os.path.join(self.tmp.name, "alt.zip"), {ROBOTER + "REBEL-6DOF-03.xml": "alt",
                                                                 GREIFER + "Schunk_EGP40_RebelMountingPlate.xml": "t"})
        neu = zip_datei(os.path.join(self.tmp.name, "neu.zip"), {ROBOTER + "REBEL-6DOF-03.xml": "neu",
                                                                 GREIFER + "Schunk_EGP40_RebelMountingPlate.xml": "t"})
        xml = os.path.join(self.ziel, "Data", "Robots", "REBEL", "REBEL-6DOF-03", "REBEL-6DOF-03.xml")
        self.assertEqual(einrichtung.irc_daten_einspielen(alt, self.ziel, lambda: {"egp40": {}})["modell"], {"egp40": {}})

        def kaputt():
            raise lehrer.LehrerFehler("Generator streikt")
        with self.assertRaises(lehrer.LehrerFehler):
            einrichtung.irc_daten_einspielen(neu, self.ziel, kaputt)
        with open(xml, encoding="utf-8") as f:
            self.assertEqual(f.read(), "alt")
        einrichtung.irc_daten_einspielen(neu, self.ziel, lambda: {})
        with open(xml, encoding="utf-8") as f:
            self.assertEqual(f.read(), "neu")
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["alt.zip", "irc", "neu.zip"])   # keine .neu/.alt-Reste


class TestPcUndExport(unittest.TestCase):
    def test_pc_pruefen(self):
        class Verbindung:
            def close(self):
                pass

        def verbinden(adresse, wartezeit):
            port = adresse[1]
            if port == 3921:
                return Verbindung()
            if port == 3920:
                raise ConnectionRefusedError(111, "Connection refused")
            raise socket.timeout()
        ergebnis = einrichtung.pc_pruefen("192.168.7.21", verbinden)
        self.assertEqual([(p["port"], p["offen"]) for p in ergebnis], [(3921, True), (3920, False), (5000, False)])
        self.assertIn("läuft iRC", ergebnis[1]["fehler"])
        self.assertIn("Firewall", ergebnis[2]["fehler"])
        for falsch in ("", "a b", "http://x", "-x", "1.2.3.4;rm"):
            with self.assertRaises(einrichtung.EinrichtungFehler):
                einrichtung.pc_pruefen(falsch, verbinden)

        def unbekannt(adresse, wartezeit):
            raise socket.gaierror(-2, "Name or service not known")
        with self.assertRaises(einrichtung.EinrichtungFehler):
            einrichtung.pc_pruefen("r212-pc07", unbekannt)

    def test_export_cmd(self):
        with open(os.path.join(ROOT, "tools", "irc_daten_export.ps1"), encoding="ascii") as f:
            ps1 = f.read()
        cmd = einrichtung.export_cmd(ps1).decode("ascii")
        self.assertTrue(cmd.startswith("@echo off\r\n"))
        self.assertNotIn("\n", cmd.replace("\r\n", ""))   # nur CRLF (cmd.exe)
        kopf, _, rest = cmd.partition("\r\n#POWERSHELL\r\n")
        self.assertIn("exit /b", kopf)                     # cmd.exe liest nicht bis ins PowerShell-Skript
        self.assertIn("$env:REBEL_SELBST", kopf)
        self.assertIn("[char]10 + '#POWERSHELL'", kopf)   # die Suchmarke steht im Kopf nicht am Zeilenanfang
        self.assertEqual(cmd.count("\n#POWERSHELL"), 1)
        self.assertEqual(rest.replace("\r\n", "\n").rstrip("\n"), ps1.replace("\r\n", "\n").rstrip("\n"))
        self.assertTrue(rest.lstrip().startswith("# Packt"))


if __name__ == "__main__":
    unittest.main()
