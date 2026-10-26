"""Greif- und Ablagepunkte einer Roboterumgebung als iRC-Programm (Plan: STAND_UND_KONZEPT.md, Abschnitt 13).

Gerechnet wird ohne Webots, nur aus dem Baukasten-Entwurf, den Merkmalen der Teile und den Greiferdaten:
  Greifpunkt am Werkstueck ("griff", siehe baukasten.griff_werte) -> TCP-Lage im Teilsystem
  -> mit der Startlage des Werkstuecks bzw. der Lage eines Ablage-Abbilds in iRC-Basiskoordinaten.
Werkzeugsystem wie im Robotermodell: z = Anfahrrichtung (vom Flansch ins Teil), die Backen schliessen entlang x,
der TCP liegt mittig zwischen den Backen. Vorposition: entlang der Werkzeugachse um vor_mm zurueck.
Alle Masse in mm und Grad wie in iRC. Nur Python-Standardbibliothek.
"""

import math
import re

import baukasten as bk
import rebel_kinematik as kin

WINKEL_TOL = math.radians(3.0)   # Greifflaechen parallel, Anfahrt quer zur Schliessrichtung


# --- Orientierung als A/B/C wie in iRC ---
# A, B, C sind die Drehungen um X, Y, Z (Angabe des Nutzers). Die Reihenfolge ist noch mit iRC zu pruefen (Abschnitt 13):
# angenommen R = Rz(C) * Ry(B) * Rx(A), also erst um X, dann Y, dann Z um die festen Achsen der Basis.
# NUR diese beiden Funktionen aendern, falls der Test etwas anderes ergibt.

def abc_aus_matrix(r):
    """Drehmatrix (Spalten = Achsen des Werkzeugs in Basiskoordinaten) -> [A, B, C] in Grad."""
    sb = max(-1.0, min(1.0, -r[2][0]))
    b = math.asin(sb)
    if abs(math.cos(b)) > 1e-6:
        a = math.atan2(r[2][1], r[2][2])
        c = math.atan2(r[1][0], r[0][0])
    else:   # B = +-90 Grad: A und C drehen um dieselbe Achse, C = 0 setzen
        a = math.atan2(-r[1][2], r[1][1])
        c = 0.0
    return [math.degrees(a), math.degrees(b), math.degrees(c)]


def matrix_aus_abc(a, b, c):
    return bk._mm(bk._mm(bk._rot_z(c), bk._rot_y(b)), bk._rot_x(a))


# --- Greifer ---

def greifer_daten(text):
    """Kenndaten eines Greifers aus dem Robotermodell ('typ=egp40 tcp=0.103 spalt=0.014 hub=0.006 ...', m) in mm."""
    w = dict(re.findall(r"(\w+)=([^\s]+)", text or ""))
    try:
        spalt, hub = float(w["spalt"]) * 1000, float(w["hub"]) * 1000
        return {"typ": w["typ"], "tcp_mm": float(w["tcp"]) * 1000, "offen_mm": 2 * spalt, "zu_mm": 2 * (spalt - hub),
                "backe_z0_mm": float(w.get("backe_z0", 0)) * 1000, "backe_z1_mm": float(w.get("backe_z1", 0)) * 1000}
    except (KeyError, ValueError):
        return None


def greifer_aus_proto(text):
    """{typ: Kenndaten} aller Greifer im erzeugten Proto (protos/IgusRebel6DOF.proto, Tabelle GREIFER)."""
    ergebnis = {}
    for teil in re.findall(r"typ=[^'\"]+", text or ""):
        g = greifer_daten(teil)
        if g:
            ergebnis[g["typ"]] = g
    return ergebnis


# --- Greifpunkt im Teil ---

def _flaeche(m, ident):
    f = next((f for f in m["flaechen"] if str(f["id"]) == str(ident)), None)
    if f is None:
        raise bk.BaukastenFehler("Die Flaeche %s ist keine ebene Flaeche dieses Teils." % ident)
    return f


