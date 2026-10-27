"""SPS-Kopplung Stufe 0: Pruefversion der App WebotsSim (Bedienfeld), Verbindung als App (irc_app.py) gegen einen
nachgebauten iRC-gRPC-Server, Pruefablauf (bedienfeld_pruefen.py) und Variablensuche (sps_opcua.py) gegen einen
OPC-UA-Testserver. Ohne grpcio/protobuf bzw. asyncua werden die betreffenden Tests uebersprungen."""

import importlib.util
import os
import queue
import sys
import threading
import time
import unittest
import xml.etree.ElementTree as ET
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

PRUEF = os.path.join(ROOT, "deploy", "irc_app", "pruefung")
MIT_GRPC = all(importlib.util.find_spec(m) for m in ("grpc", "google.protobuf")) if importlib.util.find_spec("google") else False
MIT_ASYNCUA = importlib.util.find_spec("asyncua") is not None


class TestPruefversion(unittest.TestCase):
    def test_bedienfeld_wie_im_konzept(self):
        ui = ET.parse(os.path.join(PRUEF, "WebotsSim", "ui.xml")).getroot()
        self.assertEqual(ui.tag, "ui")
        namen = {e.get("name") for e in ui.iter() if e.get("name")}
        for art in ("ein", "aus"):
            for i in range(1, 9):
                self.assertTrue({"%s_%d_var" % (art, i), "%s_%d_gsig" % (art, i), "%s_%d_zustand" % (art, i)} <= namen)
        self.assertTrue({"sps_status", "sps_adresse", "sps_lesen", "sps_alle_zeilen", "band_lesen", "band_verbinden"} <= namen)
        # nur Elemente, die iRC kennt (documentation/ui.xml.md)
        erlaubt = {"ui", "ribbon", "main", "group", "horizontal", "vertical", "button", "toggle", "dropdown", "checkbox",
                   "combobox", "item", "image", "numberbox", "separator", "text", "textbox"}
        self.assertLessEqual({e.tag for e in ui.iter()}, erlaubt)
        self.assertEqual([e.tag for e in ui], ["ribbon", "main"])
        for e in ui.iter("numberbox"):
            self.assertTrue(1 <= int(e.get("value")) <= 100)
        self.assertLess(os.path.getsize(os.path.join(PRUEF, "WebotsSim", "ui.xml")), 400 * 1024)

    def test_paket_ohne_programm_und_aktuell(self):
        rcapp = ET.parse(os.path.join(PRUEF, "WebotsSim", "rcapp.xml")).getroot()
        self.assertEqual((rcapp.get("name"), rcapp.get("version")), ("WebotsSim", "1.1.0"))
        self.assertIsNone(rcapp.find("executable"))   # die App laeuft im Container und verbindet sich von aussen
        with zipfile.ZipFile(os.path.join(PRUEF, "WebotsSim.zip")) as z:
            self.assertEqual(sorted(z.namelist()), ["WebotsSim/rcapp.xml", "WebotsSim/ui.xml"])
            for n in z.namelist():
                with open(os.path.join(PRUEF, n), "rb") as f:
                    self.assertEqual(z.read(n), f.read(), "ZIP neu packen: %s" % n)


class FalscheApp:
    """Steht fuer IrcApp: merkt sich, was ans Bedienfeld geht; Ereignisse loest der Test aus."""

    def __init__(self, ereignis):
        self.ereignis, self.gesendet, self.offen, self.ereignisse = ereignis, [], [], []
        self.gsigs, self.verbunden_ = set(), False
        self.iRC_neu = None   # [GSig, Abfragen bis dahin]: so schaltet "iRC" ein Signal ein, waehrend gewartet wird

    def aufruf(self, name, **felder):
        return {"version": "V980-15-000-2", "is_simulation": True} if name == "GetSystemInfo" else {}

    def anmelden(self):
        return {}

    def verbinden(self):
        self.verbunden_ = True

    def trennen(self):
        self.verbunden_ = False

    def text(self, name, t):
        self.offen.append(("text", name, t))

    def zahl(self, name, w):
        self.offen.append(("zahl", name, w))

    def liste(self, name, e, g):
        self.offen.append(("liste", name, list(e), g))

    def haken(self, name, an):
        self.offen.append(("haken", name, an))

    def sichtbar(self, name, an):
        self.offen.append(("sichtbar", name, an))

    def senden(self):
        self.gesendet.append(self.offen)
        self.offen = []

    def zustaende_anfragen(self, namen):
        self.ereignisse.append({"zeit": "", "aenderungen": [{"element_name": "sps_adresse", "state": {"textfield_state": {"current_text": "10.0.0.1"}}}]})

    def gsigs_setzen(self, signale):
        for nr, an in signale.items():
            (self.gsigs.add if an else self.gsigs.discard)(nr)

    def gsigs_an(self):
        if self.iRC_neu:
            self.iRC_neu[1] -= 1
            if self.iRC_neu[1] < 0:
                self.gsigs.add(self.iRC_neu[0])
                self.iRC_neu = None
        return sorted(self.gsigs)

    def klick(self, *aenderungen):
        self.ereignisse.append({"zeit": "", "aenderungen": list(aenderungen)})
        self.ereignis(list(aenderungen))


