"""Probetest fuer die SPS-Kopplung (Stufe G4), noch ohne die eigentliche Umsetzung.

Prueft die beiden Enden, die die Kopplung spaeter verbindet, und schreibt einen Bericht:

  opcua   OPC-UA-Server einer SPS (Phoenix Contact AXCF 2152): Endpunkte und Sicherheit, Anmeldung mit
          Benutzer/Passwort, Variablen ROB_IN, ROB_OUT, ROB_STEUER, ROB_PROGNR, ROB_STATUS, ROB_WERT_1..4,
          Antwortzeit. Mit --schreiben wird ROB_OUT kurz auf ein Testmuster gesetzt und zurueckgestellt.
  irc     App-Schnittstelle von iRC (gRPC, Port 5000), am Schueler-PC oder am echten ReBeL: Systeminfo,
          Lizenzen, Roboterzustand, Programmzustand, Programme im Datenordner. Mit --schreiben: ein globales
          Signal (GSig) und optional ein Eingang (DIn) an/aus, eine Programmvariable setzen. Mit --programm und
          --bewegen: Programm laden, starten, nach wenigen Sekunden stoppen (der Roboter faehrt!).

Ohne --schreiben/--bewegen wird nur gelesen.

  python3 tools/probetest_sps.py opcua --sps 192.168.1.20 --benutzer admin
  python3 tools/probetest_sps.py irc --ziel 192.168.3.11
  python3 tools/probetest_sps.py irc --ziel 192.168.3.11 --schreiben --gsig 1 --variable sps_wert=5
  python3 tools/probetest_sps.py irc --ziel 192.168.2.142 --programm test.xml --bewegen

Benoetigt fuer opcua das Paket asyncua (siehe tools/probetest_vorbereiten.sh), fuer irc grpcio und protobuf
(im Container vorhanden). Die Beschreibung der App-Schnittstelle liegt in tools/irc_api/ (MIT, Commonplace Robotics).
"""

import argparse
import getpass
import json
import os
import sys
import time

HIER = os.path.dirname(os.path.abspath(__file__))
API_BESCHREIBUNG = os.path.join(HIER, "irc_api", "robotcontrolapp.pb")
SPS_VARIABLEN = ["ROB_IN", "ROB_OUT", "ROB_STEUER", "ROB_PROGNR", "ROB_STATUS",
                 "ROB_WERT_1", "ROB_WERT_2", "ROB_WERT_3", "ROB_WERT_4"]
PLCNEXT_NAMENSRAUM = "http://phoenixcontact.com/OpcUA/PLCnext/GlobalDataSpace/"
TESTMUSTER = 0xA5


class Bericht:
    """Sammelt Ergebnisse (ok / Hinweis / Fehler), gibt sie lesbar aus und als JSON."""

    def __init__(self, titel):
        self.titel = titel
        self.punkte = []
        print("=== %s ===" % titel)

    def eintrag(self, art, was, text="", daten=None):
        zeichen = {"ok": "[ OK ]", "info": "[INFO]", "warnung": "[WARN]", "fehler": "[FEHL]"}[art]
        print("%s %s%s" % (zeichen, was, (": " + text) if text else ""))
        if daten is not None:
            for zeile in json.dumps(daten, ensure_ascii=False, indent=2, default=str).splitlines():
                print("       " + zeile)
        self.punkte.append({"art": art, "was": was, "text": text, "daten": daten})

    def ok(self, was, text="", daten=None):
        self.eintrag("ok", was, text, daten)

    def info(self, was, text="", daten=None):
        self.eintrag("info", was, text, daten)

    def warnung(self, was, text="", daten=None):
        self.eintrag("warnung", was, text, daten)

    def fehler(self, was, text="", daten=None):
        self.eintrag("fehler", was, text, daten)

    def schreiben(self, datei):
        if not datei:
            return
        with open(datei, "w", encoding="utf-8") as f:
            json.dump({"titel": self.titel, "zeit": time.strftime("%Y-%m-%d %H:%M:%S"), "punkte": self.punkte},
                      f, ensure_ascii=False, indent=2, default=str)
        print("Bericht gespeichert: %s" % datei)

    def zusammenfassung(self):
        n = {a: sum(1 for p in self.punkte if p["art"] == a) for a in ("ok", "warnung", "fehler")}
        print("--- %d ok, %d Warnungen, %d Fehler ---" % (n["ok"], n["warnung"], n["fehler"]))
        return 1 if n["fehler"] else 0