def griff_im_teil(m, g, greifer):
    """TCP-Lage (bk.Lage, mm) im Teilsystem, Angaben und Warnungen. Bei einem Zylinder, der von der Stirnseite
    gegriffen wird, ist die Drehung der Backen um die Achse frei (info["frei"]); sie legt erst griff_in_welt fest."""
    fa = _flaeche(m, g["anfahrt"])
    na = bk._norm(fa["normale"])
    z = bk._mul(na, -1.0)                 # Anfahrrichtung: auf die Anfahrflaeche zu, ins Teil hinein
    frei, warnungen = False, []
    if g["flaechen"]:
        f1, f2 = (_flaeche(m, i) for i in g["flaechen"])
        n1, n2 = bk._norm(f1["normale"]), bk._norm(f2["normale"])
        if bk._dot(n1, n2) > -math.cos(WINKEL_TOL):
            raise bk.BaukastenFehler("Die Greifflaechen liegen nicht parallel gegenueber.")
        x = n1
        breite = abs(bk._dot(bk._sub(f2["mitte"], f1["mitte"]), n1))
        mitte = bk._mul(bk._add(f1["mitte"], f2["mitte"]), 0.5)
        if abs(bk._dot(x, z)) > math.sin(WINKEL_TOL):
            raise bk.BaukastenFehler("Die Anfahrseite muss quer zu den Greifflaechen liegen.")
    else:
        b = next((b for b in m["bohrungen"] if str(b["id"]) == str(g["zylinder"])), None)
        if b is None or b.get("innen"):
            raise bk.BaukastenFehler("Die runde Flaeche %s ist keine Aussenflaeche (Zapfen) dieses Teils." % g["zylinder"])
        achse = bk._norm(bk._sub(b["enden"][1], b["enden"][0]))
        breite = b["durchmesser"]
        if abs(bk._dot(achse, z)) > math.cos(WINKEL_TOL):            # von der Stirnseite: Backen frei um die Achse
            frei, x = True, bk._x_in_ebene(z)
            mitte = b["enden"][0]
        elif abs(bk._dot(achse, z)) < math.sin(WINKEL_TOL):          # von der Seite: quer ueber den Durchmesser
            x = bk._norm(bk._kreuz(achse, z))
            s = bk._dot(bk._sub(fa["mitte"], b["enden"][0]), achse)
            mitte = bk._add(b["enden"][0], bk._mul(achse, s))
        else:
            raise bk.BaukastenFehler("Die Anfahrt muss entlang oder quer zur Achse der runden Flaeche liegen.")
    # Punkt auf der Anfahrflaeche ueber der Greifmitte, Tiefe des Teils dahinter (aus dem Huellquader)
    auf = bk._sub(mitte, bk._mul(na, bk._dot(bk._sub(mitte, fa["mitte"]), na)))
    box = m["box"]
    ecken = [[box[i] if (k >> i) & 1 == 0 else box[i + 3] for i in range(3)] for k in range(8)]
    teil_tief = max(bk._dot(bk._sub(e, auf), z) for e in ecken)
    if g["tiefe_mm"] is not None:
        tiefe = g["tiefe_mm"]
    elif g["zylinder"] is not None and not frei:   # von der Seite: TCP auf der Achse
        tiefe = bk._dot(bk._sub(mitte, auf), z)
    else:   # Backen moeglichst ganz am Teil, die Spitze 1 mm vor dessen Ende
        tiefe = max(0.0, min(-greifer["backe_z0_mm"], teil_tief - greifer["backe_z1_mm"] - 1.0))
    tcp = bk._add(auf, bk._mul(z, tiefe))
    if breite > greifer["offen_mm"] + 0.2:
        warnungen.append("%.1f mm breit, der Greifer oeffnet nur %.1f mm" % (breite, greifer["offen_mm"]))
    elif breite < greifer["zu_mm"] - 0.2:
        warnungen.append("%.1f mm breit, der Greifer schliesst nur bis %.1f mm" % (breite, greifer["zu_mm"]))
    if tiefe + greifer["backe_z1_mm"] > teil_tief + 0.5:
        warnungen.append("die Backen ragen %.1f mm ueber das Teil hinaus" % (tiefe + greifer["backe_z1_mm"] - teil_tief))
    return bk._rahmen(z, x, tcp), {"breite_mm": breite, "tiefe_mm": tiefe, "frei": frei}, warnungen


def griff_in_welt(teil_lage, griff_lage, frei=False, drehung_grad=0.0):
    """TCP-Lage in iRC-Basis. Frei drehbare Griffe (Zylinder von oben): Backen schliessen entlang der Linie
    Roboterfuss - Teil (so steht A6 bei 0), dann um drehung_grad um die Werkzeugachse gedreht."""
    lage = teil_lage.mal(griff_lage)
    if not frei:
        return lage
    z = [lage.r[i][2] for i in range(3)]
    radial = [lage.o[0], lage.o[1], 0.0]
    x = radial if bk._dot(radial, radial) > 1e-6 else [1.0, 0.0, 0.0]
    x = bk._sub(x, bk._mul(z, bk._dot(x, z)))
    if bk._dot(x, x) < 1e-6:
        x = bk._x_in_ebene(z)
    x = bk._norm(x)
    w = math.radians(drehung_grad)
    x = bk._add(bk._mul(x, math.cos(w)), bk._mul(bk._kreuz(z, x), math.sin(w)))
    return bk._rahmen(z, x, lage.o)


