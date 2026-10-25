"""Tests fuer den Simulationsverwalter (tools/verwalter.py) und die Schuelerseiten,
ohne systemd, sudo und Webots."""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import uebersicht  # noqa: E402
import verwalter  # noqa: E402

WBT = 'WorldInfo {\n  info [\n    "Info aus der Welt"\n  ]\n  title "Titel aus der Welt"\n}\n'


class FalscherHelfer:
    """Macht, was deploy/platz.sh tut, nur mit Dateien in einem Testordner."""

    def __init__(self, konf, uhr):
        self.konf, self.uhr, self.aufrufe = konf, uhr, []

    def __call__(self, befehl, platz, *rest):
        self.aufrufe.append((befehl, platz) + rest)
        pfad = os.path.join(self.konf, "%s.env" % platz)
        if befehl == "anlegen":
            pc, welt = rest[0], rest[1]
            with open(pfad, "w", encoding="utf-8") as f:
                f.write("IRC_HOST=%s\nWELT=%s\nGESTARTET=%d\nDAUERHAFT=0\n" % (pc, welt, self.uhr()))
        elif befehl == "entfernen" and os.path.exists(pfad):
            os.remove(pfad)
        return ""


class Grundlage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.konf = os.path.join(self.tmp.name, "etc")
        self.stat = os.path.join(self.tmp.name, "run")
        self.welten = os.path.join(self.tmp.name, "worlds")
        for d in (self.konf, self.stat, self.welten):
            os.makedirs(d)
        for name in ("tisch.wbt", "magazin.wbt", "geheim.wbt"):
            with open(os.path.join(self.welten, name), "w", encoding="utf-8") as f:
                f.write(WBT)
        self.schreibe(self.welten, "magazin.json", {"titel": "Magazin", "aufgabe": "Fuelle das Magazin."})
        self.schreibe(self.welten, "geheim.json", {"sichtbar": False})
        for name in ("magazin.png", "magazin.pdf", "kaputt name.wbt"):
            with open(os.path.join(self.welten, name), "wb") as f:
                f.write(b"x")
        self.schreibe(self.konf, "verwalter.json", {"max_simulationen": 2, "leerlauf_minuten": 15})
        self.jetzt = 1_800_000_000.0
        self.aktiv = set()
        self.helfer = FalscherHelfer(self.konf, lambda: self.jetzt)
        self.v = verwalter.Verwalter(
            plaetze_fn=lambda: uebersicht.lese_plaetze(self.konf),
            status_fn=lambda p: uebersicht.lese_status(p, self.stat),
            aktiv_fn=lambda p: p in self.aktiv,
            einstellungen_fn=lambda: verwalter.lade_einstellungen(self.konf),
            welten_fn=lambda: verwalter.lese_welten(self.welten),
            helfer=self.helfer, uhr=lambda: self.jetzt, log=lambda t: None,
            befehl_fn=lambda platz, befehl: uebersicht.schreibe_befehl(platz, befehl, self.stat))

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def schreibe(ordner, name, daten):
        with open(os.path.join(ordner, name), "w", encoding="utf-8") as f:
            json.dump(daten, f)

    def status(self, platz, **werte):
        self.schreibe(self.stat, "platz-%s.json" % platz, werte)


class TestWelten(Grundlage):
    def test_angaben_aus_json_wbt_und_dateien(self):
        w = {x["datei"]: x for x in verwalter.lese_welten(self.welten)}
        self.assertEqual(sorted(w), ["geheim.wbt", "magazin.wbt", "tisch.wbt"])   # "kaputt name" fehlt
        self.assertEqual((w["tisch.wbt"]["titel"], w["tisch.wbt"]["aufgabe"]), ("Titel aus der Welt", "Info aus der Welt"))
        self.assertEqual((w["magazin.wbt"]["titel"], w["magazin.wbt"]["aufgabe"]), ("Magazin", "Fuelle das Magazin."))
        self.assertEqual((w["magazin.wbt"]["bild"], w["magazin.wbt"]["pdf"]), ("magazin.png", "magazin.pdf"))
        self.assertIsNone(w["tisch.wbt"]["bild"])
        self.assertFalse(w["geheim.wbt"]["sichtbar"])

    def test_echte_beispielwelt(self):
        w = {x["datei"]: x for x in verwalter.lese_welten(os.path.join(ROOT, "vorlagen", "welten"))}
        self.assertEqual(w["rebel_tisch.wbt"]["titel"], "Stift umsetzen")
        self.assertIsInstance(w["rebel_tisch.wbt"]["sichtbar"], bool)   # im Container stellt die Lehrkraft das um