# ---------------------------------------------------------------- OPC UA (SPS)

async def _opcua(args, b):
    try:
        from asyncua import Client, ua
    except ImportError:
        b.fehler("asyncua fehlt", "einmal tools/probetest_vorbereiten.sh ausfuehren und das Skript mit "
                                  "/opt/rebel-sim-probe/bin/python3 starten")
        return
    url = "opc.tcp://%s:%d" % (args.sps, args.port)
    # 1. Endpunkte: welche Sicherheit und welche Anmeldearten bietet der Server an?
    try:
        endpunkte = await Client(url, timeout=args.zeit).connect_and_get_server_endpoints()
    except Exception as e:   # noqa: BLE001 - jede Art von Netzfehler melden
        b.fehler("Server erreichbar", "%s nicht erreichbar (%s: %s). Netz, IP und Port 4840 pruefen; "
                                     "in PLCnext Engineer muss der OPC-UA-Server aktiv sein." % (url, type(e).__name__, e))
        return
    liste = [{"sicherheit": e.SecurityPolicyUri.rsplit("#", 1)[-1], "modus": e.SecurityMode.name,
              "anmeldung": sorted({t.TokenType.name for t in e.UserIdentityTokens})} for e in endpunkte]
    b.ok("Server erreichbar", url, liste)
    ohne = [e for e in liste if e["sicherheit"] == "None" and "UserName" in e["anmeldung"]]
    # 2. Anmelden: ohne Verschluesselung, falls erlaubt, sonst Basic256Sha256 mit eigenem Zertifikat
    passwort = args.passwort or os.environ.get("SPS_PASSWORT") or getpass.getpass("Passwort fuer %s auf %s: " % (args.benutzer, args.sps))
    modus = args.sicherheit if args.sicherheit != "auto" else ("none" if ohne else "signencrypt")
    client = Client(url, timeout=args.zeit)
    client.application_uri = "urn:rebel-sim:sps-kopplung"
    client.name = "ReBeL-Simulation SPS-Kopplung (Probetest)"
    client.set_user(args.benutzer)
    client.set_password(passwort)
    if modus != "none":
        from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256
        from asyncua.crypto.cert_gen import setup_self_signed_certificate
        from cryptography.x509.oid import ExtendedKeyUsageOID
        ordner = args.zertifikate
        os.makedirs(ordner, exist_ok=True)
        zert, schluessel = os.path.join(ordner, "sps-kopplung.der"), os.path.join(ordner, "sps-kopplung.pem")
        import socket
        from pathlib import Path   # asyncua 2.x erwartet Pfad-Objekte
        await setup_self_signed_certificate(Path(schluessel), Path(zert), client.application_uri, socket.gethostname(),
                                            [ExtendedKeyUsageOID.CLIENT_AUTH], {"commonName": "rebel-sim sps-kopplung"})
        await client.set_security(SecurityPolicyBasic256Sha256, certificate=zert, private_key=schluessel,
                                  mode=ua.MessageSecurityMode.SignAndEncrypt if modus == "signencrypt" else ua.MessageSecurityMode.Sign)
        b.info("Eigenes Zertifikat", zert)
    try:
        await client.connect()
    except Exception as e:   # noqa: BLE001
        text = "%s: %s" % (type(e).__name__, e)
        if "Certificate" in text or "Security" in text:
            text += (". Vermutlich vertraut die SPS dem Zertifikat des Containers noch nicht: im Web-Based-Management "
                     "der SPS unter Sicherheit/Zertifikate das abgelehnte Zertifikat 'rebel-sim sps-kopplung' als "
                     "vertrauenswuerdig eintragen und den Test wiederholen.")
        elif "Identity" in text or "User" in text:
            text += ". Benutzername oder Passwort stimmen nicht, oder das Konto darf kein OPC UA."
        b.fehler("Anmelden (%s)" % modus, text)
        return
    b.ok("Anmelden", "als %s, Sicherheit %s" % (args.benutzer, modus))
    try:
        # 3. Namensraum der globalen Variablen und die ROB_*-Variablen finden
        namen = await client.get_namespace_array()
        ns = next((i for i, n in enumerate(namen) if n.rstrip("/") == PLCNEXT_NAMENSRAUM.rstrip("/")), None)
        if ns is None:
            ns = next((i for i, n in enumerate(namen) if "GlobalDataSpace" in n or "PLCnext" in n), None)
        b.info("Namensraeume", "PLCnext-Variablen in ns=%s" % ns, namen)
        gefunden = {}
        for name in SPS_VARIABLEN:
            knoten = await _finde_variable(client, ua, ns, name, args.programm_instanz)
            if knoten is None:
                continue
            gefunden[name] = knoten
        fehlend = [n for n in SPS_VARIABLEN if n not in gefunden]
        if not gefunden:
            b.fehler("Variablen", "keine ROB_*-Variable gefunden. In PLCnext Engineer die Variablen anlegen und "
                                  "die Spalte OPC ankreuzen, Projekt uebertragen.", {"gesucht": SPS_VARIABLEN})
            await _variablen_auflisten(client, b, ns)
            return
        (b.ok if not fehlend else b.warnung)("Variablen", "%d von %d gefunden" % (len(gefunden), len(SPS_VARIABLEN)),
                                              {"gefunden": {n: k.nodeid.to_string() for n, k in gefunden.items()}, "fehlen": fehlend})
        werte = {}
        for name, knoten in gefunden.items():
            dv = await knoten.read_data_value()
            werte[name] = {"wert": dv.Value.Value, "typ": dv.Value.VariantType.name,
                           "schreibbar": bool((await knoten.read_attribute(ua.AttributeIds.UserAccessLevel)).Value.Value & 2)}
        b.ok("Werte gelesen", daten=werte)
        # 4. Antwortzeit
        if "ROB_IN" in gefunden:
            zeiten = []
            for _ in range(20):
                t0 = time.perf_counter()
                await gefunden["ROB_IN"].read_value()
                zeiten.append((time.perf_counter() - t0) * 1000)
            b.ok("Antwortzeit", "ROB_IN lesen: Mittel %.1f ms, hoechstens %.1f ms (20 Mal)" % (sum(zeiten) / 20, max(zeiten)))
        # 5. Schreiben (nur auf Wunsch): ROB_OUT kurz auf das Testmuster, danach zurueck
        if args.schreiben:
            if "ROB_OUT" not in gefunden:
                b.fehler("Schreiben", "ROB_OUT fehlt")
            else:
                knoten, alt = gefunden["ROB_OUT"], await gefunden["ROB_OUT"].read_data_value()
                typ = alt.Value.VariantType
                try:
                    await knoten.write_value(ua.DataValue(ua.Variant(TESTMUSTER, typ)))
                    zurueck = await knoten.read_value()
                    (b.ok if zurueck == TESTMUSTER else b.fehler)(
                        "Schreiben", "ROB_OUT := %d (Testmuster 0b%s), gelesen %s; in PLCnext Engineer online zu sehen"
                        % (TESTMUSTER, format(TESTMUSTER, "08b"), zurueck))
                    if args.halten:
                        print("       ... halte das Testmuster %d s (in PLCnext Engineer pruefen) ..." % args.halten)
                        time.sleep(args.halten)
                finally:
                    await knoten.write_value(ua.DataValue(ua.Variant(alt.Value.Value, typ)))
                    b.info("Zurueckgestellt", "ROB_OUT = %s" % alt.Value.Value)
    finally:
        await client.disconnect()