def vorposition(lage, vor_mm):
    z = [lage.r[i][2] for i in range(3)]
    return bk.Lage(lage.r, bk._sub(lage.o, bk._mul(z, vor_mm)))


# --- Erreichbarkeit: Rueckwaertsrechnung des ReBeL (exakt, Kinematik wie tools/rebel_kinematik.py) ---
# Die Achsen 4, 5 und 6 schneiden sich im Handgelenk (Achse 5): Handgelenk = TCP - (LX6 + TCP-Laenge) * Werkzeug-z.
# Daraus Achse 1 (Richtung), Achsen 2 und 3 (zwei Glieder in der senkrechten Ebene, Winkel ab der Senkrechten) und
# die Hand als Rz(q4) Ry(q5) Rz(q6). Bis zu acht Loesungen (Schulter vorne/hinten, Ellbogen, Hand gedreht).

def _normiere(w):
    return (w + 180.0) % 360.0 - 180.0


def loesungen(lage, tool_mm):
    """Alle Gelenkstellungen (Grad, ohne Achsgrenzen) fuer die TCP-Lage."""
    r = lage.r
    z = [r[i][2] for i in range(3)]
    w = bk._sub([v / 1000.0 for v in lage.o], bk._mul(z, kin.LX6 + tool_mm / 1000.0))
    l1, l2 = kin.LX2, kin.LX3 + kin.LX4
    h = w[2] - (kin.LZ0 + kin.LZ1)
    ergebnis = []
    for vorne in (True, False):
        q1 = math.atan2(w[1], w[0]) if vorne else math.atan2(-w[1], -w[0])
        rad = math.hypot(w[0], w[1]) * (1 if vorne else -1)
        c3 = (rad * rad + h * h - l1 * l1 - l2 * l2) / (2 * l1 * l2)
        if abs(c3) > 1.0 + 1e-9:
            continue
        for s3 in (1, -1):
            q3 = s3 * math.acos(max(-1.0, min(1.0, c3)))
            q2 = math.atan2(rad, h) - math.atan2(l2 * math.sin(q3), l1 + l2 * math.cos(q3))
            r3 = bk._mm(bk._rot_z(math.degrees(q1)), bk._rot_y(math.degrees(q2 + q3)))
            d = bk._mm(bk._t(r3), r)                       # = Rz(q4) Ry(q5) Rz(q6)
            for s5 in (1, -1):
                q5 = math.atan2(s5 * math.hypot(d[0][2], d[1][2]), d[2][2])
                if abs(math.sin(q5)) > 1e-9:
                    q4 = math.atan2(s5 * d[1][2], s5 * d[0][2])
                    q6 = math.atan2(s5 * d[2][1], -s5 * d[2][0])
                else:   # Hand gestreckt: q4 und q6 drehen um dieselbe Achse, q4 = 0
                    q4, q6 = 0.0, math.atan2(d[1][0], d[0][0]) * (1 if d[2][2] > 0 else -1)
                ergebnis.append([_normiere(math.degrees(v)) for v in (q1, q2, q3, q4, q5, q6)])
    return ergebnis


def rueckwaerts(lage, tool_mm, grenzen_grad, start=None):
    """Gelenkwinkel (Grad) fuer die TCP-Lage innerhalb der Achsgrenzen, sonst None. Unter mehreren Loesungen die
    naechste an `start` (z.B. dem vorigen Punkt), sonst die mit der kleinsten Auslenkung."""
    passend = []
    for q in loesungen(lage, tool_mm):
        if all(lo - 1e-6 <= v <= hi + 1e-6 for v, (lo, hi) in zip(q, grenzen_grad)):
            p, _r = kin.fk(q, tool_mm / 1000.0)
            if math.dist([v * 1000 for v in p], lage.o) < 0.01:   # Probe mit der Vorwaertsrechnung
                passend.append(q)
    if not passend:
        return None
    bezug = start or [0.0] * 6
    return min(passend, key=lambda q: sum((a - b) ** 2 for a, b in zip(q, bezug)))


# --- Alle Punkte einer Roboterumgebung ---

def _punkt(name, titel, lage, warnungen=(), magazin=None, werkstueck=None):
    return {"name": name, "titel": titel, "lage": lage, "gelenke": None, "warnungen": list(warnungen), "magazin": magazin,
            "werkstueck": werkstueck}


