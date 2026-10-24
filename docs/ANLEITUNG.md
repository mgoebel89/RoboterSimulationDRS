# Anleitung: ReBeL-Simulation einrichten und im Unterricht nutzen

Inhalt

1. [So funktioniert es](#1-so-funktioniert-es)
2. [Voraussetzungen](#2-voraussetzungen)
3. [Installation auf Proxmox](#3-installation-auf-proxmox)
4. [Robotermodell aus iRC erzeugen](#4-robotermodell-aus-irc-erzeugen)
5. [Schüler-PCs vorbereiten](#5-schüler-pcs-vorbereiten)
6. [Im Unterricht: Schülerseite](#6-im-unterricht-schülerseite)
7. [Lehrerbereich](#7-lehrerbereich)
8. [Weltbaukasten](#8-weltbaukasten)
9. [Taster, Leuchtmelder und Sensoren](#9-taster-leuchtmelder-und-sensoren)
10. [Update](#10-update)
11. [Befehle im Container](#11-befehle-im-container)
12. [Fehlersuche](#12-fehlersuche)

---

## 1. So funktioniert es

- Die Schüler programmieren in **iRC V15** auf ihrem PC, mit der iRC-eigenen Simulation.
- Für jeden Schüler-PC startet im Container eine **Webots-Simulation**. Sie verbindet sich mit der iRC-Steuerung
  des Schüler-PCs (CRI, Port 3921) und **hört nur mit**: Der Roboter in Webots fährt genauso wie in iRC,
  greift echte Werkstücke und legt sie ab. iRC behält die Kontrolle.
- Die Schüler sehen die Simulation im **Browser** unter `http://<container>:8080`. Der PC wird an seiner IP erkannt,
  jeder sieht nur seine eigene Simulation.
- **Taster und Sensoren** setzen über die App-Schnittstelle von iRC (Port 5000) echte digitale Eingänge.

```
iRC auf dem Schüler-PC ──CRI (3921)──▶ Webots je Platz im Container ──Browser (8080)──▶ Schüler
        ▲                                   │
        └──── App-Schnittstelle (5000) ◀────┘
```

## 2. Voraussetzungen

**Proxmox**
- Proxmox VE 8 mit Internetzugang (für Ubuntu-Vorlage, Webots und dieses Repository).
- Richtwerte für 10 gleichzeitige Simulationen: **4 Kerne, 6 GB RAM, 20 GB Platte**.
  Gemessen: je Simulation etwa 400 MB RAM und 5 % eines Kerns.

**Netz** (wichtig für die IT)
- Schul-PCs → Container: **TCP 8080** (Webseite und 3D-Ansicht; nur dieser eine Port).
- Container → Schul-PCs: **TCP 3921** (iRC-Steuerung) und **TCP 5000** (App-Schnittstelle von iRC).
  Die Windows-Firewall-Regel „robotcontrol“, die iRC mitbringt, muss auch im **Domänenprofil** gelten.
- Zwischen Schul-PCs und Container **kein NAT/Proxy**, sonst stimmt die Erkennung an der IP nicht.
- Die Browser brauchen Internet: Die 3D-Ansicht lädt `WebotsView.js` von cyberbotics.com,
  der Baukasten three.js von cdn.jsdelivr.net.

**Schüler-PCs**
- igus Robot Control **V15** mit ReBeL-Projekt (REBEL-6DOF-03).
- **Windows-Benutzername ohne Umlaute.** iRC V15 startet sonst nicht
  („Datenverzeichnispfad scheint nicht zu existieren“). Das ist ein Fehler in iRC und betrifft auch Schülerkonten.

## 3. Installation auf Proxmox

1. In der Proxmox-Oberfläche den Knoten wählen, **Shell** öffnen.
2. Diesen Befehl einfügen:

   ```bash
   bash -c "$(curl -fsSL https://raw.githubusercontent.com/mgoebel89/RoboterSimulationDRS/main/proxmox/rebel-sim.sh)"
   ```

3. **„Neuen Container anlegen“** wählen und die Fragen beantworten (die Vorgaben passen meist):
   CT-ID, Hostname (z.B. `rebel-sim`), Kerne, RAM, Platte, Speicher, Netzwerk-Brücke,
   IP-Adresse (feste Adresse empfohlen, z.B. `192.168.7.1/16`, oder `dhcp`), Gateway, Root-Passwort.
4. Das Skript lädt die Ubuntu-24.04-Vorlage, legt den Container an (unprivilegiert, `nesting=1`,
   Start beim Booten) und installiert darin alles. Das dauert einige Minuten.
5. Zum Schluss fragt es nach dem **ersten Lehrerkonto** (Admin) und dessen Passwort.

Ohne das Proxmox-Skript, z.B. in einem schon vorhandenen Ubuntu-24.04-Container, als root:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/mgoebel89/RoboterSimulationDRS/main/install/rebel-sim-install.sh)"
rebel-sim-admin <name>
```

Danach erreichbar:
- Schüler: `http://<container>:8080`
- Lehrkräfte: `http://<container>:8080/lehrer`

## 4. Robotermodell aus iRC erzeugen

Das Modell des ReBeL und des Greifers stammt aus iRC und gehört igus. Es steht deshalb nicht im Repository,
sondern wird einmal im Container erzeugt.

1. Auf einem Windows-PC mit iRC V15 die Datei [`tools/irc_daten_export.ps1`](../tools/irc_daten_export.ps1)
   herunterladen und in PowerShell ausführen:

   ```powershell
   powershell -ExecutionPolicy Bypass -File irc_daten_export.ps1
   ```

   Es legt `irc-daten.zip` (etwa 1,5 MB) auf den Desktop: Roboter `REBEL-6DOF-03` und Greifer Schunk EGP 25.
2. Die Datei in den Container kopieren, z.B. mit WinSCP oder:

   ```powershell
   scp $HOME\Desktop\irc-daten.zip root@<container>:/root/
   ```

3. Im Container (Proxmox-Konsole des Containers oder SSH):

   ```bash
   rebel-sim-modell /root/irc-daten.zip
   ```

Die Daten bleiben unter `/var/lib/rebel-sim/irc`; bei jedem Update wird das Modell daraus neu erzeugt.

| Profiltisch mit Magazin | Basisplatte mit Anschlagkante |
|---|---|
| ![](bilder/profiltisch.jpg) | ![](bilder/basisplatte.jpg) |

## 5. Schüler-PCs vorbereiten

1. **iRC V15** installieren, ReBeL-Projekt laden. Die iRC-Oberfläche startet ihre Steuerung selbst auf Port 3921,
   eine CRI-Einstellung ist nicht nötig.
2. **Werkzeug** in iRC wählen: `Schunk_EGP25_RebelMountingPlate` (bzw. das Werkzeug der Welt, z.B.
   „EGP-25 Robi2-Backen“). Dann meldet iRC den TCP und der Abgleich zeigt „TCP“.
3. **App „WebotsSim“** (nur für Welten mit Tastern oder Sensoren): In iRC unter **Apps** die Datei
   [`deploy/irc_app/WebotsSim.zip`](../deploy/irc_app/WebotsSim.zip) installieren und die App **aktivieren**.
   Sie enthält kein Programm; sie erlaubt der Simulation nur, in der iRC-Simulation Eingänge zu setzen.
4. Firewall: Regel „robotcontrol“ für das Domänenprofil freigeben (siehe Voraussetzungen).

## 6. Im Unterricht: Schülerseite

1. **Zuerst iRC starten**, dann im Browser `http://<container>:8080` öffnen.
2. Welt wählen und **„Simulation starten“**. Oben steht, wie viele Simulationen noch frei sind.

![Weltwahl der Schüler](bilder/schueler_weltwahl.jpg)

3. Die 3D-Ansicht verbindet sich; oben links steht „iRC verbunden“. In iRC verfahren oder ein Programm starten,
   der ReBeL in Webots fährt mit.

![Schüleransicht](bilder/schueler_ansicht_taster.jpg)

**Knöpfe oben**
- **Aufgabe** blendet den Aufgabentext (und ggf. das Arbeitsblatt als PDF) ein.
- **Teile zurück** legt alle Werkstücke sofort wieder auf ihre Startlage, ohne Neustart.
- **Neu starten** startet Webots neu (nur nötig, wenn etwas hängt; dauert einige Sekunden).
- **Welt wechseln**, **Beenden**.

**Greifer Schunk EGP 25:** DOut32 EIN (DOut31 AUS) schließt, DOut31 EIN (DOut32 AUS) öffnet.
Gegriffen wird, was zwischen die offenen Backen passt und auf Backenhöhe liegt; das Teil rutscht in die Mitte.

**Einteachen:** iRC und Browser nebeneinander legen.
- Unten links zeigt die **Einteach-Hilfe**, wie weit der TCP noch von der Greifstellung des nächsten Werkstücks
  entfernt ist, in iRC-Koordinaten (z.B. `target1: noch X +2 Y -1 Z -12 mm`). Grün, sobald der Griff klappen würde.
- Unten rechts: **Ansichten** Standard, Vorne, Seite, Oben und **Greifer** (Kamera folgt dem TCP).
- Maus: linke Taste dreht, rechte verschiebt, nach einem Klick ins Bild zoomt das Rad.

Die Simulation endet automatisch nach 15 Minuten ohne iRC und zu den eingestellten Stundenenden.

## 7. Lehrerbereich

`http://<container>:8080/lehrer`, Anmeldung mit dem Lehrerkonto.

- **Simulationen:** alle laufenden Simulationen mit Ampel (iRC verbunden?), Ansehen, Steuern,
  **Teile zurück**, **Neu starten**, **Beenden**, Alle beenden.
- **Welten:** Titel, Aufgabentext, **für Schüler freigeben**, Vorschaubild erzeugen oder hochladen, Arbeitsblatt (PDF).
  Nur freigegebene Welten sehen die Schüler.

![Welten im Lehrerbereich](bilder/lehrer_welten.jpg)

- **Teile:** STEP-Dateien hochladen; der Container erzeugt daraus Netz und Verbindungspunkte (Flächen, Bohrungen,
  Kanten, Ecken) für den Baukasten. Gewinde und Normteile machen das Einlesen langsam, besser ohne exportieren.

![Teilebibliothek](bilder/lehrer_teile.jpg)

- **Baukasten:** siehe nächster Abschnitt.
- **Protokoll:** wer wann was gemacht hat.
- **Einstellungen** (nur Admins): höchstens gleichzeitige Simulationen, Ende ohne iRC, Stundenenden.
- **Benutzer** (nur Admins): Konten mit den Rollen Admin oder Lehrkraft, vorläufige Passwörter.

![Einstellungen](bilder/lehrer_einstellungen.jpg)

## 8. Weltbaukasten

Im Lehrerbereich auf **Baukasten**: Welten aus Teilen der Teilebibliothek zusammenstellen, ohne Webots zu öffnen.

![Baukasten mit Profiltisch](bilder/baukasten_profiltisch.jpg)

**Tisch und Roboter**
- **eben, Maße frei:** Länge, Breite, Höhe (z.B. −100 bei einem 100-mm-Sockel), Lage des Roboters.
- **Profiltisch 800 × 800** (Bosch Rexroth, Nut 10, Basisplatte): gemessen wird wie am echten Tisch von der
  Anschlagkante der Basisplatte zur hinteren und linken Tischkante. Nach hinten nur im Nutraster (40 mm),
  nach links stufenlos. Die Nuten sind als Kanten anklickbar (`nut-1` hinten bis `nut-20` vorne).
- „Links“ und „vorne“ gelten aus Sicht des Roboters (vorne = Blickrichtung bei 0°).

**Teile platzieren** (wie Mate Connectors in Onshape)
1. Teil aus der Bibliothek **hinzufügen**; es steht zuerst neben dem Tisch.
2. **Punkt am Teil** anklicken (Fläche, Bohrung, Kante oder Ecke), dann das **Gegenstück** (Tisch, Nut,
   Roboterfuß oder ein anderes Teil). Die beiden Punkte werden aufeinandergelegt.
3. Mit **X/Y/Z** verschieben (Pfeile rot/grün am Gegenstück), drehen, ggf. umdrehen.
4. **Aufbau** (fest) oder **Werkstück** (greifbar, Farbe, Masse).
5. **Speichern**: Die Welt wird erzeugt, Webots rendert ein Vorschaubild. Danach unter „Welten“ freigeben.

![Verbindung eines Teils](bilder/baukasten_verbindung.jpg)

Hängt ein Teil an einem anderen, wandert es mit, wenn sich das andere ändert.

## 9. Taster, Leuchtmelder und Sensoren

Voraussetzung: App „WebotsSim“ in iRC (Abschnitt 5). Nummern immer **wie in der iRC-Anzeige** (DIn21, DOut32).

**Taster und Leuchtmelder** (Baukasten, Abschnitt „Taster und Leuchtmelder“)
- Taster unten links: Beschriftung, DIn, **Schließer/Öffner**, **tastend/rastend**. Ein Öffner meldet ungedrückt 1;
  ein kurz angetippter Taster bleibt mindestens 0,3 s gedrückt, damit iRC ihn sicher sieht.
- Leuchtmelder unten rechts: Beschriftung, DOut, Farbe, auf Wunsch umgekehrt.

**Sensorfläche** (Baukasten, „+ Sensorfläche“): ein durchsichtiger Quader, der wie ein Teil platziert wird.
Die Unterseite ist schon gewählt; nur noch das Gegenstück anklicken.
- **Belegt-Sensor:** meldet, sobald ein Werkstück mindestens 0,5 mm in den Quader ragt (Schließer oder Öffner).
  Die Höhe bestimmt, ab wann ein Teil zählt.
- **Farbsensor:** erkennt die Farbe des Werkstücks (rot, grün, blau, gelb, weiß, schwarz; die nächste zur Farbe
  im Baukasten, sonst „keine“). Ausgabe wahlweise **ein Eingang je Farbe** oder **Farbcode über Bits**
  (Zahl je Farbe, 0 = keine Farbe, Bit 0 auf dem ersten DIn).

| Belegt-Sensor | Farbsensor (blau = Code 2) | Sensorfläche in der 3D-Ansicht |
|---|---|---|
| ![](bilder/schueler_ansicht_sensor.jpg) | ![](bilder/schueler_ansicht_farbsensor.jpg) | ![](bilder/sensorflaeche_belegt.jpg) |

Fehlt die App in iRC, steht unter den Tastern ein Hinweis („App WebotsSim … nicht freigeschaltet“).
Laufende Simulationen übernehmen geänderte Taster und Sensoren nach **Neu starten**.

## 10. Update

- Auf dem Proxmox-Host das Skript aus Abschnitt 3 erneut starten und **„Vorhandenen Container aktualisieren“** wählen,
- oder im Container: `rebel-sim-update`.

Das Update holt den neuesten Stand aus diesem Repository und richtet ihn ein. **Welten, Teile, Konten,
Einstellungen und das Robotermodell bleiben erhalten.** Laufende Simulationen nutzen den neuen Stand erst nach
„Neu starten“; am besten außerhalb des Unterrichts aktualisieren.

## 11. Befehle im Container

| Befehl | Zweck |
|---|---|
| `rebel-sim-update` | Update aus dem Repository |
| `rebel-sim-modell /root/irc-daten.zip` | Robotermodell aus iRC-Daten erzeugen |
| `rebel-sim-admin <name>` | Admin-Konto anlegen oder Passwort neu setzen; `--liste` zeigt die Konten |
| `rebel-sim-platz liste` | laufende Simulationen |
| `rebel-sim-platz anlegen 99 192.168.3.11 rebel_tisch.wbt dauerhaft` | fester Platz, z.B. Zwilling des echten ReBeL |
| `journalctl -u rebel-sim-web` | Protokoll der Webseite und des Verwalters |
| `journalctl -u rebel-sim@01`, `/var/log/rebel-sim/platz-01.log` | Protokoll einer Simulation |

Wichtige Orte: `/opt/rebel-sim` (Programm, Git), `/opt/rebel-sim/worlds` und `/opt/rebel-sim/teile`
(Welten und Teile, nicht im Git), `/etc/rebel-sim/verwalter.json`, `/var/lib/rebel-sim` (Konten, Protokoll,
Einstellungen, iRC-Daten).

## 12. Fehlersuche

| Problem | Ursache und Abhilfe |
|---|---|
| „warte auf iRC“, Ampel gelb | iRC auf dem Schüler-PC nicht gestartet, Firewall-Regel „robotcontrol“ gilt nicht im Domänenprofil, oder Port 3921 im Netz gesperrt. |
| Ampel rot, Roboter steht | Simulation hängt: **Neu starten**. |
| Taster ohne Wirkung, Hinweis „nicht freigeschaltet“ | App WebotsSim in iRC installieren und aktivieren; Port 5000 zum Schüler-PC freigeben. |
| iRC startet nicht, „Datenverzeichnispfad…“ | Windows-Konto ohne Umlaut verwenden. |
| Falscher PC erkannt / „nicht deine Simulation“ | NAT oder Proxy zwischen Schul-PC und Container. |
| 3D-Ansicht bleibt leer | Browser ohne Internet (WebotsView.js) oder Port 8080 gesperrt. |
| Greifer greift ins Leere | Einteach-Hilfe beachten: Teil zu breit für die Backen, Greifer drehen (A6) oder Höhe anpassen. |
| Abgleich zeigt große Abweichung | In iRC das passende Werkzeug wählen (TCP), sonst vergleicht der Abgleich mit dem Flansch. |