async def _finde_variable(client, ua, ns, name, instanz):
    """Variable als globale Variable (Arp.Plc.Eclr/NAME) oder in einer Programminstanz suchen."""
    kandidaten = ["Arp.Plc.Eclr/" + name]
    if instanz:
        kandidaten.insert(0, "Arp.Plc.Eclr/%s.%s" % (instanz, name))
    for kennung in kandidaten:
        knoten = client.get_node(ua.NodeId(kennung, ns))
        try:
            await knoten.read_browse_name()
            return knoten
        except Exception:   # noqa: BLE001 - gibt es nicht
            continue
    return None


async def _variablen_auflisten(client, b, ns):
    """Zur Fehlersuche: die ersten Variablen unter Arp.Plc.Eclr zeigen."""
    from asyncua import ua
    try:
        wurzel = client.get_node(ua.NodeId("Arp.Plc.Eclr", ns))
        kinder = await wurzel.get_children()
        b.info("Vorhandene Variablen (Auszug)", daten=[(await k.read_browse_name()).Name for k in kinder[:60]])
    except Exception as e:   # noqa: BLE001
        b.warnung("Variablen auflisten", "%s: %s" % (type(e).__name__, e))


# ---------------------------------------------------------------- App-Schnittstelle (iRC / ReBeL)

