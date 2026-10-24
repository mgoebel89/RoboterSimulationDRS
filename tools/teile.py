"""Teilebibliothek fuer den Weltbaukasten: STEP einlesen, Netz und Merkmale erzeugen.

Laeuft im Container mit gmsh/OpenCASCADE (apt install python3-gmsh).

  python3 tools/teile.py einlesen magazin.step magazin           # -> teile/magazin/
  python3 tools/teile.py einlesen egp25.stp egp25 --koerper 1,3,8   # nur diese Koerper der Baugruppe
  python3 tools/teile.py koerper egp25.stp                       # Koerper einer Baugruppe auflisten

Je Teil entsteht teile/<name>/ mit
  quelle.step   die hochgeladene Datei
  netz.obj      Dreiecksnetz in mm (jede CAD-Flaeche mit eigenen Eckpunkten: scharfe Kanten)
  netz.json     dasselbe je CAD-Flaeche getrennt, zum Anklicken im Browser
  teil.json     Masse und Merkmale fuer Verbindungspunkte (siehe baukasten.py):
                ebene Flaechen (Mitte, Normale nach aussen), Bohrungen und Zapfen (Achse, Radius,
                beide Enden), gerade Kanten (mit angrenzenden ebenen Flaechen), Ecken
"""

import argparse
import json
import math
import os
import re
import shutil
import sys

NAME = re.compile(r"[a-z0-9_-]{1,60}")


class TeilFehler(Exception):
    pass


def _sub(a, b):
    return [a[i] - b[i] for i in range(3)]


def _dot(a, b):
    return sum(a[i] * b[i] for i in range(3))


def _kreuz(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def _laenge(a):
    return math.sqrt(_dot(a, a))


def _norm(a):
    laenge = _laenge(a) or 1.0
    return [v / laenge for v in a]


def _r(werte, stellen=4):
    return [round(v, stellen) for v in werte]


def _kreis(punkte2d):
    """Kreis durch Punkte (Kasa-Verfahren): Mitte (x, y), Radius, groesste Abweichung."""
    sxx = sxy = syy = sx = sy = sxz = syz = sz = 0.0
    n = len(punkte2d)
    for x, y in punkte2d:
        z = x * x + y * y
        sxx += x * x; sxy += x * y; syy += y * y; sx += x; sy += y
        sxz += x * z; syz += y * z; sz += z
    m = [[sxx, sxy, sx], [sxy, syy, sy], [sx, sy, n]]
    rechts = [-sxz, -syz, -sz]
    det = (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1]) - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
           + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))
    if abs(det) < 1e-12:
        return None
    loes = []
    for spalte in range(3):
        mk = [row[:] for row in m]
        for r in range(3):
            mk[r][spalte] = rechts[r]
        loes.append((mk[0][0] * (mk[1][1] * mk[2][2] - mk[1][2] * mk[2][1])
                     - mk[0][1] * (mk[1][0] * mk[2][2] - mk[1][2] * mk[2][0])
                     + mk[0][2] * (mk[1][0] * mk[2][1] - mk[1][1] * mk[2][0])) / det)
    d, e, f = loes
    cx, cy = -d / 2, -e / 2
    radius = math.sqrt(max(0.0, cx * cx + cy * cy - f))
    abw = max(abs(math.hypot(x - cx, y - cy) - radius) for x, y in punkte2d)
    return cx, cy, radius, abw


def _laden(gmsh, step, koerper=None):
    gmsh.option.setNumber("General.Terminal", 0)
    gmsh.option.setString("Geometry.OCCTargetUnit", "MM")
    gmsh.model.add("teil")
    gmsh.model.occ.importShapes(step)
    gmsh.model.occ.synchronize()
    if koerper:
        weg = [(3, t) for _d, t in gmsh.model.getEntities(3) if t not in koerper]
        if weg:
            gmsh.model.occ.remove(weg, recursive=True)
            gmsh.model.occ.synchronize()
        if not gmsh.model.getEntities(3):
            raise TeilFehler("Keiner der gewaehlten Koerper ist in der Datei.")


def koerper_liste(step):
    import gmsh
    gmsh.initialize()
    try:
        _laden(gmsh, step)
        liste = []
        for _d, t in gmsh.model.getEntities(3):
            bb = gmsh.model.getBoundingBox(3, t)
            liste.append({"koerper": t, "name": gmsh.model.getEntityName(3, t).split("/")[-1],
                          "box": _r(bb, 2), "volumen_mm3": round(gmsh.model.occ.getMass(3, t), 1)})
        return liste
    finally:
        gmsh.finalize()


