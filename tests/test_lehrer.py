"""Tests fuer den Lehrerbereich (tools/lehrer.py und die Lehrer-Schnittstellen),
ohne systemd, sudo und Webots."""

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lehrer  # noqa: E402
import uebersicht  # noqa: E402
import verwalter  # noqa: E402
from test_verwalter import Grundlage  # noqa: E402

PDF, PNG, JPG = b"%PDF-1.4 test", b"\x89PNG\r\n\x1a\nxx", b"\xff\xd8\xffxx"


class TestBenutzer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = os.path.join(self.tmp.name, "lib")

    def tearDown(self):
        self.tmp.cleanup()

    def test_anlegen_prueft_angaben(self):
        self.assertFalse(lehrer.hat_benutzer(self.d))
        for name, rolle, pw in (("A", "admin", "geheim123"), ("x y", "admin", "geheim123"), ("a" * 33, "admin", "geheim123"),
                                ("goebel", "chef", "geheim123"), ("goebel", "admin", "kurz")):
            with self.assertRaises(lehrer.LehrerFehler, msg=(name, rolle, pw)):
                lehrer.benutzer_anlegen(self.d, name, "", rolle, pw)
        b = lehrer.benutzer_anlegen(self.d, " Goebel ", "Herr Göbel", "admin", "geheim123", muss_aendern=False)
        self.assertEqual((b["name"], b["anzeige"], b["rolle"], b["muss_aendern"]), ("goebel", "Herr Göbel", "admin", False))
        with self.assertRaises(lehrer.LehrerFehler):
            lehrer.benutzer_anlegen(self.d, "goebel", "", "lehrkraft", "geheim123")
        self.assertNotIn("pw", lehrer.benutzer_liste(self.d)[0])
        with open(os.path.join(self.d, "benutzer.json"), encoding="utf-8") as f:
            self.assertNotIn("geheim123", f.read())   # nur der Hash steht in der Datei

    def test_anmelden_und_sitzung(self):
        lehrer.benutzer_anlegen(self.d, "goebel", "", "admin", "geheim123", muss_aendern=False)
        lehrer.benutzer_anlegen(self.d, "mueller", "Frau Müller", "lehrkraft", "vorlaeufig1")
        for name, pw in (("goebel", "falsch123"), ("niemand", "geheim123"), ("", "")):
            with self.assertRaises(lehrer.LehrerFehler):
                lehrer.anmelden(self.d, name, pw)
        token, b = lehrer.anmelden(self.d, "MUELLER", "vorlaeufig1", jetzt=1000)
        self.assertTrue(b["muss_aendern"])
        self.assertEqual(lehrer.sitzung_pruefen(self.d, token, jetzt=1000)["name"], "mueller")
        self.assertIsNone(lehrer.sitzung_pruefen(self.d, token, jetzt=1000 + lehrer.SITZUNG_S + 1))   # abgelaufen
        name, ablauf, stand, sig = token.split(".")
        for kaputt in ("goebel.%s.%s.%s" % (ablauf, stand, sig), "mueller.%d.%s.%s" % (int(ablauf) + 99, stand, sig),
                       None, "", "a.b", "x.1.1.y"):
            self.assertIsNone(lehrer.sitzung_pruefen(self.d, kaputt, jetzt=1000), kaputt)
        lehrer.passwort_aendern(self.d, "mueller", "vorlaeufig1", "eigenes99")   # beendet die Sitzung
        self.assertIsNone(lehrer.sitzung_pruefen(self.d, token, jetzt=1000))
        token, b = lehrer.anmelden(self.d, "mueller", "eigenes99", jetzt=1000)
        self.assertFalse(b["muss_aendern"])
        lehrer.benutzer_aendern(self.d, "mueller", gesperrt=True)   # Sperren beendet die Sitzung
        self.assertIsNone(lehrer.sitzung_pruefen(self.d, token, jetzt=1000))
        with self.assertRaises(lehrer.LehrerFehler) as f:
            lehrer.anmelden(self.d, "mueller", "eigenes99")
        self.assertIn("gesperrt", str(f.exception))

    def test_passwort_aendern(self):
        lehrer.benutzer_anlegen(self.d, "goebel", "", "admin", "geheim123", muss_aendern=False)
        for alt, neu in (("falsch", "neuesPW99"), ("geheim123", "geheim123"), ("geheim123", "kurz")):
            with self.assertRaises(lehrer.LehrerFehler, msg=(alt, neu)):
                lehrer.passwort_aendern(self.d, "goebel", alt, neu)
        lehrer.passwort_aendern(self.d, "goebel", "geheim123", "neuesPW99")
        lehrer.anmelden(self.d, "goebel", "neuesPW99")

    def test_letzter_admin_bleibt(self):
        lehrer.benutzer_anlegen(self.d, "goebel", "", "admin", "geheim123", muss_aendern=False)
        lehrer.benutzer_anlegen(self.d, "mueller", "", "lehrkraft", "geheim123")
        for aktion in (lambda: lehrer.benutzer_aendern(self.d, "goebel", gesperrt=True),
                       lambda: lehrer.benutzer_aendern(self.d, "goebel", rolle="lehrkraft"),
                       lambda: lehrer.benutzer_loeschen(self.d, "goebel")):
            with self.assertRaises(lehrer.LehrerFehler):
                aktion()
        lehrer.benutzer_aendern(self.d, "mueller", rolle="admin")
        lehrer.benutzer_aendern(self.d, "goebel", rolle="lehrkraft")   # jetzt gibt es einen anderen Admin
        lehrer.benutzer_loeschen(self.d, "goebel")
        self.assertEqual([b["name"] for b in lehrer.benutzer_liste(self.d)], ["mueller"])

    def test_protokoll(self):
        self.assertEqual(lehrer.lies_protokoll(self.d), [])
        lehrer.protokolliere(self.d, "goebel", "erstes", jetzt=0)
        lehrer.protokolliere(self.d, "mu\teller", "zweites\nmit Umbruch", jetzt=60)
        e = lehrer.lies_protokoll(self.d)
        self.assertEqual([(x["wer"], x["was"]) for x in e], [("mu eller", "zweites mit Umbruch"), ("goebel", "erstes")])

    def test_sperre_nach_fehlversuchen(self):
        jetzt = [0.0]
        s = lehrer.Sperre(uhr=lambda: jetzt[0])
        for _ in range(lehrer.FEHLVERSUCHE - 1):
            s.fehlversuch("pc")
        self.assertEqual(s.wartezeit("pc"), 0)
        s.fehlversuch("pc")
        self.assertEqual(s.wartezeit("pc"), lehrer.SPERRE_S)
        self.assertEqual(s.wartezeit("anderer"), 0)
        jetzt[0] += lehrer.SPERRE_S
        self.assertEqual(s.wartezeit("pc"), 0)


