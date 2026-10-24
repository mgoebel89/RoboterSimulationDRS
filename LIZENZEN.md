# Lizenzen und fremde Daten

## Eigene Dateien

Programmcode, Welten, Anleitung sowie die STEP-Dateien `cad/tisch/basisplatte.step`,
`cad/backen/robi2/robi2_greiferbacke.step`, `cad/teile/target.step` und `cad/teile/magazin4x4.step`
stammen von der David-Roentgen-Schule Neuwied. Eine Lizenz für die Weitergabe ist noch nicht festgelegt.
Das Logo der Schule steht nicht im Repository; es wird im Lehrerbereich unter Einstellungen hochgeladen.

## Nicht enthalten: igus Robot Control

Das Robotermodell des igus ReBeL (Glieder, Kinematik) und die Greifermodelle des Schunk EGP 40 und EGP 25 aus
iRC gehören igus. Sie stehen **nicht** in diesem Repository. `tools/erzeuge_rebel_modell.py` erzeugt das
Webots-Modell im eigenen Container aus einer iRC-Installation (`tools/irc_daten_export.ps1`,
`rebel-sim-modell`). Die erzeugten Dateien (`protos/IgusRebel6DOF.proto`, `protos/meshes/`, `werkzeuge/`)
sind in `.gitignore` eingetragen und dürfen nicht weitergegeben werden.

## Schunk EGP 40 und EGP 25 (CADENAS)

`cad/greifer/egp40/schunk_egp40_n_n_b.stp` (EGP 40-N-N-B, 0310940) und
`cad/greifer/egp25/schunk_egp25_n_s_b.stp` sind die unveränderten CAD-Dateien des Herstellers,
Lizenz **CC BY-ND 4.0**: CADENAS GmbH hält die Rechte an der Datenbank in Form der CAD-Datei,
SCHUNK SE & Co. KG an dem ursprünglichen Bauteil (siehe `LIZENZ_SCHUNK_CADENAS.txt` im jeweiligen Ordner).
Daraus abgeleitete Netze entstehen nur im Container (`teile/`) und werden nicht weitergegeben.

## Weitere

- **Webots R2025a** (Cyberbotics, Apache 2.0) wird bei der Installation von GitHub geladen;
  die 3D-Ansicht im Browser lädt `WebotsView.js` von cyberbotics.com.
- **three.js 0.160** (MIT) für den Baukasten im Browser, von cdn.jsdelivr.net.
- **App-Schnittstelle von iRC:** Die Nachricht `IOStateRequest` in `controllers/irc_bridge/ea.py` folgt
  `robotcontrolapp.proto` aus [CommonplaceRobotics/RobotControlApps](https://github.com/CommonplaceRobotics/RobotControlApps) (MIT).
- **gmsh** (GPL, als Ubuntu-Paket `python3-gmsh`) liest STEP-Dateien im Container ein.
