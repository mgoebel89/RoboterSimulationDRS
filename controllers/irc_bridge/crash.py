"""Crash-Stopp (Stufe K2): eine Kollision haelt das laufende iRC-Programm an wie ein echter Crash.

Ablauf (CrashZustand, ohne Webots und ohne Netz testbar):
  frei        Kollision -> Stopp anfordern (pruefe)
  pruefe      die App-Schnittstelle fragt den Programmzustand ab:
                Programm lief      -> angehalten, Zustand crash
                kein Programm      -> Roboter wurde von Hand verfahren (Jog): nur melden, wieder frei
                Schnittstelle weg  -> nur melden mit Hinweis, wieder frei
  crash       bleibt stehen, bis der Schueler "Kollision quittieren" drueckt; startet er das Programm vorher
              wieder, wird es erneut angehalten
  freifahren  nach dem Quittieren: Beruehrungen mit den Teilen des Crashs zaehlen nicht, bis mehrere Pruefungen
              hintereinander nichts mehr finden (Roboter frei gefahren); eine Kollision mit etwas anderem stoppt wieder

Angehalten wird ueber die App-Schnittstelle von iRC (gRPC Port 5000, App "WebotsSim" wie bei den Tastern):
GetMotionState und SetMotionInterpolator mit runstate NOT_RUNNING. Die Nachrichten (robotcontrolapp.proto,
MIT-Lizenz Commonplace Robotics) werden wie in ea.py von Hand kodiert.
"""

import threading
import time

import ea

GET_MOTION = "/robotcontrolapp.RobotControlApp/GetMotionState"
SET_MOTION = "/robotcontrolapp.RobotControlApp/SetMotionInterpolator"
NOT_RUNNING, RUNNING, PAUSED = 0, 1, 2
SPERRE_TAKT_S = 0.5      # so oft wird im Zustand crash nachgesehen, ob das Programm wieder laeuft
NACHFRAGE_S = 1.0        # Beruehrung beim Verfahren von Hand: so lange nicht erneut nachfragen
NACHFRAGE_FEHLER_S = 5.0  # Schnittstelle nicht erreichbar: so lange nicht erneut versuchen
FREI_NACH = 5            # so viele Pruefungen hintereinander ohne Treffer = frei gefahren (Beruehrung an der Grenze
                         # des Sicherheitsabstands flackert sonst)


# --- Protobuf von Hand ---

def bewegung_anfrage(app):
    """GetMotionStateRequest {app_name = 1}"""
    return ea._feld(1, 2, app.encode("utf-8"))


def stopp_anfrage(app):
    """MotionInterpolatorRequest {app_name = 1; optional RunState runstate = 2} mit NOT_RUNNING. Das Feld ist
    optional: auch der Wert 0 muss mitgeschickt werden, sonst aendert iRC nichts."""
    return ea._feld(1, 2, app.encode("utf-8")) + ea._feld(2, 0, NOT_RUNNING)


def _felder(daten):
    """(Nummer, Wert) je Feld einer Protobuf-Nachricht; Wert ist Zahl (varint) oder bytes."""
    i, n = 0, len(daten)

    def varint():
        nonlocal i
        wert, schieben = 0, 0
        while True:
            if i >= n:
                raise ValueError("Nachricht zu kurz")
            b = daten[i]
            i += 1
            wert |= (b & 0x7F) << schieben
            if not b & 0x80:
                return wert
            schieben += 7
    while i < n:
        kopf = varint()
        nr, art = kopf >> 3, kopf & 7
        if art == 0:
            yield nr, varint()
        elif art == 2:
            laenge = varint()
            if i + laenge > n:
                raise ValueError("Nachricht zu kurz")
            yield nr, bytes(daten[i:i + laenge])
            i += laenge
        elif art in (1, 5):   # feste 8 bzw. 4 Bytes (kommen in MotionState nicht vor)
            i += 8 if art == 1 else 4
            yield nr, None
        else:
            raise ValueError("unbekannte Feldart %d" % art)


def _int32(wert):
    """Negative int32 kommen als 64-Bit-Zweierkomplement."""
    return wert - (1 << 64) if wert >= 1 << 63 else wert


