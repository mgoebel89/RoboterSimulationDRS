"""Erzeugt das Webots-Modell des igus ReBeL aus der lokalen iRC-Installation.

Liest:
  - Kinematik (Glied-Laengen) aus Data/Robots/<Typ>/<Typ>.xml
  - Glieder-Modelle Joint0.obj .. Joint6.obj (+ *Coll.obj) und Rebel.mtl
  - Greifer Schunk EGP 25 aus Data/Tools/Schunk_EGP25_RebelMountingPlate*.xml
    (TCP aus dem Werkzeug, Modell im offenen Zustand, Backen werden abgetrennt)
Schreibt:
  - protos/meshes/*.obj   (nach Material bzw. Bauteil getrennt, in Metern)
  - protos/IgusRebel6DOF.proto

Die Modelldateien bleiben Eigentum von igus und werden nur lokal erzeugt,
nicht weitergegeben (siehe .gitignore).

Aufruf:
  python tools/erzeuge_rebel_modell.py
  python tools/erzeuge_rebel_modell.py --irc "C:/Program Files/iRC-igusRobotControl-V15" --robot REBEL-6DOF-03
"""

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

DEFAULT_IRC = r"C:\Program Files\iRC-igusRobotControl-V15"
HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
PROTO_DIR = os.path.join(PROJECT, "protos")
MESH_DIR = os.path.join(PROTO_DIR, "meshes")

# Gelenkachsen bei Nullstellung (Arm senkrecht), passend zu den iRC-Meshes
AXES = ["0 0 1", "0 1 0", "0 1 0", "0 0 1", "0 1 0", "0 0 1"]

FALLBACK_COLORS = {
    "plastic_igusorange_shiny": (0.949, 0.42, 0.0, 0.5, 0.0),
    "plastic_black_matt": (0.05, 0.05, 0.05, 0.7, 0.0),
    "metal_black": (0.05, 0.05, 0.05, 0.2, 0.3),
}


def startup_robot_type(irc):
    """Robotertyp aus dem iRC-Startprojekt lesen, z.B. 'REBEL\\REBEL-6DOF-03'."""
    startup = ET.parse(os.path.join(irc, "StartUp.xml")).getroot()
    prj_rel = startup.find("StartUp").get("Project")
    # Pfad ist relativ zum Programmordner, z.B. "..\..\Data\Projects\..."
    parts = [p for p in re.split(r"[\\/]", prj_rel) if p not in ("..", ".", "")]
    prj = os.path.join(irc, *parts)
    robot = ET.parse(prj).getroot().find("Robot")
    return robot.get("Type")


def read_kinematics(xml_path):
    root = ET.parse(xml_path).getroot()
    kin = root.find("Kinematic")
    k = {key: float(kin.get(key, "0")) / 1000.0
         for key in ("lz0", "lx1", "lz1", "lx2", "lx3", "lz3", "lx4", "lx6")}
    for key in ("lz3",):
        if abs(k[key]) > 1e-9:
            print("WARNUNG: %s=%.4f wird im Modell nicht beruecksichtigt" % (key, k[key]))
    limits = []
    smm = root.find("SoftwareMinMax")
    for i in range(1, 7):
        lo = float(smm.get("A%dMin" % i)) if smm is not None else -180.0
        hi = float(smm.get("A%dMax" % i)) if smm is not None else 180.0
        limits.append((lo, hi))
    return kin.get("Type"), k, limits


def read_mtl(path):
    mats = {}
    if not os.path.exists(path):
        return mats
    name = None
    for line in open(path, encoding="utf-8", errors="replace"):
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "newmtl":
            name = parts[1]
            mats[name] = {"Kd": (0.5, 0.5, 0.5), "Pr": 0.5, "Pm": 0.0}
        elif name and parts[0] == "Kd":
            mats[name]["Kd"] = tuple(float(x) for x in parts[1:4])
        elif name and parts[0] in ("Pr", "Pm"):
            mats[name][parts[0]] = float(parts[1])
    return mats