class TestVergabe(Grundlage):
    def test_freier_slot_und_einer_pro_pc(self):
        self.assertEqual(self.v.starten("192.168.2.10", "tisch.wbt"), "01")
        self.assertEqual(self.v.starten("192.168.2.11", "tisch.wbt"), "02")
        self.assertEqual(self.v.starten("192.168.2.10", "tisch.wbt"), "01")   # schon da: nichts Neues
        self.assertEqual(self.helfer.aufrufe, [("anlegen", "01", "192.168.2.10", "tisch.wbt"),
                                               ("anlegen", "02", "192.168.2.11", "tisch.wbt")])
        self.assertEqual(self.v.frei(), 0)
        with self.assertRaises(verwalter.VerwalterFehler) as f:
            self.v.starten("192.168.2.12", "tisch.wbt")
        self.assertIn("Alle 2 Simulationen sind belegt", str(f.exception))

    def test_luecke_wird_wieder_vergeben(self):
        self.v.starten("pc-a", "tisch.wbt")
        self.v.starten("pc-b", "tisch.wbt")
        self.v.beenden_fuer("pc-a")
        self.assertEqual(self.v.starten("pc-c", "tisch.wbt"), "01")

    def test_welt_wechseln_im_selben_slot(self):
        self.v.starten("pc-a", "tisch.wbt")
        self.assertEqual(self.v.starten("pc-a", "magazin.wbt"), "01")
        self.assertEqual(self.helfer.aufrufe[-1], ("anlegen", "01", "pc-a", "magazin.wbt"))
        self.assertEqual(self.v.platz_von("pc-a")["welt"], "magazin.wbt")

    def test_ungueltiges_wird_abgelehnt(self):
        for pc, welt in (("pc-a", "gibtsnicht.wbt"), ("pc-a", "geheim.wbt"), ("pc-a", "../x.wbt"),
                         ("pc a;rm", "tisch.wbt"), ("", "tisch.wbt")):
            with self.assertRaises(verwalter.VerwalterFehler, msg=(pc, welt)):
                self.v.starten(pc, welt)
        self.assertEqual(self.helfer.aufrufe, [])

    def test_zuruecksetzen_und_beenden_nur_eigene(self):
        with self.assertRaises(verwalter.VerwalterFehler):
            self.v.zuruecksetzen("pc-a")
        self.v.starten("pc-a", "tisch.wbt")
        self.v.zuruecksetzen("pc-a")
        self.v.beenden_fuer("pc-a")
        self.assertEqual([a[0] for a in self.helfer.aufrufe], ["anlegen", "neustart", "entfernen"])
        self.assertIsNone(self.v.platz_von("pc-a"))


    def befehl(self, platz):
        """Befehl lesen und entfernen wie der Controller (irc_bridge.hole_befehl)."""
        pfad = os.path.join(self.stat, "platz-%s.befehl" % platz)
        with open(pfad, encoding="utf-8") as f:
            befehl = f.read().strip()
        os.remove(pfad)
        return befehl

    def test_teile_zuruecksetzen_ohne_neustart(self):
        with self.assertRaises(verwalter.VerwalterFehler):
            self.v.teile_zuruecksetzen("pc-a")             # keine Simulation
        self.v.starten("pc-a", "tisch.wbt")
        with self.assertRaises(verwalter.VerwalterFehler):
            self.v.teile_zuruecksetzen("pc-a")             # Controller meldet sich (noch) nicht
        self.status("01", verbunden=True)
        self.v.teile_zuruecksetzen("pc-a")
        self.assertEqual(self.befehl("01"), "teile_zuruecksetzen")
        self.v.teile_zuruecksetzen_platz("1")
        self.assertEqual(self.befehl("01"), "teile_zuruecksetzen")
        self.assertEqual([a[0] for a in self.helfer.aufrufe], ["anlegen"])   # kein Neustart

    def test_kollision_quittieren(self):
        with self.assertRaises(verwalter.VerwalterFehler):
            self.v.kollision_quittieren("pc-a")            # keine Simulation
        self.v.starten("pc-a", "tisch.wbt")
        with self.assertRaises(verwalter.VerwalterFehler):
            self.v.kollision_quittieren("pc-a")            # Controller meldet sich (noch) nicht
        self.status("01", verbunden=True)
        self.v.kollision_quittieren("pc-a")
        self.assertEqual(self.befehl("01"), "kollision_quittieren")

    def test_taster(self):
        with self.assertRaises(verwalter.VerwalterFehler):
            self.v.taster("pc-a", 0, True)
        self.v.starten("pc-a", "tisch.wbt")
        self.v.taster("pc-a", 2, True)
        self.v.taster("pc-a", 2, False)
        self.assertEqual(self.befehl("01").splitlines(), ["taster 2 1", "taster 2 0"])
        for nr, an in ((16, True), (-1, True), ("1", True), (True, True), (1, "ja")):
            with self.assertRaises(verwalter.VerwalterFehler, msg=(nr, an)):
                self.v.taster("pc-a", nr, an)


