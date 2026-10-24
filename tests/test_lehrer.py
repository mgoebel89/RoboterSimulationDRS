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