def _api_klassen():
    """Nachrichtenklassen aus der Beschreibung robotcontrolapp.pb (unabhaengig von der protobuf-Version)."""
    from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
    menge = descriptor_pb2.FileDescriptorSet()
    with open(API_BESCHREIBUNG, "rb") as f:
        menge.ParseFromString(f.read())
    pool = descriptor_pool.DescriptorPool()
    for datei in menge.file:
        pool.Add(datei)
    namen = [m.name for m in menge.file[0].message_type]
    klassen = {}
    for n in namen:
        d = pool.FindMessageTypeByName("robotcontrolapp." + n)
        klassen[n] = (message_factory.GetMessageClass(d) if hasattr(message_factory, "GetMessageClass")
                      else message_factory.MessageFactory(pool).GetPrototype(d))
    return klassen, pool


class App:
    """Duenne Huelle um den gRPC-Kanal: aufruf("GetRobotState", feld=wert ...) -> Antwort."""

    def __init__(self, ziel, port, app_name, zeit):
        import grpc
        self.grpc = grpc
        self.k, self.pool = _api_klassen()
        self.app_name, self.zeit = app_name, zeit
        self.kanal = grpc.insecure_channel("%s:%d" % (ziel, port))
        self.dienst = self.pool.FindServiceByName("robotcontrolapp.RobotControlApp")

    def bereit(self):
        self.grpc.channel_ready_future(self.kanal).result(timeout=self.zeit)

    def _methode(self, name):
        m = self.dienst.FindMethodByName(name)
        return m, self.k[m.input_type.name], self.k[m.output_type.name]

    def aufruf(self, name, **felder):
        m, rein, raus = self._methode(name)
        anfrage = rein(app_name=self.app_name, **felder) if "app_name" in rein.DESCRIPTOR.fields_by_name else rein(**felder)
        art = self.kanal.unary_stream if m.server_streaming else self.kanal.unary_unary
        f = art("/robotcontrolapp.RobotControlApp/" + name, request_serializer=lambda x: x.SerializeToString(),
                response_deserializer=raus.FromString)
        antwort = f(anfrage, timeout=self.zeit)
        return list(antwort) if m.server_streaming else antwort


def _als_dict(nachricht):
    from google.protobuf.json_format import MessageToDict
    return MessageToDict(nachricht, preserving_proto_field_name=True)


def _grpc_fehler(e):
    try:
        return "%s: %s" % (e.code().name, e.details())
    except Exception:   # noqa: BLE001
        return str(e)


def _irc(args, b):
    try:
        app = App(args.ziel, args.port, args.app, args.zeit)
    except ImportError as e:
        b.fehler("grpcio/protobuf fehlen", str(e))
        return
    ziel = "%s:%d" % (args.ziel, args.port)
    try:
        app.bereit()
    except Exception:   # noqa: BLE001
        b.fehler("App-Schnittstelle erreichbar", "%s antwortet nicht. Laeuft iRC bzw. ist der ReBeL eingeschaltet? "
                                                "Port 5000 im Netz offen?" % ziel)
        return
    b.ok("App-Schnittstelle erreichbar", ziel)
    grpc = app.grpc

    def lesen(was, name, **felder):
        try:
            antwort = app.aufruf(name, **felder)
        except grpc.RpcError as e:
            text = _grpc_fehler(e)
            if "PERMISSION" in text or "not enabled" in text.lower() or "unknown app" in text.lower():
                text += (". Die App '%s' ist in iRC nicht installiert oder nicht freigeschaltet "
                         "(iRC: Apps, deploy/irc_app/WebotsSim.zip)." % args.app)
            b.fehler(was, text)
            return None
        return antwort

    # 1. Anmelden der App (Versionsabgleich), Systeminfo, Lizenzen
    cap = lesen("App anmelden (SetCapabilities)", "SetCapabilities", api_version_major=1, api_version_minor=0, api_version_patch=0)
    if cap is not None:
        b.ok("App anmelden (SetCapabilities)", "App '%s'" % args.app, _als_dict(cap))
    info = lesen("Systeminfo", "GetSystemInfo")
    if info is not None:
        b.ok("Systeminfo", daten=_als_dict(info))
    lizenzen = lesen("Lizenzen", "GetLicensedFeatures")
    if lizenzen is not None:
        b.ok("Lizenzen", daten=_als_dict(lizenzen))
    # 2. Roboterzustand: Eingaenge, Ausgaenge, globale Signale, Fehler, Referenzierung
    zustand = lesen("Roboterzustand", "GetRobotState")
    if zustand is not None:
        d = _als_dict(zustand)
        kurz = {k: v for k, v in d.items() if k not in ("DIns", "DOuts", "GSigs", "joints", "external_joints")}
        an = lambda liste: [e.get("id", 0) for e in d.get(liste, []) if e.get("state") == "HIGH"]   # noqa: E731
        b.ok("Roboterzustand", "%d DIns, %d DOuts, %d GSigs; an: DIn %s, DOut %s, GSig %s" % (
            len(d.get("DIns", [])), len(d.get("DOuts", [])), len(d.get("GSigs", [])), an("DIns"), an("DOuts"), an("GSigs")), kurz)
    bewegung = lesen("Programmzustand", "GetMotionState")
    if bewegung is not None:
        b.ok("Programmzustand", daten=_als_dict(bewegung))
    dateien = lesen("Programme im Datenordner", "ListFiles", path=args.programmordner)
    if dateien is not None:
        d = _als_dict(dateien)
        b.ok("Programme im Datenordner", "Ordner %s" % args.programmordner,
             [e.get("name") for e in d.get("entries", [])][:80] or d)
    # 3. Schreiben (nur auf Wunsch): GSig, DIn, Programmvariable
    if args.schreiben:
        for art, nr in (("GSigs", args.gsig), ("DIns", args.din)):
            if nr is None:
                continue
            _signal_testen(app, b, grpc, art, nr)
    if args.variable:
        _variable_testen(app, b, grpc, args)
    # 4. Programm laden, starten, stoppen (nur mit --bewegen)
    if args.programm:
        if not args.bewegen:
            b.warnung("Programm starten", "uebersprungen: dafuer --bewegen angeben (der Roboter faehrt!)")
        else:
            _programm_testen(app, b, grpc, args)