def _paar(liste, name, titel, lage, vor_mm, warnungen=(), magazin=None, werkstueck=None):
    """Vorposition (entlang der Werkzeugachse zurueck) und Punkt anhaengen. werkstueck: das Teil zwischen den Backen."""
    liste.append(_punkt(name + "_vor", titel + ", Vorposition", vorposition(lage, vor_mm), warnungen, magazin, werkstueck))
    liste.append(_punkt(name, titel, lage, warnungen, magazin, werkstueck))


def gedreht180(lage):
    """Dieselbe Lage um 180 Grad um die Werkzeugachse gedreht: fuer zwei gleiche Backen derselbe Griff."""
    return bk.Lage([[-lage.r[i][0], -lage.r[i][1], lage.r[i][2]] for i in range(3)], lage.o)


def _drehung_waehlen(lage, tool, grenzen, start):
    """True, wenn der um 180 Grad gedrehte Griff besser erreichbar ist (erreichbar bzw. kleineres A6)."""
    q, q_gedreht = rueckwaerts(lage, tool, grenzen, start), rueckwaerts(gedreht180(lage), tool, grenzen, start)
    if q_gedreht is None:
        return False
    return q is None or abs(q_gedreht[5]) < abs(q[5]) - 1e-6


def matrix_lage(a, b, c, i, j, spalten, zeilen):
    """Lage der Tasche (Spalte i, Zeile j, ab 0) wie der Matrix-Befehl von iRC: von A nach B in Spalten, dann in
    Richtung C versetzt; Orientierung wie A."""
    fx = i / (spalten - 1) if spalten > 1 else 0.0
    fy = j / (zeilen - 1) if zeilen > 1 else 0.0
    o = list(a.o)
    if b is not None:
        o = bk._add(o, bk._mul(bk._sub(b.o, a.o), fx))
    if c is not None:
        o = bk._add(o, bk._mul(bk._sub(c.o, a.o), fy))
    return bk.Lage(a.r, o)


# --- Kollision am Punkt: Greifer und Backen (offen) gegen Tisch, Aufbauten und andere Werkstuecke ---
# Wie im Controller (controllers/irc_bridge/kollision.py): Huellquader des Greifers aus protos/meshes/huellen.json
# (im Flanschsystem, Backen offen), die Umgebung als Punktwolken der Teile-Netze; Masse dort in m.

def lies_obj(pfad):
    """Punkte und Dreiecke einer OBJ-Datei (wie netz.obj der Teilebibliothek, mm)."""
    punkte_obj, dreiecke = [], []
    with open(pfad, encoding="utf-8", errors="replace") as f:
        for zeile in f:
            teile = zeile.split()
            if not teile:
                continue
            if teile[0] == "v":
                punkte_obj.append([float(v) for v in teile[1:4]])
            elif teile[0] == "f":
                ecken = [int(v.split("/")[0]) - 1 for v in teile[1:]]
                dreiecke += [(ecken[0], ecken[k], ecken[k + 1]) for k in range(1, len(ecken) - 1)]
    return punkte_obj, dreiecke


def _kollision_modul():
    import os
    import sys
    pfad = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "controllers", "irc_bridge")
    if pfad not in sys.path:
        sys.path.insert(0, pfad)
    import kollision
    return kollision


def _lage_m(lage):
    return [v / 1000.0 for v in lage.o], [lage.r[i][k] for i in range(3) for k in range(3)]


def pruefe_kollisionen(ergebnis, entwurf, lagen, netz_von, huelle, tcp_mm):
    """Warnungen an den Punkten (und Magazintaschen), an denen Greifer oder Backen etwas beruehren. huelle:
    {"grund": {mitte, halb}, "links": ..., "rechts": ...} in m im Flanschsystem; netz_von(teil) -> (Punkte, Dreiecke) mm."""
    kollision = _kollision_modul()
    wolken = {}

    def wolke(name):
        if name not in wolken:
            pkt, dr = netz_von(name)
            wolken[name] = kollision.Punktwolke(kollision.abtasten([[v / 1000.0 for v in p] for p in pkt], dr))
        return wolken[name]
    umgebung = []
    for t in entwurf["teile"]:
        if t.get("art", "aufbau") in ("aufbau", "werkstueck") and t.get("teil"):
            try:
                umgebung.append(kollision.Umgebungsteil(t["id"], t.get("art", "aufbau"), wolke(t["teil"]),
                                                        _lage_m(lagen[t["id"]]), t["id"]))
            except (OSError, ValueError, KeyError):
                pass   # ohne Netz keine Pruefung fuer dieses Teil
    tisch_z = bk.tisch_werte(entwurf)["hoehe_mm"] / 1000.0
    def treffer(lage, ohne):
        z = [lage.r[i][2] for i in range(3)]
        flansch = _lage_m(bk.Lage(lage.r, bk._sub(lage.o, bk._mul(z, tcp_mm))))
        koerper = [kollision.Quader(name, flansch, h["mitte"], h["halb"]) for name, h in huelle.items()]
        andere = [u for u in umgebung if u.kennung != ohne]
        return sorted({t["b"] for t in kollision.pruefe(koerper, andere, tisch_z, 0.0, 0.0, hoechstens=3)})
    for p in ergebnis["punkte"]:
        ohne = p.get("werkstueck")
        b = treffer(p["lage"], ohne)
        if b:
            p["warnungen"].append("Greifer stoesst an: %s" % ", ".join(b))
    for m in ergebnis["magazine"]:
        for i, j, lage in m.get("taschen", []):
            for was, l in (("", lage), (" (Vorposition)", vorposition(lage, m["vor_mm"]))):
                b = treffer(l, m["von"])
                if b:
                    m["warnungen"].append("Tasche %d/%d%s: Greifer stoesst an %s" % (i + 1, j + 1, was, ", ".join(b)))
    return ergebnis