class TestAutomatischesEnde(Grundlage):
    def test_leerlauf(self):
        self.assertEqual(verwalter.leerlauf_s(None, 100.0, 400.0), 300.0)
        self.assertEqual(verwalter.leerlauf_s({"_alter_s": 1, "sekunden_seit_irc": 7.0}, 100.0, 400.0), 7.0)
        self.assertEqual(verwalter.leerlauf_s({"_alter_s": 1, "sekunden_seit_irc": None, "laufzeit_s": 50}, 0, 9e9), 50)
        self.assertEqual(verwalter.leerlauf_s({"_alter_s": 99, "sekunden_seit_irc": 1.0}, 100.0, 400.0), 300.0)

    def test_ohne_irc_nach_15_minuten(self):
        self.v.starten("pc-a", "tisch.wbt")
        self.v.starten("pc-b", "tisch.wbt")
        self.aktiv.update({"01", "02"})
        self.jetzt += 16 * 60
        self.status("01", sekunden_seit_irc=0.2)            # iRC verbunden
        self.status("02", sekunden_seit_irc=None, laufzeit_s=16 * 60)   # nie verbunden
        self.assertEqual(self.v.pruefen(), [("02", "15 Minuten ohne Verbindung zu iRC")])
        self.assertEqual(self.v.meldungen["pc-b"], "15 Minuten ohne Verbindung zu iRC")
        self.assertIsNotNone(self.v.platz_von("pc-a"))
        self.v.starten("pc-b", "tisch.wbt")     # neu gestartet: Meldung ist weg
        self.assertNotIn("pc-b", self.v.meldungen)

    def test_stundenende_und_dauerhaft(self):
        self.schreibe(self.konf, "verwalter.json", {"ende_uhrzeiten": ["09:30", "kaputt"]})
        t = time.localtime(self.jetzt)
        self.jetzt = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 9, 29, 0, 0, 0, -1))
        self.v.letzte_pruefung = self.jetzt
        self.v.starten("pc-a", "tisch.wbt")
        with open(os.path.join(self.konf, "99.env"), "w", encoding="utf-8") as f:
            f.write("IRC_HOST=192.168.3.11\nDAUERHAFT=1\n")
        self.aktiv.update({"01", "99"})
        self.status("01", sekunden_seit_irc=0.1)
        self.assertEqual(self.v.pruefen(), [])
        self.jetzt += 120   # 09:31
        self.assertEqual(self.v.pruefen(), [("01", "Stundenende")])
        self.assertEqual([p["platz"] for p in uebersicht.lese_plaetze(self.konf)], ["99"])
        self.v.starten("pc-a", "tisch.wbt")
        self.jetzt += 60
        self.assertEqual(self.v.pruefen(), [])   # Stundenende nur einmal

    def test_nicht_laufende_simulation_wird_aufgeraeumt(self):
        self.v.starten("pc-a", "tisch.wbt")
        self.assertEqual(self.v.pruefen(), [])   # Dienst startet gerade
        self.jetzt += verwalter.ANLAUF_S + 1
        self.assertEqual(self.v.pruefen(), [("01", "Simulation lief nicht mehr")])

    def test_dauerhafter_platz_zaehlt_nicht_zum_pool(self):
        with open(os.path.join(self.konf, "99.env"), "w", encoding="utf-8") as f:
            f.write("IRC_HOST=192.168.3.11\nDAUERHAFT=1\n")
        self.assertEqual(self.v.frei(), 2)