def split_obj(src, scale=0.001):
    """OBJ einlesen und Flaechen nach Material gruppieren.
    Rueckgabe: {material: (verts, normals, faces)} mit neu nummerierten Indizes."""
    v, vn = [], []
    groups = {}
    mat = "default"
    for line in open(src, encoding="utf-8", errors="replace"):
        if line.startswith("v "):
            v.append([float(x) * scale for x in line.split()[1:4]])
        elif line.startswith("vn "):
            vn.append([float(x) for x in line.split()[1:4]])
        elif line.startswith("usemtl"):
            mat = line.split()[1] if len(line.split()) > 1 else "default"
        elif line.startswith("f "):
            corners = []
            for c in line.split()[1:]:
                p = c.split("/")
                vi = int(p[0])
                vi = vi - 1 if vi > 0 else len(v) + vi
                ni = None
                if len(p) >= 3 and p[2]:
                    ni = int(p[2])
                    ni = ni - 1 if ni > 0 else len(vn) + ni
                corners.append((vi, ni))
            groups.setdefault(mat, []).append(corners)
    result = {}
    for mat, faces in groups.items():
        vmap, nmap, ov, on, of = {}, {}, [], [], []
        for face in faces:
            out = []
            for vi, ni in face:
                if vi not in vmap:
                    vmap[vi] = len(ov) + 1
                    ov.append(v[vi])
                nidx = None
                if ni is not None:
                    if ni not in nmap:
                        nmap[ni] = len(on) + 1
                        on.append(vn[ni])
                    nidx = nmap[ni]
                out.append((vmap[vi], nidx))
            of.append(out)
        result[mat] = (ov, on, of)
    return result


def write_obj(path, verts, normals, faces):
    with open(path, "w", encoding="ascii") as f:
        f.write("# erzeugt aus iRC-Daten, nicht weitergeben\n")
        for x, y, z in verts:
            f.write("v %.6f %.6f %.6f\n" % (x, y, z))
        for x, y, z in normals:
            f.write("vn %.5f %.5f %.5f\n" % (x, y, z))
        for face in faces:
            f.write("f " + " ".join(
                "%d//%d" % (vi, ni) if ni else "%d" % vi for vi, ni in face) + "\n")


def safe(name):
    return re.sub(r"[^A-Za-z0-9_]+", "_", name)


def convert_link(robot_dir, i, mtl):
    """Gibt (Liste von Shape-Texten, bounding-Mesh-Dateiname oder None) zurueck."""
    shapes = []
    src = os.path.join(robot_dir, "Joint%d.obj" % i)
    for mat, (verts, normals, faces) in sorted(split_obj(src).items()):
        fname = "rebel_j%d_%s.obj" % (i, safe(mat))
        write_obj(os.path.join(MESH_DIR, fname), verts, normals, faces)
        if mat in mtl:
            # reines Schwarz wirkt in Webots wie ein Loch, daher leicht aufhellen
            r, g, b = (max(c, 0.06) for c in mtl[mat]["Kd"])
            rough, metal = mtl[mat]["Pr"], mtl[mat]["Pm"]
        else:
            r, g, b, rough, metal = FALLBACK_COLORS.get(mat, (0.6, 0.6, 0.6, 0.5, 0.0))
        shapes.append(
            'Shape { appearance PBRAppearance { baseColor %.3f %.3f %.3f roughness %.2f metalness %.2f } '
            'geometry Mesh { url "meshes/%s" } }' % (r, g, b, rough, metal, fname))
    coll = None
    csrc = os.path.join(robot_dir, "Joint%dColl.obj" % i)
    # Der Fuss (Glied 0) steht fest auf dem Tisch; sein Kollisionskoerper
    # wuerde nur dauernd Tischkontakte erzeugen und die Konsole fluten.
    if i > 0 and os.path.exists(csrc):
        merged_v, merged_f = [], []
        for verts, _normals, faces in split_obj(csrc).values():
            off = len(merged_v)
            merged_v += verts
            merged_f += [[(vi + off, None) for vi, _ in face] for face in faces]
        coll = "rebel_j%d_coll.obj" % i
        write_obj(os.path.join(MESH_DIR, coll), merged_v, [], merged_f)
    return shapes, coll