def berechne(entwurf, merkmale_von, greifer, netz_von=None, huelle=None):
    """Greif-, Ablage- und Magazinpunkte mit Vorpositionen in iRC-Basis, mit Erreichbarkeit (Gelenkwinkel) und
    Warnungen. greifer: Kenndaten (greifer_daten); mit netz_von und huelle auch die Kollisionspruefung am Punkt.
    Rueckgabe: {"punkte", "magazine", "meldungen"}."""
    if not greifer or not greifer.get("offen_mm"):
        raise bk.BaukastenFehler("Greifpunkte gibt es nur fuer Backengreifer (dieser Greifer hat keine Backen).")
    bk.pruefe_punkte(entwurf)
    lagen = bk.loese(entwurf, merkmale_von)
    teile = {t["id"]: t for t in entwurf["teile"]}
    magazine = [bk.magazin_werte(m, teile) for m in entwurf.get("magazine") or []]
    im_magazin = {m[k] for m in magazine for k in "abc" if m[k]}
    punkte, meldungen, griffe = [], [], {}
    for t in entwurf["teile"]:
        g = bk.griff_werte(t) if t.get("art") == "werkstueck" else None
        if not g:
            continue
        try:
            griff, info, warnungen = griff_im_teil(merkmale_von(t["teil"]), g, greifer)
        except bk.BaukastenFehler as e:
            meldungen.append("%s: %s" % (t["id"], e))
            continue
        griffe[t["id"]] = (griff, info, g)
        _paar(punkte, g["name"] + "_greifen", "%s greifen" % t["id"],
              griff_in_welt(lagen[t["id"]], griff, info["frei"], g["drehung_grad"]), g["vor_mm"], warnungen,
              werkstueck=t["id"])
    ablagen = {}
    for t in entwurf["teile"]:
        if t.get("art") != "ablage":
            continue
        a = bk.ablage_werte(t, teile)
        if a["von"] not in griffe:
            meldungen.append("%s: das Werkstueck %s hat keinen Greifpunkt." % (t["id"], a["von"]))
            continue
        griff, info, g = griffe[a["von"]]
        lage = griff_in_welt(lagen[t["id"]], griff, info["frei"], g["drehung_grad"])
        vor = a["vor_mm"] if a["vor_mm"] is not None else g["vor_mm"]
        ablagen[t["id"]] = (lage, vor, a["von"])
        if t["id"] not in im_magazin:
            _paar(punkte, a["name"] + "_ablegen", "%s ablegen (%s)" % (a["von"], t["id"]), lage, vor, werkstueck=a["von"])
    mags = []
    for m in magazine:
        if any(m[k] and m[k] not in ablagen for k in "abc"):
            meldungen.append("Magazin %s: eine der Ablagen A/B/C hat keinen Greifpunkt." % m["name"])
            continue
        a, b, c = (ablagen[m[k]][0] if m[k] else None for k in "abc")
        vor = ablagen[m["a"]][1]
        for k, lage in (("A", a), ("B", b), ("C", c)):
            if lage is not None:
                _paar(punkte, "%s_%s" % (m["name"], k), "Magazin %s, Position %s" % (m["name"], k), lage, vor,
                      magazin=m["name"], werkstueck=ablagen[m["a"]][2])
        mags.append(dict(m, vor_mm=vor, von=ablagen[m["a"]][2], warnungen=[]))
    # Zwei gleiche Backen: je Punkt die gleichwertige, um 180 Grad gedrehte Lage nehmen, wenn A6 dann weniger dreht
    # (bzw. nur so erreichbar ist); Vorposition wie ihr Punkt, ein Magazin einheitlich wie seine Position A
    grenzen = bk.grenzen_werte(entwurf)["achsen"] or bk.IRC_ACHSGRENZEN   # None = Grenzen von iRC
    tool, letzte, magazin_gedreht = greifer["tcp_mm"], None, {}
    for vor_p, p in zip(punkte[0::2], punkte[1::2]):
        if p["magazin"] and p["magazin"] in magazin_gedreht:
            drehen = magazin_gedreht[p["magazin"]]
        else:
            drehen = _drehung_waehlen(p["lage"], tool, grenzen, letzte)
            if p["magazin"]:
                magazin_gedreht[p["magazin"]] = drehen
        for q in (vor_p, p):
            if drehen:
                q["lage"] = gedreht180(q["lage"])
            q["gelenke"] = rueckwaerts(q["lage"], tool, grenzen, letzte)
            if q["gelenke"] is None:
                q["warnungen"].append("nicht erreichbar (Reichweite oder Achsgrenzen)")
            else:
                letzte = q["gelenke"]
    # Magazine: jede Tasche mit Vorposition, wie der Matrix-Befehl sie aus A, B und C rechnet
    lagen_nach_name = {p["name"]: p["lage"] for p in punkte}
    for m in mags:
        a, b, c = (lagen_nach_name.get("%s_%s" % (m["name"], k)) for k in "ABC")
        m["taschen"] = [(i, j, matrix_lage(a, b, c, i, j, m["spalten"], m["zeilen"]))
                        for j in range(m["zeilen"]) for i in range(m["spalten"])]
        for i, j, lage in m["taschen"]:
            for was, l in (("", lage), (" (Vorposition)", vorposition(lage, m["vor_mm"]))):
                if rueckwaerts(l, tool, grenzen) is None:
                    m["warnungen"].append("Tasche %d/%d%s nicht erreichbar" % (i + 1, j + 1, was))
    ergebnis = {"punkte": punkte, "magazine": mags, "meldungen": meldungen}
    if netz_von is not None and huelle:
        pruefe_kollisionen(ergebnis, entwurf, lagen, netz_von, huelle, tool)
    return ergebnis