class TestSchuelerseiten(Grundlage):
    """Ueber HTTP, wie ein Schueler-PC (keine Lehrkraft)."""

    def setUp(self):
        super().setUp()
        self.alt = (uebersicht.KONFIG_DIR, uebersicht.STATUS_DIR, uebersicht.WELTEN_DIR, uebersicht.dienst_aktiv,
                    uebersicht.ist_lehrer, uebersicht.VERWALTER, uebersicht.ZUSTAND_DIR)
        uebersicht.KONFIG_DIR, uebersicht.STATUS_DIR, uebersicht.WELTEN_DIR = self.konf, self.stat, self.welten
        uebersicht.ZUSTAND_DIR = os.path.join(self.tmp.name, "lib")
        uebersicht.dienst_aktiv = lambda p: True
        self.lehrer = False
        uebersicht.ist_lehrer = lambda pc, token=None: self.lehrer
        uebersicht.VERWALTER = self.v
        self.server = uebersicht.ThreadingHTTPServer(("127.0.0.1", 0), uebersicht.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.basis = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        (uebersicht.KONFIG_DIR, uebersicht.STATUS_DIR, uebersicht.WELTEN_DIR, uebersicht.dienst_aktiv,
         uebersicht.ist_lehrer, uebersicht.VERWALTER, uebersicht.ZUSTAND_DIR) = self.alt
        super().tearDown()

    def holen(self, pfad):
        with urllib.request.urlopen(self.basis + pfad, timeout=5) as r:
            return r.read().decode("utf-8")

    def senden(self, pfad, daten=None, typ="application/json"):
        anfrage = urllib.request.Request(self.basis + pfad, data=json.dumps(daten or {}).encode(),
                                         headers={"Content-Type": typ}, method="POST")
        try:
            with urllib.request.urlopen(anfrage, timeout=5) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.load(e)

    def code(self, pfad):
        try:
            with urllib.request.urlopen(self.basis + pfad, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            e.close()
            return e.code

    def test_ablauf_starten_ansehen_beenden(self):
        wahl = self.holen("/")
        self.assertIn("Dein PC: 127.0.0.1", wahl)
        self.assertIn('data-welt="magazin.wbt"', wahl)
        self.assertNotIn("geheim.wbt", wahl)                   # nicht freigegeben
        self.assertIn("/welten/magazin.png", wahl)
        self.assertIn("Noch 2 von 2 Simulationen frei", wahl)

        self.assertEqual(self.senden("/api/starten", {"welt": "magazin.wbt"}), (200, {"ok": True, "platz": "01"}))
        ansicht = self.holen("/")
        self.assertIn("location.host + '/sim/01'", ansicht)
        self.assertIn("knopf-reset", ansicht)
        self.assertIn("Fuelle das Magazin.", ansicht)
        self.assertIn("/welten/magazin.pdf", ansicht)
        ich = json.loads(self.holen("/api/ich"))
        self.assertEqual((ich["platz"]["platz"], ich["platz"]["welt"]), ("01", "magazin.wbt"))
        self.assertIn("läuft gerade", self.holen("/?wechseln=1"))

        self.assertIn("knopf-teile", ansicht)
        self.status("01", verbunden=True)
        self.assertEqual(self.senden("/api/teile_zuruecksetzen")[0], 200)
        self.assertTrue(os.path.exists(os.path.join(self.stat, "platz-01.befehl")))
        self.assertEqual(self.senden("/api/kollision_quittieren")[0], 200)
        with open(os.path.join(self.stat, "platz-01.befehl"), encoding="utf-8") as f:
            self.assertIn("kollision_quittieren", f.read())
        self.assertIn("const KOLLISION_ZEIGEN = true;", ansicht)
        self.assertIn('id="kollision-quittieren"', ansicht)
        self.status("01", verbunden=True, crash={"zustand": "crash", "a": "Greifer", "b": "magazin"})
        self.assertEqual(json.loads(self.holen("/api/platz/01"))["crash"]["zustand"], "crash")
        self.assertEqual(self.senden("/api/taster", {"nr": 0, "an": True})[0], 200)
        self.assertEqual(self.senden("/api/taster", {"nr": 0, "an": "ja"})[0], 409)
        self.assertEqual(self.senden("/api/zuruecksetzen")[0], 200)
        self.assertEqual(self.senden("/api/beenden")[0], 200)
        self.assertIsNone(json.loads(self.holen("/api/ich"))["platz"])
        self.assertEqual(self.senden("/api/beenden")[0], 409)

    def test_taster_und_leuchten_auf_der_seite(self):
        self.schreibe(self.welten, "tisch.json", {"titel": "Tisch", "ea": {
            "taster": [{"name": "Start <1>", "din": 21, "art": "oeffner", "modus": "rastend"}],
            "leuchten": [{"name": "Greifer", "dout": 32, "farbe": "rot"}],
            "sensoren": [{"name": "Platz 1", "din": 25, "knoten": "SENSOR_0"},
                         {"name": "Farbe", "typ": "farbe", "ausgabe": "bits", "din": 30, "bits": 2, "knoten": "SENSOR_1",
                          "farben": [{"farbe": "rot", "code": 1}, {"farbe": "blau", "code": 2}]},
                         {"name": "Farbe 2", "typ": "farbe", "knoten": "SENSOR_2",
                          "farben": [{"farbe": "gelb", "din": 40}]}],
            "naeherung": [{"name": "Metall?", "knoten": "NAEHERUNG_0", "typ": "induktiv", "bauform": "M12", "din": 22}]}})
        self.senden("/api/starten", {"welt": "tisch.wbt"})
        ansicht = self.holen("/")
        self.assertIn('class="taster" data-nr="0"', ansicht)
        self.assertIn("Start &lt;1&gt;", ansicht)
        self.assertIn("DIn21 · Öffner · rastend", ansicht)
        self.assertIn('data-farbe="rot"', ansicht)
        self.assertIn('class="sensor" data-nr="0"', ansicht)
        self.assertIn("Sensor · DIn25 · Schließer", ansicht)
        self.assertIn("Farbsensor · Code an DIn30–31: rot=1, blau=2", ansicht)
        self.assertIn("Farbsensor · gelb→DIn40", ansicht)
        self.assertIn("const EA_STEUERN = true;", ansicht)
        self.assertIn("Metall?<small>Induktiv M12 · Sn 2 mm · DIn22 · Schließer", ansicht)
        self.status("01", taster=[True], leuchten=[False], sensoren=[True], ea_ok=False, ea_meldung="App fehlt")
        d = json.loads(self.holen("/api/platz/01"))
        self.assertEqual((d["taster"], d["leuchten"], d["sensoren"], d["ea_ok"]), ([True], [False], [True], False))
        self.schreibe(self.welten, "tisch.json", {"titel": "Tisch"})
        self.assertNotIn("EA_STEUERN", self.holen("/"))

    def test_ansichten_und_greiferdaten(self):
        self.senden("/api/starten", {"welt": "tisch.wbt"})
        ansicht = self.holen("/")
        for knopf in ("standard", "vorne", "seite", "oben", "greifer"):
            self.assertIn('data-ansicht="%s"' % knopf, ansicht)
        self.assertIn("fetch('/api/platz/01'", ansicht)
        self.assertIn("(i + 1) % 3", ansicht)   # %%-Platzhalter richtig aufgeloest
        self.status("01", tcp=[0.35, 0, 0.016], tcp_y=[0, 1, 0], hilfe={"text": "stift: Greifstellung erreicht"},
                    sekunden_seit_irc=0.1, geheim="x")
        d = json.loads(self.holen("/api/platz/1"))
        self.assertEqual((d["tcp"], d["tcp_y"], d["hilfe"]["text"]),
                         ([0.35, 0, 0.016], [0, 1, 0], "stift: Greifstellung erreicht"))
        self.assertNotIn("geheim", d)

    def test_fremdes_ist_gesperrt(self):
        self.helfer("anlegen", "02", "192.168.2.99", "tisch.wbt")
        for pfad in ("/status.json", "/platz/02", "/sim/02/", "/sim/02/meshes/x.obj", "/api/platz/02",
                     "/api/lehrer/welten", "/api/lehrer/einstellungen"):
            self.assertEqual(self.code(pfad), 403, pfad)
        self.assertIn("Benutzername", self.holen("/lehrer"))   # Anmeldung statt Lehrerseite
        self.lehrer = True
        self.assertIn("Alle beenden", self.holen("/lehrer"))
        self.assertEqual(self.code("/platz/02"), 200)

    def test_nur_json_und_nur_erlaubte_dateien(self):
        self.assertEqual(self.senden("/api/starten", {"welt": "tisch.wbt"}, typ="text/plain")[0], 415)
        self.assertEqual(self.senden("/api/starten", {"welt": "geheim.wbt"})[0], 409)
        self.assertEqual(self.code("/welten/magazin.pdf"), 200)
        for pfad in ("/welten/magazin.json", "/welten/tisch.wbt", "/welten/..%2Fetc.png", "/welten/fehlt.png"):
            self.assertEqual(self.code(pfad), 404, pfad)

    def test_alle_belegt(self):
        self.helfer("anlegen", "01", "pc-x", "tisch.wbt")
        self.helfer("anlegen", "02", "pc-y", "tisch.wbt")
        wahl = self.holen("/")
        self.assertIn("Alle 2 Simulationen sind belegt", wahl)
        self.assertIn("disabled", wahl)


if __name__ == "__main__":
    unittest.main()
