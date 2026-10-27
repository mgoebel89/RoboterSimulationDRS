"""Stufe 0 der SPS-Kopplung: Bedienfeld der App WebotsSim 1.1 in iRC pruefen, bevor der eigentliche Dienst gebaut wird.

Vorher in iRC die Pruefversion der App installieren ("Update App..."): deploy/irc_app/pruefung/WebotsSim.zip.
Der Schritt verbindet sich als App mit iRC am Schueler-PC (oder am echten ReBeL), fuellt das Bedienfeld, fragt die
Lehrkraft, was sie in iRC sieht, schreibt alle Klicks und Eingaben mit und ermittelt die Nummerierung der globalen
Signale (GSig) in beide Richtungen. Geprueft wird:

  1  erscheint das Bedienfeld, kommen Texte, Auswahllisten und Ausblenden an (Darstellung, Umlaute)
  2  kommen Klicks und Eingaben beim Container an, reagiert das Bedienfeld darauf (Statuszeile, Zeilen einblenden)
  3  liefert die Steuerung den Zustand der Felder auf Anfrage (fuer das Wiederverbinden)
  4  GSig-Nummern: Schnittstelle -> Anzeige in iRC und umgekehrt
  5  was das Bedienfeld zeigt, wenn der Container nicht verbunden ist

Aufruf ueber den Messkoffer (rebel-sim-messung, Schritt 9) oder direkt:
  /opt/rebel-sim-probe/bin/python3 tools/bedienfeld_pruefen.py --ziel 192.168.7.21 [--sps 192.168.7.50]
"""

import argparse
import sys
import time

ZEILEN = 8
FREI = "- frei -"
BEISPIELE = ["Start_Taster (Bool)", "Band_belegt (Bool)", "Not_Halt_OK (Bool)", "Teil_fertig (Bool)",
             "Roboter_bereit (Bool)", "ROB_IN (Byte)", "ROB_OUT (Byte)"]
ELEMENTE = ["sps_lesen", "band_lesen", "sps_verbinden", "band_verbinden", "sps_trennen", "band_alles_aus",
            "sps_adresse", "sps_alle_zeilen", "ein_1_var", "ein_2_gsig", "aus_1_var", "aus_1_gsig"]


def zeilen_namen(art, i):
    return ["%s_%d_var" % (art, i), "%s_%d_gsig" % (art, i), "%s_%d_zustand" % (art, i)]