def bericht(ergebnis):
    """Pruefbericht als JSON fuer die Lehrerseite."""
    def lage(l):
        return {"mm": [round(v, 2) for v in l.o], "abc": [round(v, 2) for v in abc_aus_matrix(l.r)]}
    anzahl = (sum(len(p["warnungen"]) for p in ergebnis["punkte"]) + sum(len(m["warnungen"]) for m in ergebnis["magazine"])
              + len(ergebnis["meldungen"]))
    return {"punkte": [dict(lage(p["lage"]), name=p["name"], titel=p["titel"], warnungen=p["warnungen"],
                            gelenke=[round(v, 2) for v in p["gelenke"]] if p["gelenke"] else None)
                       for p in ergebnis["punkte"]],
            "magazine": [{"name": m["name"], "spalten": m["spalten"], "zeilen": m["zeilen"], "warnungen": m["warnungen"]}
                         for m in ergebnis["magazine"]],
            "meldungen": ergebnis["meldungen"], "warnungen": anzahl}


# --- iRC-Programme (XML wie die Beispielprogramme von igus) ---

def _attr(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
            .replace("\n", " "))


def _kommentar(text):
    return "<!-- %s -->" % str(text).replace("--", "-")


def _kopfzeilen(kopf):
    return ['<?xml version="1.0" encoding="utf-8"?>'] + [_kommentar(k) for k in kopf] + ["<Program>", "    <Header />"]


def punkte_xml(ergebnis, kopf):
    """Unterprogramm mit allen Punkten als Positionsvariablen (Store). kopf: Kommentarzeilen."""
    zeilen = _kopfzeilen(kopf)
    for nr, p in enumerate(ergebnis["punkte"], 1):
        werte = list(p["lage"].o) + abc_aus_matrix(p["lage"].r)
        gelenke = (p["gelenke"] or [0.0] * 6) + [0.0, 0.0, 0.0]
        descr = p["titel"] + ("; ACHTUNG: " + "; ".join(p["warnungen"]) if p["warnungen"] else "")
        zeilen.append('    <Store Nr="%d" Type="position" Value="%s" ValueJoints="%s" Name="%s" Descr="%s" />' % (
            nr, " ".join("%.2f" % v for v in werte), " ".join("%.3f" % v for v in gelenke), p["name"], _attr(descr)))
    for m in ergebnis["magazine"]:
        if m["warnungen"]:
            zeilen.append("    " + _kommentar("Magazin %s: %s" % (m["name"], "; ".join(m["warnungen"]))))
    return "\n".join(zeilen + ["</Program>"]) + "\n"


