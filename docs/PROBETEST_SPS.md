# Probetest SPS-Kopplung (OPC UA ↔ iRC)

Vor dem Bau der SPS-Kopplung (Stufe G4) wird geprüft, ob beide Enden so funktionieren wie geplant:

- **SPS:** Phoenix Contact AXCF 2152 mit OPC-UA-Server, Anmeldung mit dem Standardkonto.
- **Roboter:** App-Schnittstelle von iRC (gRPC, Port 5000), an einem Schüler-PC und am echten ReBeL.

Das Skript `tools/probetest_sps.py` liest nur, solange man nicht `--schreiben` bzw. `--bewegen` angibt. Jeder Lauf
kann mit `--bericht datei.json` einen Bericht ablegen; die Berichte bitte aufheben (zur Auswertung).

## Einfacher: Messkoffer (ab 26.10.2026)

Im Container als root `rebel-sim-messung` starten: ein Menü führt durch Container-Check, SPS lesen/schreiben,
App-Schnittstelle am Schüler-PC und am echten ReBeL (lesen, ein GSig) und die CRI-Aufzeichnung fester Stellungen
nahe Home (A/B/C-Reihenfolge, Werkzeuglänge). „Fertig“ schreibt `messung_<Zeit>.zip` nach
`/var/lib/rebel-sim/messungen`; Download im Lehrerbereich unter Einstellungen → Messungen (nur Admins). Passwörter,
Konten und Welten kommen nicht in die ZIP. Ablauf zum Ausdrucken: [`testablauf_schule.html`](testablauf_schule.html).
Die Einzelbefehle unten bleiben für Sonderfälle (z.B. `--din`, Programmstart mit `--bewegen`).

## Was vorab schon geprüft ist (25.10.2026)

- iRC V15.0.2 (Simulation, Steuerung ohne Oberfläche): App-Schnittstelle erreichbar, Systeminfo, Lizenzen,
  Roboterzustand (64 DIn, 64 DOut, 100 globale Signale), Programmzustand, Programmliste.
- Schreiben in der Simulation: Eingang (DIn) und globales Signal (GSig) an/aus, Programmvariable setzen
  (wird auch ohne geladenes Programm angelegt), Programm laden, starten und stoppen.
- Globale Signale werden nur mit `target_state` gesetzt (nur `state` wirkt nicht) – im Skript berücksichtigt.
- Zählweise am Laptop bestätigt: Signal n der Schnittstelle erscheint in iRC als globales Signal n+1
  (wie bei Ein- und Ausgängen). Die iRC-Signale 1–8 sind also die Schnittstellen-Nummern 0–7.
- OPC UA an der eigenen AXCF 2152 (192.168.2.51) vom Container aus: anonym, `ROB_IN`/`ROB_OUT` gefunden,
  Lesen ca. 4 ms, Testmuster in PLCnext Engineer sichtbar.
- In der Simulation meldet iRC bei den Lizenzen `test_duration_remaining_seconds` (rund 30 Minuten) – am echten
  ReBeL und an den Schüler-PCs nachsehen.
- OPC UA gegen einen Testserver, der sich wie PLCnext verhält: ohne und mit Verschlüsselung (Basic256Sha256),
  Anmeldung mit Benutzer/Passwort, Variablen finden, lesen, Testmuster schreiben und zurückstellen.

## 1. SPS vorbereiten (PLCnext Engineer)

1. Globale Variablen anlegen und in der Spalte **OPC** ankreuzen:

   | Variable | Typ | Richtung |
   |---|---|---|
   | `ROB_IN` | BYTE | SPS → Roboter (8 Signale) |
   | `ROB_OUT` | BYTE | Roboter → SPS (8 Signale) |
   | `ROB_STEUER` | BYTE | SPS → Roboter (später: Motoren an, Quittieren, Start, Stopp, Pause) |
   | `ROB_PROGNR` | INT | SPS → Roboter (später: Programmnummer) |
   | `ROB_STATUS` | BYTE | Roboter → SPS (später: bereit, läuft, Fehler ...) |
   | `ROB_WERT_1` … `ROB_WERT_4` | DINT | SPS → Roboter (später: Zahlen für iRC-Variablen) |

   Für den Test reichen `ROB_IN` und `ROB_OUT`. Liegen die Variablen in einer Programminstanz statt global,
   beim Aufruf `--programm-instanz <Instanzname>` angeben.
2. OPC-UA-Server der Steuerung aktiv lassen (Projekt → OPC UA), Projekt übertragen, `ROB_IN` z.B. auf 3 setzen.
3. IP-Adresse der SPS notieren. Benutzer: Standardkonto (z.B. `admin`) mit Passwort.

