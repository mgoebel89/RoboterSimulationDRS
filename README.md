# ReBeL-Simulation (DRS Neuwied)

Robotik-Unterricht mit dem **igus ReBeL 6DOF**: Die Schüler programmieren wie gewohnt in
**igus Robot Control (iRC) V15** auf ihrem PC. Ein zentraler Container auf **Proxmox** zeigt dazu in
**Webots** den Robotertisch mit Werkstücken, Greifer, Tastern und Sensoren. Die Schüler sehen ihre
Simulation **im Browser**, der Roboter fährt 1:1 mit iRC mit.

![Schüleransicht mit Tastern und Leuchtmeldern](docs/bilder/schueler_ansicht_taster.jpg)

## Was die Simulation kann

- **Selbstbedienung:** Jeder Schüler-PC wird an seiner IP erkannt; der Schüler wählt eine Welt und startet
  „seine“ Simulation (bis zu 10 gleichzeitig). Automatisches Ende nach 15 Minuten ohne iRC.
- **Mithören statt Steuern:** Webots liest Gelenke und Ausgänge über die CRI-Schnittstelle mit,
  iRC behält die Kontrolle. Abweichung zur iRC-Kinematik unter 0,1 mm.
- **Greifer Schunk EGP 40** (Standard) oder EGP 25 (DOut31 öffnen, DOut32 schließen) und eigene Backensätze mit
  einstellbarem TCP und Werkzeugpaket für iRC; gegriffen wird an der
  echten Form der CAD-Werkstücke. Einteach-Hilfe zeigt den Weg zur Greifstellung in iRC-Koordinaten.
- **Taster, Leuchtmelder, Belegt- und Farbsensoren**, die echte Eingänge in iRC setzen
  (über die App-Schnittstelle von iRC, die iRC-Oberfläche bleibt dabei bedienbar).
- **Startseite auf Port 80** für alle: Schüler starten dort ihre Welt, Lehrkräfte melden sich dort an.
- **Lehrerbereich** mit Konten: Simulationen steuern, Welten freigeben, löschen und Greifer wählen,
  Teile aus STEP-Dateien einlesen, Logo wählen. Gestaltung nach dem Corporate Design der DRS.
- **Welten exportieren/importieren** als ZIP-Paket mit allen Teilen und Backensätzen, z.B. zuhause bauen und in
  der Schule einspielen. Aufgabentexte in Markdown, für Schüler als Seitenleiste neben der 3D-Ansicht.
- **Weltbaukasten im Browser:** Teile über Verbindungspunkte platzieren (wie Mate Connectors in Onshape),
  ebener Tisch oder Profiltisch 800 × 800 mm (Bosch Rexroth, Nut 10) mit Basisplatte im Nutraster.

## Schnellstart

Auf dem **Proxmox-Host** (Shell des Knotens) als root:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/mgoebel89/RoboterSimulationDRS/main/proxmox/rebel-sim.sh)"
```

Das Skript legt einen Ubuntu-24.04-Container an, installiert Webots R2025a und die Simulation und fragt nach
dem ersten Lehrerkonto. Danach noch:

1. **Robotermodell erzeugen:** Die igus-Modelldaten dürfen nicht weitergegeben werden und stehen deshalb nicht
   im Repository. Auf einem PC mit iRC [`tools/irc_daten_export.ps1`](tools/irc_daten_export.ps1) ausführen,
   `irc-daten.zip` in den Container kopieren und dort `rebel-sim-modell /root/irc-daten.zip` aufrufen.
2. **Schüler-PCs:** die App [`deploy/irc_app/WebotsSim.zip`](deploy/irc_app/WebotsSim.zip) einmal in iRC
   installieren und freischalten (für Taster und Sensoren).

**Update:** dasselbe Skript erneut starten und „aktualisieren“ wählen, oder im Container `rebel-sim-update`.

Ausführlich, mit Bildern: **[Anleitung](docs/ANLEITUNG.md)**.

## Aufbau

```
iRC auf dem Schüler-PC ──CRI (3921)──▶ Webots je Platz im Container ──Browser (80)──▶ Schüler
        ▲                                   │
        └──── App-Schnittstelle (5000) ◀────┘  Taster und Sensoren setzen Eingänge
```

| Ordner | Inhalt |
|---|---|
| `controllers/irc_bridge` | Webots-Controller: CRI-Client, Greifer, Taster/Sensoren (`ea.py`) |
| `tools` | Webseite und Verwalter (`uebersicht.py`, `verwalter.py`, `lehrer.py`), Baukasten, Modell-Generator, Werkzeuge |
| `protos` | Werkstück, Aufbau, Sensorfeld (das ReBeL-Modell erzeugt `tools/erzeuge_rebel_modell.py`) |
| `cad` | STEP-Dateien: Basisplatte, Robi2-Backen, Schunk EGP 40 und EGP 25, Werkstücke |
| `vorlagen` | Beispielwelten und Teileliste; landen bei der Installation in `worlds/` und `teile/` |
| `deploy` | systemd-Dienste, Platz-Helfer, App für iRC |
| `install`, `proxmox` | Installation und Update |
| `tests` | `python3 -m unittest discover -s tests` |

## Lizenzen

Eigene Dateien: siehe [LIZENZEN.md](LIZENZEN.md). Fremde Daten (igus, Schunk/CADENAS, Commonplace Robotics,
Cyberbotics) sind dort mit ihren Bedingungen aufgeführt.