def split_components(src, scale=0.001):
    """OBJ einlesen und in zusammenhaengende Bauteile zerlegen (Flaechen, die
    sich Eckpunkte teilen). Rueckgabe: Liste von (verts, normals, faces),
    groesstes Bauteil zuerst."""
    v, vn, faces = [], [], []
    for line in open(src, encoding="utf-8", errors="replace"):
        if line.startswith("v "):
            v.append([float(x) * scale for x in line.split()[1:4]])
        elif line.startswith("vn "):
            vn.append([float(x) for x in line.split()[1:4]])
        elif line.startswith("f "):
            corners = []
            for c in line.split()[1:]:
                p = c.split("/")
                ni = int(p[2]) - 1 if len(p) >= 3 and p[2] else None
                corners.append((int(p[0]) - 1, ni))
            faces.append(corners)
    parent = list(range(len(v)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for face in faces:
        for vi, _ni in face[1:]:
            parent[find(vi)] = find(face[0][0])
    groups = {}
    for face in faces:
        groups.setdefault(find(face[0][0]), []).append(face)
    result = []
    for group in groups.values():
        vmap, nmap, ov, on, of = {}, {}, [], [], []
        for face in group:
            out = []
            for vi, ni in face:
                if vi not in vmap:
                    vmap[vi] = len(ov) + 1
                    ov.append(v[vi])
                nidx = None
                if ni is not None:
                    if ni not in nmap:
                        nmap[ni] = len(on) + 1
                        on.append(vn[ni])
                    nidx = nmap[ni]
                out.append((vmap[vi], nidx))
            of.append(out)
        result.append((ov, on, of))
    result.sort(key=lambda r: -len(r[0]))
    return result


# Greifer aus iRC: Name im Proto -> Werkzeugdatei in Data/Tools
EGP_VARIANTEN = [
    ("egp25", "Schunk_EGP25_RebelMountingPlate.xml"),
    ("egp25_finger", "Schunk_EGP25_RebelMountingPlate_Finger.xml"),
]


def _merge(parts):
    mv, mn, mf = [], [], []
    for verts, normals, faces in parts:
        ov, on = len(mv), len(mn)
        mv += verts
        mn += normals
        mf += [[(vi + ov, None if ni is None else ni + on) for vi, ni in face] for face in faces]
    return mv, mn, mf


def lade_egp(irc, xml_name, scale=0.001):
    """iRC-Werkzeugdatei und Modell im offenen Zustand: (XML, Grundkoerper, linke, rechte Backe)."""
    tools = os.path.join(irc, "Data", "Tools")
    root = ET.parse(os.path.join(tools, xml_name)).getroot()
    obj = root.find("CAD").get("FileStandard")   # offener Zustand
    teile = split_components(os.path.join(tools, obj), scale)
    if len(teile) < 3:
        sys.exit("%s: Backen lassen sich nicht vom Grundkoerper trennen" % obj)
    grund, backen = teile[0], teile[1:]
    links = [b for b in backen if sum(p[0] for p in b[0]) < 0]
    rechts = [b for b in backen if sum(p[0] for p in b[0]) >= 0]
    if not links or not rechts:
        sys.exit("%s: linke und rechte Backe nicht gefunden" % obj)
    return root, grund, _merge(links), _merge(rechts)


# --- Backensaetze: eigene Backen (aus STEP, siehe tools/step_zu_netz.py) am EGP ---

BACKEN_DIR = os.path.join(PROJECT, "cad", "backen")
WERKZEUG_DIR = os.path.join(PROJECT, "werkzeuge")
ACHSE = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}


def lade_backensaetze(backen_dir=BACKEN_DIR):
    """Alle cad/backen/<name>/backensatz.json mit vorhandenem Netz."""
    saetze = []
    if not os.path.isdir(backen_dir):
        return saetze
    for name in sorted(os.listdir(backen_dir)):
        pfad = os.path.join(backen_dir, name, "backensatz.json")
        if not os.path.exists(pfad):
            continue
        with open(pfad, encoding="utf-8") as f:
            satz = {k: v for k, v in json.load(f).items() if not k.startswith("_")}
        satz["ordner"] = os.path.dirname(pfad)
        if not os.path.exists(os.path.join(satz["ordner"], satz["netz"])):
            print("WARNUNG: %s: Netz %s fehlt, Backensatz uebersprungen" % (name, satz["netz"]))
            continue
        saetze.append(satz)
    return saetze


def backen_lage(satz):
    """Drehmatrix (Spalten = CAD-Achsen im Werkzeugsystem) und Verschiebung in mm der linken Backe."""
    spalten = []
    for achse in "xyz":
        ziel = satz["achsen"][achse].strip()
        vorz = -1 if ziel.startswith("-") else 1
        spalten.append(tuple(vorz * c for c in ACHSE[ziel[-1]]))
    m = [[spalten[c][r] for c in range(3)] for r in range(3)]
    det = (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1]) - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
           + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))
    if det != 1:
        sys.exit("Backensatz %s: 'achsen' ist keine Drehung (gespiegelt oder doppelt)" % satz["name"])
    return m, satz["verschiebung_mm"]


