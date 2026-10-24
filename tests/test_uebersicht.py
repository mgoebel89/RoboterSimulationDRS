"""Tests fuer die Uebersichtsseite (tools/uebersicht.py), ohne systemd und Webots."""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import uebersicht  # noqa: E402


class Grundlage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.konf = os.path.join(self.tmp.name, "etc")
        self.stat = os.path.join(self.tmp.name, "run")
        os.makedirs(self.konf)
        os.makedirs(self.stat)
        with open(os.path.join(self.konf, "01.env"), "w", encoding="utf-8") as f:
            f.write("# Platz 1\nIRC_HOST=192.168.2.142\n")
        with open(os.path.join(self.konf, "07.env"), "w", encoding="utf-8") as f:
            f.write('IRC_HOST="R212-PC07"\nWELT=lf7.wbt\nSTREAM_PORT=1300\n')
        with open(os.path.join(self.konf, "notizen.txt"), "w", encoding="utf-8") as f:
            f.write("kein Platz")

    def tearDown(self):
        self.tmp.cleanup()

    def status(self, platz, **werte):
        daten = {"verbunden": True, "sekunden_seit_irc": 0.1, "teil": None}
        daten.update(werte)
        with open(os.path.join(self.stat, "platz-%s.json" % platz), "w", encoding="utf-8") as f:
            json.dump(daten, f)


class TestPlaetze(Grundlage):
    def test_plaetze_aus_env_dateien(self):
        p = uebersicht.lese_plaetze(self.konf)
        self.assertEqual([x["platz"] for x in p], ["01", "07"])
        self.assertEqual({k: p[0][k] for k in ("platz", "irc_host", "welt", "stream_port", "dauerhaft")},
                         {"platz": "01", "irc_host": "192.168.2.142", "welt": "rebel_tisch.wbt",
                          "stream_port": 1241, "dauerhaft": False})
        # ohne GESTARTET (Platz von vor dem Verwalter) gilt die Aenderungszeit der Datei
        self.assertAlmostEqual(p[0]["gestartet"], os.path.getmtime(os.path.join(self.konf, "01.env")))
        self.assertEqual((p[1]["irc_host"], p[1]["welt"], p[1]["stream_port"]), ("R212-PC07", "lf7.wbt", 1300))

    def test_fehlender_ordner(self):
        self.assertEqual(uebersicht.lese_plaetze(os.path.join(self.tmp.name, "gibtsnicht")), [])


class TestAmpel(Grundlage):
    def test_ampel_faelle(self):
        a = uebersicht.ampel
        self.assertEqual(a(None, False)[0], "rot")
        self.assertEqual(a(None, True)[0], "rot")
        self.assertEqual(a({"_alter_s": 30, "verbunden": True}, True)[0], "rot")
        self.assertEqual(a({"_alter_s": 1, "verbunden": False}, True),
                         ("gelb", "wartet auf iRC - iRC am PC starten"))
        self.assertEqual(a({"_alter_s": 1, "verbunden": True, "sekunden_seit_irc": 12}, True)[0], "gelb")
        self.assertEqual(a({"_alter_s": 1, "verbunden": True, "sekunden_seit_irc": 0.1}, True), ("gruen", "verbunden"))
        self.assertEqual(a({"_alter_s": 1, "verbunden": True, "teil": "wuerfel"}, True)[1], "verbunden - greift wuerfel")

    def test_gesamtstatus(self):
        self.status("01", gelenke_grad=[0, 20, 97, 0, 62, 0], abweichung_mm=0.01)
        plaetze = uebersicht.lese_plaetze(self.konf)
        g = uebersicht.gesamtstatus(plaetze, aktiv_fn=lambda p: p == "01", status_dir=self.stat)
        self.assertEqual([(x["platz"], x["farbe"]) for x in g], [("01", "gruen"), ("07", "rot")])
        self.assertEqual(g[0]["gelenke_grad"], [0, 20, 97, 0, 62, 0])


