"""Abgleich gegen die echte iRC-Steuerung.

Faehrt die iRC-Steuerung per CRI in Teststellungen und vergleicht die
Flanschposition, die iRC selbst berechnet (POSCARTROBOT), mit der
Kinematik des Webots-Modells (tools/rebel_kinematik.py).

Voraussetzung: iRC-Steuerung im Simulationsmodus, z.B. ohne Oberflaeche:
  cd %APPDATA%\\RobotControl\\V15
  "C:\\Program Files\\iRC-igusRobotControl-V15\\bin-core\\RobotControl.exe" --simulate --project "ReBeL/REBEL-6DOF/REBEL-6DOF-03.prj"

  python tools/abgleich_irc.py             # Achsen pruefen
  python tools/abgleich_irc.py --greifen   # danach Pick-and-Place mit DOut31

NICHT gegen den echten Roboter laufen lassen: das Skript bewegt den Arm.
"""

import argparse
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "controllers", "irc_bridge"))
import rebel_kinematik as kin  # noqa: E402
from cri_client import CriClient  # noqa: E402

BASIS = [0.0, 20.0, 60.0, 0.0, 60.0, 0.0]


def warte_auf(cli, bedingung, zeit=20.0):
    ende = time.monotonic() + zeit
    while time.monotonic() < ende:
        st = cli.state
        if st is not None and bedingung(st):
            return st
        time.sleep(0.05)
    return None


def fahre(cli, q, vel=60):
    werte = " ".join("%.3f" % v for v in q)
    cli.send("CMD Move Joint %s 0 0 0 %d" % (werte, vel))
    st = warte_auf(cli, lambda s: max(abs(a - b) for a, b in zip(s.joints("setpoint"), q)) < 0.05
                   and max(abs(a - b) for a, b in zip(s.joints(), q)) < 0.05, 90)
    time.sleep(0.3)   # eine frische Statusmeldung abwarten
    return cli.state if st is not None else None


def vorbereiten(cli):
    if warte_auf(cli, lambda s: True, 10) is None:
        sys.exit("Keine Statusmeldung von iRC - laeuft die Steuerung?")
    # Override im Projekt ist 40 % - fuer den Test auf 100 % stellen
    for befehl in ("CMD SetActive true", "CMD Reset", "CMD Enable", "CMD Override 100"):
        cli.send(befehl)
        time.sleep(0.8)
    cli.send("CMD GetReferencingInfo")
    time.sleep(0.5)
    if not any("ReferencingInfo 1" in m for m in cli.messages):
        print("Referenziere alle Achsen ...")
        cli.send("CMD ReferenceAllJoints")
        ende = time.monotonic() + 60
        while time.monotonic() < ende:
            time.sleep(1.0)
            cli.send("CMD GetReferencingInfo")
            time.sleep(0.3)
            if any("ReferencingInfo 1" in m for m in cli.messages[-5:]):
                break
    st = cli.state
    print("Zustand: KINSTATE %s, Fehler %s" % (st.kinstate, " ".join(st.fields.get("ERROR", [])[:7])))


def achsen_pruefen(cli):
    stellungen = [("Basis", BASIS)]
    for i in range(6):
        q = list(BASIS)
        q[i] += 30.0
        stellungen.append(("A%d +30" % (i + 1), q))
    stellungen += [("Grundstellung", [0, -20, 110, 0, 90, 0]),
                   ("gemischt 1", [45, 10, 80, 30, 40, -60]),
                   ("gemischt 2", [-60, 40, 50, -45, 70, 90])]
    print()
    print("%-14s %-38s %-26s %-26s %8s" % ("Stellung", "A1..A6 [Grad]", "iRC Flansch [mm]",
                                         "Modell Flansch [mm]", "Abw.[mm]"))
    groesste = 0.0
    for name, q in stellungen:
        st = fahre(cli, q)
        if st is None:
            print("%-14s Stellung nicht erreicht (KINSTATE %s)" % (name, cli.state.kinstate))
            continue
        p, _r = kin.fk(st.joints())
        modell = [v * 1000 for v in p]
        irc = st.cart[:3]
        abw = math.sqrt(sum((a - b) ** 2 for a, b in zip(irc, modell)))
        groesste = max(groesste, abw)
        print("%-14s %-38s %-26s %-26s %8.2f" % (
            name, " ".join("%5.0f" % v for v in q),
            " ".join("%8.1f" % v for v in irc), " ".join("%8.1f" % v for v in modell), abw))
    print()
    print("Groesste Abweichung: %.2f mm -> %s" % (groesste, "Modell passt zu iRC" if groesste < 2
                                                    else "Modell weicht ab, bitte pruefen"))
    return groesste