class TestPruefablauf(unittest.TestCase):
    def test_ganzer_ablauf_mit_antworten(self):
        import bedienfeld_pruefen
        apps = []
        antworten = iter(["j", "j", "n", "Zeile 4 noch da", "j", "Namen werden abgeschnitten",   # Darstellung
                          "ENTER_EREIGNISSE", "j",                                                 # Ereignisse, Reaktion
                          "1 100", "",                                                             # GSig-Nummern
                          "letzter Text bleibt stehen", "j"])                                     # ohne Container
        ausgabe = []

        def eingabe(text):
            a = next(antworten)
            if a == "ENTER_EREIGNISSE":   # waehrend die Lehrkraft in iRC klickt
                app = apps[0]
                app.klick({"element_name": "sps_lesen", "state": {"button_state": "CLICKED"}})
                app.klick({"element_name": "ein_1_var", "state": {"dropdown_state": {"selected_option": "Band_belegt (Bool)"}}})
                app.klick({"element_name": "ein_2_gsig", "state": {"numberfield_state": {"current_number": 7}}})
                app.klick({"element_name": "sps_adresse", "state": {"textfield_state": {"current_text": "10.0.0.1"}}})
                app.klick({"element_name": "sps_alle_zeilen", "state": {"checkbox_state": "CHECKED"}})
                app.iRC_neu = [12, 2]   # "iRC" schaltet bei der GSig-Frage GSig 11 ein, Schnittstelle meldet 12
                return ""
            return a

        def fabrik(ereignis):
            apps.append(FalscheApp(ereignis))
            return apps[0]
        p = bedienfeld_pruefen.Pruefung("10.0.0.5", eingabe=eingabe, ausgabe=ausgabe.append, app_fabrik=fabrik, warte=lambda s: None)
        b = p.ablauf()
        app = apps[0]
        erste = app.gesendet[0]
        self.assertIn(("liste", "ein_1_var", [bedienfeld_pruefen.FREI] + bedienfeld_pruefen.BEISPIELE,
                       bedienfeld_pruefen.BEISPIELE[0]), erste)
        self.assertIn(("zahl", "aus_1_gsig", 11), erste)
        self.assertIn(("sichtbar", "ein_5_var", False), erste)
        self.assertIn(("sichtbar", "aus_4_zustand", True), erste)
        # Reaktion: Haken blendet alle Zeilen ein, Zahl landet in der Zustandsspalte, Statuszeile meldet den Klick
        alles = [x for paket in app.gesendet for x in paket]
        self.assertIn(("sichtbar", "aus_8_gsig", True), alles)
        self.assertIn(("text", "ein_2_zustand", "-> GSig 7"), alles)
        self.assertTrue(any(x[0] == "text" and x[1] == "sps_status" and "Variablen lesen" in x[2] for x in alles))
        self.assertEqual(p.reaktionen, 5)
        punkte = {x["was"]: x for x in b.punkte}
        self.assertEqual(punkte["Bedienfeld sichtbar"]["art"], "ok")
        self.assertEqual((punkte["Zeilen ausgeblendet"]["art"], punkte["Zeilen ausgeblendet"]["text"]), ("warnung", "nein: Zeile 4 noch da"))
        self.assertEqual(punkte["Ereignisse aus iRC"]["art"], "ok")
        self.assertEqual(punkte["Zustand auf Anfrage"]["art"], "ok")
        self.assertEqual(punkte["GSig-Nummer Container -> iRC"]["art"], "ok")
        self.assertEqual(punkte["GSig-Nummer iRC -> Container"]["art"], "warnung")
        self.assertIn("als Nummer 12", punkte["GSig-Nummer iRC -> Container"]["text"])
        self.assertEqual(punkte["Ohne Container"]["text"], "letzter Text bleibt stehen")
        self.assertFalse(app.verbunden_)
        self.assertEqual(app.gsigs, {12})   # GSig 1 und 100 wieder aus

    def test_ohne_ereignisse_klare_meldung(self):
        import bedienfeld_pruefen
        app = []
        antworten = iter(["j"] * 3 + ["", "", "", "", "", "", "", "", ""])
        p = bedienfeld_pruefen.Pruefung("10.0.0.5", eingabe=lambda t: next(antworten, ""), ausgabe=lambda t: None,
                                        app_fabrik=lambda ereignis: app.append(FalscheApp(ereignis)) or app[0],
                                        warte=lambda s: None)
        p.verbinden()
        p.ereignisse()
        self.assertEqual(p.b.punkte[-1]["art"], "fehler")
        self.assertIn("Schuelerseite", p.b.punkte[-1]["text"])


