"""Halter fuer Naeherungssensoren M12/M18 auf dem Profiltisch (Nut 10): STEP-Dateien zum Drucken und fuer den Baukasten.

Winkel aus Grundplatte und senkrechter Wand. Unter der Grundplatte eine Feder, die in die Nut greift (verdrehsicher),
dazu zwei Bohrungen fuer M5-Schrauben mit Nutenstein. Der Sensor steckt waagerecht in der Wand und wird mit seinen
beiden Muttern geklemmt.

  quer   Sensorachse quer zur Nut
  laengs Sensorachse entlang der Nut

CAD-Ursprung = Mitte der Grundplatten-Unterseite (liegt auf dem Tisch), x entlang der Nut, z nach oben. Die
Unterseite ist EINE Flaeche (die Feder ist kuerzer als die Platte), ihre Mitte liegt auf der Nutmitte: im Baukasten
die Unterseite an die Nut haengen, dann sitzt der Halter in der Nut, Versatz x = Lage entlang der Nut.

  python tools/halter_naeherung.py                  # -> cad/halter/naeherung_halter_<m12|m18>_<quer|laengs>.step
  python tools/halter_naeherung.py --ziel ordner

Braucht gmsh (pip install gmsh bzw. apt install python3-gmsh).
"""

import argparse
import os

# Sensor: Lochdurchmesser mit Spiel fuer den 3D-Druck, Hoehe der Sensorachse ueber dem Tisch, Breite der Wand.
# Die Achshoehe ist so gewaehlt, dass die Sensormuttern (Ecken M12 10,4 mm / M18 13,9 mm vom Gewinde) ueber den
# Schraubenkoepfen bleiben.
BAUFORMEN = {"m12": {"loch_mm": 12.4, "achse_mm": 25.0, "wand_breite_mm": 30.0},
             "m18": {"loch_mm": 18.4, "achse_mm": 30.0, "wand_breite_mm": 40.0}}
PLATTE_MM = 6.0          # Dicke der Grundplatte
WAND_MM = 6.0            # Dicke der Wand (Klemmlaenge zwischen den Sensormuttern)
RIPPE_MM = 4.0           # Dicke der Stuetzrippen
FEDER_BREITE_MM = 9.5    # Nut 10 mit Spiel
FEDER_TIEFE_MM = 3.0
SCHRAUBE_MM = 5.5        # Durchgang M5
UEBER_LOCH_MM = 7.0      # Wand ueber dem Sensorloch
QUER_TIEFE_MM = 24.0     # Grundplatte quer zur Nut bei "quer"
LAENGS_LAENGE_MM = 48.0  # Grundplatte entlang der Nut bei "laengs"


def _quader(occ, x0, y0, z0, x1, y1, z1):
    return (3, occ.addBox(x0, y0, z0, x1 - x0, y1 - y0, z1 - z0))


def _rippe(occ, ebene, a, dicke, punkte):
    """Dreieck (u, z) in der Ebene "xz" (bei y = a) oder "yz" (bei x = a), um dicke in die Normalenrichtung."""
    p = [occ.addPoint(a, u, z) if ebene == "yz" else occ.addPoint(u, a, z) for u, z in punkte]
    linien = [occ.addLine(p[i], p[(i + 1) % 3]) for i in range(3)]
    flaeche = occ.addPlaneSurface([occ.addCurveLoop(linien)])
    d = (dicke, 0, 0) if ebene == "yz" else (0, dicke, 0)
    return next(e for e in occ.extrude([(2, flaeche)], *d) if e[0] == 3)


def halter(occ, bauform, richtung):
    """Baut den Halter als ein Volumen; Rueckgabe [(3, tag)]."""
    b = BAUFORMEN[bauform]
    w, h, loch = b["wand_breite_mm"], b["achse_mm"], b["loch_mm"]
    oben = h + loch / 2 + UEBER_LOCH_MM
    rippe_h = 0.6 * h
    teile, loecher = [], []
    if richtung == "quer":       # Wand entlang der Nut, Sensorachse in y
        lx, ly = w / 2 + 16.0, QUER_TIEFE_MM / 2
        teile.append(_quader(occ, -lx, -ly, 0, lx, ly, PLATTE_MM))
        teile.append(_quader(occ, -w / 2, -WAND_MM / 2, 0, w / 2, WAND_MM / 2, oben))
        for sx in (-1, 1):
            x = sx * w / 2 - (RIPPE_MM if sx > 0 else 0)
            for sy in (-1, 1):
                teile.append(_rippe(occ, "yz", x, RIPPE_MM, [(sy * WAND_MM / 2, PLATTE_MM), (sy * ly, PLATTE_MM),
                                                             (sy * WAND_MM / 2, PLATTE_MM + rippe_h)]))
        loecher.append((3, occ.addCylinder(0, -ly, h, 0, 2 * ly, 0, loch / 2)))
        schrauben = [(-(w / 2 + 9.0), 0), (w / 2 + 9.0, 0)]
    else:                        # Wand quer zur Nut, Sensorachse in x
        lx, ly = LAENGS_LAENGE_MM / 2, w / 2
        teile.append(_quader(occ, -lx, -ly, 0, lx, ly, PLATTE_MM))
        teile.append(_quader(occ, -WAND_MM / 2, -ly, 0, WAND_MM / 2, ly, oben))
        for sy in (-1, 1):
            y = sy * ly - (RIPPE_MM if sy > 0 else 0)
            for sx in (-1, 1):
                teile.append(_rippe(occ, "xz", y, RIPPE_MM, [(sx * WAND_MM / 2, PLATTE_MM), (sx * lx, PLATTE_MM),
                                                             (sx * WAND_MM / 2, PLATTE_MM + rippe_h)]))
        loecher.append((3, occ.addCylinder(-lx, 0, h, 2 * lx, 0, 0, loch / 2)))
        schrauben = [(-(WAND_MM / 2 + 12.0), 0), (WAND_MM / 2 + 12.0, 0)]
    # Feder 1 mm kuerzer als die Platte: so bleibt die Unterseite eine einzige Flaeche mit der Mitte auf der Nut
    teile.append(_quader(occ, -lx + 1, -FEDER_BREITE_MM / 2, -FEDER_TIEFE_MM, lx - 1, FEDER_BREITE_MM / 2, 0))
    for x, y in schrauben:
        loecher.append((3, occ.addCylinder(x, y, -FEDER_TIEFE_MM - 1, 0, 0, PLATTE_MM + FEDER_TIEFE_MM + 2, SCHRAUBE_MM / 2)))
    koerper, _ = occ.fuse(teile[:1], teile[1:])
    koerper, _ = occ.cut(koerper, loecher)
    occ.synchronize()
    return koerper


def schreiben(ziel, bauformen=None, richtungen=("quer", "laengs")):
    import gmsh
    os.makedirs(ziel, exist_ok=True)
    dateien = []
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setString("Geometry.OCCTargetUnit", "MM")
        for bauform in bauformen or sorted(BAUFORMEN):
            for richtung in richtungen:
                gmsh.clear()
                gmsh.model.add("halter")
                halter(gmsh.model.occ, bauform, richtung)
                datei = os.path.join(ziel, "naeherung_halter_%s_%s.step" % (bauform, richtung))
                gmsh.write(datei)
                dateien.append(datei)
    finally:
        gmsh.finalize()
    return dateien


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ziel", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cad", "halter"))
    for datei in schreiben(ap.parse_args().ziel):
        print(datei)


if __name__ == "__main__":
    main()