def backe_platzieren(satz, scale=0.001):
    """Netz der Backe fuer links und rechts ins Werkzeugsystem setzen (Einheit nach scale)."""
    verts, normals, faces = split_obj(os.path.join(satz["ordner"], satz["netz"]), scale)["default"]
    m, t = backen_lage(satz)

    def dreh(p):
        return tuple(sum(m[r][c] * p[c] for c in range(3)) for r in range(3))

    links_v = [tuple(a + b * scale for a, b in zip(dreh(p), t)) for p in verts]
    links_n = [dreh(n) for n in normals]
    # rechte Backe: dieselbe, 180 Grad um die Werkzeugachse gedreht
    rechts_v = [(-x, -y, z) for x, y, z in links_v]
    rechts_n = [(-x, -y, z) for x, y, z in links_n]
    return (links_v, links_n, faces), (rechts_v, rechts_n, faces)


def convert_egp(irc, key, xml_name, satz=None):
    """Schunk EGP aus der iRC-Werkzeugdatei: Grundkoerper fest am Flansch,
    die zwei Backentraeger als Bauteile, die sich zur Mitte schieben; mit
    Backensatz zusaetzlich die eigenen Backen auf den Traegern.
    Rueckgabe: Proto-Text des Greifers und Kenndaten fuer den Controller."""
    root, grund, links, rechts = lade_egp(irc, xml_name)
    kanal = root.find("INTERFACE")
    dateien = {}
    for name, teil in (("grund", grund), ("links", links), ("rechts", rechts)):
        fname = "%s_%s.obj" % (key if satz is None else satz["basis"], name)
        write_obj(os.path.join(MESH_DIR, fname), *teil)
        dateien[name] = fname
    traeger_v = links[0] + rechts[0]
    spalt_traeger = min(abs(p[0]) for p in traeger_v)   # halbe Innenweite der offenen Traeger
    hub = spalt_traeger - 0.0001                        # geschlossen liegen die Traeger aneinander
    if satz is None:
        tcp = float(root.find("OFFSET").get("OffsetZ")) / 1000.0
        greif_v = traeger_v
        titel = "%s aus iRC (%s)" % (root.find("INFO").get("NAME"), xml_name)
    else:
        b_links, b_rechts = backe_platzieren(satz)
        for name, teil in (("backe_links", b_links), ("backe_rechts", b_rechts)):
            fname = "%s_%s.obj" % (key, name)
            write_obj(os.path.join(MESH_DIR, fname), *teil)
            dateien[name] = fname
        greif_v = b_links[0] + b_rechts[0]
        tcp = max(p[2] for p in greif_v) if satz.get("tcp", "spitze") == "spitze" else float(satz["tcp"]) / 1000.0
        hub = min(hub, min(abs(p[0]) for p in greif_v) - 0.0001)   # Backen beruehren sich zuerst
        titel = "%s mit %s%s" % (root.find("INFO").get("NAME"), satz["titel"],
                                 " (Lage provisorisch)" if satz.get("provisorisch") else "")
    spalt = min(abs(p[0]) for p in greif_v)             # halbe Innenweite bei offenem Greifer
    daten = {
        "typ": key,
        "tcp": tcp,
        "spalt": spalt,
        "hub": hub,
        "backe_y": max(abs(p[1]) for p in greif_v),
        "backe_z0": min(p[2] for p in greif_v) - tcp,
        "backe_z1": max(p[2] for p in greif_v) - tcp,
    }
    text = [
        "# %s: TCP %.1f mm, oeffnen DOut%s, schliessen DOut%s"
        % (titel, tcp * 1000, kanal.get("ChannelOpen"), kanal.get("ChannelClose")),
        'Shape { appearance PBRAppearance { baseColor 0.62 0.63 0.66 roughness 0.45 metalness 0.6 } '
        'geometry Mesh { url "meshes/%s" } }' % dateien["grund"],
    ]
    for seite, achse in (("links", "1 0 0"), ("rechts", "-1 0 0")):
        kinder = ['Shape { appearance PBRAppearance { baseColor 0.30 0.31 0.33 roughness 0.4 metalness 0.7 } '
                  'geometry Mesh { url "meshes/%s" } }' % dateien[seite]]
        if satz is not None:
            kinder.append('Shape { appearance PBRAppearance { baseColor 0.18 0.42 0.78 roughness 0.7 metalness 0 } '
                          'geometry Mesh { url "meshes/%s" } }' % dateien["backe_" + seite])
        text += [
            "SliderJoint {",
            "  jointParameters JointParameters { axis %s }" % achse,
            '  device [ LinearMotor { name "finger_%s" minPosition 0 maxPosition %.5f maxVelocity 0.05 } ]' % (seite, hub),
            '  endPoint Solid { name "finger_%s" children [ %s ] }' % (seite, " ".join(kinder)),
            "}",
        ]
    text.append("DEF TCP Pose { translation 0 0 %.5f }" % tcp)
    return text, daten