@unittest.skipUnless(MIT_GRPC, "grpcio/protobuf fehlen")
class TestIrcAppGegenNachbau(unittest.TestCase):
    """irc_app.IrcApp gegen einen gRPC-Server mit derselben Beschreibung (robotcontrolapp.pb) wie iRC."""

    def setUp(self):
        import grpc
        from concurrent import futures
        import probetest_sps
        self.k, pool = probetest_sps._api_klassen()
        dienst = pool.FindServiceByName("robotcontrolapp.RobotControlApp")
        self.gsigs, self.aktionen, self.events = set(), [], queue.Queue()

        def einfach(name, antwort):
            m = dienst.FindMethodByName(name)
            return grpc.unary_unary_rpc_method_handler(antwort, request_deserializer=self.k[m.input_type.name].FromString,
                                                       response_serializer=lambda x: x.SerializeToString())

        def io(req, ctx):
            for g in req.GSigs:
                (self.gsigs.add if g.target_state == 1 else self.gsigs.discard)(g.id)
            return self.k["IOStateResponse"]()

        def zustand(req, ctx):
            r = self.k["RobotState"]()
            for i in range(1, 101):
                g = r.GSigs.add()
                g.id, g.state = i, 1 if i in self.gsigs else 0
            return r

        def strom(anfragen, ctx):
            threading.Thread(target=lambda: [self.aktionen.append(a) for a in anfragen], daemon=True).start()
            while True:
                e = self.events.get()
                if e is None:
                    return
                yield e

        handler = {"SetCapabilities": einfach("SetCapabilities", lambda r, c: self.k["CapabilitiesResponse"]()),
                   "GetSystemInfo": einfach("GetSystemInfo", lambda r, c: self.k["SystemInfo"](version="V980-15-000-2", is_simulation=True)),
                   "SetIOState": einfach("SetIOState", io), "GetRobotState": einfach("GetRobotState", zustand),
                   "RecieveActions": grpc.stream_stream_rpc_method_handler(
                       strom, request_deserializer=self.k["AppAction"].FromString, response_serializer=lambda x: x.SerializeToString())}
        self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
        self.server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler("robotcontrolapp.RobotControlApp", handler),))
        self.port = self.server.add_insecure_port("127.0.0.1:0")
        self.server.start()

    def tearDown(self):
        self.events.put(None)
        self.server.stop(0)

    def warte_auf(self, bedingung, sekunden=5):
        ende = time.time() + sekunden
        while time.time() < ende:
            if bedingung():
                return True
            time.sleep(0.02)
        return False

    def test_bedienfeld_ereignisse_und_gsigs(self):
        import irc_app
        empfangen = []
        app = irc_app.IrcApp("127.0.0.1", self.port, ereignis=empfangen.extend)
        self.assertEqual(app.aufruf("GetSystemInfo")["version"], "V980-15-000-2")
        app.anmelden()
        app.verbinden()
        self.assertTrue(self.warte_auf(lambda: self.aktionen))
        self.assertEqual(self.aktionen[0].app_name, "WebotsSim")   # erste Nachricht meldet die App an
        app.text("sps_status", "Pruefung läuft ●")
        app.liste("ein_1_var", ["- frei -", "Start_Taster (Bool)"], "Start_Taster (Bool)")
        app.zahl("ein_1_gsig", 3)
        app.haken("sps_alle_zeilen", True)
        app.sichtbar("ein_5_var", False)
        app.senden()
        self.assertTrue(self.warte_auf(lambda: len(self.aktionen) >= 2))
        ui = {e.element_name: e for e in self.aktionen[1].ui_changes}
        self.assertEqual(ui["sps_status"].state.textfield_state.current_text, "Pruefung läuft ●")
        self.assertEqual(list(ui["ein_1_var"].state.dropdown_state.options), ["- frei -", "Start_Taster (Bool)"])
        self.assertEqual(ui["ein_1_var"].state.dropdown_state.selected_option, "Start_Taster (Bool)")
        self.assertEqual(ui["ein_1_gsig"].state.numberfield_state.current_number, 3.0)
        self.assertEqual(ui["sps_alle_zeilen"].state.checkbox_state, 1)
        self.assertTrue(ui["ein_5_var"].HasField("is_visible"))
        self.assertFalse(ui["ein_5_var"].is_visible)
        app.zustaende_anfragen(["sps_adresse"])
        self.assertTrue(self.warte_auf(lambda: len(self.aktionen) >= 3))
        self.assertEqual(list(self.aktionen[2].request_ui_state), ["sps_adresse"])
        # Klick in "iRC": kommt als Ereignis an, auch ein nicht gesetzter Haken (Standardwert 0)
        e = self.k["Event"]()
        for name, feld, wert in (("sps_lesen", "button_state", 1), ("sps_alle_zeilen", "checkbox_state", 0)):
            u = e.ui_updates.add()
            u.element_name = name
            setattr(u.state, feld, wert)
        self.events.put(e)
        self.assertTrue(self.warte_auf(lambda: len(empfangen) >= 2))
        self.assertEqual([irc_app.wert_aus(a) for a in empfangen], [True, False])
        self.assertEqual(app.ereignisse[0]["aenderungen"][0]["element_name"], "sps_lesen")
        # globale Signale: nur die genannten aendern sich
        app.gsigs_setzen({1: True, 100: True})
        app.gsigs_setzen({50: False})
        self.assertEqual(app.gsigs_an(), [1, 100])
        app.gsigs_setzen({1: False})
        self.assertEqual(app.gsigs_an(), [100])
        app.trennen()