def bewegungszustand(daten):
    """MotionState -> {"quelle", "runstate", "laeuft", "programm", "befehl"}.
    MotionState {MotionSource current_source = 1 (JOG 0, IPO 1, ...); InterpolatorState motion_ipo = 2}
    InterpolatorState {runstate = 1; main_program_name = 3; current_program_name = 4; current_command_idx = 7}
    "programm" ist das Hauptprogramm (fehlt es, das gerade laufende Unterprogramm)."""
    z = {"quelle": 0, "runstate": NOT_RUNNING, "programm": "", "befehl": 0}
    for nr, wert in _felder(daten):
        if nr == 1 and isinstance(wert, int):
            z["quelle"] = wert
        elif nr == 2 and isinstance(wert, bytes):
            for unr, uwert in _felder(wert):
                if unr == 1 and isinstance(uwert, int):
                    z["runstate"] = uwert
                elif unr in (3, 4) and isinstance(uwert, bytes) and (unr == 3 or not z["programm"]):   # Hauptprogramm
                    z["programm"] = uwert.decode("utf-8", "replace")
                elif unr == 7 and isinstance(uwert, int):
                    z["befehl"] = _int32(uwert)
    z["laeuft"] = z["runstate"] in (RUNNING, PAUSED)   # pausiert zaehlt: es geht sonst nach dem Crash einfach weiter
    return z


# --- Zustand (Logik) ---

class CrashZustand:
    """Entscheidet, wann angehalten wird; die Verbindung zu iRC macht ProgrammStopp."""

    def __init__(self, reaktion="stopp", log=print):
        self.stoppen = reaktion == "stopp"
        self.log = log
        self.zustand = "frei"
        self.ausloeser = None       # Treffer, der den Crash ausgeloest hat (bzw. gerade geprueft wird)
        self.programm = ""
        self.meldung = None         # Zusatz zur Kollisionsmeldung, z.B. "von Hand verfahren: nur Meldung"
        self.toleriert = set()      # beim Freifahren: Beruehrungen mit diesen Teilen zaehlen nicht
        self.nicht_vor = 0.0        # vorher nicht erneut nachfragen (Jog bzw. Schnittstelle weg)
        self.wieder_gestoppt = 0
        self.frei_pruefungen = 0    # beim Freifahren: Pruefungen ohne Treffer hintereinander

    def kollision(self, treffer, jetzt):
        """Ergebnis einer Pruefung (Liste, auch leer). True = jetzt Stopp anfordern."""
        if not self.stoppen:
            return False
        if self.zustand == "freifahren":
            self.frei_pruefungen = 0 if treffer else self.frei_pruefungen + 1
            if self.frei_pruefungen >= FREI_NACH:
                self.zustand, self.toleriert = "frei", set()
                self.log("Kollision: Roboter frei gefahren, Pruefung wieder scharf")
                return False
            treffer = [t for t in treffer if t["b"] not in self.toleriert]
        if self.zustand not in ("frei", "freifahren") or not treffer or jetzt < self.nicht_vor:
            return False
        self.zustand, self.ausloeser, self.meldung = "pruefe", dict(treffer[0]), None
        return True

    def ergebnis(self, art, jetzt, programm="", text=None):
        """Antwort von ProgrammStopp: "gestoppt", "kein_programm", "fehler" (text = Grund) oder
        "wieder_gestoppt" (im Zustand crash erneut gestartet und angehalten)."""
        if art == "wieder_gestoppt":
            self.wieder_gestoppt += 1
            self.meldung = "Programm erneut angehalten: erst die Kollision quittieren"
            self.log("CRASH: Programm %s erneut gestartet und angehalten (noch nicht quittiert)" % (programm or "?"))
            return
        if self.zustand != "pruefe":
            return
        a = self.ausloeser or {"a": "?", "b": "?"}
        if art == "gestoppt":
            self.zustand, self.programm, self.meldung = "crash", programm, None
            self.log("CRASH: %s mit %s, Programm %s angehalten" % (a["a"], a["b"], programm or "?"))
        elif art == "kein_programm":
            self.zustand, self.nicht_vor = "frei", jetzt + NACHFRAGE_S
            self.meldung = "kein Programm aktiv (von Hand verfahren): nur Meldung"
        else:
            meldung = "Programm konnte nicht angehalten werden: %s" % (text or "iRC-App nicht erreichbar")
            if meldung != self.meldung:   # nur einmal ins Log, nicht alle 5 s
                self.log("Kollision: %s" % meldung)
            self.zustand, self.nicht_vor, self.meldung = "frei", jetzt + NACHFRAGE_FEHLER_S, meldung

    def quittieren(self, treffer):
        """Knopf "Kollision quittieren". treffer: Stand der letzten Pruefung. False, wenn kein Crash ansteht."""
        if self.zustand != "crash":
            return False
        self.toleriert = {self.ausloeser["b"]} | {t["b"] for t in treffer}
        self.zustand, self.meldung, self.wieder_gestoppt, self.frei_pruefungen = "freifahren", None, 0, 0
        self.log("Kollision quittiert, frei fahren erlaubt (%s)" % ", ".join(sorted(self.toleriert)))
        return True

    @property
    def gesperrt(self):
        return self.zustand == "crash"

    def status(self, in_mm):
        """Fuer die Statusdatei; in_mm rechnet einen Weltpunkt in Basis-Koordinaten (mm) um."""
        if self.zustand == "frei" and not self.meldung:
            return None
        d = {"zustand": self.zustand, "meldung": self.meldung, "programm": self.programm or None}
        if self.zustand in ("crash", "pruefe", "freifahren") and self.ausloeser:
            p = self.ausloeser["punkt"]
            d.update(a=self.ausloeser["a"], b=self.ausloeser["b"], punkt=[round(v, 4) for v in p],
                     punkt_mm=[round(v, 1) for v in in_mm(p)], text=self.ausloeser.get("text"))
        return d