class _Programm:
    """Befehlsliste mit fortlaufender Nummer."""

    def __init__(self):
        self.zeilen, self.nr = [], 0

    def befehl(self, text):
        self.nr += 1
        self.zeilen.append("    " + text.replace("%NR%", str(self.nr)))

    def kommentar(self, text):
        self.zeilen.append("    " + _kommentar(text))

    def joint(self, variable):
        self.befehl('<Joint AbortCondition="False" Nr="%%NR%%" Source="Variable" velPercent="40" acc="40" smooth="0" '
                    'VariableName="%s" Descr="" />' % variable)

    def linear(self, variable, vel=50):
        self.befehl('<Linear AbortCondition="False" Nr="%%NR%%" Source="Variable" vel="%d" acc="40" smooth="0" '
                    'VariableName="%s" Descr="" />' % (vel, variable))

    def relativ(self, z_mm):
        self.befehl('<Relative AbortCondition="False" Nr="%%NR%%" acc="40" smooth="0" MoType="CartTool" vel="30" '
                    'x="0" y="0" z="%.1f" Descr="" />' % z_mm)

    def greifer(self, zu, dout_auf, dout_zu):
        self.befehl('<Output Nr="%%NR%%" Channel="DOut%d" State="%s" Descr="Greifer %s" />'
                    % (dout_auf, "False" if zu else "True", "zu" if zu else "auf"))
        self.befehl('<Output Nr="%%NR%%" Channel="DOut%d" State="%s" Descr="" />' % (dout_zu, "True" if zu else "False"))
        self.befehl('<Wait Nr="%NR%" Type="Time" Seconds="0.5" Descr="" />')


def beispiel_xml(ergebnis, punkte_datei, kopf, dout_auf=31, dout_zu=32):
    """Beispiel-Hauptprogramm: Punkte laden, jedes Werkstueck greifen und ablegen (eigene Ablage, sonst erste Tasche
    seines Magazins, sonst zuruecklegen), danach jedes Magazin einmal Tasche fuer Tasche anfahren."""
    namen = {p["name"] for p in ergebnis["punkte"]}
    prg = _Programm()
    prg.befehl('<Sub Nr="%%NR%%" File="%s" Descr="Punkte laden" />' % _attr(punkte_datei))
    prg.greifer(False, dout_auf, dout_zu)
    for p in ergebnis["punkte"]:
        if not p["name"].endswith("_greifen"):
            continue
        basis, werkstueck = p["name"][:-len("_greifen")], p["titel"][:-len(" greifen")]
        prg.kommentar(p["titel"])
        prg.joint(basis + "_greifen_vor")
        prg.linear(basis + "_greifen")
        prg.greifer(True, dout_auf, dout_zu)
        prg.linear(basis + "_greifen_vor")
        ziel = next((q["name"][:-len("_ablegen")] for q in ergebnis["punkte"]
                     if q["name"].endswith("_ablegen") and q["titel"].startswith(werkstueck + " ablegen")), None)
        mag = next((m for m in ergebnis["magazine"] if m["von"] == werkstueck), None)
        if ziel:
            ziel_vor, ziel_punkt = ziel + "_ablegen_vor", ziel + "_ablegen"
        elif mag:
            ziel_vor, ziel_punkt = mag["name"] + "_A_vor", mag["name"] + "_A"
        else:
            ziel_vor, ziel_punkt = basis + "_greifen_vor", basis + "_greifen"
        prg.kommentar("ablegen" if ziel or mag else "kein Ablageplatz: zuruecklegen")
        prg.joint(ziel_vor)
        prg.linear(ziel_punkt)
        prg.greifer(False, dout_auf, dout_zu)
        prg.linear(ziel_vor)
    for m in ergebnis["magazine"]:
        a = m["name"] + "_A_vor"
        b = m["name"] + "_B_vor" if m["name"] + "_B_vor" in namen else a
        c = m["name"] + "_C_vor" if m["name"] + "_C_vor" in namen else a
        prg.kommentar("Magazin %s: jede Tasche anfahren (%d Spalten A nach B, %d Zeilen A nach C)"
                      % (m["name"], m["spalten"], m["zeilen"]))
        prg.befehl('<Matrix Nr="%%NR%%" CntX="%d" CntY="%d" VarPointA="%s" VarPointB="%s" VarPointC="%s" '
                   'VarTargetPos="%s_pos" VarCntX="%s_x" VarCntY="%s_y" Descr="" />'
                   % (m["spalten"], m["zeilen"], a, b, c, m["name"], m["name"], m["name"]))
        prg.linear(m["name"] + "_pos")
        prg.relativ(m["vor_mm"])
        prg.relativ(-m["vor_mm"])
        prg.befehl('<MatrixEnd Nr="%NR%" Descr="" />')
    return "\n".join(_kopfzeilen(kopf) + prg.zeilen + ["</Program>"]) + "\n"


