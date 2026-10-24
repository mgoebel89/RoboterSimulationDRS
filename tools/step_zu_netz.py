"""STEP-Datei (z.B. aus Onshape, FreeCAD, Fusion) in ein Dreiecksnetz umwandeln.

Laeuft im Container mit gmsh/OpenCASCADE (apt install python3-gmsh).

  python3 tools/step_zu_netz.py teil.step teil.obj            # Netz in mm (wie die iRC-Werkzeuge)
  python3 tools/step_zu_netz.py teil.step teil.obj --meter    # Netz in m (fuer Webots ohne Skalierung)
  python3 tools/step_zu_netz.py teil.step --analyse           # nur Masse, ebene Flaechen und Bohrungen

Jede CAD-Flaeche bekommt eigene Eckpunkte, so bleiben Kanten im Bild scharf.
"""

import argparse
import json
import math
import sys


def _laden(gmsh, step, einheit):
    gmsh.option.setNumber("General.Terminal", 0)
    gmsh.option.setString("Geometry.OCCTargetUnit", einheit)
    gmsh.model.add("teil")
    gmsh.model.occ.importShapes(step)
    gmsh.model.occ.synchronize()


def analyse(step):
    """Hauptmasse in mm, ebene Flaechen (Normale, Flaeche, Lage) und Bohrungen."""
    import gmsh
    gmsh.initialize()
    try:
        _laden(gmsh, step, "MM")
        x0, y0, z0, x1, y1, z1 = gmsh.model.getBoundingBox(-1, -1)
        flaechen, bohrungen = [], []
        for _dim, tag in gmsh.model.getEntities(2):
            art = gmsh.model.getType(2, tag)
            groesse = gmsh.model.occ.getMass(2, tag)
            mitte = gmsh.model.occ.getCenterOfMass(2, tag)
            bb = gmsh.model.getBoundingBox(2, tag)
            if art == "Plane":
                para = gmsh.model.getParametrizationBounds(2, tag)
                u = [(para[0][0] + para[1][0]) / 2, (para[0][1] + para[1][1]) / 2]
                n = gmsh.model.getNormal(tag, u)
                flaechen.append({"tag": tag, "normale": [round(v, 3) for v in n], "flaeche_mm2": round(groesse, 1),
                                 "mitte": [round(v, 2) for v in mitte], "box": [round(v, 2) for v in bb]})
            elif art in ("Cylinder", "Cone"):
                bohrungen.append({"tag": tag, "art": art, "flaeche_mm2": round(groesse, 1),
                                  "mitte": [round(v, 2) for v in mitte], "box": [round(v, 2) for v in bb]})
        flaechen.sort(key=lambda f: -f["flaeche_mm2"])
        return {"box_mm": [round(v, 3) for v in (x0, y0, z0, x1, y1, z1)],
                "groesse_mm": [round(x1 - x0, 3), round(y1 - y0, 3), round(z1 - z0, 3)],
                "volumen_mm3": round(sum(gmsh.model.occ.getMass(3, t) for _d, t in gmsh.model.getEntities(3)), 1),
                "ebene_flaechen": flaechen, "runde_flaechen": bohrungen}
    finally:
        gmsh.finalize()


def netz(step, ziel, meter=False, feinheit=0.5):
    """Dreiecksnetz als OBJ mit Normalen. feinheit: groesste Kantenlaenge in mm."""
    import gmsh
    gmsh.initialize()
    try:
        _laden(gmsh, step, "MM")
        gmsh.option.setNumber("Mesh.MeshSizeMax", max(feinheit, 0.2) * 4)
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 24)   # Kreise mit ~24 Segmenten
        gmsh.option.setNumber("Mesh.Algorithm", 6)
        gmsh.model.mesh.generate(2)
        skala = 0.001 if meter else 1.0
        knoten_tags, koord, _ = gmsh.model.mesh.getNodes()
        lage = {int(t): (koord[3 * i] * skala, koord[3 * i + 1] * skala, koord[3 * i + 2] * skala)
                for i, t in enumerate(knoten_tags)}
        v, vn, f = [], [], []
        for _dim, tag in gmsh.model.getEntities(2):
            typen, _el, knoten = gmsh.model.mesh.getElements(2, tag)
            dreiecke = []
            for typ, kn in zip(typen, knoten):
                if typ == 2:   # 3-Knoten-Dreieck
                    dreiecke += [tuple(int(k) for k in kn[i:i + 3]) for i in range(0, len(kn), 3)]
            if not dreiecke:
                continue
            # eigene Eckpunkte je CAD-Flaeche, Normale aus den Dreiecken gemittelt
            index, summe = {}, {}
            for d in dreiecke:
                a, b, c = (lage[k] for k in d)
                n = _kreuz(_minus(b, a), _minus(c, a))
                for k in d:
                    if k not in index:
                        index[k] = len(v) + 1
                        v.append(lage[k])
                        summe[k] = [0.0, 0.0, 0.0]
                    summe[k] = [summe[k][j] + n[j] for j in range(3)]
            basis = len(vn)
            reihe = {}
            for k, i in index.items():
                reihe[k] = basis + len(reihe) + 1
                vn.append(_einheit(summe[k]))
            f += [tuple((index[k], reihe[k]) for k in d) for d in dreiecke]
        with open(ziel, "w", encoding="ascii") as datei:
            datei.write("# aus %s, Einheit %s\n" % (step.replace("\\", "/").rsplit("/", 1)[-1], "m" if meter else "mm"))
            for p in v:
                datei.write("v %.6f %.6f %.6f\n" % p)
            for n in vn:
                datei.write("vn %.5f %.5f %.5f\n" % n)
            for d in f:
                datei.write("f " + " ".join("%d//%d" % e for e in d) + "\n")
        return {"eckpunkte": len(v), "dreiecke": len(f)}
    finally:
        gmsh.finalize()


def _minus(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _kreuz(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _einheit(a):
    laenge = math.sqrt(sum(x * x for x in a)) or 1.0
    return tuple(x / laenge for x in a)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step")
    ap.add_argument("ziel", nargs="?")
    ap.add_argument("--meter", action="store_true")
    ap.add_argument("--analyse", action="store_true")
    ap.add_argument("--feinheit", type=float, default=0.5)
    args = ap.parse_args()
    if args.analyse or not args.ziel:
        print(json.dumps(analyse(args.step), indent=1))
    if args.ziel:
        print(netz(args.step, args.ziel, args.meter, args.feinheit))


if __name__ == "__main__":
    sys.exit(main())