@unittest.skipUnless(MIT_ASYNCUA, "asyncua fehlt")
class TestSpsVariablen(unittest.TestCase):
    def test_durchsuchen_wie_plcnext(self):
        import asyncio
        import socket
        import sps_opcua
        from asyncua import Server, ua

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        async def ablauf():
            server = Server()
            await server.init()
            server.set_endpoint("opc.tcp://127.0.0.1:%d/" % port)
            ns = await server.register_namespace(sps_opcua.PLCNEXT_NAMENSRAUM)
            eclr = await server.nodes.objects.add_object(ua.NodeId("Arp.Plc.Eclr", ns), "Eclr")
            for name, wert, typ, schreib in (("Start_Taster", False, ua.VariantType.Boolean, True),
                                             ("ROB_IN", 5, ua.VariantType.Byte, True),
                                             ("Status", 0, ua.VariantType.UInt16, False)):
                v = await eclr.add_variable(ua.NodeId("Arp.Plc.Eclr/" + name, ns), name, ua.Variant(wert, typ))
                if schreib:
                    await v.set_writable()
            inst = await eclr.add_object(ua.NodeId("Arp.Plc.Eclr/MainInstance", ns), "MainInstance")
            await inst.add_variable(ua.NodeId("Arp.Plc.Eclr/MainInstance.Zaehler", ns), "Zaehler", ua.Variant(0, ua.VariantType.Int16))
            async with server:
                return await sps_opcua._variablen_lesen("127.0.0.1", port, None, None, 5.0)

        liste = asyncio.run(ablauf())
        self.assertEqual([(v["name"], v["typ"], v["bits"], v["schreibbar"]) for v in liste],
                         [("MainInstance.Zaehler", "Int", 0, False), ("ROB_IN", "Byte", 8, True),
                          ("Start_Taster", "Bool", 1, True), ("Status", "Word", 16, False)])
        self.assertEqual(sps_opcua.anzeige(liste[2]), "Start_Taster (Bool)")


if __name__ == "__main__":
    unittest.main()