def werkzeugpaket(irc, key, xml_name, satz, daten):
    """Werkzeug fuer iRC (XML + OBJ offen/geschlossen in mm) nach werkzeuge/<name>/,
    damit TCP und Aussehen in iRC und Simulation gleich sind. Zum Einbau den
    Ordnerinhalt nach <iRC>/Data/Tools kopieren und in iRC das Werkzeug waehlen."""
    root, grund, links, rechts = lade_egp(irc, xml_name, scale=1.0)
    b_links, b_rechts = backe_platzieren(satz, scale=1.0)
    hub_mm = daten["hub"] * 1000
    ordner = os.path.join(WERKZEUG_DIR, key)
    os.makedirs(ordner, exist_ok=True)
    basis = "Schunk_EGP25_%s" % satz["name"].capitalize()

    def verschoben(teil, dx):
        v, n, f = teil
        return [(x + dx, y, z) for x, y, z in v], n, f

    for zustand, dx in (("Offen", 0.0), ("Geschlossen", hub_mm)):
        teile = [grund, verschoben(links, dx), verschoben(b_links, dx),
                 verschoben(rechts, -dx), verschoben(b_rechts, -dx)]
        write_obj(os.path.join(ordner, "%s_%s.obj" % (basis, zustand)), *_merge(teile))
    tcp_mm = daten["tcp"] * 1000
    xml = ET.tostring(root, encoding="unicode")
    xml = re.sub(r'<INFO NAME="[^"]*"', '<INFO NAME="EGP-25 %s"' % satz["titel"], xml)
    xml = re.sub(r'OffsetZ="[^"]*"', 'OffsetZ="%.1f"' % tcp_mm, xml)
    xml = re.sub(r'FileStandard="[^"]*"', 'FileStandard="%s_Offen.obj"' % basis, xml)
    xml = re.sub(r'FileActive="[^"]*"', 'FileActive="%s_Geschlossen.obj"' % basis, xml)
    kopf = ("<!-- Erzeugt von rebel-webots-sim (tools/erzeuge_rebel_modell.py): Schunk EGP 25 mit %s.\n"
            "     TCP %.1f mm = mittig zwischen den Backen auf Hoehe der Backenspitze.%s\n"
            "     Enthaelt Modelldaten von igus: nur fuer den Unterricht, nicht weitergeben. -->\n"
            % (satz["titel"], tcp_mm, "\n     LAGE DER BACKEN PROVISORISCH, noch nicht am echten Greifer gemessen."
               if satz.get("provisorisch") else ""))
    with open(os.path.join(ordner, basis + ".xml"), "w", encoding="utf-8") as f:
        f.write(kopf + xml + "\n")
    return ordner, basis


def greiferdaten_text(daten):
    """Kenndaten als 'schluessel=wert'-Liste fuer Robot.customData."""
    return " ".join("%s=%s" % (k, v if isinstance(v, str) else "%.5f" % v) for k, v in daten.items())