# --- Verbindung zu iRC ---

class ProgrammStopp:
    """Fragt im Hintergrund den Programmzustand ab und haelt an (blockiert die Simulation nie).
    anhalten(): einmal pruefen und ggf. anhalten; sperren(True): im Crash immer wieder nachsehen.
    Ergebnisse holt der Controller mit ergebnisse() ab."""

    def __init__(self, host, port=5000, app="WebotsSim", log=print, kanal_fn=None):
        self.ziel = "%s:%d" % (host, port)
        self.app = app
        self.log = log
        self.kanal_fn = kanal_fn   # fuer Tests: liefert aufruf(methode, bytes) -> bytes
        self._auftrag = False
        self._gesperrt = False
        self._liste = []
        self._schloss = threading.Lock()
        self._neu = threading.Event()
        self._stopp = threading.Event()
        self._faden = threading.Thread(target=self._lauf, daemon=True)

    def start(self):
        self._faden.start()
        return self

    def anhalten(self):
        self._auftrag = True
        self._neu.set()

    def sperren(self, an):
        self._gesperrt = an
        self._neu.set()

    def ergebnisse(self):
        with self._schloss:
            liste, self._liste = self._liste, []
        return liste

    def beenden(self):
        self._stopp.set()
        self._neu.set()

    def _melden(self, *eintrag):
        with self._schloss:
            self._liste.append(eintrag)

    def _aufrufer(self):
        if self.kanal_fn:
            return self.kanal_fn()
        import grpc   # erst hier: ohne Crash-Stopp braucht es kein gRPC
        kanal = grpc.insecure_channel(self.ziel)
        methoden = {m: kanal.unary_unary(m, request_serializer=lambda b: b, response_deserializer=lambda b: b)
                    for m in (GET_MOTION, SET_MOTION)}
        return lambda methode, daten: methoden[methode](daten, timeout=2)

    def einmal(self, aufruf, gesperrt):
        """Ein Durchgang: Zustand lesen, laeuft ein Programm -> anhalten. Gibt (art, programm) zurueck."""
        z = bewegungszustand(aufruf(GET_MOTION, bewegung_anfrage(self.app)))
        if not z["laeuft"]:
            return ("kein_programm", z["programm"])
        aufruf(SET_MOTION, stopp_anfrage(self.app))
        return ("wieder_gestoppt" if gesperrt else "gestoppt", z["programm"])

    def _lauf(self):
        try:   # gRPC schon jetzt laden und den Kanal anlegen: sonst kostet der erste Stopp 1 bis 2 s
            aufruf = self._aufrufer()
        except Exception:   # noqa: BLE001 - fehlt gRPC, meldet es der erste Auftrag
            aufruf = None
        while not self._stopp.is_set():
            self._neu.wait(SPERRE_TAKT_S if self._gesperrt else None)
            self._neu.clear()
            if self._stopp.is_set():
                break
            auftrag, self._auftrag = self._auftrag, False
            if not auftrag and not self._gesperrt:
                continue
            try:
                if aufruf is None:
                    aufruf = self._aufrufer()
                art, programm = self.einmal(aufruf, self._gesperrt and not auftrag)
                if auftrag or art == "wieder_gestoppt":
                    self._melden(art, programm, None)
            except ImportError:
                if auftrag:
                    self._melden("fehler", "", "gRPC fehlt im Container (python3-grpcio)")
            except Exception as e:   # grpc.RpcError und Netzfehler
                text = ea.fehlertext(e, self.app)
                if auftrag:
                    self._melden("fehler", "", text)
                time.sleep(0.5)
