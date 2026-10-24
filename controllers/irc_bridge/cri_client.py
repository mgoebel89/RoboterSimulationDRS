"""CRI-Client fuer igus Robot Control (iRC) und die ReBeL-Steuerung.

Protokoll laut "Robot Interface CRI V17" (Commonplace Robotics):
- iRC ist der Server, Standard-Port 3920 (TCP).
- Jede Nachricht: "CRISTART <zaehler> <TYP> ... CRIEND".
- Der Client muss mindestens alle 2 s eine ALIVEJOG-Nachricht schicken,
  sonst trennt iRC die Verbindung.
- iRC schickt periodisch STATUS (Gelenkwinkel, kartesische Lage, DIN/DOUT).

Dieses Modul hat keine Abhaengigkeit zu Webots und wird auch von den
Werkzeugen in tools/ benutzt.
"""

import re
import socket
import threading
import time

KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")


def parse_int_mask(token):
    """DIN/DOUT-Wert als Bitmaske lesen.

    iRC V15 sendet hexadezimal ohne Praefix: DOut31 (Bit 30) kommt als
    "40000000" (am 22.10.2026 gegen die echte iRC-Steuerung geprueft)."""
    if token is None:
        return 0
    try:
        return int(token, 16)
    except ValueError:
        return 0


def parse_status(tokens):
    """Tokens einer STATUS-Nachricht (ohne CRISTART/Zaehler/STATUS/CRIEND)
    in ein Woerterbuch {SCHLUESSEL: [werte...]} zerlegen."""
    fields = {}
    current = None
    for tok in tokens:
        if KEY_RE.match(tok):
            current = tok
            fields[current] = []
        elif current is not None:
            fields[current].append(tok)
    return fields


def _floats(values, n=None):
    out = []
    for v in values:
        try:
            out.append(float(v))
        except ValueError:
            break
    if n is not None:
        out = (out + [0.0] * n)[:n]
    return out


class RobotState:
    """Ein ausgewerteter STATUS-Datensatz."""

    def __init__(self, fields, raw):
        self.raw = raw
        self.fields = fields
        self.mode = (fields.get("MODE") or ["?"])[0]
        self.joints_setpoint = _floats(fields.get("POSJOINTSETPOINT", []), 16)
        self.joints_current = _floats(fields.get("POSJOINTCURRENT", []), 16)
        self.cart = _floats(fields.get("POSCARTROBOT", []), 6)
        self.din_raw = (fields.get("DIN") or ["0"])[0]
        self.dout_raw = (fields.get("DOUT") or ["0"])[0]
        self.din = parse_int_mask(self.din_raw)
        self.dout = parse_int_mask(self.dout_raw)
        self.kinstate = (fields.get("KINSTATE") or ["?"])[0]
        self.received = time.monotonic()

    def joints(self, source="current"):
        """Die 6 Armgelenke in Grad."""
        values = self.joints_setpoint if source == "setpoint" else self.joints_current
        return values[:6]

    def dout_bit(self, bit):
        return bool((self.dout >> bit) & 1)


class CriClient:
    """Haelt im Hintergrund die Verbindung zu iRC und merkt sich den
    letzten STATUS. Thread-sicher genug fuer einen Leser (Webots-Schleife)."""

    def __init__(self, host="127.0.0.1", ports=(3920,), passive=True,
                 alive_interval=0.2, log=print):
        self.host = host
        self.ports = list(ports)
        self.passive = passive
        self.alive_interval = alive_interval
        self.log = log
        self.state = None
        self.runstate = None
        self.connected = False
        self.connected_port = None
        self.status_period = 0.05
        self.messages = []        # sonstige Antworten (CMDACK, INFO, ...) fuer Werkzeuge
        self._sock = None
        self._send_lock = threading.Lock()
        self._counter = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    # -- oeffentlich ---------------------------------------------------
    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def send(self, body):
        """Befehl an iRC schicken, z.B. "CMD Enable". Gibt den Zaehler zurueck."""
        if self._sock is None:
            raise OSError("nicht verbunden")
        return self._send(self._sock, body)

    # -- intern --------------------------------------------------------
    def _next_count(self):
        self._counter = self._counter % 9999 + 1
        return self._counter

    def _send(self, sock, body):
        with self._send_lock:
            cnt = self._next_count()
            sock.sendall(("CRISTART %d %s CRIEND" % (cnt, body)).encode("ascii"))
        return cnt

    def _run(self):
        port_index = 0
        while not self._stop.is_set():
            port = self.ports[port_index % len(self.ports)]
            try:
                sock = socket.create_connection((self.host, port), timeout=2.0)
            except OSError:
                port_index += 1
                self._stop.wait(1.0)
                continue
            self.connected_port = port
            self._sock = sock
            self.connected = True
            self.log("CRI: verbunden mit %s:%d" % (self.host, port))
            try:
                self._session(sock)
            except OSError as exc:
                self.log("CRI: Verbindung unterbrochen (%s)" % exc)
            finally:
                self.connected = False
                self._sock = None
                try:
                    sock.close()
                except OSError:
                    pass
            # iRC ist laut Doku 1 s nach dem Trennen wieder bereit
            self._stop.wait(1.5)

    def _session(self, sock):
        sock.settimeout(0.05)
        if self.passive:
            # nur mithoeren, iRC-Oberflaeche behaelt die Kontrolle
            self._send(sock, "CMD SetActive false")
        buf = ""
        last_alive = 0.0
        last_status = None
        while not self._stop.is_set():
            now = time.monotonic()
            if now - last_alive >= self.alive_interval:
                self._send(sock, "ALIVEJOG 0.0 0.0 0.0 0.0 0.0 0.0 0.0 0.0 0.0")
                last_alive = now
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                raise OSError("Gegenstelle hat geschlossen")
            buf += chunk.decode("ascii", errors="replace")
            while "CRIEND" in buf:
                msg, buf = buf.split("CRIEND", 1)
                start = msg.find("CRISTART")
                if start < 0:
                    continue
                tokens = msg[start:].split()
                if len(tokens) < 3:
                    continue
                kind = tokens[2]
                if kind == "STATUS":
                    state = RobotState(parse_status(tokens[3:]), msg[start:])
                    if last_status is not None:
                        dt = state.received - last_status
                        if 0.001 < dt < 1.0:
                            self.status_period = 0.8 * self.status_period + 0.2 * dt
                    last_status = state.received
                    self.state = state
                elif kind == "RUNSTATE":
                    self.runstate = tokens[3:]
                elif kind not in ("GSIG", "OPINFO", "LOGMSG", "SUPPLY"):
                    self.messages.append(" ".join(tokens[2:]))
                    del self.messages[:-200]