def _flaechen_netz(gmsh):
    """Dreiecke je CAD-Flaeche: {tag: (punkte, normalen, dreiecke)} mit eigenen Eckpunkten."""
    tags, koord, _ = gmsh.model.mesh.getNodes()
    lage = {int(t): (koord[3 * i], koord[3 * i + 1], koord[3 * i + 2]) for i, t in enumerate(tags)}
    netz = {}
    for _dim, tag in gmsh.model.getEntities(2):
        typen, _el, knoten = gmsh.model.mesh.getElements(2, tag)
        dreiecke = []
        for typ, kn in zip(typen, knoten):
            if typ == 2:
                dreiecke += [tuple(int(k) for k in kn[i:i + 3]) for i in range(0, len(kn), 3)]
        if not dreiecke:
            continue
        index, punkte, summe = {}, [], []
        for d in dreiecke:
            a, b, c = (lage[k] for k in d)
            n = _kreuz(_sub(b, a), _sub(c, a))
            for k in d:
                if k not in index:
                    index[k] = len(punkte)
                    punkte.append(lage[k])
                    summe.append([0.0, 0.0, 0.0])
                s = summe[index[k]]
                for j in range(3):
                    s[j] += n[j]
        netz[tag] = (punkte, [_norm(s) for s in summe], [tuple(index[k] for k in d) for d in dreiecke])
    return netz


def _ebene(gmsh, tag):
    p = gmsh.model.getParametrizationBounds(2, tag)
    n = gmsh.model.getNormal(tag, [(p[0][0] + p[1][0]) / 2, (p[0][1] + p[1][1]) / 2])
    return {"id": tag, "normale": _r(_norm(list(n)), 5), "mitte": _r(gmsh.model.occ.getCenterOfMass(2, tag)),
            "flaeche_mm2": round(gmsh.model.occ.getMass(2, tag), 2)}