class TestEinstellungenUndWelten(Grundlage):
    def test_einstellungen_pruefen_und_ueberlagern(self):
        werte = lehrer.pruefe_einstellungen({"max_simulationen": "8", "leerlauf_minuten": 20,
                                             "ende_uhrzeiten": "11:15, 9:30;13:00 9:30"})
        self.assertEqual(werte, {"max_simulationen": 8, "leerlauf_minuten": 20,
                                 "ende_uhrzeiten": ["09:30", "11:15", "13:00"]})
        for falsch in ({"max_simulationen": 0, "leerlauf_minuten": 15}, {"max_simulationen": 5, "leerlauf_minuten": "x"},
                       {"max_simulationen": 5, "leerlauf_minuten": 15, "ende_uhrzeiten": "25:00"}, {}):
            with self.assertRaises(lehrer.LehrerFehler, msg=falsch):
                lehrer.pruefe_einstellungen(falsch)
        zustand = os.path.join(self.tmp.name, "lib")
        lehrer.speichere_einstellungen(zustand, {"max_simulationen": 3, "leerlauf_minuten": 5})
        e = verwalter.lade_einstellungen(self.konf, zustand)
        self.assertEqual((e["max_simulationen"], e["leerlauf_minuten"], e["ende_uhrzeiten"]), (3, 5, []))
        self.assertEqual(verwalter.lade_einstellungen(self.konf)["max_simulationen"], 2)   # verwalter.json allein

    def test_weltangaben_behalten_fremde_schluessel(self):
        self.schreibe(self.welten, "magazin.json", {"titel": "alt", "eigenes": 42})
        lehrer.speichere_weltangaben(self.welten, "magazin.wbt", " Neu ", "Tu was.", False)
        w = {x["datei"]: x for x in verwalter.lese_welten(self.welten)}["magazin.wbt"]
        self.assertEqual((w["titel"], w["aufgabe"], w["sichtbar"]), ("Neu", "Tu was.", False))
        with open(os.path.join(self.welten, "magazin.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["eigenes"], 42)
        for datei in ("gibtsnicht.wbt", "../x.wbt", "magazin.json"):
            with self.assertRaises(lehrer.LehrerFehler):
                lehrer.speichere_weltangaben(self.welten, datei, "t", "a", True)
        with self.assertRaises(lehrer.LehrerFehler):
            lehrer.speichere_weltangaben(self.welten, "magazin.wbt", "t" * 101, "", True)

    def test_dateien_nach_inhalt(self):
        self.assertEqual(lehrer.speichere_weltdatei(self.welten, "tisch.wbt", PDF), "tisch.pdf")
        self.assertEqual(lehrer.speichere_weltdatei(self.welten, "tisch.wbt", JPG), "tisch.jpg")
        self.assertEqual(lehrer.speichere_weltdatei(self.welten, "tisch.wbt", PNG), "tisch.png")
        self.assertFalse(os.path.exists(os.path.join(self.welten, "tisch.jpg")))   # nur ein Vorschaubild
        with self.assertRaises(lehrer.LehrerFehler):
            lehrer.speichere_weltdatei(self.welten, "tisch.wbt", b"<html>boese</html>")
        lehrer.loesche_weltdatei(self.welten, "tisch.wbt", "pdf")
        lehrer.loesche_weltdatei(self.welten, "tisch.wbt", "bild")
        w = {x["datei"]: x for x in verwalter.lese_welten(self.welten)}["tisch.wbt"]
        self.assertEqual((w["pdf"], w["bild"]), (None, None))


class TestLehrerSeiten(Grundlage):
    """Ueber HTTP von einem PC, der nicht in lehrer_ips steht: nur mit Konto."""

    def setUp(self):
        super().setUp()
        self.zustand = os.path.join(self.tmp.name, "lib")
        self.alt = (uebersicht.KONFIG_DIR, uebersicht.STATUS_DIR, uebersicht.WELTEN_DIR, uebersicht.ZUSTAND_DIR,
                    uebersicht.dienst_aktiv, uebersicht.vertraute_ip, uebersicht.VERWALTER, uebersicht.SPERRE,
                    uebersicht.vorschau_erzeugen, uebersicht.TEILE_DIR)
        uebersicht.KONFIG_DIR, uebersicht.STATUS_DIR, uebersicht.WELTEN_DIR = self.konf, self.stat, self.welten
        uebersicht.TEILE_DIR = os.path.join(self.tmp.name, "teile")
        uebersicht.ZUSTAND_DIR = self.zustand
        uebersicht.dienst_aktiv = lambda p: True
        self.vertraut = False
        uebersicht.vertraute_ip = lambda pc: self.vertraut
        uebersicht.VERWALTER = self.v
        uebersicht.SPERRE = lehrer.Sperre()
        self.vorschauen = []
        uebersicht.vorschau_erzeugen = self.vorschauen.append
        self.v.einstellungen_fn = lambda: verwalter.lade_einstellungen(self.konf, self.zustand)
        self.server = uebersicht.ThreadingHTTPServer(("127.0.0.1", 0), uebersicht.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.cookie = None

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        (uebersicht.KONFIG_DIR, uebersicht.STATUS_DIR, uebersicht.WELTEN_DIR, uebersicht.ZUSTAND_DIR,
         uebersicht.dienst_aktiv, uebersicht.vertraute_ip, uebersicht.VERWALTER, uebersicht.SPERRE,
         uebersicht.vorschau_erzeugen, uebersicht.TEILE_DIR) = self.alt
        super().tearDown()

    def anfrage(self, methode, pfad, daten=None, roh=None, typ="application/json", lehrer_kopf=True):
        c = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        kopf = {}
        if self.cookie:
            kopf["Cookie"] = self.cookie
        if methode == "POST":
            kopf["Content-Type"] = typ
            if lehrer_kopf:
                kopf["X-Lehrer"] = "1"
        body = roh if roh is not None else (json.dumps(daten).encode() if daten is not None else None)
        c.request(methode, pfad, body=body, headers=kopf)
        r = c.getresponse()
        inhalt = r.read().decode("utf-8", "replace")
        neu = r.getheader("Set-Cookie")
        if neu:
            self.cookie, self.cookie_kopf = neu.split(";", 1)[0], neu
        c.close()
        try:
            return r.status, json.loads(inhalt)
        except ValueError:
            return r.status, inhalt

    def konto(self, name="goebel", rolle="admin", passwort="geheim123", muss_aendern=False):
        lehrer.benutzer_anlegen(self.zustand, name, name, rolle, passwort, muss_aendern=muss_aendern)

    def anmelden(self, name="goebel", passwort="geheim123"):
        return self.anfrage("POST", "/lehrer/anmelden", {"name": name, "passwort": passwort})

    def test_ohne_konto_gesperrt(self):
        status, seite = self.anfrage("GET", "/lehrer")
        self.assertIn("Benutzername", seite)
        self.assertIn("noch kein Konto", seite)
        self.assertEqual(self.anmelden()[0], 403)
        for pfad in ("/status.json", "/api/lehrer/welten", "/api/lehrer/ich", "/api/lehrer/protokoll"):
            self.assertEqual(self.anfrage("GET", pfad)[0], 403, pfad)
        self.assertEqual(self.anfrage("POST", "/api/lehrer/alle_beenden", {})[0], 403)

    def test_erstes_konto_vom_lehrer_pc(self):
        self.vertraut = True
        ich = self.anfrage("GET", "/api/lehrer/ich")[1]
        self.assertEqual((ich["rolle"], ich["vertraut"], ich["benutzer_vorhanden"]), ("admin", True, False))
        self.assertIn("Benutzername", self.anfrage("GET", "/lehrer?anmelden=1")[1])   # Anmelden trotzdem moeglich
        self.assertEqual(self.anfrage("POST", "/api/lehrer/benutzer_anlegen",
                                      {"name": "goebel", "anzeige": "Herr Göbel", "rolle": "admin",
                                       "passwort": "geheim123"})[0], 200)
        self.assertFalse(lehrer.benutzer_liste(self.zustand)[0]["muss_aendern"])   # eigenes, kein vorlaeufiges
        self.vertraut = False
        self.assertEqual(self.anmelden()[0], 200)
        ich = self.anfrage("GET", "/api/lehrer/ich")[1]
        self.assertEqual((ich["name"], ich["anzeige"], ich["rolle"]), ("goebel", "Herr Göbel", "admin"))
        self.assertIn("pc-127.0.0.1", self.anfrage("GET", "/api/lehrer/protokoll")[1][-1]["wer"])

    def test_anmelden_und_sperre(self):
        self.konto()
        self.assertNotIn("noch kein Konto", self.anfrage("GET", "/lehrer")[1])
        self.assertEqual(self.anmelden(passwort="falsch123"), (403, {"fehler": "Benutzername oder Passwort stimmt nicht."}))
        self.assertIsNone(self.cookie)
        self.assertEqual(self.anmelden(), (200, {"ok": True, "muss_aendern": False}))
        self.assertIn("HttpOnly", self.cookie_kopf)
        self.assertIn("SameSite=Strict", self.cookie_kopf)
        self.assertIn('data-tab="benutzer"', self.anfrage("GET", "/lehrer")[1])
        self.assertEqual(self.anfrage("GET", "/status.json"), (200, []))
        self.assertEqual(self.anfrage("POST", "/lehrer/abmelden", {})[0], 200)
        self.assertIn("Benutzername", self.anfrage("GET", "/lehrer")[1])
        for _ in range(lehrer.FEHLVERSUCHE):
            self.anmelden(passwort="falsch123")
        self.assertEqual(self.anmelden()[0], 429)   # auch das richtige Passwort wartet jetzt

    def test_lehrkraft_muss_erst_passwort_aendern_und_darf_weniger(self):
        self.konto()
        self.konto("mueller", "lehrkraft", "vorlaeufig1", muss_aendern=True)
        self.assertEqual(self.anmelden("mueller", "vorlaeufig1"), (200, {"ok": True, "muss_aendern": True}))
        self.v.starten("pc-a", "tisch.wbt")
        self.assertEqual(self.anfrage("POST", "/api/lehrer/beenden", {"platz": "01"}),
                         (409, {"fehler": "Bitte zuerst ein eigenes Passwort festlegen."}))
        self.assertEqual(self.anfrage("POST", "/api/lehrer/upload?datei=tisch.wbt", roh=PDF, typ="application/pdf")[0], 409)
        alt = self.cookie
        self.assertEqual(self.anfrage("POST", "/api/lehrer/passwort", {"alt": "vorlaeufig1", "neu": "eigenes99"})[0], 200)
        self.assertNotEqual(self.cookie, alt)   # gleich wieder angemeldet
        self.assertEqual(self.anfrage("POST", "/api/lehrer/beenden", {"platz": "01"})[0], 200)
        for aktion, daten in (("einstellungen", {"max_simulationen": 5, "leerlauf_minuten": 5}),
                              ("benutzer_anlegen", {"name": "x1", "rolle": "admin", "passwort": "geheim123"})):
            self.assertEqual(self.anfrage("POST", "/api/lehrer/" + aktion, daten),
                             (409, {"fehler": "Das dürfen nur Admins."}), aktion)
        for pfad in ("/api/lehrer/einstellungen", "/api/lehrer/benutzer"):
            self.assertEqual(self.anfrage("GET", pfad)[0], 403, pfad)
        self.cookie = alt                       # die alte Anmeldung gilt nicht mehr
        self.assertEqual(self.anfrage("GET", "/api/lehrer/ich")[0], 403)

    def test_simulationen_steuern_mit_protokoll(self):
        self.konto()
        self.anmelden()
        self.v.starten("pc-a", "tisch.wbt")
        self.v.starten("pc-b", "magazin.wbt")
        status, liste = self.anfrage("GET", "/status.json")
        self.assertEqual([(p["platz"], p["titel"]) for p in liste], [("01", "Titel aus der Welt"), ("02", "Magazin")])
        # ohne eigenen Kopf X-Lehrer nicht (Schutz gegen fremde Seiten)
        self.assertEqual(self.anfrage("POST", "/api/lehrer/beenden", {"platz": "01"}, lehrer_kopf=False)[0], 403)
        self.assertEqual(self.anfrage("POST", "/api/lehrer/zuruecksetzen", {"platz": "1"})[0], 200)
        self.assertEqual(self.anfrage("POST", "/api/lehrer/beenden", {"platz": "01"})[0], 200)
        self.assertEqual(self.v.meldungen["pc-a"], "von der Lehrkraft beendet")
        self.assertEqual(self.anfrage("POST", "/api/lehrer/beenden", {"platz": "07"})[0], 409)
        self.assertEqual(self.anfrage("POST", "/api/lehrer/alle_beenden", {}), (200, {"ok": True, "anzahl": 1}))
        self.assertEqual([a[0] for a in self.helfer.aufrufe], ["anlegen", "anlegen", "neustart", "entfernen", "entfernen"])
        was = [(e["wer"], e["was"]) for e in self.anfrage("GET", "/api/lehrer/protokoll")[1]]
        self.assertEqual(was[:3], [("goebel", "alle Simulationen beendet (1)"), ("goebel", "Platz 01 beendet (PC pc-a)"),
                                   ("goebel", "Platz 1 zurueckgesetzt")])

    def test_welten_pflegen(self):
        self.konto("mueller", "lehrkraft")
        self.konto()
        self.anmelden("mueller")
        self.assertEqual(self.anfrage("POST", "/api/lehrer/welt", {"datei": "geheim.wbt", "titel": "Jetzt offen",
                                                                   "aufgabe": "A", "sichtbar": True})[0], 200)
        welten = {w["datei"]: w for w in self.anfrage("GET", "/api/lehrer/welten")[1]}
        self.assertTrue(welten["geheim.wbt"]["sichtbar"])
        self.assertEqual(self.v.starten("pc-a", "geheim.wbt"), "01")   # jetzt fuer Schueler startbar
        self.assertEqual(self.anfrage("POST", "/api/lehrer/upload?datei=tisch.wbt", roh=PDF, typ="application/pdf"),
                         (200, {"ok": True, "datei": "tisch.pdf"}))
        self.assertEqual(self.anfrage("POST", "/api/lehrer/upload?datei=tisch.wbt", roh=b"MZ boese",
                                      typ="application/pdf")[0], 409)
        self.assertEqual(self.anfrage("POST", "/api/lehrer/upload?datei=tisch.wbt", roh=PNG, typ="image/png",
                                      lehrer_kopf=False)[0], 403)
        self.assertEqual(self.anfrage("POST", "/api/lehrer/datei_loeschen", {"datei": "tisch.wbt", "art": "pdf"})[0], 200)
        self.assertIsNone({w["datei"]: w for w in self.anfrage("GET", "/api/lehrer/welten")[1]}["tisch.wbt"]["pdf"])
        self.assertEqual(self.anfrage("POST", "/api/lehrer/vorschau", {"datei": "magazin.wbt"})[0], 200)
        self.assertEqual(self.vorschauen, ["magazin.wbt"])
        self.assertFalse(os.path.exists(os.path.join(self.welten, "magazin.png")))   # altes Bild weg
        self.assertEqual(self.anfrage("POST", "/api/lehrer/vorschau", {"datei": "../etc.wbt"})[0], 409)
        self.assertIn("Welt tisch.wbt: tisch.pdf hochgeladen",
                      [e["was"] for e in self.anfrage("GET", "/api/lehrer/protokoll")[1]])

    def test_benutzer_verwalten(self):
        self.konto()
        self.anmelden()
        neu = {"name": "Mueller", "anzeige": "Frau Müller", "rolle": "lehrkraft", "passwort": "vorlaeufig1"}
        self.assertEqual(self.anfrage("POST", "/api/lehrer/benutzer_anlegen", neu)[0], 200)
        self.assertEqual(self.anfrage("POST", "/api/lehrer/benutzer_anlegen", neu)[0], 409)   # gibt es schon
        liste = {b["name"]: b for b in self.anfrage("GET", "/api/lehrer/benutzer")[1]}
        self.assertTrue(liste["mueller"]["muss_aendern"])
        # sich selbst aussperren geht nicht
        for daten in ({"name": "goebel", "gesperrt": True}, {"name": "goebel", "rolle": "lehrkraft"}):
            self.assertEqual(self.anfrage("POST", "/api/lehrer/benutzer_aendern", daten)[0], 409)
        self.assertEqual(self.anfrage("POST", "/api/lehrer/benutzer_loeschen", {"name": "goebel"})[0], 409)
        admin = self.cookie
        self.anmelden("mueller", "vorlaeufig1")
        mueller = self.cookie
        self.cookie = admin
        self.assertEqual(self.anfrage("POST", "/api/lehrer/benutzer_passwort",
                                      {"name": "mueller", "passwort": "neuvorl22"})[0], 200)
        self.assertEqual(self.anfrage("POST", "/api/lehrer/benutzer_aendern", {"name": "mueller", "gesperrt": True})[0], 200)
        self.cookie = mueller
        self.assertEqual(self.anfrage("GET", "/api/lehrer/ich")[0], 403)   # Sitzung beendet
        self.assertEqual(self.anmelden("mueller", "neuvorl22"), (403, {"fehler": "Dieses Konto ist gesperrt."}))
        self.cookie = admin
        self.assertEqual(self.anfrage("POST", "/api/lehrer/benutzer_loeschen", {"name": "mueller"})[0], 200)
        self.assertEqual([b["name"] for b in self.anfrage("GET", "/api/lehrer/benutzer")[1]], ["goebel"])
        was = [e["was"] for e in self.anfrage("GET", "/api/lehrer/protokoll")[1]]
        for eintrag in ("Benutzer mueller angelegt (lehrkraft)", "Passwort von mueller zurueckgesetzt",
                        "Benutzer mueller: lehrkraft, gesperrt", "Benutzer mueller geloescht"):
            self.assertIn(eintrag, was)

    def test_backensatz(self):
        from test_baukasten import wuerfel, SCHUNK
        for name, m in (("vorrichtung", wuerfel(40, bohrung=8)), ("schunk_egp25", SCHUNK)):
            os.makedirs(os.path.join(uebersicht.TEILE_DIR, name))
            with open(os.path.join(uebersicht.TEILE_DIR, name, "teil.json"), "w", encoding="utf-8") as f:
                json.dump(dict(m, quelle=name + ".step", groesse_mm=[20, 20, 20]), f)
        alt = (uebersicht.BACKEN_DIR, uebersicht.WERKZEUG_DIR, uebersicht.modell_erzeugen, uebersicht.FESTE_BACKEN_DIR)
        uebersicht.BACKEN_DIR = os.path.join(self.tmp.name, "backen")
        uebersicht.WERKZEUG_DIR = os.path.join(self.tmp.name, "werkzeuge")
        uebersicht.FESTE_BACKEN_DIR = os.path.join(self.tmp.name, "cad_backen")
        aufrufe = []

        def modell_erzeugen():   # statt tools/erzeuge_rebel_modell.py: Werkzeugpaket anlegen, Kenndaten melden
            aufrufe.append(1)
            for b in os.listdir(uebersicht.BACKEN_DIR):
                os.makedirs(os.path.join(uebersicht.WERKZEUG_DIR, "egp25_" + b), exist_ok=True)
                with open(os.path.join(uebersicht.WERKZEUG_DIR, "egp25_" + b, "Schunk_EGP25_X.xml"), "w") as f:
                    f.write("<TOOL/>")
            return {"egp25_" + b: {"tcp_mm": 130.7, "offen_mm": 11.0, "hub_mm": 5.4} for b in os.listdir(uebersicht.BACKEN_DIR)}
        uebersicht.modell_erzeugen = modell_erzeugen
        try:
            self.konto("mueller", "lehrkraft")
            self.anmelden("mueller")
            entwurf = {"titel": "Test-Backen", "teile": [{"id": "backe", "teil": "vorrichtung", "art": "aufbau", "verbindung": {
                "punkt": {"art": "bohrung", "id": 31, "ende": 0}, "an": "greifer",
                "an_punkt": {"art": "bohrung", "id": 454, "ende": 0}, "versatz_mm": [0, 0, 0]}}]}
            status, d = self.anfrage("POST", "/api/lehrer/baukasten_rechnen", {"entwurf": dict(entwurf, art="backensatz")})
            self.assertEqual(status, 200, d)
            self.assertEqual(self.anfrage("POST", "/api/lehrer/backensatz_speichern", {"name": "Falsch", "entwurf": entwurf})[0], 409)
            status, d = self.anfrage("POST", "/api/lehrer/backensatz_speichern", {"name": "robi3", "entwurf": entwurf})
            self.assertEqual((status, d["greifer"], d["kenndaten"]["tcp_mm"]), (200, "egp25_robi3", 130.7), d)
            liste = self.anfrage("GET", "/api/lehrer/backensaetze")[1]
            self.assertEqual([(b["name"], b["fest"], b["werkzeugpaket"]) for b in liste], [("robi3", False, True)])
            gelesen = self.anfrage("GET", "/api/lehrer/backensatz?name=robi3")[1]
            self.assertEqual((gelesen["besitzer"], gelesen["entwurf"]["art"], gelesen["teil"]), ("lehrer:mueller", "backensatz", "vorrichtung"))
            self.assertIn("Backensatz robi3", next(t for t in self.anfrage("GET", "/api/lehrer/teile")[1]
                                                    if t["name"] == "vorrichtung")["verwendet_in"])
            c = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
            c.request("GET", "/api/lehrer/werkzeugpaket?name=robi3", headers={"Cookie": self.cookie})
            paket = c.getresponse()
            self.assertEqual((paket.status, paket.getheader("Content-Type"), paket.read(2)), (200, "application/zip", b"PK"))
            c.close()
            # in einer Welt verwendet: nicht loeschbar
            with open(os.path.join(self.welten, "w.wbt"), "w", encoding="utf-8") as f:
                f.write('#VRML_SIM R2025a utf8\nDEF REBEL IgusRebel6DOF {\n  greifer "egp25_robi3"\n}\n')
            self.assertEqual(self.anfrage("POST", "/api/lehrer/backensatz_loeschen", {"name": "robi3"})[0], 409)
            os.remove(os.path.join(self.welten, "w.wbt"))
            self.assertEqual(self.anfrage("POST", "/api/lehrer/backensatz_loeschen", {"name": "robi3"})[0], 200)
            self.assertEqual(self.anfrage("GET", "/api/lehrer/backensaetze")[1], [])
            self.assertEqual(len(aufrufe), 2)
        finally:
            uebersicht.BACKEN_DIR, uebersicht.WERKZEUG_DIR, uebersicht.modell_erzeugen, uebersicht.FESTE_BACKEN_DIR = alt

    def test_teile_und_baukasten(self):
        from test_baukasten import wuerfel
        for name, m in (("wuerfel", wuerfel()), ("vorrichtung", wuerfel(40, bohrung=8))):
            os.makedirs(os.path.join(uebersicht.TEILE_DIR, name))
            with open(os.path.join(uebersicht.TEILE_DIR, name, "teil.json"), "w", encoding="utf-8") as f:
                json.dump(dict(m, quelle=name + ".step", groesse_mm=[20, 20, 20]), f)
        self.konto("mueller", "lehrkraft")
        self.anmelden("mueller")
        self.assertEqual([t["name"] for t in self.anfrage("GET", "/api/lehrer/teile")[1]], ["vorrichtung", "wuerfel"])
        self.assertEqual(self.anfrage("GET", "/api/lehrer/teil?name=wuerfel")[1]["flaechen"][0]["id"], 1)
        self.assertEqual(self.anfrage("GET", "/api/lehrer/teil?name=../x")[0], 404)
        # keine STEP-Datei: wird abgelehnt, bevor gmsh startet
        self.assertEqual(self.anfrage("POST", "/api/lehrer/teil_hochladen?name=x", roh=b"MZ", typ="application/octet-stream"),
                         (409, {"fehler": "Das ist keine STEP-Datei (.step/.stp)."}))
        entwurf = {"titel": "Magazin", "aufgabe": "Teile einsortieren", "teile": [
            {"id": "v", "teil": "vorrichtung", "art": "aufbau", "verbindung": {
                "punkt": {"art": "flaeche", "id": 1}, "an": "tisch", "an_punkt": {"art": "ecke", "id": "x+y-"},
                "versatz_mm": [0, 0, 0]}}]}
        status, d = self.anfrage("POST", "/api/lehrer/baukasten_rechnen", {"entwurf": entwurf})
        self.assertEqual((status, d["lagen"]["v"]["ursprung_mm"]), (200, [770.0, -420.0, 0.0]))   # Flaechenmitte auf der Tischecke, um 90 Grad gedreht
        falsch = json.loads(json.dumps(entwurf))
        falsch["teile"][0]["teil"] = "gibtsnicht"
        self.assertEqual(self.anfrage("POST", "/api/lehrer/baukasten_rechnen", {"entwurf": falsch}),
                         (409, {"fehler": "Das Teil gibtsnicht gibt es nicht in der Teilebibliothek."}))
        entwurf["teile"][0]["verbindung"]["an_punkt"] = {"art": "flaeche", "id": "oben"}
        status, d = self.anfrage("POST", "/api/lehrer/baukasten_rechnen", {"entwurf": entwurf})
        self.assertEqual((status, d["lagen"]["v"]["ursprung_mm"]), (200, [230.0, -20.0, 0.0]))
        status, d = self.anfrage("POST", "/api/lehrer/baukasten_speichern", {"welt": "lager", "entwurf": entwurf})
        self.assertEqual(status, 200, d)
        with open(os.path.join(self.welten, "lager.wbt"), encoding="utf-8") as f:
            self.assertIn('modell [ "../teile/vorrichtung/netz.obj" ]', f.read())
        with open(os.path.join(self.welten, "lager.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"titel": "Magazin", "aufgabe": "Teile einsortieren", "sichtbar": False,
                                            "ea": {"taster": [], "leuchten": [], "sensoren": []}})
        self.assertEqual(self.vorschauen, ["lager.wbt"])
        self.assertEqual(self.anfrage("GET", "/api/lehrer/baukasten?welt=lager")[1]["besitzer"], "lehrer:mueller")
        self.assertEqual(self.anfrage("GET", "/api/lehrer/entwuerfe")[1], ["lager"])
        # nicht mit dem Baukasten gebaute Welten bleiben unangetastet
        self.assertEqual(self.anfrage("POST", "/api/lehrer/baukasten_speichern", {"welt": "magazin", "entwurf": entwurf})[0], 409)
        # verwendete Teile lassen sich nicht loeschen, unbenutzte schon
        self.assertEqual(self.anfrage("POST", "/api/lehrer/teil_loeschen", {"name": "vorrichtung"})[0], 409)
        self.assertEqual(self.anfrage("POST", "/api/lehrer/teil_loeschen", {"name": "wuerfel"})[0], 200)
        self.assertIn("Baukasten-Welt lager gespeichert", [e["was"] for e in self.anfrage("GET", "/api/lehrer/protokoll")[1]])

    def test_baukasten_seite_nur_fuer_lehrkraefte(self):
        status, _ = self.anfrage("GET", "/lehrer/baukasten")
        self.assertEqual(status, 302)   # ohne Anmeldung zur Anmeldeseite
        self.konto()
        self.anmelden()
        status, seite = self.anfrage("GET", "/lehrer/baukasten")
        self.assertEqual(status, 200)
        self.assertIn('"three": "https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js"', seite)
        self.assertNotIn("%%STIL%%", seite)
        self.assertIn("baukasten_rechnen", seite)
        self.assertIn('href="/lehrer/baukasten"', self.anfrage("GET", "/lehrer")[1])

    def test_welt_loeschen_und_greifer(self):
        self.konto("mueller", "lehrkraft")
        self.anmelden("mueller")
        with open(os.path.join(self.welten, "magazin.wbt"), "w", encoding="utf-8") as f:
            f.write('#VRML_SIM R2025a utf8\nDEF REBEL IgusRebel6DOF {\n  greifer "egp25"\n}\n')
        lehrer.schreibe_json(os.path.join(self.welten, "magazin.baukasten.json"), {"greifer": "egp25", "teile": []})
        alt = uebersicht.greifer_liste
        uebersicht.greifer_liste = lambda: ["egp40", "egp25", "kein"]
        try:
            w = {w["datei"]: w for w in self.anfrage("GET", "/api/lehrer/welten")[1]}["magazin.wbt"]
            self.assertEqual((w["greifer"], w["baukasten"], w["laeuft"]), ("egp25", True, []))
            self.assertEqual([g["titel"] for g in self.anfrage("GET", "/api/lehrer/greifer_liste")[1]],
                             ["Schunk EGP 40", "Schunk EGP 25", "kein Greifer"])
            neu = {"datei": "magazin.wbt", "titel": "Magazin", "aufgabe": "A", "sichtbar": True, "greifer": "egp40"}
            self.assertEqual(self.anfrage("POST", "/api/lehrer/welt", neu)[0], 200)
            self.assertEqual(lehrer.welt_greifer(self.welten, "magazin.wbt"), "egp40")
            with open(os.path.join(self.welten, "magazin.baukasten.json"), encoding="utf-8") as f:
                self.assertEqual(json.load(f)["greifer"], "egp40")
            self.assertEqual(self.anfrage("POST", "/api/lehrer/welt", dict(neu, greifer="egp99"))[0], 409)
            self.assertEqual(self.anfrage("POST", "/api/lehrer/welt", dict(neu, datei="tisch.wbt", greifer="egp40"))[0], 409)   # kein Feld
            # laeuft gerade: nicht loeschbar
            self.v.starten("pc-a", "magazin.wbt")
            self.assertEqual(self.anfrage("GET", "/api/lehrer/welten")[1][1]["laeuft"], ["01"])
            status, d = self.anfrage("POST", "/api/lehrer/welt_loeschen", {"datei": "magazin.wbt"})
            self.assertEqual(status, 409)
            self.assertIn("Platz 01", d["fehler"])
            self.anfrage("POST", "/api/lehrer/beenden", {"platz": "01"})
            status, d = self.anfrage("POST", "/api/lehrer/welt_loeschen", {"datei": "magazin.wbt"})
            self.assertEqual(status, 200, d)
            self.assertEqual(sorted(d["dateien"]), ["magazin.baukasten.json", "magazin.json", "magazin.pdf", "magazin.png", "magazin.wbt"])
            self.assertNotIn("magazin.wbt", [w["datei"] for w in self.anfrage("GET", "/api/lehrer/welten")[1]])
            korb = os.listdir(os.path.join(self.welten, "papierkorb"))
            self.assertEqual(len(korb), 1)
            self.assertIn("magazin.wbt", os.listdir(os.path.join(self.welten, "papierkorb", korb[0])))
            self.assertEqual(self.anfrage("POST", "/api/lehrer/welt_loeschen", {"datei": "../tisch.wbt"})[0], 409)
            self.assertIn("Welt magazin.wbt geloescht", self.anfrage("GET", "/api/lehrer/protokoll")[1][0]["was"])
        finally:
            uebersicht.greifer_liste = alt

    def test_welt_export_und_import(self):
        self.konto("mueller", "lehrkraft")
        self.anmelden("mueller")
        with open(os.path.join(self.welten, "magazin.wbt"), "w", encoding="utf-8") as f:
            f.write('#VRML_SIM R2025a utf8\nEXTERNPROTO "../protos/Aufbau.proto"\nDEF REBEL IgusRebel6DOF {\n  greifer "egp40"\n}\n')
        alt = uebersicht.BACKEN_DIR
        uebersicht.BACKEN_DIR = os.path.join(self.tmp.name, "backen")
        try:
            c = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
            c.request("GET", "/api/lehrer/welt_export?datei=magazin.wbt", headers={"Cookie": self.cookie})
            r = c.getresponse()
            paket = r.read()
            self.assertEqual((r.status, r.getheader("Content-Type"), paket[:2]), (200, "application/zip", b"PK"))
            self.assertIn("rebel-welt-magazin.zip", r.getheader("Content-Disposition"))
            c.close()
            status, d = self.anfrage("POST", "/api/lehrer/welt_import", roh=paket, typ="application/zip")
            self.assertEqual((status, d.get("vorhanden"), d.get("vorschlag")), (409, "magazin", "magazin-2"), d)
            status, d = self.anfrage("POST", "/api/lehrer/welt_import?name=magazin-2", roh=paket, typ="application/zip")
            self.assertEqual((status, d["welt"], d["greifer"], d["hinweise"]), (200, "magazin-2.wbt", "egp40", []), d)
            w = {w["datei"]: w for w in self.anfrage("GET", "/api/lehrer/welten")[1]}["magazin-2.wbt"]
            self.assertEqual((w["titel"], w["sichtbar"], w["pdf"]), ("Magazin", False, "magazin-2.pdf"))
            self.assertEqual(self.anfrage("POST", "/api/lehrer/welt_import", roh=b"kein zip", typ="application/zip")[0], 409)
            self.assertIn("Welt magazin-2.wbt importiert", self.anfrage("GET", "/api/lehrer/protokoll")[1][0]["was"])
            self.assertEqual(self.anfrage("GET", "/api/lehrer/welt_export?datei=..%2Fx.wbt")[0], 409)
            self.assertEqual(self.anfrage("POST", "/api/lehrer/markdown", {"text": "**a**"})[1]["html"], "<p><strong>a</strong></p>")
        finally:
            uebersicht.BACKEN_DIR = alt

    def test_logo_und_startseite(self):
        self.konto()
        self.konto("mueller", "lehrkraft")
        status, seite = self.anfrage("GET", "/")
        self.assertIn("Anmeldung Lehrkraft", seite)   # Startseite fuehrt zum Lehrerbereich
        self.assertIn('href="/lehrer"', seite)
        self.assertNotIn('<img src="/logo', seite)
        self.anmelden("mueller")
        self.assertIn("Lehrerbereich</a>", self.anfrage("GET", "/")[1])
        self.assertEqual(self.anfrage("POST", "/api/lehrer/logo_hochladen?name=x", roh=PNG, typ="application/octet-stream"),
                         (409, {"fehler": "Das Logo ändern nur Admins."}))
        self.assertEqual(self.anfrage("POST", "/api/lehrer/logo_waehlen", {"name": ""})[0], 409)
        self.anmelden()
        self.assertEqual(self.anfrage("POST", "/api/lehrer/logo_hochladen?name=DRS%20quer.png", roh=PNG,
                                      typ="application/octet-stream"), (200, {"ok": True, "datei": "drs_quer.png"}))
        boese = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        self.assertEqual(self.anfrage("POST", "/api/lehrer/logo_hochladen?name=b", roh=boese, typ="application/octet-stream")[0], 409)
        self.assertEqual(self.anfrage("POST", "/api/lehrer/logo_hochladen?name=b", roh=PDF, typ="application/octet-stream")[0], 409)
        gut = b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><path d="M0 0h10v10z"/></svg>'
        self.assertEqual(self.anfrage("POST", "/api/lehrer/logo_hochladen?name=drs", roh=gut, typ="application/octet-stream")[1]["datei"],
                         "drs.svg")
        self.assertEqual(self.anfrage("GET", "/api/lehrer/logos")[1], {"logos": ["drs.svg", "drs_quer.png"], "gewaehlt": None})
        self.assertEqual(self.anfrage("POST", "/api/lehrer/logo_waehlen", {"name": "fehlt.png"})[0], 409)
        self.assertEqual(self.anfrage("POST", "/api/lehrer/logo_waehlen", {"name": "drs.svg"})[0], 200)
        # Einstellungen speichern behaelt das Logo
        self.anfrage("POST", "/api/lehrer/einstellungen", {"max_simulationen": 4, "leerlauf_minuten": 10})
        self.assertEqual(self.anfrage("GET", "/api/lehrer/logos")[1]["gewaehlt"], "drs.svg")
        for pfad in ("/", "/lehrer", "/lehrer/baukasten"):
            self.assertIn('<img src="/logo?drs.svg-', self.anfrage("GET", pfad)[1], pfad)
        c = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        c.request("GET", "/logo")
        r = c.getresponse()
        self.assertEqual((r.status, r.getheader("Content-Type"), r.read()), (200, "image/svg+xml", gut))
        self.assertIn("sandbox", r.getheader("Content-Security-Policy"))
        c.close()
        self.assertEqual(self.anfrage("GET", "/logos/drs_quer.png")[0], 200)
        self.assertEqual(self.anfrage("GET", "/logos/..%2Fbenutzer.json")[0], 404)
        self.assertEqual(self.anfrage("POST", "/api/lehrer/logo_loeschen", {"name": "drs.svg"})[0], 200)
        self.assertEqual(self.anfrage("GET", "/api/lehrer/logos")[1], {"logos": ["drs_quer.png"], "gewaehlt": None})
        self.assertEqual(self.anfrage("GET", "/logo")[0], 404)

    def test_alter_port_leitet_um(self):
        self.assertEqual(uebersicht.ziel_umleitung("192.168.7.1:8080", "/lehrer?anmelden=1", 80), "http://192.168.7.1/lehrer?anmelden=1")
        self.assertEqual(uebersicht.ziel_umleitung("[::1]:8080", "/", 80), "http://[::1]/")
        self.assertEqual(uebersicht.ziel_umleitung("rebel-sim:8080", "/", 8081), "http://rebel-sim:8081/")
        self.assertEqual(uebersicht.ziel_umleitung("bo se\r\n", "x", 80), "http://localhost/")
        server = uebersicht.ThreadingHTTPServer(("127.0.0.1", 0), uebersicht.Umleitung)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            c.request("GET", "/lehrer", headers={"Host": "rebel-sim:8080"})
            r = c.getresponse()
            self.assertEqual((r.status, r.getheader("Location")), (301, "http://rebel-sim/lehrer" if uebersicht.PORT == 80
                                                                  else "http://rebel-sim:%d/lehrer" % uebersicht.PORT))
            c.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_einstellungen(self):
        self.konto()
        self.anmelden()
        self.assertEqual(self.anfrage("POST", "/api/lehrer/einstellungen",
                                      {"max_simulationen": 6, "leerlauf_minuten": 10, "ende_uhrzeiten": "13:00"})[0], 200)
        self.assertEqual(self.anfrage("GET", "/api/lehrer/einstellungen")[1],
                         {"max_simulationen": 6, "leerlauf_minuten": 10, "ende_uhrzeiten": ["13:00"]})
        self.assertEqual(self.v.frei(), 6)
        self.assertEqual(self.anfrage("POST", "/api/lehrer/einstellungen", {"max_simulationen": 99,
                                                                            "leerlauf_minuten": 10})[0], 409)


if __name__ == "__main__":
    unittest.main()