## 2. OPC UA testen (im Container)

Einmalig (ist im Container schon erledigt):

```bash
sudo bash /opt/rebel-sim/tools/probetest_vorbereiten.sh
```

Lesen:

```bash
sudo -u rebelsim /opt/rebel-sim-probe/bin/python3 /opt/rebel-sim/tools/probetest_sps.py opcua --sps <IP-der-SPS> --benutzer admin --bericht /tmp/bericht_opcua.json
```

- **„Anmelden“ scheitert mit einem Zertifikatsfehler:** Die SPS kennt das Zertifikat des Containers noch nicht. Im
  Web-Based-Management der SPS unter Sicherheit/Zertifikate das abgelehnte Zertifikat „rebel-sim sps-kopplung“ als
  vertrauenswürdig eintragen, dann erneut starten.
- **„Variablen: keine gefunden“:** OPC-Haken und Übertragung prüfen. Das Skript listet dann die vorhandenen Variablen auf.

Schreiben: `ROB_OUT` steht 20 s auf dem Testmuster 165 (binär 10100101), damit man es in PLCnext Engineer online sieht,
danach wird der alte Wert zurückgestellt.

```bash
sudo -u rebelsim /opt/rebel-sim-probe/bin/python3 /opt/rebel-sim/tools/probetest_sps.py opcua --sps <IP-der-SPS> --benutzer admin --schreiben --halten 20 --bericht /tmp/bericht_opcua_schreiben.json
```

## 3. App-Schnittstelle an einem Schüler-PC testen

Voraussetzung: iRC läuft am PC, die App „WebotsSim“ ist installiert und freigeschaltet (wie für die Taster).

```bash
sudo -u rebelsim /opt/rebel-sim-probe/bin/python3 /opt/rebel-sim/tools/probetest_sps.py irc --ziel <IP-des-PCs> --bericht /tmp/bericht_pc.json
sudo -u rebelsim /opt/rebel-sim-probe/bin/python3 /opt/rebel-sim/tools/probetest_sps.py irc --ziel <IP-des-PCs> --schreiben --gsig 1 --variable sps_wert=5 --bericht /tmp/bericht_pc_schreiben.json
```

In iRC dabei nachsehen: Wird das globale Signal 1 kurz angezeigt? Welche Nummer zeigt iRC dafür an? Das zeigt,
wie Anzeige und Schnittstelle zählen.

## 4. Echter ReBeL (192.168.3.11)

**Sicherheit:** Arbeitsbereich frei, Not-Halt in Reichweite, Override niedrig. Die Schritte a und b bewegen den
Roboter nicht.

1. iRC mit dem echten ReBeL verbinden und dort die App „WebotsSim“ installieren (Apps, `deploy/irc_app/WebotsSim.zip`)
   und freischalten. Geht das nicht, notieren, was iRC meldet.
2. a) Lesen:

   ```bash
   sudo -u rebelsim /opt/rebel-sim-probe/bin/python3 /opt/rebel-sim/tools/probetest_sps.py irc --ziel 192.168.3.11 --bericht /tmp/bericht_rebel.json
   ```

   Wichtig im Bericht: iRC-Version, `is_simulation`, **Lizenzen**, Anzahl der globalen Signale.
3. b) Signale und Variable (ohne Bewegung):

   ```bash
   sudo -u rebelsim /opt/rebel-sim-probe/bin/python3 /opt/rebel-sim/tools/probetest_sps.py irc --ziel 192.168.3.11 --schreiben --gsig 1 --din 20 --variable sps_wert=5 --bericht /tmp/bericht_rebel_schreiben.json
   ```

   Die Antwort auf `--din 20` zeigt, ob die echte Steuerung Hardware-Eingänge von außen annimmt. Für die Kopplung
   sollen ohnehin die globalen Signale genommen werden.
4. c) Nur wenn a und b klappen: ein harmloses Testprogramm (z.B. langsam in die Nullstellung fahren) laden, starten und
   nach 5 s stoppen lassen.

   ```bash
   sudo -u rebelsim /opt/rebel-sim-probe/bin/python3 /opt/rebel-sim/tools/probetest_sps.py irc --ziel 192.168.3.11 --programm ReBeL_MoveToZero.xml --bewegen --dauer 5 --bericht /tmp/bericht_rebel_programm.json
   ```

## 5. Ergebnis

Die Berichte `/tmp/bericht_*.json` im Container liegen lassen (oder nach `/root` kopieren). Daraus entsteht der
endgültige Plan für die Kopplung: Variablennamen, Zuordnung zu globalen Signalen, Handshake für Start/Stopp und
Programmwahl, Verhalten bei Verbindungsverlust.