def greifen(cli, auf, zu):
    """Pick-and-Place mit dem Schunk EGP 25 (Stift 20 mm hoch auf der gelben Marke).
    auf/zu: CRI-Nummern der Ausgaenge oeffnen/schliessen."""
    tool = kin.TCP_EGP25
    w = math.radians(60)
    pick, place = (0.35, 0.0), (0.35 * math.cos(w), 0.35 * math.sin(w))
    ablauf = [
        ("oeffnen", None, False),
        ("ueber Stift", kin.ik_nach_unten(pick[0], pick[1], 0.12, tool), None),
        ("am Stift", kin.ik_nach_unten(pick[0], pick[1], 0.0155, tool), True),
        ("anheben", kin.ik_nach_unten(pick[0], pick[1], 0.12, tool), None),
        ("ueber Ablage", kin.ik_nach_unten(place[0], place[1], 0.12, tool), None),
        ("an Ablage", kin.ik_nach_unten(place[0], place[1], 0.0175, tool), False),
        ("anheben", kin.ik_nach_unten(place[0], place[1], 0.12, tool), None),
    ]
    print()
    print("Pick-and-Place: oeffnen DOut%d, schliessen DOut%d (CRI-Nummern %d / %d):" % (auf + 1, zu + 1, auf, zu))
    for name, q, schliessen in ablauf:
        if q is not None:
            st = fahre(cli, q, vel=40)
            print("  %-14s %s" % (name, "erreicht" if st else "NICHT erreicht"))
        if schliessen is not None:
            time.sleep(1.5)   # Webots Zeit geben, die Stellung zu uebernehmen
            # wie iRC beim EGP: einen Kanal aus, den anderen ein
            cli.send("CMD DOUT %d false" % (auf if schliessen else zu))
            cli.send("CMD DOUT %d true" % (zu if schliessen else auf))
            ein = zu if schliessen else auf
            st = warte_auf(cli, lambda s: s.dout_bit(ein) and not s.dout_bit(auf if schliessen else zu), 5)
            print("  %s: %s" % ("schliessen" if schliessen else "oeffnen", "bestaetigt" if st else "NICHT bestaetigt"))
            time.sleep(1.5)
    fahre(cli, [0, -20, 110, 0, 90, 0])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=3920)
    ap.add_argument("--greifen", action="store_true", help="zusaetzlich Pick-and-Place fahren")
    ap.add_argument("--dout-oeffnen", type=int, default=31, help="Ausgang oeffnen wie in iRC angezeigt (Standard 31)")
    ap.add_argument("--dout-schliessen", type=int, default=32, help="Ausgang schliessen wie in iRC angezeigt (Standard 32)")
    args = ap.parse_args()
    if args.host not in ("127.0.0.1", "localhost"):
        sys.exit("Nur gegen die lokale Simulation erlaubt.")

    cli = CriClient(args.host, [args.port], passive=False, log=lambda m: None).start()
    try:
        vorbereiten(cli)
        achsen_pruefen(cli)
        if args.greifen:
            greifen(cli, args.dout_oeffnen - 1, args.dout_schliessen - 1)
    finally:
        cli.stop()


if __name__ == "__main__":
    main()