def _signal_testen(app, b, grpc, art, nr):
    """Ein globales Signal bzw. einen Eingang einschalten, im Zustand nachsehen, wieder ausschalten."""
    klasse = app.k["GSig" if art == "GSigs" else "DIn"]
    was = "%s %d setzen" % ("GSig" if art == "GSigs" else "DIn", nr)

    def setzen(an):
        # globale Signale werden erst mit target_state gesetzt (in iRC V15.0.2 so festgestellt), Eingaenge mit state
        felder = {"id": nr, "state": 1 if an else 0}
        if art == "GSigs":
            felder["target_state"] = 1 if an else 0
        app.aufruf("SetIOState", **{art: [klasse(**felder)]})
        time.sleep(0.3)
        d = _als_dict(app.aufruf("GetRobotState"))
        return [e.get("id", 0) for e in d.get(art, []) if e.get("state") == "HIGH"]
    try:
        vorher = setzen(True)
        nachher = setzen(False)
    except grpc.RpcError as e:
        b.fehler(was, _grpc_fehler(e))
        return
    if nr in vorher and nr not in nachher:
        b.ok(was, "an und wieder aus, im Roboterzustand sichtbar (Nummer %d der Schnittstelle)" % nr)
    elif vorher and set(vorher) - set(nachher):
        b.warnung(was, "eingeschaltet, im Zustand aber als %s gemeldet (Zaehlweise Anzeige/Schnittstelle pruefen)"
                  % sorted(set(vorher) - set(nachher)))
    else:
        b.fehler(was, "kein Unterschied im Roboterzustand: die Steuerung nimmt %s von der App nicht an "
                      "(am echten ReBeL bei Hardware-Eingaengen moeglich)" % art)


def _variable_testen(app, b, grpc, args):
    name, _, wert = args.variable.partition("=")
    try:
        vorher = app.aufruf("GetProgramVariables", variable_names=[name])
        b.ok("Programmvariable %s lesen" % name, daten=[_als_dict(v) for v in vorher] or "keine Antwort (Variable unbekannt "
             "oder kein Programm geladen)")
        if wert and args.schreiben:
            app.aufruf("SetProgramVariables", variables=[app.k["ProgramVariable"](name=name, number=float(wert))])
            nachher = app.aufruf("GetProgramVariables", variable_names=[name])
            werte = [_als_dict(v) for v in nachher]
            (b.ok if any(abs(v.get("number", 1e99) - float(wert)) < 1e-9 for v in werte) else b.warnung)(
                "Programmvariable %s setzen" % name, "%s gesetzt, gelesen %s" % (wert, werte))
    except grpc.RpcError as e:
        b.fehler("Programmvariable %s" % name, _grpc_fehler(e))


