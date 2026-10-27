"""OPC UA zur SPS (Phoenix Contact PLCnext, z.B. AXCF 2152): freigegebene Variablen finden, mit Datentyp.

PLCnext zeigt nur Variablen mit OPC-Haken (PLCnext Engineer, Datenliste); sie liegen im Namensraum GlobalDataSpace
unter Arp.Plc.Eclr, globale direkt (Arp.Plc.Eclr/Start_Taster), Programmvariablen in ihrer Instanz
(Arp.Plc.Eclr/MainInstance.Start_Taster). Fuer die Kopplung mit iRC taugen Bool (ein GSig), Byte (8 GSigs) und
Word (16 GSigs).

Braucht asyncua (im Container in /opt/rebel-sim-probe, siehe tools/probetest_vorbereiten.sh).
"""

import asyncio

PLCNEXT_NAMENSRAUM = "http://phoenixcontact.com/OpcUA/PLCnext/GlobalDataSpace/"
WURZEL = "Arp.Plc.Eclr"
# OPC-UA-Datentyp -> Name wie in PLCnext Engineer und Zahl der Bits fuer die Kopplung (0 = nicht koppelbar)
TYPEN = {"Boolean": ("Bool", 1), "Byte": ("Byte", 8), "UInt16": ("Word", 16), "SByte": ("SInt", 0), "Int16": ("Int", 0),
         "Int32": ("DInt", 0), "UInt32": ("DWord", 0), "Int64": ("LInt", 0), "UInt64": ("LWord", 0), "Float": ("Real", 0),
         "Double": ("LReal", 0), "String": ("String", 0)}
HOECHSTENS = 400   # so viele Variablen hoechstens auflisten (Schutz vor riesigen Projekten)


async def namensraum(client):
    namen = await client.get_namespace_array()
    ns = next((i for i, n in enumerate(namen) if n.rstrip("/") == PLCNEXT_NAMENSRAUM.rstrip("/")), None)
    if ns is None:
        ns = next((i for i, n in enumerate(namen) if "GlobalDataSpace" in n), None)
    return ns


async def durchsuchen(client, ns, tiefe=4):
    """Alle Variablen unter Arp.Plc.Eclr: [{"name", "knoten", "typ", "bits", "schreibbar"}], nach Namen sortiert.
    name ist der Pfad ohne "Arp.Plc.Eclr/" (z.B. "MainInstance.Start_Taster")."""
    from asyncua import ua
    ergebnis, gesehen = [], set()

    async def besuchen(knoten, rest):
        if len(ergebnis) >= HOECHSTENS or rest < 0:
            return
        for kind in await knoten.get_children():
            kennung = kind.nodeid.to_string()
            if kennung in gesehen:
                continue
            gesehen.add(kennung)
            klasse = await kind.read_node_class()
            if klasse == ua.NodeClass.Variable:
                ergebnis.append(await _variable(kind, ua))
                if len(ergebnis) >= HOECHSTENS:
                    return
            elif klasse == ua.NodeClass.Object:
                await besuchen(kind, rest - 1)

    await besuchen(client.get_node(ua.NodeId(WURZEL, ns)), tiefe)
    return sorted(ergebnis, key=lambda v: v["name"].lower())


async def _variable(knoten, ua):
    kennung = knoten.nodeid.Identifier if isinstance(knoten.nodeid.Identifier, str) else knoten.nodeid.to_string()
    name = kennung.split(WURZEL + "/", 1)[-1]
    try:
        typ_ua = (await knoten.read_data_type_as_variant_type()).name
    except Exception:   # noqa: BLE001 - zusammengesetzte Typen (Strukturen, Felder)
        typ_ua = "?"
    typ, bits = TYPEN.get(typ_ua, (typ_ua, 0))
    try:
        zugriff = (await knoten.read_attribute(ua.AttributeIds.UserAccessLevel)).Value.Value
    except Exception:   # noqa: BLE001
        zugriff = 0
    return {"name": name, "knoten": knoten.nodeid.to_string(), "typ": typ, "bits": bits, "schreibbar": bool(zugriff & 2)}


async def _variablen_lesen(ip, port, benutzer, passwort, zeit):
    from asyncua import Client
    client = Client("opc.tcp://%s:%d" % (ip, port), timeout=zeit)
    client.application_uri = "urn:rebel-sim:sps-kopplung"
    client.name = "ReBeL-Simulation SPS-Kopplung"
    if benutzer:
        client.set_user(benutzer)
        client.set_password(passwort or "")
    await client.connect()
    try:
        ns = await namensraum(client)
        if ns is None:
            raise RuntimeError("kein PLCnext-Namensraum (GlobalDataSpace) auf dem Server")
        return await durchsuchen(client, ns)
    finally:
        await client.disconnect()


def variablen_lesen(ip, port=4840, benutzer=None, passwort=None, zeit=5.0):
    """Ohne Verschluesselung, anonym oder mit Benutzer (so am 27.10.2026 an der AXCF 2152 erreichbar)."""
    return asyncio.run(_variablen_lesen(ip, port, benutzer, passwort, zeit))


def anzeige(v):
    """Eintrag fuer die Auswahlliste in iRC, z.B. "Start_Taster (Bool)"."""
    return "%s (%s)" % (v["name"], v["typ"])