GRIPPER_TEMPLATE = r"""
            %%EGP%%
            %< } else if (fields.greifer.value === 'vakuum') { >%
            Pose {
              translation 0 0 0.0012
              children [
                # Sauggreifer (vereinfachte Geometrie)
                Pose { translation 0 0 0.005 children [ Shape { appearance DEF GRAU PBRAppearance { baseColor 0.25 0.25 0.27 roughness 0.6 metalness 0.2 } geometry Cylinder { radius 0.03 height 0.01 } } ] }
                Pose { translation 0 0 0.035 children [ Shape { appearance PBRAppearance { baseColor 0.75 0.75 0.78 roughness 0.3 metalness 0.8 } geometry Cylinder { radius 0.02 height 0.05 } } ] }
                Pose { translation 0 0 0.066 children [ Shape { appearance PBRAppearance { baseColor 0.05 0.05 0.05 roughness 0.9 } geometry Cylinder { radius 0.016 height 0.012 } } ] }
                LED {
                  translation 0.022 0 0.035
                  name "vakuum_led"
                  color [ 0 1 0 ]
                  children [ Shape { appearance PBRAppearance { baseColor 0.1 0.3 0.1 roughness 0.4 } geometry Sphere { radius 0.006 } } ]
                }
                DEF TCP Pose { translation 0 0 0.072 }
              ]
            }
            %< } else { >%
            DEF TCP Pose { }
            %< } >%"""


def gripper_text(egp):
    """Greifer-Abschnitt des Protos: je EGP-Variante ein Zweig der Vorlage."""
    out = []
    for i, (key, (text, _daten)) in enumerate(egp):
        out.append("%%< %sif (fields.greifer.value === '%s') { >%%" % ("} else " if i else "", key))
        out.extend(text)
    return GRIPPER_TEMPLATE.replace("%%EGP%%", "\n            ".join(out))


