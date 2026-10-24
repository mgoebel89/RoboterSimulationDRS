"""Demo-Server, der sich wie iRC mit aktivem CRI-Server verhaelt.

Faehrt in Endlosschleife ein Pick-and-Place: Stift bei x=350 mm aufnehmen,
um 60 Grad gedreht wieder ablegen. Damit laesst sich die Webots-Welt ohne iRC
testen. iRC muss dafuer geschlossen sein (gleicher Port).

  python tools/cri_demo_server.py                   # Schunk EGP 25: DOut31 oeffnen, DOut32 schliessen
  python tools/cri_demo_server.py --greifer vakuum  # Sauggreifer, DOut32
"""

import argparse
import math
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rebel_kinematik as kin  # noqa: E402

HOME = [0.0, -20.0, 110.0, 0.0, 90.0, 0.0]


def ablauf(greifer):
    """Liste von (Gelenkwinkel, Dauer in s, Greifer zu?)."""
    tool = {"egp40": kin.TCP_EGP40, "egp25": kin.TCP_EGP25, "egp25_robi2": kin.TCP_EGP25_ROBI2}.get(greifer, kin.TCP_VAKUUM)
    # Stift 20 mm hoch: Backen am oberen Ende, lange Backen mit der Spitze knapp ueber
    # dem Tisch, Sauger auf der Oberseite
    greif_z = {"egp40": 0.012, "egp25": 0.0155, "egp25_robi2": 0.001}.get(greifer, 0.02)
    r, winkel = 0.35, math.radians(60)
    pick = (r, 0.0)
    place = (r * math.cos(winkel), r * math.sin(winkel))

    def p(xy, z):
        return kin.ik_nach_unten(xy[0], xy[1], z, tool)

    return [
        (HOME, 1.5, False),
        (p(pick, greif_z + 0.10), 2.0, False),
        (p(pick, greif_z), 1.5, False),
        (p(pick, greif_z), 0.6, True),
        (p(pick, greif_z + 0.10), 1.2, True),
        (p(place, greif_z + 0.10), 2.0, True),
        (p(place, greif_z + 0.002), 1.5, True),
        (p(place, greif_z + 0.002), 0.6, False),
        (p(place, greif_z + 0.10), 1.2, False),
        (HOME, 2.0, False),
        (p(place, greif_z + 0.10), 2.0, False),
        (p(place, greif_z + 0.002), 1.5, False),
        (p(place, greif_z + 0.002), 0.6, True),
        (p(place, greif_z + 0.10), 1.2, True),
        (p(pick, greif_z + 0.10), 2.0, True),
        (p(pick, greif_z), 1.5, True),
        (p(pick, greif_z), 0.6, False),
        (p(pick, greif_z + 0.10), 1.2, False),
    ]


class Bahn:
    def __init__(self, schritte):
        self.schritte = schritte
        self.gesamt = sum(d for _q, d, _g in schritte)
        self.start = time.monotonic()

    def jetzt(self):
        t = (time.monotonic() - self.start) % self.gesamt
        vorher = self.schritte[-1][0]
        for q, dauer, zu in self.schritte:
            if t <= dauer:
                s = t / dauer
                s = s * s * (3 - 2 * s)   # sanft anfahren und abbremsen
                return [a + (b - a) * s for a, b in zip(vorher, q)], zu
            t -= dauer
            vorher = q
        return vorher, False


def status_nachricht(cnt, q, dout):
    werte = " ".join("%.3f" % v for v in q + [0.0] * 10)
    pos, _r = kin.fk(q)
    cart = "%.2f %.2f %.2f 0.00 180.00 0.00" % tuple(v * 1000 for v in pos)
    return ("CRISTART %d STATUS MODE joint POSJOINTSETPOINT %s POSJOINTCURRENT %s "
            "POSCARTROBOT %s POSCARTPLATFORM 0.0 0.0 0.0 OVERRIDE 40.0 DIN 0 DOUT %x "
            "ESTOP 3 SUPPLY 24000 CURRENTALL 0 CURRENTJOINTS %s ERROR no_error %s "
            "KINSTATE 0 OPMODE -1 CARTSPEED 0.0 CRIEND"
            % (cnt, werte, werte, cart, dout, " ".join(["0"] * 16), " ".join(["0"] * 16)))


def dout_maske(greifer, zu):
    """Ausgaenge wie iRC: EGP 25 mit zwei Kanaelen (Bit 30 = DOut31 oeffnen,
    Bit 31 = DOut32 schliessen), Sauger nur DOut32."""
    if greifer.startswith("egp"):
        return (1 << 31) if zu else (1 << 30)
    return (1 << 31) if zu else 0


def bediene(conn, bahn, greifer, takt, log):
    conn.settimeout(0.0)
    cnt = 0
    alive = time.monotonic()
    empfangen = 0
    try:
        while True:
            try:
                daten = conn.recv(4096)
                if not daten:
                    break
                empfangen += daten.count(b"ALIVEJOG")
                if b"ALIVEJOG" in daten:
                    alive = time.monotonic()
            except BlockingIOError:
                pass
            if time.monotonic() - alive > 2.0:
                log("Demo: kein ALIVEJOG seit 2 s, trenne (wie iRC)")
                break
            q, zu = bahn.jetzt()
            cnt = cnt % 9999 + 1
            conn.sendall(status_nachricht(cnt, q, dout_maske(greifer, zu)).encode("ascii"))
            time.sleep(takt)
    except OSError:
        pass
    finally:
        conn.close()
    return empfangen


def serve(port=3920, greifer="egp25", takt=0.05, log=print, stop=None, bereit=None):
    bahn = Bahn(ablauf(greifer))
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    srv.settimeout(0.5)
    if bereit is not None:
        bereit.set()
    log("Demo-CRI-Server auf 127.0.0.1:%d, Greifer %s" % (port, greifer))
    while stop is None or not stop.is_set():
        try:
            conn, addr = srv.accept()
        except socket.timeout:
            continue
        log("Demo: Client verbunden %s:%d" % addr)
        threading.Thread(target=bediene, args=(conn, bahn, greifer, takt, log), daemon=True).start()
    srv.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=3920)
    ap.add_argument("--greifer", choices=["egp40", "egp25", "egp25_robi2", "vakuum"], default="egp40")
    args = ap.parse_args()
    try:
        serve(args.port, args.greifer)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