def _zylinder_probe(gmsh, tag, punkte):
    """Achse, Achspunkt, Radius, innen? und Winkelbereich einer Flaeche, falls sie zylindrisch ist."""
    p0, p1 = gmsh.model.getParametrizationBounds(2, tag)
    params = []
    for i in range(6):
        for j in range(6):
            params += [p0[0] + (p1[0] - p0[0]) * (i + 0.5) / 6, p0[1] + (p1[1] - p0[1]) * (j + 0.5) / 6]
    n = gmsh.model.getNormal(tag, params)
    normalen = [_norm([n[3 * k], n[3 * k + 1], n[3 * k + 2]]) for k in range(len(params) // 2)]
    achse, beste = None, 0.0
    for a in normalen[::3]:
        for b in normalen:
            c = _kreuz(a, b)
            if _laenge(c) > beste:
                achse, beste = c, _laenge(c)
    if achse is None or beste < 0.1:
        return None
    achse = _norm(achse)
    if max(abs(_dot(nn, achse)) for nn in normalen) > 0.05:
        return None   # Normalen nicht senkrecht zur Achse: kein Zylinder
    hilf = [1.0, 0.0, 0.0] if abs(achse[0]) < 0.9 else [0.0, 1.0, 0.0]
    u = _norm(_kreuz(achse, hilf))
    v = _kreuz(achse, u)
    kreis = _kreis([(_dot(q, u), _dot(q, v)) for q in punkte])
    if kreis is None:
        return None
    cx, cy, radius, abw = kreis
    if radius <= 0 or abw > 0.02 * radius + 0.02:
        return None
    mitte = [u[k] * cx + v[k] * cy for k in range(3)]   # Achspunkt bei Achskoordinate 0
    # Punkte auf den Randkanten dazu: kurze Zylinder haben sonst zu wenige Netzpunkte,
    # um zu erkennen, ob sie rundherum gehen
    punkte = list(punkte)
    for _dim, kante in gmsh.model.getBoundary([(2, tag)], oriented=False):
        k0, k1 = gmsh.model.getParametrizationBounds(1, abs(kante))
        ts = [k0[0] + (k1[0] - k0[0]) * i / 23 for i in range(24)]
        werte = gmsh.model.getValue(1, abs(kante), ts)
        punkte += [werte[3 * i:3 * i + 3] for i in range(len(ts))]
    werte = gmsh.model.getValue(2, tag, params[:2])
    probe = list(werte[:3])
    zur_achse = _sub(mitte, _sub(probe, [achse[k] * _dot(probe, achse) for k in range(3)]))
    innen = _dot(normalen[0], zur_achse) > 0   # Normale zeigt zur Achse: Bohrung
    winkel = {int((math.degrees(math.atan2(_dot(q, v) - cy, _dot(q, u) - cx)) + 360) % 360 // 10) for q in punkte}
    t = [_dot(q, achse) for q in punkte]
    return {"achse": achse, "mitte": mitte, "radius": radius, "innen": innen, "winkel": winkel,
            "t": (min(t), max(t)), "flaechen": [tag]}


def _gleiche_achse(a, b):
    if abs(abs(_dot(a["achse"], b["achse"])) - 1) > 1e-4 or abs(a["radius"] - b["radius"]) > 0.01:
        return False
    d = _sub(a["mitte"], b["mitte"])
    seitlich = _sub(d, [a["achse"][k] * _dot(d, a["achse"]) for k in range(3)])
    return _laenge(seitlich) < 0.01


def merkmale(gmsh, netz):
    """Ebene Flaechen, Bohrungen/Zapfen, gerade Kanten und Ecken des geladenen Modells."""
    flaechen, gruppen = [], []
    ebene_ids = set()
    for _d, tag in gmsh.model.getEntities(2):
        art = gmsh.model.getType(2, tag)
        if art == "Plane":
            flaechen.append(_ebene(gmsh, tag))
            ebene_ids.add(tag)
        elif tag in netz:
            z = _zylinder_probe(gmsh, tag, netz[tag][0])
            if z is None:
                continue
            for g in gruppen:   # OCC teilt volle Zylinder oft in zwei Haelften
                if _gleiche_achse(g, z) and g["innen"] == z["innen"]:
                    t = z["t"] if _dot(g["achse"], z["achse"]) > 0 else (-z["t"][1], -z["t"][0])
                    if t[0] > g["t"][1] + 0.01 or g["t"][0] > t[1] + 0.01:
                        continue   # gleiche Achse, aber getrennt (z.B. Gewinde beider Backentraeger)
                    z["t"] = t
                    g["winkel"] |= z["winkel"]
                    g["t"] = (min(g["t"][0], z["t"][0]), max(g["t"][1], z["t"][1]))
                    g["flaechen"].append(tag)
                    break
            else:
                gruppen.append(z)
    bohrungen = []
    for g in gruppen:
        if len(g["winkel"]) < 33:   # nur (fast) volle Zylinder, keine Rundungen
            continue
        enden = [[g["mitte"][k] + g["achse"][k] * t for k in range(3)] for t in g["t"]]
        bohrungen.append({"id": min(g["flaechen"]), "flaechen": sorted(g["flaechen"]), "innen": g["innen"],
                          "radius": round(g["radius"], 4), "durchmesser": round(2 * g["radius"], 3),
                          "achse": _r(g["achse"], 5), "enden": [_r(e) for e in enden],
                          "laenge": round(g["t"][1] - g["t"][0], 3)})

    def punkt(vtag):
        return list(gmsh.model.getValue(0, vtag, []))

    kanten, kanten_von_punkt = [], {}
    for _d, tag in gmsh.model.getEntities(1):
        if gmsh.model.getType(1, tag) != "Line":
            continue
        ecken = [abs(t) for _dim, t in gmsh.model.getBoundary([(1, tag)], oriented=False)]
        if len(ecken) != 2:
            continue
        nachbarn = [f for f in gmsh.model.getAdjacencies(1, tag)[0] if f in ebene_ids]
        if not nachbarn:
            continue
        a, b = punkt(ecken[0]), punkt(ecken[1])
        if _laenge(_sub(b, a)) < 0.05:
            continue
        kanten.append({"id": tag, "a": _r(a), "b": _r(b), "flaechen": nachbarn, "laenge": round(_laenge(_sub(b, a)), 3)})
        for e, anderes in ((ecken[0], b), (ecken[1], a)):
            kanten_von_punkt.setdefault(e, []).append((tag, anderes, set(nachbarn)))

    ecken_liste = []
    for vtag, liste in kanten_von_punkt.items():
        if len(liste) < 2:
            continue
        p = punkt(vtag)
        seiten = []
        for f in sorted({f for _k, _o, fs in liste for f in fs}):
            in_f = [(k, o) for k, o, fs in liste if f in fs]
            if len(in_f) >= 2:
                seiten.append({"flaeche": f, "kanten": [in_f[0][0], in_f[1][0]],
                               "x": _r(_norm(_sub(in_f[0][1], p)), 5), "y": _r(_norm(_sub(in_f[1][1], p)), 5)})
        if seiten:
            ecken_liste.append({"id": vtag, "punkt": _r(p), "flaechen": seiten})
    return flaechen, bohrungen, kanten, ecken_liste


def einlesen(step, teile_dir, name, koerper=None, feinheit=None):
    """STEP -> teile/<name>/ (quelle.step, netz.obj, netz.json, teil.json). Gibt teil.json zurueck."""
    import gmsh
    if not NAME.fullmatch(name or ""):
        raise TeilFehler("Teilename: 1 bis 60 Zeichen, nur a-z, 0-9, - und _.")
    with open(step, "rb") as f:
        if not f.read(64).lstrip().startswith(b"ISO-10303-21"):
            raise TeilFehler("Das ist keine STEP-Datei.")
    ordner = os.path.join(teile_dir, name)
    tmp = ordner + ".neu"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    gmsh.initialize()
    try:
        _laden(gmsh, step, koerper)
        x0, y0, z0, x1, y1, z1 = gmsh.model.getBoundingBox(-1, -1)
        diagonale = math.dist((x0, y0, z0), (x1, y1, z1))
        gmsh.option.setNumber("Mesh.MeshSizeMax", feinheit or max(1.0, diagonale / 30))
        # Untergrenze: Gewinde und andere Schraubenflaechen wuerden sonst in winzige
        # Dreiecke zerfallen (Schunk-Modell: ueber 4 GB Speicher)
        gmsh.option.setNumber("Mesh.MeshSizeMin", max(0.2, diagonale / 300))
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 12)
        gmsh.option.setNumber("Mesh.Algorithm", 6)
        gmsh.model.mesh.generate(2)
        netz = _flaechen_netz(gmsh)
        flaechen, bohrungen, kanten, ecken = merkmale(gmsh, netz)
        alle = [p for punkte, _n, _d in netz.values() for p in punkte]
        box = [min(p[i] for p in alle) for i in range(3)] + [max(p[i] for p in alle) for i in range(3)]
        teil = {"name": name, "quelle": os.path.basename(step), "einheit": "mm",
                "koerper": [{"koerper": t, "name": gmsh.model.getEntityName(3, t).split("/")[-1]}
                            for _d, t in gmsh.model.getEntities(3)],
                "box": _r(box), "groesse_mm": _r([box[i + 3] - box[i] for i in range(3)], 3),
                "volumen_mm3": round(sum(gmsh.model.occ.getMass(3, t) for _d, t in gmsh.model.getEntities(3)), 1),
                "flaechen": flaechen, "bohrungen": bohrungen, "kanten": kanten, "ecken": ecken}
    finally:
        gmsh.finalize()
    _schreibe_netz(tmp, netz)
    shutil.copyfile(step, os.path.join(tmp, "quelle.step"))
    with open(os.path.join(tmp, "teil.json"), "w", encoding="utf-8") as f:
        json.dump(teil, f, ensure_ascii=False)
    shutil.rmtree(ordner, ignore_errors=True)
    os.replace(tmp, ordner)
    return teil


def _schreibe_netz(ordner, netz):
    with open(os.path.join(ordner, "netz.obj"), "w", encoding="ascii") as obj:
        obj.write("# Teilebibliothek, Einheit mm\n")
        basis = 0
        for tag in sorted(netz):
            punkte, normalen, dreiecke = netz[tag]
            obj.write("g f%d\n" % tag)
            for p in punkte:
                obj.write("v %.4f %.4f %.4f\n" % tuple(p))
            for n in normalen:
                obj.write("vn %.4f %.4f %.4f\n" % tuple(n))
            for d in dreiecke:
                obj.write("f %s\n" % " ".join("%d//%d" % (basis + i + 1, basis + i + 1) for i in d))
            basis += len(punkte)
    browser = {str(tag): {"p": [round(v, 3) for p in punkte for v in p], "i": [i for d in dreiecke for i in d]}
               for tag, (punkte, _n, dreiecke) in netz.items()}
    with open(os.path.join(ordner, "netz.json"), "w", encoding="utf-8") as f:
        json.dump(browser, f, separators=(",", ":"))


SPEICHER_MB = 1500   # harte Grenze fuer einen Import (der Container hat nur wenige GB)


def speicher_begrenzen(mb=SPEICHER_MB):
    """Adressraum dieses Prozesses begrenzen: lieber ein Fehler als ein volles System."""
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (mb * 1024 * 1024, mb * 1024 * 1024))
    except (ImportError, ValueError, OSError):
        pass


def main():
    speicher_begrenzen()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("befehl", choices=["einlesen", "koerper"])
    ap.add_argument("step")
    ap.add_argument("name", nargs="?")
    ap.add_argument("--teile", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "teile"))
    ap.add_argument("--koerper", help="nur diese Koerper, z.B. 1,3,8")
    ap.add_argument("--feinheit", type=float, help="groesste Kantenlaenge im Netz in mm")
    args = ap.parse_args()
    try:
        if args.befehl == "koerper":
            for k in koerper_liste(args.step):
                print("%3d  %-45s  V %9.0f mm3  box %s" % (k["koerper"], k["name"][:45], k["volumen_mm3"], k["box"]))
            return 0
        koerper = [int(k) for k in args.koerper.split(",")] if args.koerper else None
        t = einlesen(args.step, args.teile, args.name, koerper, args.feinheit)
    except TeilFehler as e:
        print("Fehler: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps({"name": t["name"], "groesse_mm": t["groesse_mm"], "flaechen": len(t["flaechen"]),
                      "bohrungen": len(t["bohrungen"]), "kanten": len(t["kanten"]), "ecken": len(t["ecken"])},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
