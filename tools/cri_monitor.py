"""CRI-Monitor: zeigt, was iRC ueber die CRI-Schnittstelle sendet.

Erster Test, ganz ohne Webots:
  1. iRC starten, CRI-Server aktivieren (siehe README).
  2. python tools/cri_monitor.py
  3. In iRC Achsen bewegen und DOut31/DOut32 schalten.
Der Monitor zeigt Gelenkwinkel, TCP-Lage und jede Aenderung der Ausgaenge
mit Bitnummer an. So sieht man sofort, welcher Ausgang welches Bit ist.

  python tools/cri_monitor.py --host 192.168.3.11   # echter ReBeL
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "controllers", "irc_bridge"))
from cri_client import CriClient  # noqa: E402


def bits(mask):
    return [b for b in range(64) if (mask >> b) & 1]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--ports", default="3920,3921")
    ap.add_argument("--roh", action="store_true", help="erste STATUS-Nachricht komplett ausgeben")
    args = ap.parse_args()

    cli = CriClient(args.host, [int(p) for p in args.ports.split(",")]).start()
    print("Verbinde mit %s (Ports %s) ... Abbrechen mit Strg+C" % (args.host, args.ports))
    letzte_dout = None
    letzte_ausgabe = 0.0
    roh_gezeigt = False
    try:
        while True:
            time.sleep(0.05)
            st = cli.state
            if st is None:
                continue
            if args.roh and not roh_gezeigt:
                print("ROH:", st.raw)
                roh_gezeigt = True
            if st.dout != letzte_dout:
                if letzte_dout is not None:
                    for b in bits(st.dout ^ letzte_dout):
                        print("  DOUT Bit %2d  (iRC-Anzeige DOut%d)  -> %s"
                              % (b, b + 1, "EIN" if (st.dout >> b) & 1 else "AUS"))
                print("  DOUT roh=%s  aktive Bits=%s" % (st.dout_raw, bits(st.dout)))
                letzte_dout = st.dout
            if time.monotonic() - letzte_ausgabe > 1.0:
                letzte_ausgabe = time.monotonic()
                print("A1..A6 ist [%s] Grad | TCP %.1f %.1f %.1f mm | Takt %.0f ms | KINSTATE %s"
                      % (" ".join("%7.2f" % v for v in st.joints()), st.cart[0], st.cart[1], st.cart[2],
                         cli.status_period * 1000, st.kinstate))
    except KeyboardInterrupt:
        cli.stop()


if __name__ == "__main__":
    main()