def _programm_testen(app, b, grpc, args):
    rs = app.pool.FindEnumTypeByName("robotcontrolapp.RunState").values_by_name
    print("       ACHTUNG: Programm %s wird gestartet, der Roboter faehrt %d s. Abbruch mit Strg+C." % (args.programm, args.dauer))
    time.sleep(3)
    try:
        geladen = app.aufruf("SetMotionInterpolator", main_program=args.programm)
        b.ok("Programm laden", args.programm, _als_dict(geladen))
        gestartet = app.aufruf("SetMotionInterpolator", runstate=rs["RUNNING"].number)
        b.ok("Programm starten", daten=_als_dict(gestartet))
        ende = time.time() + args.dauer
        while time.time() < ende:
            z = _als_dict(app.aufruf("GetMotionState")).get("motion_ipo", {})
            print("       laeuft: %s, Programm %s, Befehl %s/%s" % (z.get("runstate", "NOT_RUNNING"), z.get("current_program_name"),
                                                                 z.get("current_command_idx", 0), z.get("command_count", 0)))
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    except grpc.RpcError as e:
        b.fehler("Programm steuern", _grpc_fehler(e))
    finally:
        try:
            gestoppt = app.aufruf("SetMotionInterpolator", runstate=rs["NOT_RUNNING"].number)
            b.ok("Programm stoppen", daten=_als_dict(gestoppt))
        except grpc.RpcError as e:
            b.fehler("Programm stoppen", _grpc_fehler(e) + " - Roboter in iRC von Hand stoppen!")


# ---------------------------------------------------------------- Aufruf

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    teile = ap.add_subparsers(dest="teil", required=True)
    o = teile.add_parser("opcua", help="OPC-UA-Server der SPS pruefen")
    o.add_argument("--sps", required=True, help="IP-Adresse der SPS")
    o.add_argument("--port", type=int, default=4840)
    o.add_argument("--benutzer", default="admin")
    o.add_argument("--passwort", help="sonst Umgebungsvariable SPS_PASSWORT oder Abfrage")
    o.add_argument("--sicherheit", choices=["auto", "none", "sign", "signencrypt"], default="auto")
    o.add_argument("--programm-instanz", help="falls die Variablen in einer Programminstanz liegen, z.B. MainInstance")
    o.add_argument("--zertifikate", default=os.environ.get("ZERTIFIKATE", "/var/lib/rebel-sim/opcua"))
    o.add_argument("--schreiben", action="store_true", help="ROB_OUT kurz auf das Testmuster setzen")
    o.add_argument("--halten", type=int, default=0, help="so viele Sekunden das Testmuster stehen lassen")
    i = teile.add_parser("irc", help="App-Schnittstelle von iRC bzw. des ReBeL pruefen")
    i.add_argument("--ziel", required=True, help="IP des Schueler-PCs (iRC) oder des echten ReBeL")
    i.add_argument("--port", type=int, default=5000)
    i.add_argument("--app", default="WebotsSim", help="Name der in iRC freigeschalteten App")
    i.add_argument("--programmordner", default="Programs", help="Ordner relativ zu Data, fuer die Programmliste")
    i.add_argument("--schreiben", action="store_true", help="Signale bzw. Variable wirklich setzen")
    i.add_argument("--gsig", type=int, default=1, help="Nummer des globalen Signals fuer den Schreibtest")
    i.add_argument("--din", type=int, help="zusaetzlich diesen Eingang testen (Schnittstellen-Nummer)")
    i.add_argument("--variable", help="NAME oder NAME=WERT (Zahl) einer Programmvariablen")
    i.add_argument("--programm", help="Programmdatei laden und starten, z.B. test.xml")
    i.add_argument("--bewegen", action="store_true", help="erst damit wird ein Programm wirklich gestartet")
    i.add_argument("--dauer", type=int, default=5, help="Sekunden, bis das Programm wieder gestoppt wird")
    for p in (o, i):
        p.add_argument("--zeit", type=float, default=5.0, help="Zeitgrenze je Anfrage in Sekunden")
        p.add_argument("--bericht", help="Bericht zusaetzlich als JSON-Datei")
    args = ap.parse_args(argv)
    if args.teil == "opcua":
        import asyncio
        b = Bericht("OPC UA an %s" % args.sps)
        asyncio.run(_opcua(args, b))
    else:
        b = Bericht("App-Schnittstelle an %s" % args.ziel)
        _irc(args, b)
    b.schreiben(args.bericht)
    return b.zusammenfassung()


if __name__ == "__main__":
    sys.exit(main())