def build_proto(robot_type, kin_type, k, limits, links, source_dir, egp):
    lines = []
    w = lines.append
    w("#VRML_SIM R2025a utf8")
    w("# template language: javascript")
    w("# Automatisch erzeugt von tools/erzeuge_rebel_modell.py - nicht von Hand aendern.")
    w("# Quelle: %s (Kinematik %s)" % (source_dir.replace("\\", "/"), kin_type))
    w("# igus ReBeL 6DOF als digitaler Zwilling, Gelenke A1..A6 werden vom Controller gesetzt.")
    w("")
    w("PROTO IgusRebel6DOF [")
    w("  field SFVec3f    translation    0 0 0")
    w("  field SFRotation rotation       0 0 1 0")
    w('  field SFString   name           "ReBeL"')
    w('  field SFString   controller     "irc_bridge"')
    w("  field MFString   controllerArgs []")
    w("  field SFBool     supervisor     TRUE")
    namen = [key for key, _ in egp] + ["vakuum", "kein"]
    w('  field SFString{%s} greifer "%s"' % (", ".join('"%s"' % n for n in namen), namen[0]))
    w("]")
    w("{")
    w("  Robot {")
    w("    translation IS translation")
    w("    rotation IS rotation")
    w("    name IS name")
    w("    controller IS controller")
    w("    controllerArgs IS controllerArgs")
    w("    supervisor IS supervisor")
    # Kenndaten des Greifers fuer den Controller (Backenspalt, Hub, TCP)
    w("    %%< const GREIFER = { %s }; >%%" % ", ".join(
        "'%s': '%s'" % (key, greiferdaten_text(daten)) for key, (_text, daten) in egp))
    w("    customData \"%<= GREIFER[fields.greifer.value] || 'typ=' + fields.greifer.value >%\"")

    offsets = [(0, 0, k["lz0"]), (k["lx1"], 0, k["lz1"]), (0, 0, k["lx2"]),
               (0, 0, k["lx3"]), (0, 0, k["lx4"]), (0, 0, k["lx6"])]

    def emit_link(idx, indent):
        shapes, coll = links[idx]
        pad = " " * indent
        out = [pad + "children ["]
        for s in shapes:
            out.append(pad + "  " + s)
        if idx < 6:
            j = idx  # Gelenk A(j+1) haengt an Glied idx
            x, y, z = offsets[j]
            lo, hi = limits[j]
            out.append(pad + "  HingeJoint {")
            out.append(pad + "    jointParameters HingeJointParameters { axis %s anchor %.5f %.5f %.5f }" % (AXES[j], x, y, z))
            out.append(pad + '    device [ RotationalMotor { name "A%d" maxVelocity 100 maxTorque 1000 } '
                       'PositionSensor { name "A%d_sensor" } ]' % (j + 1, j + 1))
            out.append(pad + "    # iRC-Softwaregrenzen A%d: %.0f .. %.0f Grad (werden hier nicht erzwungen)" % (j + 1, lo, hi))
            out.append(pad + "    endPoint Solid {")
            out.append(pad + "      translation %.5f %.5f %.5f" % (x, y, z))
            out.append(pad + '      name "glied%d"' % (j + 1))
            out.extend(emit_link(idx + 1, indent + 6))
            out.append(pad + "    }")
            out.append(pad + "  }")
        else:
            out.append(pad + "  DEF FLANSCH Pose { }")
            out.extend(gripper_text(egp).replace("\n            ", "\n" + pad + "  ").splitlines())
        out.append(pad + "]")
        if coll:
            out.append(pad + 'boundingObject Mesh { url "meshes/%s" }' % coll)
        return out

    lines.extend(emit_link(0, 4))
    w("  }")
    w("}")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--irc", default=DEFAULT_IRC, help="iRC-Installationsordner")
    ap.add_argument("--robot", help="Robotertyp, z.B. REBEL-6DOF-03 (Standard: aus dem iRC-Startprojekt)")
    args = ap.parse_args()

    if args.robot:
        robot_type = args.robot if "\\" in args.robot or "/" in args.robot else "REBEL\\" + args.robot
    else:
        robot_type = startup_robot_type(args.irc)
    robot_dir = os.path.join(args.irc, "Data", "Robots", *re.split(r"[\\/]", robot_type))
    name = os.path.basename(robot_dir)
    xml_path = os.path.join(robot_dir, name + ".xml")
    if not os.path.exists(xml_path):
        sys.exit("Roboterdatei nicht gefunden: %s" % xml_path)
    print("Roboter: %s" % robot_dir)

    kin_type, k, limits = read_kinematics(xml_path)
    print("Kinematik %s: %s" % (kin_type, ", ".join("%s=%.1f mm" % (n, val * 1000) for n, val in k.items())))
    mtl = read_mtl(os.path.join(robot_dir, "Rebel.mtl"))

    os.makedirs(MESH_DIR, exist_ok=True)
    for old in os.listdir(MESH_DIR):
        if old.startswith("rebel_") and old.endswith(".obj"):
            os.remove(os.path.join(MESH_DIR, old))
    links = [convert_link(robot_dir, i, mtl) for i in range(7)]
    for old in os.listdir(MESH_DIR):
        if old.startswith("egp") and old.endswith(".obj"):
            os.remove(os.path.join(MESH_DIR, old))
    egp = [(key, convert_egp(args.irc, key, xml)) for key, xml in EGP_VARIANTEN]
    xml_von = dict(EGP_VARIANTEN)
    for satz in lade_backensaetze():
        key = "%s_%s" % (satz["basis"], satz["name"])
        text, daten = convert_egp(args.irc, key, xml_von[satz["basis"]], satz)
        egp.append((key, (text, daten)))
        ordner, basis = werkzeugpaket(args.irc, key, xml_von[satz["basis"]], satz, daten)
        print("Werkzeug fuer iRC: %s (%s.xml + OBJ) -> nach <iRC>/Data/Tools kopieren" % (ordner, basis))
    for key, (_text, daten) in egp:
        print("Greifer %s: TCP %.1f mm, Backen offen %.1f mm auseinander, Hub je Backe %.1f mm"
              % (key, daten["tcp"] * 1000, daten["spalt"] * 2000, daten["hub"] * 1000))
    proto = build_proto(robot_type, kin_type, k, limits, links, robot_dir, egp)
    out = os.path.join(PROTO_DIR, "IgusRebel6DOF.proto")
    with open(out, "w", encoding="utf-8") as f:
        f.write(proto)
    n_meshes = sum(len(s) + (1 if c else 0) for s, c in links) + 3 * len(egp)   # ungefaehr
    print("Geschrieben: %s und %d Mesh-Dateien in %s" % (out, n_meshes, MESH_DIR))


if __name__ == "__main__":
    main()