class Pruefung:
    def __init__(self, ziel, sps=None, eingabe=input, ausgabe=print, app_fabrik=None, warte=time.sleep, port=5000):
        import probetest_sps
        self.b = probetest_sps.Bericht("iRC-Bedienfeld an %s" % ziel)
        self.ziel, self.sps, self.port = ziel, sps, port
        self.eingabe, self.ausgabe, self.warte = eingabe, ausgabe, warte
        self.app_fabrik = app_fabrik
        self.app = None
        self.gesehen = {}          # Elementname -> letzter Wert
        self.reaktionen = 0        # wie oft der Container auf einen Klick geantwortet hat

    # --- Fragen an die Lehrkraft ---

    def frage(self, was, text):
        a = self.eingabe("%s (j/n) " % text).strip().lower()
        if a in ("j", "ja", "y"):
            self.b.ok(was, "ja")
            return True
        notiz = self.eingabe("  Was ist anders? (Enter = nichts) ").strip()
        self.b.warnung(was, "nein" + (": " + notiz if notiz else ""))
        return False

    def notiz(self, was, text):
        n = self.eingabe(text + " (Enter = nichts) ").strip()
        if n:
            self.b.info(was, n)
        return n

    # --- Reaktion auf Klicks und Eingaben aus iRC ---

    def bei_ereignis(self, aenderungen):
        from irc_app import wert_aus
        for a in aenderungen:
            name, wert = a.get("element_name", "?"), wert_aus(a)
            self.gesehen[name] = wert
            self.ausgabe("       Ereignis aus iRC: %s = %r" % (name, wert))
            if self.app is None:
                continue
            uhr = time.strftime("%H:%M:%S")
            if name in ("sps_lesen", "band_lesen") and wert:
                self.app.text("sps_status", "Klick angekommen (%s): Variablen lesen" % uhr)
            elif name in ("sps_verbinden", "band_verbinden", "sps_trennen", "band_alles_aus"):
                self.app.text("sps_status", "Klick angekommen (%s): %s" % (uhr, name))
            elif name == "sps_alle_zeilen":
                self.zeilen_zeigen(ZEILEN if wert else 4)
                self.app.text("sps_status", "Haken angekommen (%s): %s Zeilen" % (uhr, ZEILEN if wert else 4))
            elif name == "sps_adresse":
                self.app.text("sps_status", "Adresse angekommen (%s): %s" % (uhr, wert))
            elif name.endswith("_gsig"):
                self.app.text(name.replace("_gsig", "_zustand"), "-> GSig %d" % int(wert or 0))
            elif name.endswith("_var"):
                self.app.text("sps_status", "Auswahl angekommen (%s): %s" % (uhr, wert))
            else:
                continue
            self.reaktionen += 1
            self.app.senden()

    def zeilen_zeigen(self, anzahl):
        for art in ("ein", "aus"):
            for i in range(1, ZEILEN + 1):
                for name in zeilen_namen(art, i):
                    self.app.sichtbar(name, i <= anzahl)

    # --- Ablauf ---

    def variablen(self):
        if not self.sps:
            self.b.info("Variablen", "ohne SPS: Beispielnamen")
            return BEISPIELE
        try:
            import sps_opcua
            liste = sps_opcua.variablen_lesen(self.sps)
        except Exception as e:   # noqa: BLE001 - Pruefung geht mit Beispielnamen weiter
            self.b.warnung("Variablen der SPS %s" % self.sps, "%s: %s; weiter mit Beispielnamen" % (type(e).__name__, e))
            return BEISPIELE
        namen = [sps_opcua.anzeige(v) for v in liste]
        self.b.ok("Variablen der SPS %s" % self.sps, "%d freigegeben" % len(liste), liste)
        return namen or BEISPIELE

    def verbinden(self):
        from irc_app import IrcApp
        fabrik = self.app_fabrik or (lambda **k: IrcApp(self.ziel, self.port, **k))
        self.app = fabrik(ereignis=self.bei_ereignis)
        try:
            info = self.app.aufruf("GetSystemInfo")
            self.app.anmelden()
        except Exception as e:   # noqa: BLE001 - grpc.RpcError, Netz
            self.b.fehler("App-Schnittstelle", "%s:%d: %s. Laeuft iRC, ist die App WebotsSim installiert und aktiv?"
                          % (self.ziel, self.port, getattr(e, "details", lambda: e)()))
            return False
        self.b.ok("App-Schnittstelle", "iRC %s, %s" % (info.get("version", "?"), "Simulation" if info.get("is_simulation") else "Steuerung"))
        self.app.verbinden()
        return True

    def fuellen(self, namen):
        a = self.app
        a.text("sps_status", "Pruefung laeuft: ReBeL-Simulation verbunden (Container)")
        a.text("sps_adresse", self.sps or "")
        for art, start in (("ein", 1), ("aus", 11)):
            for i in range(1, ZEILEN + 1):
                a.liste("%s_%d_var" % (art, i), [FREI] + list(namen), namen[0] if i == 1 else FREI)
                a.zahl("%s_%d_gsig" % (art, i), start + i - 1)
                a.text("%s_%d_zustand" % (art, i), "●" if i == 1 else "○")
        a.haken("sps_alle_zeilen", False)
        self.zeilen_zeigen(4)
        a.senden()
        self.warte(1.0)

    def darstellung(self):
        self.ausgabe("\nIn iRC den Reiter \"Webots und SPS\" oeffnen; das Bedienfeld ggf. neben der 3D-Ansicht anzeigen lassen.")
        if not self.frage("Bedienfeld sichtbar", "Ist das Bedienfeld zu sehen und steht oben \"Pruefung laeuft: ...\"?"):
            self.notiz("Bedienfeld", "Was zeigt iRC stattdessen (Fehlermeldung, leerer Reiter, gar nichts)?")
        self.frage("Auswahlliste gefuellt", "Zeile 1 aufklappen: Stehen dort die Variablen zur Auswahl?")
        self.frage("Zeilen ausgeblendet", "Sind je Richtung nur die Zeilen 1 bis 4 zu sehen?")
        self.frage("Umlaute und Zeichen", "Steht der Umlaut-Test richtig da (Pruefung, Umlaute, Pfeil) und in Zeile 1 ein gefuellter Punkt?")
        self.notiz("Breite", "Passen die Variablennamen in die Liste? Kurze Notiz zur Breite des Bedienfelds")

    def ereignisse(self):
        self.ausgabe("\nJetzt in iRC nacheinander (Ereignisse erscheinen hier sofort):\n"
                     "  a) im Bedienfeld \"Variablen lesen\" klicken, dann oben im Band \"Variablen lesen\"\n"
                     "  b) in Zeile 1 (SPS -> Roboter) eine andere Variable waehlen\n"
                     "  c) bei Zeile 2 die GSig-Nummer auf 7 aendern\n"
                     "  d) eine SPS-Adresse eintragen (z.B. 10.0.0.1) und das Feld verlassen\n"
                     "  e) den Haken \"Alle 8 Zeilen zeigen\" setzen\n"
                     "  f) im Band \"Verbinden\" umschalten\n"
                     "Fertig? Enter druecken.")
        self.eingabe("")
        arten = {"Knopf": [n for n in self.gesehen if n.endswith(("_lesen", "_trennen", "_alles_aus"))],
                 "Auswahlliste": [n for n in self.gesehen if n.endswith("_var")],
                 "Zahlenfeld": [n for n in self.gesehen if n.endswith("_gsig")],
                 "Textfeld": [n for n in self.gesehen if n == "sps_adresse"],
                 "Haken/Schalter": [n for n in self.gesehen if n in ("sps_alle_zeilen", "band_verbinden")]}
        fehlt = [a for a, n in arten.items() if not n]
        if not self.gesehen:
            self.b.fehler("Ereignisse aus iRC", "keine angekommen: iRC schickt Klicks nicht an eine von aussen verbundene App "
                                               "(oder die App ist nicht aktiv). Dann Bedienung auf der Schuelerseite.")
        else:
            (self.b.ok if not fehlt else self.b.warnung)(
                "Ereignisse aus iRC", "%d Elemente%s" % (len(self.gesehen), ", ohne Ereignis: " + ", ".join(fehlt) if fehlt else ""),
                {"werte": self.gesehen, "verlauf": self.app.ereignisse})
        if self.gesehen:
            self.frage("Reaktion im Bedienfeld", "Hat die Statuszeile auf die Klicks reagiert (\"... angekommen\") und "
                                                 "erschienen mit dem Haken alle 8 Zeilen?")

    def zustand_anfragen(self):
        vorher = len(self.app.ereignisse)
        self.app.zustaende_anfragen(ELEMENTE)
        self.warte(2.0)
        neu = self.app.ereignisse[vorher:]
        namen = sorted({a.get("element_name") for e in neu for a in e["aenderungen"]})
        (self.b.ok if namen else self.b.warnung)("Zustand auf Anfrage", "%d Elemente gemeldet" % len(namen)
                                                 if namen else "keine Antwort (Zuordnung muss der Container selbst speichern)", namen)

    def gsig_nummern(self, warten_s=60):
        a = self.app
        try:
            a.gsigs_setzen({1: True, 100: True})
            self.warte(0.5)
            gemeldet = a.gsigs_an()
        except Exception as e:   # noqa: BLE001
            self.b.fehler("GSig setzen", str(getattr(e, "details", lambda: e)()))
            return
        self.b.info("GSig 1 und 100 gesetzt", "Steuerung meldet an: %s" % gemeldet)
        anzeige = self.eingabe("In iRC die globalen Signale ansehen (Ein-/Ausgaenge): Welche GSigs sind jetzt an? "
                               "(z.B. \"1 100\", Enter = nicht nachsehbar) ").strip()
        if anzeige:
            (self.b.ok if anzeige.split() == ["1", "100"] else self.b.warnung)(
                "GSig-Nummer Container -> iRC", "gesetzt 1 und 100, iRC zeigt %s" % anzeige)
        a.gsigs_setzen({1: False, 100: False})
        self.ausgabe("\nJetzt in iRC GSig 11 einschalten (Ein-/Ausgaenge, oder eine Programmzeile \"Digitaler Ausgang\","
                     " Kanaltyp GSig, Kanal 11, an\" einmal ausfuehren). Ich warte bis zu %d s." % warten_s)
        vorher, ende, neu = set(a.gsigs_an()), time.time() + warten_s, None
        while time.time() < ende and neu is None:
            self.warte(0.5)
            dazu = sorted(set(a.gsigs_an()) - vorher)
            neu = dazu[0] if dazu else None
        if neu is None:
            self.b.warnung("GSig-Nummer iRC -> Container", "kein neues GSig gesehen")
        else:
            (self.b.ok if neu == 11 else self.b.warnung)("GSig-Nummer iRC -> Container",
                                                         "iRC GSig 11 kommt als Nummer %d an" % neu)
        self.eingabe("GSig 11 in iRC wieder ausschalten, dann Enter. ")

    def ohne_dienst(self):
        self.app.text("sps_status", "Pruefung beendet, Container trennt jetzt")
        self.app.senden()
        self.warte(1.0)
        self.app.trennen()
        self.warte(2.0)
        self.notiz("Ohne Container", "Verbindung getrennt. Was zeigt das Bedienfeld jetzt (letzter Text, Vorgabetext "
                                     "\"Dienst ... nicht verbunden\", ausgegraut, Meldung)?")
        self.frage("Klick ohne Container", "Einmal \"Variablen lesen\" klicken: Bleibt iRC dabei normal bedienbar?")

    def ablauf(self):
        namen = self.variablen()
        if not self.verbinden():
            return self.b
        self.fuellen(namen)
        self.darstellung()
        self.ereignisse()
        self.zustand_anfragen()
        self.gsig_nummern()
        self.ohne_dienst()
        return self.b


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ziel", required=True, help="IP des Schueler-PCs mit iRC oder des echten ReBeL")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--sps", help="IP der SPS, um echte Variablennamen zu zeigen")
    ap.add_argument("--bericht", help="Bericht zusaetzlich als JSON-Datei")
    args = ap.parse_args(argv)
    b = Pruefung(args.ziel, args.sps, port=args.port).ablauf()
    b.schreiben(args.bericht)
    return b.zusammenfassung()


if __name__ == "__main__":
    sys.exit(main())
