"""Verbindung zur App-Schnittstelle von iRC (gRPC, Port 5000) als App "WebotsSim": Bedienfeld fuellen, Klicks und
Eingaben empfangen, globale Signale (GSig) setzen und lesen.

Wie der Beispiel-Client von Commonplace Robotics (GitHub RobotControlApps, control_python/AppClient.py): ein offener
Strom RecieveActions, auf dem die App ihre AppActions schickt (Bedienfeld aendern, Zustaende anfragen) und die
Steuerung Events zurueckgibt (Klicks, Eingaben, App-Funktionen). Das Bedienfeld selbst steht in der ui.xml der App
(deploy/irc_app/.../WebotsSim/ui.xml); seine Struktur ist fest, aendern lassen sich Texte, Zahlen, Auswahllisten,
Haken und Sichtbarkeit.

Die Nachrichten kommen aus tools/irc_api/robotcontrolapp.pb (wie in probetest_sps.py), damit es mit jeder
protobuf-Version im Container geht. Gebraucht werden grpcio und protobuf.
"""

import queue
import threading
import time

from probetest_sps import _api_klassen

DIENST = "/robotcontrolapp.RobotControlApp/"


class IrcApp:
    """Eine Verbindung als App. ereignis(liste_von_dicts) wird fuer jede Bedienfeld-Aenderung aus iRC aufgerufen,
    jeder Eintrag z.B. {"element_name": "sps_lesen", "state": {"button_state": "CLICKED"}}."""

    def __init__(self, ziel, port=5000, app="WebotsSim", zeit=5.0, ereignis=None, kanal=None):
        from google.protobuf import json_format
        self._json = json_format
        self.k, self.pool = _api_klassen()
        self.app, self.zeit = app, zeit
        self.ziel = "%s:%d" % (ziel, port)
        if kanal is None:
            import grpc
            kanal = grpc.insecure_channel(self.ziel)
        self.kanal = kanal
        self.ereignis = ereignis or (lambda liste: None)
        self.ereignisse = []          # alle empfangenen Bedienfeld-Ereignisse mit Zeit (fuer Berichte)
        self.getrennt_grund = None
        self._schlange = None
        self._leser = None
        self._gesammelt = []          # gesammelte Bedienfeld-Aenderungen bis senden()

    # --- einfache Aufrufe ---

    def aufruf(self, name, **felder):
        m = self.pool.FindServiceByName("robotcontrolapp.RobotControlApp").FindMethodByName(name)
        rein, raus = self.k[m.input_type.name], self.k[m.output_type.name]
        if "app_name" in rein.DESCRIPTOR.fields_by_name:
            felder.setdefault("app_name", self.app)
        anfrage = self._json.ParseDict(felder, rein())
        art = self.kanal.unary_stream if m.server_streaming else self.kanal.unary_unary
        f = art(DIENST + name, request_serializer=lambda x: x.SerializeToString(), response_deserializer=raus.FromString)
        antwort = f(anfrage, timeout=self.zeit)
        return [self.als_dict(a) for a in antwort] if m.server_streaming else self.als_dict(antwort)

    def als_dict(self, nachricht):
        return self._json.MessageToDict(nachricht, preserving_proto_field_name=True)

    def anmelden(self):
        """Versionsabgleich wie AppClient.SendCapabilities."""
        return self.aufruf("SetCapabilities", api_version_major=1, api_version_minor=0, api_version_patch=0)

    # --- globale Signale ---

    def gsigs_setzen(self, signale):
        """{nummer: True/False}; setzt nur diese, alle anderen bleiben. Bei GSigs gilt target_state (iRC V15.0.2)."""
        liste = [{"id": nr, "state": "HIGH" if an else "LOW", "target_state": "HIGH" if an else "LOW"}
                 for nr, an in sorted(signale.items())]
        return self.aufruf("SetIOState", GSigs=liste)

    def gsigs_an(self):
        """Nummern der globalen Signale, die die Steuerung gerade als an meldet."""
        zustand = self.aufruf("GetRobotState")
        return sorted(int(e.get("id", 0)) for e in zustand.get("GSigs", []) if e.get("state") == "HIGH")

    # --- Strom fuer Bedienfeld und App-Funktionen ---

    def verbinden(self):
        """Strom RecieveActions oeffnen (erste Nachricht: leere AppAction mit dem App-Namen, wie AppClient.Connect)."""
        self._schlange = queue.Queue()
        self._schlange.put(self._aktion({}))
        f = self.kanal.stream_stream(DIENST + "RecieveActions", request_serializer=lambda x: x.SerializeToString(),
                                     response_deserializer=self.k["Event"].FromString)
        antworten = f(iter(self._schlange.get, None))
        self._leser = threading.Thread(target=self._lesen, args=(antworten,), daemon=True)
        self._leser.start()

    def trennen(self):
        if self._schlange is not None:
            self._schlange.put(None)
            self._schlange = None

    def verbunden(self):
        return self._schlange is not None and self.getrennt_grund is None

    def _aktion(self, daten):
        daten = dict(daten, app_name=self.app)
        return self._json.ParseDict(daten, self.k["AppAction"]())

    def _lesen(self, antworten):
        try:
            for event in antworten:
                d = self.als_dict(event)
                if d.get("ui_updates"):
                    self.ereignisse.append({"zeit": time.strftime("%H:%M:%S"), "aenderungen": d["ui_updates"]})
                    self.ereignis(d["ui_updates"])
                if "disconnect_request" in d:
                    self.getrennt_grund = d["disconnect_request"].get("reason") or "Steuerung trennt"
                    return
        except Exception as e:   # noqa: BLE001 - grpc.RpcError beim Trennen oder Netzausfall
            if self._schlange is not None:
                self.getrennt_grund = str(getattr(e, "details", lambda: e)())

    # --- Bedienfeld aendern (gesammelt, dann eine Nachricht wie AppClient.SendQueuedUIUpdates) ---

    def text(self, name, text):
        self._gesammelt.append({"element_name": name, "state": {"textfield_state": {"current_text": str(text)}}})

    def zahl(self, name, wert):
        self._gesammelt.append({"element_name": name, "state": {"numberfield_state": {"current_number": float(wert)}}})

    def liste(self, name, eintraege, gewaehlt):
        self._gesammelt.append({"element_name": name, "state": {"dropdown_state": {"options": list(eintraege),
                                                                                   "selected_option": gewaehlt}}})

    def haken(self, name, an):
        self._gesammelt.append({"element_name": name, "state": {"checkbox_state": "CHECKED" if an else "UNCHECKED"}})

    def sichtbar(self, name, an):
        self._gesammelt.append({"element_name": name, "is_visible": bool(an)})

    def senden(self):
        if self._gesammelt and self._schlange is not None:
            self._schlange.put(self._aktion({"ui_changes": self._gesammelt}))
        self._gesammelt = []

    def zustaende_anfragen(self, namen):
        """Die Steuerung antwortet mit Ereignissen fuer die Elemente, die sich seit dem Start geaendert haben."""
        if self._schlange is not None:
            self._schlange.put(self._aktion({"request_ui_state": list(namen)}))


def wert_aus(aenderung):
    """Der Inhalt einer Bedienfeld-Aenderung als einfacher Wert (Text, Zahl, Auswahl, True/False fuer Knopf/Haken)."""
    s = aenderung.get("state", {})
    if "button_state" in s:
        return s["button_state"] == "CLICKED"
    if "checkbox_state" in s:
        return s["checkbox_state"] == "CHECKED"
    if "dropdown_state" in s:
        return s["dropdown_state"].get("selected_option", "")
    if "textfield_state" in s:
        return s["textfield_state"].get("current_text", "")
    if "numberfield_state" in s:
        return s["numberfield_state"].get("current_number", 0.0)
    return None