INSTALLIEREN_CMD = "\r\n".join([
    "@echo off",
    "rem Programme der ReBeL-Simulation fuer igus Robot Control V15 installieren",
    "set ZIEL=%APPDATA%\\RobotControl\\V15\\Data\\Programs",
    "if not exist \"%ZIEL%\" (",
    "  echo iRC-Programmordner nicht gefunden: %ZIEL%",
    "  echo iRC V15 einmal starten und dieses Skript dann erneut ausfuehren.",
    "  pause",
    "  exit /b 1",
    ")",
    "copy /Y \"%~dp0*.xml\" \"%ZIEL%\" >nul",
    "if errorlevel 1 (echo Kopieren ging nicht. & pause & exit /b 1)",
    "echo Fertig: Programme nach %ZIEL% kopiert. In iRC das Beispiel oder ein eigenes Programm laden.",
    "pause",
    ""])


def paket(ergebnis, welt, titel, greifer_titel, greifer, datum):
    """ZIP fuer den Schueler-PC: Unterprogramm, Beispielprogramm, installieren.cmd, LIESMICH.txt.
    Rueckgabe (dateiname, bytes)."""
    import io
    import zipfile
    punkte_datei, beispiel_datei = "%s_punkte.xml" % welt, "%s_beispiel.xml" % welt
    kopf = ["ReBeL-Simulation, Roboterumgebung %s (%s), erzeugt %s" % (titel, welt, datum),
            "Werkzeug in iRC: %s, TCP %.1f mm ab Flansch - dieses Werkzeug in iRC waehlen" % (greifer_titel, greifer["tcp_mm"]),
            "Positionen: Basis des Roboters in mm, A/B/C in Grad (Drehung um X/Y/Z)"]
    hinweise = [p["titel"] + ": " + "; ".join(p["warnungen"]) for p in ergebnis["punkte"] if p["warnungen"]]
    hinweise += ["Magazin %s: %s" % (m["name"], "; ".join(m["warnungen"])) for m in ergebnis["magazine"] if m["warnungen"]]
    hinweise += ergebnis["meldungen"]
    zeilen = ["Punkte der Roboterumgebung %s fuer igus Robot Control V15" % titel, "",
              "%s  Unterprogramm: legt alle Punkte als Positionsvariablen an." % punkte_datei,
              "%s  Beispiel: ruft das Unterprogramm auf, greift und legt ab." % beispiel_datei,
              "installieren.cmd  kopiert beide in den Programmordner von iRC (dort liegen alle Programme).", "",
              "In iRC das Werkzeug \"%s\" (TCP %.1f mm) waehlen, sonst stimmen die Punkte nicht." % (greifer_titel, greifer["tcp_mm"]),
              "Eigene Programme: am Anfang das Unterprogramm %s aufrufen, danach die Variablen nutzen," % punkte_datei,
              "z.B. Gelenkbewegung zu <Name>_vor, linear zu <Name>, Greifer, linear zurueck zu <Name>_vor.", "", "Variablen:"]
    zeilen += ["  %-32s %s" % (p["name"], p["titel"]) for p in ergebnis["punkte"]]
    for m in ergebnis["magazine"]:
        zeilen += ["", "Magazin %s: %d Spalten (A nach B) x %d Zeilen (A nach C); Matrix mit %s_A_vor, %s_B_vor, %s_C_vor,"
                   % (m["name"], m["spalten"], m["zeilen"], m["name"], m["name"], m["name"]),
                   "  dann relativ entlang der Werkzeugachse %.0f mm hinein und wieder heraus (siehe Beispiel)." % m["vor_mm"]]
    if hinweise:
        zeilen += ["", "Hinweise der Pruefung:"] + ["  " + h for h in hinweise]
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(punkte_datei, punkte_xml(ergebnis, kopf))
        z.writestr(beispiel_datei, beispiel_xml(ergebnis, punkte_datei, kopf))
        z.writestr("installieren.cmd", INSTALLIEREN_CMD)
        z.writestr("LIESMICH.txt", "\r\n".join(zeilen + [""]))
    return "iRC_Punkte_%s.zip" % welt, puffer.getvalue()