class TestServer(Grundlage):
    def test_seiten_ueber_http(self):
        self.status("01")
        alt = (uebersicht.KONFIG_DIR, uebersicht.STATUS_DIR, uebersicht.dienst_aktiv)
        uebersicht.KONFIG_DIR, uebersicht.STATUS_DIR = self.konf, self.stat
        uebersicht.dienst_aktiv = lambda p: True
        server = uebersicht.ThreadingHTTPServer(("127.0.0.1", 0), uebersicht.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        basis = "http://127.0.0.1:%d" % server.server_address[1]
        try:
            liste = json.load(urllib.request.urlopen(basis + "/status.json", timeout=5))
            self.assertEqual([(x["platz"], x["farbe"]) for x in liste], [("01", "gruen"), ("07", "rot")])
            lehrer_seite = urllib.request.urlopen(basis + "/lehrer", timeout=5).read().decode("utf-8")
            self.assertIn("/status.json", lehrer_seite)
            viewer = urllib.request.urlopen(basis + "/platz/7", timeout=5).read().decode("utf-8")
            self.assertIn("location.host + '/sim/07'", viewer)   # Stream ueber dieselbe Adresse
            self.assertIn("currentWorld = 'worlds/lf7.wbt'", viewer)   # Weltname fuer Meshes
            self.assertIn("'w3d', true", viewer)            # Schueler: nur zuschauen
            lehrer = urllib.request.urlopen(basis + "/platz/07?steuern=1", timeout=5).read().decode("utf-8")
            self.assertIn("'w3d', false", lehrer)
            with self.assertRaises(urllib.error.HTTPError) as fehler:
                urllib.request.urlopen(basis + "/platz/09", timeout=5)
            self.assertEqual(fehler.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()
            uebersicht.KONFIG_DIR, uebersicht.STATUS_DIR, uebersicht.dienst_aktiv = alt


class FalschesWebots:
    """Nachgebauter Webots-Stream: liefert Dateien, beantwortet WebSocket-Upgrade
    mit 101 und spiegelt danach alle Bytes zurueck."""

    def __init__(self):
        import socket
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        self.anfragen = []
        threading.Thread(target=self._laufen, daemon=True).start()

    def _laufen(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._bedienen, args=(c,), daemon=True).start()

    def _bedienen(self, c):
        daten = b""
        while b"\r\n\r\n" not in daten:
            teil = c.recv(4096)
            if not teil:
                return
            daten += teil
        kopf = daten.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        self.anfragen.append(kopf)
        if "Upgrade: websocket" in kopf:
            c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
            while True:
                teil = c.recv(4096)
                if not teil:
                    break
                c.sendall(teil)
        else:
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhallo")
        c.close()

    def schliessen(self):
        self.srv.close()


class TestDurchleitung(Grundlage):
    def setUp(self):
        import socket
        super().setUp()
        self.webots = FalschesWebots()
        with open(os.path.join(self.konf, "07.env"), "w", encoding="utf-8") as f:
            f.write("IRC_HOST=R212-PC07\nSTREAM_PORT=%d\n" % self.webots.port)
        frei = socket.socket()
        frei.bind(("127.0.0.1", 0))
        self.freier_port = frei.getsockname()[1]
        frei.close()
        with open(os.path.join(self.konf, "01.env"), "w", encoding="utf-8") as f:
            f.write("IRC_HOST=x\nSTREAM_PORT=%d\n" % self.freier_port)
        self.alt = (uebersicht.KONFIG_DIR, uebersicht.STATUS_DIR)
        uebersicht.KONFIG_DIR, uebersicht.STATUS_DIR = self.konf, self.stat
        self.server = uebersicht.ThreadingHTTPServer(("127.0.0.1", 0), uebersicht.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.webots.schliessen()
        uebersicht.KONFIG_DIR, uebersicht.STATUS_DIR = self.alt
        super().tearDown()

    def test_dateien_werden_durchgeleitet(self):
        antwort = urllib.request.urlopen("http://127.0.0.1:%d/sim/7/meshes/rebel.obj?x=1" % self.port, timeout=5)
        self.assertEqual(antwort.read(), b"hallo")
        kopf = self.webots.anfragen[-1]
        self.assertTrue(kopf.startswith("GET /meshes/rebel.obj?x=1 HTTP/1.1"), kopf)
        self.assertIn("Host: 127.0.0.1:%d" % self.webots.port, kopf)
        self.assertIn("Connection: close", kopf)

    def test_websocket_wird_durchgeleitet(self):
        import socket
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall(b"GET /sim/07/ HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  b"Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: abc\r\n\r\n")
        antwort = b""
        while b"\r\n\r\n" not in antwort:
            antwort += s.recv(4096)
        self.assertTrue(antwort.startswith(b"HTTP/1.1 101"), antwort)
        rest = antwort.split(b"\r\n\r\n", 1)[1]

        # Anmeldung als Nur-Zuschauer, dann ein Steuerbefehl und eine harmlose Nachricht
        s.sendall(uebersicht.baue_rahmen("w3d;broadcast"))
        s.sendall(uebersicht.baue_rahmen("pause"))
        s.sendall(uebersicht.baue_rahmen("robot:ReBeL:hallo"))
        texte, puffer = [], rest
        s.settimeout(2)
        while len(texte) < 3:
            try:
                puffer += s.recv(4096)
            except OSError:
                break
            while True:
                r = uebersicht.zerlege_rahmen(puffer)
                if r is None:
                    break
                puffer = puffer[len(r[0]):]
                texte.append(r[3].decode())
        s.close()
        # Das falsche Webots spiegelt alles zurueck, was bei ihm ankam:
        self.assertEqual(texte, ["w3d;broadcast", "real-time:-1", "robot:ReBeL:hallo"])
        kopf = self.webots.anfragen[-1]
        self.assertTrue(kopf.startswith("GET / HTTP/1.1"), kopf)
        self.assertIn("Connection: Upgrade", kopf)
        self.assertIn("Sec-WebSocket-Key: abc", kopf)

    def test_rahmen_hin_und_zurueck(self):
        for text in ("w3d", "x" * 200, "y" * 70000):
            roh = uebersicht.baue_rahmen(text)
            r = uebersicht.zerlege_rahmen(roh + b"rest")
            self.assertEqual((r[0], r[1], r[2], r[3].decode()), (roh, 1, True, text))
            self.assertIsNone(uebersicht.zerlege_rahmen(roh[:-1]))   # unvollstaendig

    def test_simulation_aus_gibt_502(self):
        with self.assertRaises(urllib.error.HTTPError) as fehler:
            urllib.request.urlopen("http://127.0.0.1:%d/sim/01/" % self.port, timeout=5)
        self.assertEqual(fehler.exception.code, 502)
        fehler.exception.close()


if __name__ == "__main__":
    unittest.main()
