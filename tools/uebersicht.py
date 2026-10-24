"""Webseite der ReBeL-Simulation (laeuft im Container, Port 80; der alte Port 8080 leitet um).

Startseite / fuer alle: freigegebene Welten zum Starten und der Zugang zum Lehrerbereich.

Schueler (erkannt an der IP ihres PCs, siehe verwalter.py):
  /               ohne Simulation: Weltenliste mit "Simulation starten";
                  mit Simulation: 3D-Ansicht mit Aufgabe, Teile zurueck (ohne Neustart),
                  Neu starten, Welt wechseln und Beenden
  /?wechseln=1    Weltenliste, um die Welt der laufenden Simulation zu wechseln
  /api/ich        eigener Zustand (JSON); POST /api/starten, /api/teile_zuruecksetzen,
                  /api/zuruecksetzen (Neustart), /api/beenden
  /welten/x.png   Vorschaubild bzw. Arbeitsblatt (x.pdf) einer Welt

Lehrkraft (lehrer_ips in verwalter.json, im Container selbst immer):
  /lehrer         Liste aller Simulationen mit Ampel, aktualisiert sich selbst
  /status.json    Zustand aller Simulationen (fuer die Seite und fuer Skripte)
  /platz/07       3D-Ansicht von Platz 07 mit Teile zurueck, Neu starten, Beenden (nur zuschauen:
                  den Roboter bewegt immer iRC am Schueler-PC)

Fuer alle, aber nur fuer die eigene Simulation (Lehrkraft: alle):
  /sim/07/...     Durchleitung zum Webots-Stream von Platz 07 (WebSocket und
                  Dateien wie Meshes). Noetig, weil aktuelle Browser
                  Verbindungen von einer http-Seite zu einem ANDEREN Port im
                  lokalen Netz sperren ("Local Network Access"). So kommt
                  alles vom selben Ursprung, und im Netz muss nur Port 80
                  offen sein.

Plaetze kommen aus /etc/rebel-sim/*.env (angelegt vom Verwalter bzw. mit
deploy/platz.sh), der Zustand aus den Statusdateien der Controller in
/run/rebel-sim. Nur Python-Standardbibliothek.
"""

import html
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import baukasten
import lehrer
import verwalter
import welt_paket
import aufgabe_md

KONFIG_DIR = os.environ.get("KONFIG_DIR", "/etc/rebel-sim")
STATUS_DIR = os.environ.get("STATUS_DIR", "/run/rebel-sim")
WELTEN_DIR = os.environ.get("WELTEN_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "worlds"))
ZUSTAND_DIR = lehrer.ZUSTAND_DIR
TEILE_DIR = os.environ.get("TEILE_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "teile"))
WELT_NAME = re.compile(r"[a-z0-9_-]{1,50}")
TEIL_NAME = re.compile(r"[a-z0-9_-]{1,60}")
PORT = int(os.environ.get("UEBERSICHT_PORT", "80"))
WEBOTS_VIEW_JS = os.environ.get("WEBOTS_VIEW_JS", "https://cyberbotics.com/wwi/R2025a/WebotsView.js")
STREAM_HOST = os.environ.get("STREAM_HOST", "127.0.0.1")
VERALTET_NACH_S = 5.0
# Kopfzeilen, die bei der Durchleitung nicht 1:1 weitergegeben werden
OHNE_WEITERGABE = {"host", "connection", "keep-alive", "proxy-connection"}


def lese_env(pfad):
    werte = {}
    with open(pfad, encoding="utf-8") as f:
        for zeile in f:
            zeile = zeile.strip()
            if zeile and not zeile.startswith("#") and "=" in zeile:
                k, v = zeile.split("=", 1)
                werte[k.strip()] = v.strip().strip('"')
    return werte


def lese_plaetze(konfig_dir=None):
    konfig_dir = KONFIG_DIR if konfig_dir is None else konfig_dir
    plaetze = []
    if not os.path.isdir(konfig_dir):
        return plaetze
    for name in sorted(os.listdir(konfig_dir)):
        m = re.fullmatch(r"(\d{1,2})\.env", name)
        if not m:
            continue
        pfad = os.path.join(konfig_dir, name)
        env = lese_env(pfad)
        platz = m.group(1)
        try:
            gestartet = float(env["GESTARTET"])
        except (KeyError, ValueError):
            gestartet = os.path.getmtime(pfad)   # Plaetze aus der Zeit vor dem Verwalter
        plaetze.append({
            "platz": platz,
            "irc_host": env.get("IRC_HOST", "?"),
            "welt": env.get("WELT", "rebel_tisch.wbt"),
            "stream_port": int(env.get("STREAM_PORT", 1240 + int(platz))),
            "gestartet": gestartet,
            "dauerhaft": env.get("DAUERHAFT") == "1",
        })
    return plaetze


def schreibe_befehl(platz, befehl, status_dir=None):
    """Befehl an den Controller eines Platzes (liest sie in jedem Schritt, siehe irc_bridge.hole_befehle)."""
    status_dir = STATUS_DIR if status_dir is None else status_dir
    pfad = os.path.join(status_dir, "platz-%s.befehl" % platz)
    with open(pfad, "a", encoding="utf-8") as f:   # eine Zeile je Befehl; der Controller benennt vor dem Lesen um
        f.write(befehl + "\n")


def lese_status(platz, status_dir=None):
    status_dir = STATUS_DIR if status_dir is None else status_dir
    pfad = os.path.join(status_dir, "platz-%s.json" % platz)
    try:
        with open(pfad, encoding="utf-8") as f:
            daten = json.load(f)
        daten["_alter_s"] = time.time() - os.path.getmtime(pfad)
        return daten
    except (OSError, ValueError):
        return None


def dienst_aktiv(platz):
    try:
        r = subprocess.run(["systemctl", "is-active", "rebel-sim@%s" % platz],
                           capture_output=True, text=True, timeout=3)
        return r.stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return False


def ampel(status, aktiv):
    """(Farbe, Text) fuer einen Platz."""
    if not aktiv:
        return "rot", "Simulation aus"
    if status is None or status.get("_alter_s", 1e9) > VERALTET_NACH_S:
        return "rot", "Simulation startet oder hängt"
    if not status.get("verbunden"):
        return "gelb", "wartet auf iRC - iRC am PC starten"
    if status.get("sekunden_seit_irc") is not None and status["sekunden_seit_irc"] > VERALTET_NACH_S:
        return "gelb", "iRC antwortet nicht mehr"
    if status.get("teil"):
        return "gruen", "verbunden - greift %s" % status["teil"]
    return "gruen", "verbunden"


def gesamtstatus(plaetze=None, aktiv_fn=None, status_dir=None):
    aktiv_fn = dienst_aktiv if aktiv_fn is None else aktiv_fn
    ergebnis = []
    for p in (lese_plaetze() if plaetze is None else plaetze):
        st = lese_status(p["platz"], status_dir)
        farbe, text = ampel(st, aktiv_fn(p["platz"]))
        eintrag = dict(p, farbe=farbe, text=text)
        if st:
            eintrag.update({k: st.get(k) for k in ("gelenke_grad", "greifer", "abweichung_mm", "echtzeit")})
            eintrag["hilfe"] = (st.get("hilfe") or {}).get("text")
        ergebnis.append(eintrag)
    return ergebnis


# Gestaltung nach dem Corporate Design der David-Roentgen-Schule: Hauptfarben DRS-Blau und Schwarz,
# Akzente Gruen, Pink, Gelb; Schrift DIN Pro bzw. Calibri; Flaechen mit EINER abgerundeten Ecke;
# das blaue DRS-Dreieck hebt Ueberschriften hervor. Das Logo waehlt ein Admin unter Einstellungen.
STIL = """
:root { --blau:#00639C; --blau-dunkel:#004a75; --blau-hell:#E6F3FA; --gruen:#B1C800; --pink:#D4005E; --gelb:#F8B442;
        --schwarz:#1a1a1a; --grau:#a6a6a6; --grau-hell:#d1d1d1; --linie:#e2e5e9; --bg:#f4f5f6; --karte:#fff;
        --text:#1a1a1a; --leise:#666; --rot:#D4005E; --ecke:14px; --knopf:#eceef1; }
* { box-sizing:border-box; }
body { margin:0; font-family:'DIN Pro', Calibri, 'Segoe UI', Arial, sans-serif; background:var(--bg); color:var(--text); }
header { background:var(--karte); color:var(--text); padding:8px 20px; display:flex; gap:8px 12px; align-items:center;
  border-bottom:3px solid var(--blau); min-height:58px; flex-wrap:wrap; }
header > * { white-space:nowrap; }
.marke { display:flex; align-items:center; gap:14px; color:var(--blau); text-decoration:none; font-weight:700;
  font-size:1.15em; white-space:nowrap; }
.marke img { height:40px; width:auto; display:block; }
.dreieck { width:.75em; height:.75em; fill:var(--blau); flex:none; vertical-align:-.05em; margin-right:.35em; }
header .unter { color:var(--leise); }
main { padding:20px; display:grid; grid-template-columns:repeat(auto-fill,minmax(230px,1fr)); gap:14px; }
.platz { background:var(--karte); border-radius:var(--ecke) 0 0 0; padding:14px; border:1px solid var(--linie);
  box-shadow:0 1px 2px rgba(0,0,0,.04), 0 4px 12px rgba(0,0,0,.04); display:flex; flex-direction:column; gap:6px; }
.kopf { display:flex; align-items:center; gap:10px; font-size:1.25em; font-weight:700; }
.punkt { width:16px; height:16px; border-radius:50%; flex:none; }
.gruen { background:var(--gruen); } .gelb { background:var(--gelb); } .rot { background:var(--pink); }
.leise { color:var(--leise); font-size:.9em; }
a.knopf { margin-top:6px; text-align:center; background:var(--blau); color:#fff; text-decoration:none;
          padding:8px; border-radius:3px; font-weight:700; }
a.knopf:hover { background:var(--blau-dunkel); }
.leer { padding:40px; color:var(--leise); grid-column:1/-1; }
.md { line-height:1.45; } .md > :first-child { margin-top:0; } .md > :last-child { margin-bottom:0; }
.md p, .md ul, .md ol, .md blockquote, .md table { margin:0 0 .65em; } .md ul, .md ol { padding-left:1.3em; }
.md h3, .md h4, .md h5 { margin:.9em 0 .35em; color:var(--blau); } .md h3 { font-size:1.08em; } .md h4, .md h5 { font-size:1em; }
.md code { background:var(--bg); border:1px solid var(--linie); border-radius:3px; padding:0 4px; font-size:.92em; }
.md blockquote { border-left:4px solid var(--gelb); background:#fff8ea; padding:6px 10px; }
.md table { border-collapse:collapse; } .md th, .md td { border:1px solid var(--linie); padding:3px 7px; text-align:left; }
.md th { background:var(--bg); } .md hr { border:0; border-top:1px solid var(--linie); margin:.8em 0; }
.md a { color:var(--blau); }
"""

# Blaues DRS-Dreieck (Gestaltungselement), als Inline-SVG
DREIECK = ('<svg class="dreieck" viewBox="0 0 10 10" aria-hidden="true"><path d="M1.6 .9C1 .6.6.9.6 1.6v6.8c0 .7.4 1 '
           '1 .7l6.9-3.4c.6-.3.6-1.1 0-1.4z"/></svg>')


def logo_url():
    """Adresse des gewaehlten Logos (mit Stand, damit ein neues sofort erscheint) oder None."""
    name = lehrer.gewaehltes_logo(ZUSTAND_DIR)
    if not name:
        return None
    try:
        stand = int(os.path.getmtime(os.path.join(lehrer.logo_dir(ZUSTAND_DIR), name)))
    except OSError:
        stand = 0
    return "/logo?%s-%d" % (urllib.parse.quote(name), stand)


def marke():
    """Linker Teil der Kopfzeile: Logo (falls gewaehlt) und Name, fuehrt zur Startseite."""
    url = logo_url()
    bild = '<img src="%s" alt="Logo">' % html.escape(url) if url else ""
    return '<a class="marke" href="/" title="Startseite">%s<span>%sReBeL-Simulation</span></a>' % (bild, DREIECK)

ANMELDUNG = """<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ReBeL - Lehrerbereich</title><style>%%STIL%%
.anmelden { max-width:360px; margin:60px auto; background:var(--karte); border-radius:var(--ecke) 0 0 0; padding:24px;
  border:1px solid var(--linie); box-shadow:0 4px 12px rgba(0,0,0,.05); display:flex; flex-direction:column; gap:10px; }
.anmelden h1 { margin:0 0 6px; font-size:1.25em; color:var(--blau); }
.anmelden input { font:inherit; font-size:1.1em; padding:8px 10px; border:1px solid var(--grau-hell); border-radius:3px; }
.anmelden input:focus { outline:2px solid var(--blau); outline-offset:-1px; }
.anmelden button { font:inherit; font-weight:700; background:var(--blau); color:#fff; border:0; border-radius:3px;
  padding:10px; cursor:pointer; margin-top:6px; }
.anmelden button:hover { background:var(--blau-dunkel); }
.anmelden a { color:var(--blau); font-size:.92em; }
#fehler { color:var(--rot); min-height:1.2em; }
</style></head>
<body><header>%%MARKE%% <span class="unter">Lehrerbereich</span></header>
<form class="anmelden" id="form">
  <h1>%%DREIECK%%Anmelden</h1>
  <label for="name">Benutzername</label>
  <input id="name" autocomplete="username" autocapitalize="none" autofocus>
  <label for="passwort">Passwort</label>
  <input id="passwort" type="password" autocomplete="current-password">
  <button>Anmelden</button>
  <div id="fehler"></div>%%HINWEIS%%
  <a href="/">Zurück zur Startseite</a>
</form>
<script>
document.getElementById('form').onsubmit = async ev => {
  ev.preventDefault();
  const r = await fetch('/lehrer/anmelden', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name: document.getElementById('name').value, passwort: document.getElementById('passwort').value})});
  const d = await r.json().catch(() => ({}));
  if (r.ok) location.href = '/lehrer';
  else { document.getElementById('fehler').textContent = d.fehler || 'Anmeldung fehlgeschlagen.'; document.getElementById('passwort').select(); }
};
</script></body></html>"""

KEINE_BENUTZER = """<div class="leise">Es gibt noch kein Konto. Den ersten Admin legt man im Container an:
<code>sudo -u rebelsim python3 /opt/rebel-sim/tools/lehrer.py admin &lt;name&gt;</code>, oder von einem Lehrer-PC aus
(<code>lehrer_ips</code> in verwalter.json) im Reiter Benutzer.</div>"""

LEHRER_SEITE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ReBeL - Lehrerbereich</title><style>%%STIL%%
header { flex-wrap:wrap; }
nav { display:flex; gap:4px; margin-left:12px; flex-wrap:wrap; }
nav button, nav a.nav-link, header .rechts { color:var(--text); background:transparent; border:0; border-radius:3px;
  padding:7px 11px; font:inherit; cursor:pointer; text-decoration:none; }
nav button:hover, nav a.nav-link:hover, header .rechts:hover { background:var(--blau-hell); color:var(--blau); }
nav button.an { background:var(--blau); color:#fff; font-weight:700; }
#ich { margin-left:auto; color:var(--leise); font-size:.9em; }
.leiste { display:flex; gap:12px; align-items:center; padding:16px 20px 0; flex-wrap:wrap; }
.knopfreihe { display:flex; gap:6px; flex-wrap:wrap; margin-top:6px; }
.knopfreihe a, .knopfreihe button, .leiste button, .leiste a, .feld button, label.knopf-datei, td button, td a.klein {
  font:inherit; font-size:.88em; border:0; border-radius:3px; padding:7px 10px; cursor:pointer;
  background:var(--knopf); color:var(--text); text-decoration:none; display:inline-block; }
.knopfreihe a:hover, .knopfreihe button:hover, .leiste button:hover, .feld button:hover, label.knopf-datei:hover,
  td button:hover, td a.klein:hover { background:var(--blau-hell); color:var(--blau); }
button.haupt, a.haupt { background:var(--blau) !important; color:#fff !important; font-weight:700; }
button.haupt:hover, a.haupt:hover { background:var(--blau-dunkel) !important; }
button.warnen { background:var(--pink) !important; color:#fff !important; }
button:disabled { opacity:.55; cursor:default; }
.hilfe { font-size:.88em; color:var(--leise); }
.feld input, .feld select, .feld textarea, td select { font:inherit; padding:6px 8px;
  border:1px solid var(--grau-hell); border-radius:3px; width:100%; background:#fff; }
.feld input:focus, .feld select:focus, .feld textarea:focus { outline:2px solid var(--blau); outline-offset:-1px; }
.feld input[type=checkbox], .feld input[type=radio] { width:auto; }
td select { width:auto; }
.feld textarea { min-height:9em; resize:vertical; font-family:Consolas, 'Cascadia Mono', monospace; font-size:.9em; }
.feld button.an { background:var(--blau); color:#fff; }
.spalte { padding:20px; display:flex; flex-direction:column; gap:16px; max-width:860px; }
.spalte.breit { max-width:1200px; }
.feld { background:var(--karte); border-radius:var(--ecke) 0 0 0; padding:14px 16px; border:1px solid var(--linie);
  box-shadow:0 1px 2px rgba(0,0,0,.04), 0 4px 12px rgba(0,0,0,.04); display:flex; flex-direction:column; gap:8px; overflow-x:auto; }
.feld h2 { margin:0; font-size:1.05em; color:var(--blau); }
.zeile { display:flex; gap:8px; flex-wrap:wrap; } .zeile > * { flex:1; min-width:140px; }
table { border-collapse:collapse; width:100%; font-size:.92em; }
th { font-weight:700; color:var(--leise); font-size:.85em; text-transform:uppercase; letter-spacing:.03em; }
th, td { text-align:left; padding:7px 8px; border-bottom:1px solid var(--linie); vertical-align:middle; }
td.knoepfe { white-space:nowrap; text-align:right; }
.gesperrt { color:var(--rot); font-weight:700; }
/* Weltenliste */
table.welten td { padding:8px; }
table.welten tr.zeile-welt { cursor:pointer; } table.welten tr.zeile-welt:hover td { background:var(--blau-hell); }
.mini { width:84px; height:63px; border-radius:8px 0 0 0; background:var(--bg) center/cover no-repeat; display:flex;
  align-items:center; justify-content:center; font-size:.7em; color:var(--grau); border:1px solid var(--linie); }
.welt-titel { font-weight:700; } .welt-datei { font-size:.8em; color:var(--leise); }
.welt-aufgabe { font-size:.85em; color:var(--leise); max-width:420px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.chip { display:inline-block; font-size:.8em; font-weight:700; padding:3px 9px; border-radius:10px; border:0; cursor:pointer;
  font-family:inherit; white-space:nowrap; }
.chip.frei { background:var(--gruen); color:var(--schwarz); } .chip.zu { background:var(--knopf); color:var(--leise); }
.chip.laeuft { background:var(--gelb); color:var(--schwarz); cursor:default; }
.filter { display:flex; gap:8px; align-items:center; }
.filter input { font:inherit; padding:6px 9px; border:1px solid var(--grau-hell); border-radius:3px; min-width:220px; }
/* Logos */
.logos { display:grid; grid-template-columns:repeat(auto-fill, minmax(180px, 1fr)); gap:10px; }
.logo-wahl { border:2px solid var(--linie); border-radius:var(--ecke) 0 0 0; padding:10px; display:flex; flex-direction:column;
  gap:8px; align-items:stretch; cursor:pointer; background:#fff; }
.logo-wahl.an { border-color:var(--blau); box-shadow:0 0 0 3px var(--blau-hell); }
.logo-wahl .bildchen { height:64px; display:flex; align-items:center; justify-content:center; }
.logo-wahl img { max-height:64px; max-width:100%; }
.logo-wahl .unten { display:flex; justify-content:space-between; align-items:center; gap:6px; font-size:.85em; }
#meldung { position:fixed; left:50%; bottom:20px; transform:translateX(-50%); background:var(--schwarz); color:#fff;
  padding:10px 16px; border-radius:var(--ecke) 0 0 0; z-index:30; box-shadow:0 2px 10px rgba(0,0,0,.3); }
#meldung.fehler { background:var(--rot); }
[hidden] { display:none !important; }   /* sonst gewinnt z.B. display:flex gegen hidden */
.dialog { position:fixed; inset:0; background:rgba(26,26,26,.55); z-index:15; display:flex; align-items:center;
  justify-content:center; padding:16px; }
.dialog > .feld { width:min(380px, 100%); max-height:calc(100vh - 32px); overflow:auto; }
.dialog > .feld.gross { width:min(720px, 100%); }
.dialog .vorschau { width:100%; aspect-ratio:4/3; max-height:260px; border-radius:var(--ecke) 0 0 0; background:var(--bg) center/contain no-repeat;
  border:1px solid var(--linie); display:flex; align-items:center; justify-content:center; color:var(--grau); }
input[type=file] { display:none; }
</style></head>
<body><header>%%MARKE%% <span class="unter">Lehrerbereich</span>
<nav><button data-tab="sim" class="an">Simulationen</button><button data-tab="welten">Welten</button>
<button data-tab="teile">Teile</button><a class="nav-link" href="/lehrer/baukasten">Baukasten</a><a class="nav-link" href="/lehrer/baukasten?modus=backen">Backensätze</a><button data-tab="protokoll">Protokoll</button><button data-tab="einst" class="nur-admin">Einstellungen</button>
<button data-tab="benutzer" class="nur-admin">Benutzer</button></nav>
<span id="ich"></span><button class="rechts" id="pw-knopf">Passwort</button>
<button class="rechts" id="abmelden">Abmelden</button></header>
<div id="meldung" hidden></div>

<div id="pw-dialog" class="dialog" hidden><div class="feld"><h2 id="pw-titel">Passwort ändern</h2>
  <div class="hilfe" id="pw-hinweis"></div>
  <label>Bisheriges Passwort <input id="pw-alt" type="password" autocomplete="current-password"></label>
  <label>Neues Passwort (mindestens 8 Zeichen) <input id="pw-neu" type="password" autocomplete="new-password"></label>
  <label>Neues Passwort wiederholen <input id="pw-neu2" type="password" autocomplete="new-password"></label>
  <div class="knopfreihe"><button class="haupt" id="pw-speichern">Speichern</button><button id="pw-abbrechen">Abbrechen</button></div>
</div></div>

<section id="tab-sim">
  <div class="leiste"><span id="belegt" class="hilfe"></span>
    <button id="alle-beenden" class="warnen" style="margin-left:auto">Alle beenden</button></div>
  <main id="liste"><div class="leer">lade ...</div></main>
</section>

<section id="tab-welten" hidden>
  <div class="spalte breit">
    <div class="filter"><input id="w-suche" type="search" placeholder="Welten durchsuchen ...">
      <span class="hilfe" id="w-anzahl"></span>
      <label class="knopf-datei" style="margin-left:auto" title="Welt-Paket (ZIP) aus „Exportieren“ einer anderen Instanz">Welt importieren
        <input type="file" id="w-import" accept=".zip,application/zip"></label>
      <a class="haupt" href="/lehrer/baukasten" style="padding:7px 12px;border-radius:3px;text-decoration:none">Neue Welt im Baukasten</a></div>
    <div class="feld"><table class="welten"><thead><tr><th></th><th>Welt</th><th>Greifer</th><th>Schüler</th><th></th></tr></thead>
      <tbody id="welten"></tbody></table>
      <div class="hilfe">Zeile anklicken zum Bearbeiten. Nur freigegebene Welten sehen die Schüler auf der Startseite;
        Änderungen gelten für neu gestartete Simulationen. Gelöschte Welten landen im Papierkorb
        (<code>worlds/papierkorb</code> im Container).</div></div>
  </div>
</section>

<div id="welt-dialog" class="dialog" hidden><div class="feld gross">
  <h2 id="wd-titel">Welt</h2><div class="welt-datei" id="wd-datei"></div>
  <label>Titel <input id="wd-name" maxlength="100"></label>
  <div class="zeile" style="align-items:center"><span style="flex:1">Aufgabentext (Markdown)</span>
    <span style="flex:0;white-space:nowrap"><button id="wd-md-text" class="an">Text</button><button id="wd-md-vorschau">Vorschau</button></span></div>
  <textarea id="wd-aufgabe" maxlength="5000" spellcheck="true"></textarea>
  <div id="wd-md-bild" class="md feld" style="min-height:9em;box-shadow:none" hidden></div>
  <div class="hilfe"><code># Überschrift</code> · <code>**fett**</code> · <code>*kursiv*</code> · <code>- Aufzählung</code> ·
    <code>1. Schritt</code> · <code>&gt; Hinweis</code> · <code>`DOut31`</code> · <code>[Link](https://…)</code> ·
    Tabelle: <code>| A | B |</code> und darunter <code>|---|---|</code>. Leerzeile = neuer Absatz.</div>
  <div class="zeile"><label>Greifer <select id="wd-greifer"></select></label>
    <label style="display:flex;gap:8px;align-items:center;margin-top:18px"><input type="checkbox" id="wd-sichtbar"> für Schüler freigegeben</label></div>
  <div class="hilfe" id="wd-greifer-hilfe"></div>
  <div class="zeile">
    <div><div class="vorschau" id="wd-bild">kein Vorschaubild</div>
      <div class="knopfreihe"><button id="wd-vorschau">Vorschaubild erzeugen</button>
        <label class="knopf-datei">Bild hochladen<input type="file" class="wd-datei" accept="image/png,image/jpeg"></label></div></div>
    <div><div class="hilfe">Arbeitsblatt (PDF, optional)</div>
      <div class="knopfreihe" id="wd-pdf"></div>
      <div class="hilfe" id="wd-laeuft" style="margin-top:12px"></div>
      <div class="knopfreihe" id="wd-baukasten"></div></div>
  </div>
  <div class="knopfreihe" style="margin-top:10px"><button class="haupt" id="wd-speichern">Speichern</button>
    <button id="wd-abbrechen">Schließen</button>
    <a id="wd-export" title="Welt mit allen Teilen und dem Backensatz als ZIP, z.B. für die Instanz in der Schule">Exportieren (ZIP)</a>
    <button class="warnen" id="wd-loeschen" style="margin-left:auto">Welt löschen</button></div>
</div></div>

<div id="import-dialog" class="dialog" hidden><div class="feld"><h2>Welt importieren</h2>
  <div id="im-text"></div>
  <label>Neuer Name <input id="im-name" autocapitalize="none"></label>
  <div class="hilfe">a-z, 0-9, - und _. „Ersetzen“ legt die vorhandene Welt in den Papierkorb.</div>
  <div class="knopfreihe"><button class="haupt" id="im-neu">Als neue Welt importieren</button>
    <button class="warnen" id="im-ersetzen">Vorhandene ersetzen</button><button id="im-abbrechen">Abbrechen</button></div>
</div></div>

<section id="tab-teile" hidden>
  <div class="spalte">
    <div class="feld"><h2>Teil aus STEP-Datei einlesen</h2>
      <div class="zeile"><label>Name in der Bibliothek <input id="t-name" placeholder="z.B. magazin_v2" autocapitalize="none"></label>
        <label>STEP-Datei <input id="t-datei" type="file" accept=".step,.stp,.STEP,.STP" style="display:block"></label></div>
      <div><button class="haupt" id="t-hochladen">Einlesen</button></div>
      <div class="hilfe">Erlaubt sind a-z, 0-9, - und _. Gewinde und Kleinteile machen das Einlesen langsam: am besten ohne
        Normteile exportieren. Platziert werden die Teile im Baukasten (kommt als Nächstes).</div></div>
    <div class="feld"><h2>Teilebibliothek</h2>
      <table><thead><tr><th>Name</th><th>Größe (mm)</th><th>Flächen</th><th>Bohrungen</th><th>Kanten</th><th>Ecken</th>
        <th>verwendet in</th><th></th></tr></thead><tbody id="teile"></tbody></table></div>
  </div>
</section>

<section id="tab-protokoll" hidden>
  <div class="spalte"><div class="feld"><h2>Wer hat was gemacht (neueste zuerst)</h2>
    <table><thead><tr><th>Zeit</th><th>Wer</th><th>Was</th></tr></thead><tbody id="protokoll"></tbody></table></div></div>
</section>

<section id="tab-einst" hidden>
  <div class="spalte">
    <div class="feld"><h2>Simulationen</h2>
      <label>Gleichzeitig höchstens <input id="e-max" type="number" min="1" max="20"></label>
      <label>Beenden nach so vielen Minuten ohne iRC <input id="e-leer" type="number" min="1" max="240"></label>
      <label>Alle beenden um (Stundenende, z.B. 09:30, 11:15) <input id="e-uhr" type="text" placeholder="keine"></label>
      <div><button class="haupt" id="e-speichern">Speichern</button></div></div>
    <div class="feld"><h2>Logo</h2>
      <div class="hilfe">Erscheint oben links auf allen Seiten (Startseite, Simulation, Lehrerbereich). Anklicken wählt es aus.
        PNG, JPG oder SVG, am besten das Logo „quer“ auf weißem Grund.</div>
      <div class="logos" id="logos"></div>
      <div class="zeile"><label>Name <input id="l-name" placeholder="z.B. drs_quer" autocapitalize="none"></label>
        <label class="knopf-datei" style="flex:0;align-self:end;white-space:nowrap">Logo hochladen<input type="file" id="l-datei" accept="image/png,image/jpeg,image/svg+xml,.svg"></label></div>
    </div>
  </div>
</section>

<section id="tab-benutzer" hidden>
  <div class="spalte">
    <div class="feld"><h2>Konten</h2>
      <table><thead><tr><th>Benutzer</th><th>Name</th><th>Rolle</th><th>Zustand</th><th></th></tr></thead>
        <tbody id="benutzer"></tbody></table>
      <div class="hilfe">Admins verwalten Konten und Einstellungen. Lehrkräfte steuern Simulationen und pflegen Welten.
        Ein zurückgesetztes Passwort muss beim nächsten Anmelden geändert werden.</div></div>
    <div class="feld"><h2>Neues Konto</h2>
      <div class="zeile"><label>Benutzername <input id="n-name" placeholder="z.B. mueller" autocapitalize="none"></label>
        <label>Anzeigename <input id="n-anzeige" placeholder="z.B. Frau Müller"></label></div>
      <div class="zeile"><label>Rolle <select id="n-rolle"><option value="lehrkraft">Lehrkraft</option>
          <option value="admin">Admin</option></select></label>
        <label><span id="n-pw-text">Vorläufiges Passwort</span> <input id="n-passwort" type="text" autocomplete="off"></label></div>
      <div><button class="haupt" id="n-anlegen">Anlegen</button></div>
      <div class="hilfe" id="n-hinweis"></div></div>
  </div>
</section>

<script>
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}
const $ = id => document.getElementById(id);
let meldungZeit, ich = {};
function melden(text, fehler) {
  const m = $('meldung');
  m.textContent = text; m.className = fehler ? 'fehler' : ''; m.hidden = false;
  // lange Meldungen (z.B. Bericht des Imports) bleiben laenger stehen
  clearTimeout(meldungZeit); meldungZeit = setTimeout(() => m.hidden = true, Math.max(fehler ? 6000 : 2500, text.length * 70));
}
async function api(aktion, daten) {
  const r = await fetch('/api/lehrer/' + aktion, {method: 'POST', body: JSON.stringify(daten || {}),
    headers: {'Content-Type': 'application/json', 'X-Lehrer': '1'}});
  const d = await r.json().catch(() => ({}));
  if (r.status === 403) { location.reload(); throw new Error(); }
  if (!r.ok) { melden(d.fehler || 'Das hat nicht geklappt.', true); throw new Error(d.fehler); }
  return d;
}
function vorschlag() {   // vorlaeufiges Passwort zum Weitergeben
  const z = 'abcdefghjkmnpqrstuvwxyz23456789';
  return Array.from(crypto.getRandomValues(new Uint8Array(10)), b => z[b % z.length]).join('');
}

// --- wer bin ich, Rechte, Passwort ---
async function anmeldung() {
  ich = await (await fetch('/api/lehrer/ich', {cache: 'no-store'})).json();
  $('ich').textContent = ich.anzeige + (ich.rolle === 'admin' ? ' (Admin)' : '');
  document.querySelectorAll('.nur-admin').forEach(b => b.hidden = ich.rolle !== 'admin');
  $('pw-knopf').hidden = !!ich.vertraut;
  if (ich.vertraut) {   // Lehrer-PC ohne Anmeldung: mit eigenem Konto anmelden koennen
    $('abmelden').textContent = 'Anmelden'; $('abmelden').onclick = () => location.href = '/lehrer?anmelden=1';
  }
  if (ich.muss_aendern) passwortDialog(true);
  if (ich.vertraut && !ich.benutzer_vorhanden) {
    melden('Noch kein Konto vorhanden: im Reiter Benutzer zuerst ein Admin-Konto für dich anlegen.');
    document.querySelector('[data-tab="benutzer"]').click();
  }
}
function passwortDialog(pflicht) {
  $('pw-dialog').hidden = false;
  $('pw-titel').textContent = pflicht ? 'Bitte eigenes Passwort festlegen' : 'Passwort ändern';
  $('pw-hinweis').textContent = pflicht ? 'Du hast ein vorläufiges Passwort bekommen. Lege jetzt ein eigenes fest.' : '';
  $('pw-abbrechen').hidden = pflicht;
  $('pw-alt').focus();
}
$('pw-knopf').onclick = () => passwortDialog(false);
$('pw-abbrechen').onclick = () => $('pw-dialog').hidden = true;
$('pw-speichern').onclick = async () => {
  if ($('pw-neu').value !== $('pw-neu2').value) { melden('Die beiden neuen Passwörter sind verschieden.', true); return; }
  await api('passwort', {alt: $('pw-alt').value, neu: $('pw-neu').value});
  ['pw-alt', 'pw-neu', 'pw-neu2'].forEach(id => $(id).value = '');
  $('pw-dialog').hidden = true; melden('Passwort gespeichert.'); anmeldung();
};
$('abmelden').onclick = async () => {
  await fetch('/lehrer/abmelden', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
  location.reload();
};

// --- Reiter ---
const LADEN = {teile: () => teile(), welten: () => welten(), protokoll: () => protokoll(), einst: () => einstellungen(), benutzer: () => benutzer()};
document.querySelectorAll('nav button').forEach(b => b.onclick = () => {
  document.querySelectorAll('nav button').forEach(x => x.classList.toggle('an', x === b));
  ['sim', 'welten', 'teile', 'protokoll', 'einst', 'benutzer'].forEach(t => $('tab-' + t).hidden = t !== b.dataset.tab);
  if (LADEN[b.dataset.tab]) LADEN[b.dataset.tab]();
});

// --- Simulationen ---
function karte(p) {
  const gelenke = p.gelenke_grad ? 'A1..A6: ' + p.gelenke_grad.map(v => v.toFixed(0)).join(' / ') : '';
  return '<div class="platz"><div class="kopf"><span class="punkt ' + esc(p.farbe) + '"></span>Platz ' + esc(p.platz) + '</div>'
    + '<div>' + esc(p.text) + '</div>'
    + '<div class="leise">PC ' + esc(p.irc_host) + ' &middot; ' + esc(p.titel) + (p.dauerhaft ? ' (dauerhaft)' : '') + '</div>'
    + '<div class="leise">' + esc(gelenke) + '</div>'
    + (p.hilfe ? '<div class="leise">' + esc(p.hilfe) + '</div>' : '')
    + '<div class="knopfreihe"><a href="/platz/' + esc(p.platz) + '" target="_blank">Ansehen</a>'
    + '<button data-teile="' + esc(p.platz) + '" title="Werkstücke auf die Startlage, ohne Neustart">Teile zurück</button>'
    + '<button data-reset="' + esc(p.platz) + '" title="Webots neu starten">Neu starten</button>'
    + '<button data-ende="' + esc(p.platz) + '" class="warnen">Beenden</button></div></div>';
}
async function simulationen() {
  try {
    const r = await fetch('/status.json', {cache: 'no-store'});
    if (r.status === 403) { location.reload(); return; }
    const liste = await r.json();
    $('belegt').textContent = liste.length + (liste.length === 1 ? ' Simulation läuft' : ' Simulationen laufen');
    $('liste').innerHTML = liste.length ? liste.map(karte).join('')
      : '<div class="leer">Gerade läuft keine Simulation. Schüler starten sie selbst unter ' + esc(location.host) + '.</div>';
  } catch (e) { $('liste').innerHTML = '<div class="leer">Server nicht erreichbar</div>'; }
}
$('liste').onclick = async ev => {
  const b = ev.target.closest('button');
  if (!b) return;
  if (b.dataset.teile) {
    await api('teile_zuruecksetzen', {platz: b.dataset.teile}); melden('Platz ' + b.dataset.teile + ': Teile liegen wieder am Start.');
  }
  if (b.dataset.reset && confirm('Simulation auf Platz ' + b.dataset.reset + ' neu starten?')) {
    await api('zuruecksetzen', {platz: b.dataset.reset}); melden('Platz ' + b.dataset.reset + ' wird neu gestartet.');
  }
  if (b.dataset.ende && confirm('Simulation auf Platz ' + b.dataset.ende + ' beenden?')) {
    await api('beenden', {platz: b.dataset.ende}); melden('Platz ' + b.dataset.ende + ' beendet.'); simulationen();
  }
};
$('alle-beenden').onclick = async () => {
  if (!confirm('Wirklich ALLE Simulationen beenden?')) return;
  const d = await api('alle_beenden'); melden(d.anzahl + ' Simulation(en) beendet.'); simulationen();
};

// --- Welten: Liste, Klick oeffnet den Dialog ---
let weltListe = [], greiferNamen = [], weltOffen = null;
const greiferText = n => (greiferNamen.find(g => g.name === n) || {}).titel || n || '–';
async function welten() {
  [weltListe, greiferNamen] = await Promise.all([fetch('/api/lehrer/welten', {cache: 'no-store'}).then(r => r.json()),
                                                 fetch('/api/lehrer/greifer_liste', {cache: 'no-store'}).then(r => r.json())]);
  weltenZeigen();
  if (weltOffen) { const w = weltListe.find(w => w.datei === weltOffen); if (w) weltDialog(w, true); }
}
function weltenZeigen() {
  const such = $('w-suche').value.trim().toLowerCase(), t = Date.now();
  const liste = weltListe.filter(w => !such || (w.titel + ' ' + w.datei + ' ' + w.aufgabe).toLowerCase().includes(such));
  $('w-anzahl').textContent = liste.length + ' von ' + weltListe.length + ' Welten, ' + weltListe.filter(w => w.sichtbar).length + ' freigegeben';
  $('welten').innerHTML = liste.map(w => '<tr class="zeile-welt" data-datei="' + esc(w.datei) + '">'
    + '<td style="width:96px">' + (w.bild ? '<div class="mini" style="background-image:url(\\'/welten/' + esc(w.bild) + '?' + t + '\\')"></div>'
                                          : '<div class="mini">kein Bild</div>') + '</td>'
    + '<td><div class="welt-titel">' + esc(w.titel) + '</div><div class="welt-datei">' + esc(w.datei)
    + (w.baukasten ? ' · Baukasten' : '') + (w.pdf ? ' · Arbeitsblatt' : '') + '</div>'
    + '<div class="welt-aufgabe">' + esc(w.aufgabe_kurz || '') + '</div></td>'
    + '<td>' + esc(greiferText(w.greifer)) + '</td>'
    + '<td><button class="chip ' + (w.sichtbar ? 'frei' : 'zu') + '" data-freigabe title="Klick schaltet um">'
    + (w.sichtbar ? 'freigegeben' : 'gesperrt') + '</button>'
    + (w.laeuft.length ? ' <span class="chip laeuft" title="läuft gerade">Platz ' + w.laeuft.map(esc).join(', ') + '</span>' : '') + '</td>'
    + '<td class="knoepfe">' + (w.baukasten ? '<a class="klein" href="/lehrer/baukasten?welt=' + encodeURIComponent(w.datei.replace(/[.]wbt$/, ''))
       + '">Baukasten</a> ' : '') + '<button data-bearbeiten>Bearbeiten</button></td></tr>').join('')
    || '<tr><td colspan="5" class="hilfe">' + (weltListe.length ? 'Keine Welt passt zur Suche.' : 'Noch keine Welten.') + '</td></tr>';
}
$('w-suche').oninput = weltenZeigen;
$('welten').onclick = async ev => {
  const zeile = ev.target.closest('[data-datei]'); if (!zeile || ev.target.closest('a')) return;
  const w = weltListe.find(w => w.datei === zeile.dataset.datei);
  if (ev.target.closest('[data-freigabe]')) {
    await api('welt', {datei: w.datei, titel: w.titel, aufgabe: w.aufgabe, sichtbar: !w.sichtbar});
    melden(w.titel + (w.sichtbar ? ': für Schüler gesperrt.' : ': für Schüler freigegeben.')); welten(); return;
  }
  weltDialog(w);
};
function weltDialog(w, nurDateien) {
  weltOffen = w.datei;
  if (!nurDateien) {   // beim Neuladen nach Bild/PDF die Eingaben nicht ueberschreiben
    $('wd-titel').textContent = w.titel; $('wd-name').value = w.titel; $('wd-aufgabe').value = w.aufgabe || '';
    $('wd-sichtbar').checked = w.sichtbar;
    const namen = greiferNamen.map(g => g.name);
    $('wd-greifer').innerHTML = (w.greifer && !namen.includes(w.greifer) ? '<option value="' + esc(w.greifer) + '">' + esc(w.greifer) + ' (unbekannt)</option>' : '')
      + greiferNamen.map(g => '<option value="' + esc(g.name) + '">' + esc(g.titel) + '</option>').join('');
    $('wd-greifer').value = w.greifer || ''; $('wd-greifer').disabled = !w.greifer;
    $('wd-greifer-hilfe').textContent = w.greifer ? 'In iRC dasselbe Werkzeug wählen, sonst stimmt der TCP nicht.'
                                                  : 'Diese Welt hat kein Greifer-Feld am ReBeL.';
  }
  $('wd-datei').textContent = w.datei + (w.baukasten ? ' · im Baukasten gebaut' : '');
  const bild = $('wd-bild');
  bild.style.backgroundImage = w.bild ? 'url(\\'/welten/' + w.bild + '?' + Date.now() + '\\')' : ''; bild.textContent = w.bild ? '' : 'kein Vorschaubild';
  $('wd-pdf').innerHTML = (w.pdf ? '<a href="/welten/' + esc(w.pdf) + '" target="_blank">ansehen</a><button data-pdf-weg>entfernen</button>' : '')
    + '<label class="knopf-datei">' + (w.pdf ? 'ersetzen' : 'hochladen') + '<input type="file" class="wd-datei" accept="application/pdf"></label>';
  $('wd-laeuft').textContent = w.laeuft.length ? 'Läuft gerade auf Platz ' + w.laeuft.join(', ') + ' – Löschen geht erst nach dem Beenden.' : '';
  $('wd-loeschen').disabled = w.laeuft.length > 0;
  $('wd-export').href = '/api/lehrer/welt_export?datei=' + encodeURIComponent(w.datei);
  $('wd-baukasten').innerHTML = w.baukasten ? '<a href="/lehrer/baukasten?welt=' + encodeURIComponent(w.datei.replace(/[.]wbt$/, '')) + '">Im Baukasten öffnen</a>' : '';
  $('welt-dialog').hidden = false;
}
function weltDialogZu() { $('welt-dialog').hidden = true; weltOffen = null; mdModus(false); }
// Aufgabe: Text bearbeiten oder Vorschau (gerendert wie bei den Schuelern, tools/aufgabe_md.py)
async function mdModus(vorschau) {
  $('wd-md-text').classList.toggle('an', !vorschau); $('wd-md-vorschau').classList.toggle('an', vorschau);
  $('wd-aufgabe').hidden = vorschau; $('wd-md-bild').hidden = !vorschau;
  if (vorschau) $('wd-md-bild').innerHTML = (await api('markdown', {text: $('wd-aufgabe').value})).html
    || '<span class="hilfe">(leer)</span>';
}
$('wd-md-text').onclick = () => mdModus(false);
$('wd-md-vorschau').onclick = () => mdModus(true);
$('wd-abbrechen').onclick = weltDialogZu;
$('welt-dialog').addEventListener('click', ev => { if (ev.target === $('welt-dialog')) weltDialogZu(); });
$('wd-speichern').onclick = async () => {
  const w = weltListe.find(w => w.datei === weltOffen);
  await api('welt', {datei: w.datei, titel: $('wd-name').value, aufgabe: $('wd-aufgabe').value, sichtbar: $('wd-sichtbar').checked,
                     greifer: $('wd-greifer').disabled ? undefined : $('wd-greifer').value});
  melden('Gespeichert.'); weltDialogZu(); welten();
};
$('wd-vorschau').onclick = async () => {
  const b = $('wd-vorschau'); b.disabled = true; b.textContent = 'Webots rechnet ...';
  try { await api('vorschau', {datei: weltOffen}); melden('Vorschaubild erzeugt.'); }
  finally { b.disabled = false; b.textContent = 'Vorschaubild erzeugen'; welten(); }
};
$('wd-pdf').onclick = async ev => {
  if (ev.target.closest('[data-pdf-weg]') && confirm('Arbeitsblatt entfernen?')) { await api('datei_loeschen', {datei: weltOffen, art: 'pdf'}); welten(); }
};
$('welt-dialog').addEventListener('change', async ev => {
  if (!ev.target.classList.contains('wd-datei') || !ev.target.files.length) return;
  const f = ev.target.files[0];
  const r = await fetch('/api/lehrer/upload?datei=' + encodeURIComponent(weltOffen), {method: 'POST', body: f,
    headers: {'Content-Type': f.type || 'application/octet-stream', 'X-Lehrer': '1'}});
  const d = await r.json().catch(() => ({}));
  if (r.ok) melden('Hochgeladen: ' + d.datei); else melden(d.fehler || 'Hochladen fehlgeschlagen.', true);
  welten();
});
// Import: ZIP hochladen; gibt es die Welt schon, fragt der Dialog nach neuem Namen oder Ersetzen
let importDatei = null, importVorhanden = null;
async function importSenden(name, ersetzen) {
  const q = new URLSearchParams();
  if (name) q.set('name', name);
  if (ersetzen) q.set('ersetzen', '1');
  melden('importiere ' + importDatei.name + ' ...');
  const r = await fetch('/api/lehrer/welt_import?' + q, {method: 'POST', body: importDatei,
    headers: {'Content-Type': 'application/zip', 'X-Lehrer': '1'}});
  const d = await r.json().catch(() => ({}));
  if (r.status === 409 && d.vorhanden) {
    importVorhanden = d.vorhanden;
    $('im-text').textContent = 'Die Welt „' + d.vorhanden + '“ gibt es hier schon. Unter neuem Namen importieren oder die vorhandene ersetzen?';
    $('im-name').value = d.vorschlag; $('import-dialog').hidden = false; $('meldung').hidden = true;
    return;
  }
  if (!r.ok) { melden(d.fehler || 'Import fehlgeschlagen.', true); return; }
  $('import-dialog').hidden = true;
  const teile = d.teile_neu.length ? ', neue Teile: ' + d.teile_neu.join(', ') : '';
  const umbenannt = Object.entries(d.teile_umbenannt).map(([a, n]) => a + ' → ' + n).join(', ');
  melden('Importiert: „' + d.titel + '“ (' + d.welt + ')' + teile + (umbenannt ? ' (abweichend, daher umbenannt: ' + umbenannt + ')' : '')
         + (d.backensatz_neu ? ', Backensatz ' + d.backensatz : '') + '. Für Schüler noch gesperrt.'
         + (d.hinweise.length ? ' Achtung: ' + d.hinweise.join(' ') : ''), d.hinweise.length > 0);
  await welten();
  $('w-suche').value = ''; weltenZeigen();
}
$('w-import').onchange = () => {
  importDatei = $('w-import').files[0]; $('w-import').value = '';
  if (importDatei) importSenden(null, false);
};
$('im-neu').onclick = () => importSenden($('im-name').value.trim().toLowerCase(), false);
$('im-ersetzen').onclick = () => {
  if (confirm('Vorhandene Welt „' + importVorhanden + '“ ersetzen? Sie wandert in den Papierkorb.')) importSenden(importVorhanden, true);
};
$('im-abbrechen').onclick = () => { $('import-dialog').hidden = true; };
$('wd-loeschen').onclick = async () => {
  const w = weltListe.find(w => w.datei === weltOffen);
  const dateien = ['.wbt', '.json'].concat(w.baukasten ? ['.baukasten.json'] : [], w.bild ? [w.bild.slice(w.bild.lastIndexOf('.'))] : [],
                                           w.pdf ? ['.pdf'] : []).map(e => w.datei.replace(/[.]wbt$/, '') + e);
  if (!confirm('Welt „' + w.titel + '“ löschen?\\n\\nSie verschwindet aus der Liste und von der Startseite der Schüler.\\n'
               + 'Diese Dateien wandern in den Papierkorb (worlds/papierkorb im Container):\\n  ' + dateien.join('\\n  '))) return;
  await api('welt_loeschen', {datei: w.datei});
  melden('Welt „' + w.titel + '“ gelöscht (im Papierkorb).'); weltDialogZu(); welten();
};

// --- Teile ---
async function teile() {
  const liste = await (await fetch('/api/lehrer/teile', {cache: 'no-store'})).json();
  $('teile').innerHTML = liste.map(t => '<tr data-name="' + esc(t.name) + '"><td>' + esc(t.name)
    + '<div class="hilfe">' + esc(t.quelle) + '</div></td><td>' + (t.groesse_mm || []).map(v => v.toFixed(1)).join(' × ')
    + '</td><td>' + t.anzahl.flaechen + '</td><td>' + t.anzahl.bohrungen + '</td><td>' + t.anzahl.kanten + '</td><td>'
    + t.anzahl.ecken + '</td><td>' + esc(t.verwendet_in.join(', ')) + '</td><td class="knoepfe">'
    + (t.verwendet_in.length ? '' : '<button class="t-loeschen warnen">Löschen</button>') + '</td></tr>').join('')
    || '<tr><td colspan="8" class="hilfe">Noch keine Teile.</td></tr>';
}
$('t-hochladen').onclick = async () => {
  const f = $('t-datei').files[0], name = $('t-name').value.trim().toLowerCase();
  if (!f || !name) { melden('Bitte Name und STEP-Datei angeben.', true); return; }
  const b = $('t-hochladen'); b.disabled = true; b.textContent = 'lese ein ...';
  try {
    const r = await fetch('/api/lehrer/teil_hochladen?name=' + encodeURIComponent(name), {method: 'POST', body: f,
      headers: {'Content-Type': 'application/octet-stream', 'X-Lehrer': '1'}});
    const d = await r.json().catch(() => ({}));
    if (r.ok) { melden('Eingelesen: ' + name + ' (' + d.flaechen + ' Flächen, ' + d.bohrungen + ' Bohrungen)'); $('t-name').value = ''; }
    else melden(d.fehler || 'Einlesen fehlgeschlagen.', true);
  } finally { b.disabled = false; b.textContent = 'Einlesen'; teile(); }
};
$('teile').onclick = async ev => {
  const b = ev.target.closest('button.t-loeschen');
  if (!b) return;
  const name = b.closest('[data-name]').dataset.name;
  if (confirm('Teil ' + name + ' aus der Bibliothek löschen?')) { await api('teil_loeschen', {name}); teile(); }
};

// --- Protokoll ---
async function protokoll() {
  const liste = await (await fetch('/api/lehrer/protokoll', {cache: 'no-store'})).json();
  $('protokoll').innerHTML = liste.map(e => '<tr><td>' + esc(e.zeit) + '</td><td>' + esc(e.wer) + '</td><td>'
    + esc(e.was) + '</td></tr>').join('') || '<tr><td colspan="3" class="hilfe">Noch keine Einträge.</td></tr>';
}

// --- Einstellungen (Admin) ---
async function einstellungen() {
  const e = await (await fetch('/api/lehrer/einstellungen', {cache: 'no-store'})).json();
  $('e-max').value = e.max_simulationen;
  $('e-leer').value = e.leerlauf_minuten;
  $('e-uhr').value = (e.ende_uhrzeiten || []).join(', ');
  logosZeigen();
}
async function logosZeigen() {
  const d = await (await fetch('/api/lehrer/logos', {cache: 'no-store'})).json(), t = Date.now();
  $('logos').innerHTML = '<div class="logo-wahl' + (d.gewaehlt ? '' : ' an') + '" data-logo=""><div class="bildchen hilfe">kein Logo</div>'
    + '<div class="unten"><span>nur Schriftzug</span></div></div>'
    + d.logos.map(n => '<div class="logo-wahl' + (n === d.gewaehlt ? ' an' : '') + '" data-logo="' + esc(n) + '">'
      + '<div class="bildchen"><img src="/logos/' + encodeURIComponent(n) + '?' + t + '" alt=""></div>'
      + '<div class="unten"><span>' + esc(n) + '</span><button data-weg title="Logo löschen">Löschen</button></div></div>').join('');
}
$('logos').onclick = async ev => {
  const k = ev.target.closest('[data-logo]'); if (!k) return;
  const name = k.dataset.logo;
  if (ev.target.closest('[data-weg]')) {
    if (!confirm('Logo ' + name + ' löschen?')) return;
    await api('logo_loeschen', {name}); melden('Logo gelöscht.');
  } else { await api('logo_waehlen', {name}); melden(name ? 'Logo gewählt.' : 'Kein Logo mehr.'); }
  location.reload();   // die Kopfzeile zeigt das neue Logo
};
$('l-datei').onchange = async () => {
  const f = $('l-datei').files[0]; if (!f) return;
  const name = $('l-name').value.trim() || f.name;
  const r = await fetch('/api/lehrer/logo_hochladen?name=' + encodeURIComponent(name), {method: 'POST', body: f,
    headers: {'Content-Type': 'application/octet-stream', 'X-Lehrer': '1'}});
  const d = await r.json().catch(() => ({}));
  $('l-datei').value = '';
  if (r.ok) { melden('Hochgeladen: ' + d.datei + ' – zum Verwenden anklicken.'); $('l-name').value = ''; logosZeigen(); }
  else melden(d.fehler || 'Hochladen fehlgeschlagen.', true);
};
$('e-speichern').onclick = async () => {
  await api('einstellungen', {max_simulationen: $('e-max').value, leerlauf_minuten: $('e-leer').value,
                              ende_uhrzeiten: $('e-uhr').value});
  melden('Einstellungen gespeichert.'); einstellungen();
};

// --- Benutzer (Admin) ---
async function benutzer() {
  const liste = await (await fetch('/api/lehrer/benutzer', {cache: 'no-store'})).json();
  $('benutzer').innerHTML = liste.map(b => '<tr data-name="' + esc(b.name) + '"><td>' + esc(b.name)
    + (b.name === ich.name ? ' (du)' : '') + '</td><td>' + esc(b.anzeige) + '</td>'
    + '<td><select class="b-rolle"><option value="lehrkraft"' + (b.rolle === 'lehrkraft' ? ' selected' : '') + '>Lehrkraft</option>'
    + '<option value="admin"' + (b.rolle === 'admin' ? ' selected' : '') + '>Admin</option></select></td>'
    + '<td>' + (b.gesperrt ? '<span class="gesperrt">gesperrt</span>' : 'aktiv')
    + (b.muss_aendern ? '<div class="hilfe">vorläufiges Passwort</div>' : '') + '</td>'
    + '<td class="knoepfe"><button class="b-pw">Passwort zurücksetzen</button> '
    + '<button class="b-sperren">' + (b.gesperrt ? 'Entsperren' : 'Sperren') + '</button> '
    + '<button class="b-loeschen warnen">Löschen</button></td></tr>').join('')
    || '<tr><td colspan="5" class="hilfe">Noch kein Konto.</td></tr>';
  $('n-passwort').value = vorschlag();
  if (!ich.benutzer_vorhanden) {
    $('n-rolle').value = 'admin';
    $('n-hinweis').textContent = 'Das erste Konto ist dein eigenes Admin-Konto. Wähle dafür ein eigenes Passwort.';
    $('n-passwort').value = '';
    $('n-pw-text').textContent = 'Dein Passwort (mindestens 8 Zeichen)';
  }
}
$('benutzer').onchange = async ev => {
  const name = ev.target.closest('[data-name]').dataset.name;
  try { await api('benutzer_aendern', {name, rolle: ev.target.value}); melden('Rolle geändert.'); } finally { benutzer(); }
};
$('benutzer').onclick = async ev => {
  const b = ev.target.closest('button'), zeile = ev.target.closest('[data-name]');
  if (!b || !zeile) return;
  const name = zeile.dataset.name;
  if (b.classList.contains('b-pw')) {
    const pw = prompt('Vorläufiges Passwort für ' + name + ' (mindestens 8 Zeichen, bitte notieren):', vorschlag());
    if (!pw) return;
    await api('benutzer_passwort', {name, passwort: pw});
    melden('Passwort von ' + name + ' zurückgesetzt.');
  } else if (b.classList.contains('b-sperren')) {
    await api('benutzer_aendern', {name, gesperrt: b.textContent === 'Sperren'});
  } else if (b.classList.contains('b-loeschen')) {
    if (!confirm('Konto ' + name + ' wirklich löschen?')) return;
    await api('benutzer_loeschen', {name});
  }
  benutzer();
};
$('n-anlegen').onclick = async () => {
  const erstes = !ich.benutzer_vorhanden, name = $('n-name').value.trim().toLowerCase(), pw = $('n-passwort').value;
  await api('benutzer_anlegen', {name, anzeige: $('n-anzeige').value, rolle: $('n-rolle').value, passwort: pw});
  $('n-name').value = $('n-anzeige').value = ''; $('n-hinweis').textContent = '';
  if (erstes) { melden('Konto angelegt. Bitte jetzt damit anmelden.'); setTimeout(() => location.href = '/lehrer?anmelden=1', 1500); return; }
  melden('Angelegt: ' + name + ', vorläufiges Passwort ' + pw);
  await anmeldung(); benutzer();
};

anmeldung().then(() => { simulationen(); setInterval(() => { if (!$('tab-sim').hidden) simulationen(); }, 2000); });
</script></body></html>"""

VIEWER = """<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>%(titel)s - ReBeL</title>
<style>%(stil)s
html,body { height:100%%; } body { display:flex; flex-direction:column; }
header { flex-wrap:wrap; }
.ansicht { flex:1; min-height:0; display:flex; }
.bildflaeche { flex:1; min-width:0; position:relative; }
webots-view { display:block; width:100%%; height:100%%; position:relative; }
#hinweis { font-size:.9em; color:var(--leise); }
.knoepfe { margin-left:auto; display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
.knoepfe a, .knoepfe button { color:var(--text); background:var(--knopf); border:0; border-radius:3px; padding:7px 11px;
  font:inherit; font-size:.9em; cursor:pointer; text-decoration:none; }
.knoepfe a:hover, .knoepfe button:hover { background:var(--blau-hell); color:var(--blau); }
.knoepfe button.warnen { background:var(--pink); color:#fff; }
#irc { font-size:.9em; padding:4px 10px; border-radius:10px; background:var(--knopf); font-weight:700; }
#irc.gruen { background:var(--gruen); color:var(--schwarz); } #irc.gelb { background:var(--gelb); color:var(--schwarz); }
#irc.rot { background:var(--pink); color:#fff; }
/* Aufgabe: Seitenleiste rechts, erst auf Klick; das 3D-Bild wird schmaler statt verdeckt */
#aufgabe { width:min(400px, 42vw); flex:none; overflow:auto; background:var(--karte); border-left:3px solid var(--blau);
  padding:14px 18px 18px; }
#aufgabe .kopf { display:flex; align-items:flex-start; gap:8px; margin-bottom:10px; }
#aufgabe h2 { margin:0; font-size:1.15em; color:var(--blau); flex:1; }
#aufgabe .zu { border:0; background:var(--knopf); border-radius:3px; font:inherit; font-size:1.1em; line-height:1;
  padding:5px 9px; cursor:pointer; }
#aufgabe .pdf { display:inline-block; margin-top:6px; color:var(--blau); font-weight:700; }
.knoepfe button.an { background:var(--blau); color:#fff; }
[hidden] { display:none !important; }
@media (max-width: 760px) { .ansicht { position:relative; }
  #aufgabe { position:absolute; inset:0; width:auto; z-index:8; border-left:0; } }
.ansichten { position:absolute; right:10px; bottom:52px; z-index:5; display:flex; gap:4px; flex-wrap:wrap;
  background:rgba(26,26,26,.72); padding:5px; border-radius:var(--ecke) 0 0 0; }
.ansichten button { color:#fff; background:transparent; border:0; border-radius:5px; padding:5px 9px;
  font:inherit; font-size:.85em; cursor:pointer; }
.ansichten button.an { background:var(--blau); }
.ea { position:absolute; bottom:52px; z-index:5; display:flex; flex-direction:column; gap:5px; max-width:44%%;
  background:rgba(26,26,26,.75); padding:8px; border-radius:var(--ecke) 0 0 0; color:#fff; font-size:.88em; }
.ea.links { left:10px; bottom:100px; } .ea.rechts { right:10px; bottom:100px; }   /* unten links steht die Einteach-Hilfe */
.ea .taster, .ea .leuchte { display:flex; align-items:center; gap:8px; background:transparent; color:#fff; border:0;
  font:inherit; padding:2px 4px; text-align:left; touch-action:none; user-select:none; -webkit-user-select:none; }
.ea .taster { cursor:pointer; border-radius:8px; } .ea .taster:hover { background:rgba(255,255,255,.08); }
.ea small { color:#aeb6c6; font-size:.82em; display:block; }
.ea .kappe { width:30px; height:30px; flex:none; border-radius:50%%; background:#c9ced8; border:3px solid #5d6577;
  box-shadow:0 3px 0 #2c3242; transition:transform .05s; }
.ea .taster.druck .kappe, .ea .taster.an .kappe { transform:translateY(2px); box-shadow:0 1px 0 #2c3242; }
.ea .taster.an .kappe { background:var(--gelb); }
.ea .lampe { width:22px; height:22px; flex:none; border-radius:50%%; background:#3b4152; border:2px solid #5d6577; }
.ea .leuchte.an .lampe { border-color:#fff; }
.ea .sensor { display:flex; align-items:center; gap:8px; padding:2px 4px; }
.ea .sensor .lampe { width:16px; height:16px; margin:0 7px; border-radius:3px; }
.ea .sensor.an .lampe { background:#2ecc71; box-shadow:0 0 10px #2ecc71; border-color:#fff; }
.ea .leuchte.an[data-farbe=gruen] .lampe { background:#2ecc71; box-shadow:0 0 12px #2ecc71; }
.ea .leuchte.an[data-farbe=gelb] .lampe { background:#f1c40f; box-shadow:0 0 12px #f1c40f; }
.ea .leuchte.an[data-farbe=rot] .lampe { background:#e74c3c; box-shadow:0 0 12px #e74c3c; }
.ea .leuchte.an[data-farbe=blau] .lampe { background:#3498db; box-shadow:0 0 12px #3498db; }
.ea .leuchte.an[data-farbe=weiss] .lampe { background:#fff; box-shadow:0 0 12px #fff; }
#ea-meldung { color:#ffd27a; font-size:.85em; max-width:240px; }
.ea.nur-sehen .taster { cursor:default; pointer-events:none; }
</style></head>
<body><header>%(marke)s <span class="unter">%(titel)s</span> <span id="hinweis">verbinde ...</span>
<div class="knoepfe">%(knoepfe)s</div></header>
<div class="ansicht"><div class="bildflaeche"><webots-view></webots-view>
<div class="ansichten" title="Blickrichtung. Mit der Maus: linke Taste drehen, rechte schieben, nach einem Klick Rad zoomen.">
<button data-ansicht="standard" class="an">Standard</button><button data-ansicht="vorne">Vorne</button>
<button data-ansicht="seite">Seite</button><button data-ansicht="oben">Oben</button>
<button data-ansicht="greifer" title="Kamera folgt dem Greifer, Blick quer zu den Backen">Greifer</button></div>%(ea)s</div>%(aufgabe)s</div>
<script type="module">
import "%(webots_js)s";
import MeshLoader from "%(wwi)s/MeshLoader.js";
import ImageLoader from "%(wwi)s/ImageLoader.js";
import WbWorld from "%(wwi)s/nodes/WbWorld.js";
import WbVector3 from "%(wwi)s/nodes/utils/WbVector3.js";
import WbVector4 from "%(wwi)s/nodes/utils/WbVector4.js";
// Im Nur-Zuschauen-Modus schickt Webots den Weltnamen nicht mit; ohne ihn findet
// der Viewer die Robotermodelle (Meshes) nicht. Deshalb hier selbst setzen.
MeshLoader.currentWorld = ImageLoader.currentWorld = 'worlds/%(welt)s';
const mobil = /Android|webOS|iPhone|iPad|iPod/i.test(navigator.userAgent);
const hinweis = document.getElementById('hinweis');
const view = document.querySelector('webots-view');
// Weltauswahl aus: die Werkzeugleiste von R2025a stuerzt im Nur-Zuschauen-Modus
// sonst ab (sie erwartet eine Weltenliste, die Webots dann nicht schickt).
view.showWorldSelection = false;
function verbinden() {
  hinweis.textContent = 'verbinde ...';
  view.onready = () => { hinweis.textContent = 'verbunden (PC %(irc_host)s)'; };
  view.ondisconnect = () => { hinweis.textContent = 'getrennt - neuer Versuch in 5 s'; setTimeout(verbinden, 5000); };
  // Stream ueber die eigene Adresse (Durchleitung /sim/NN), nicht direkt auf
  // Port %(stream_port)d: Browser sperren Verbindungen zu fremden Ports im lokalen Netz.
  // broadcast=true: nur zuschauen, keine Steuerknoepfe fuer Pause/Reset
  const ws = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/sim/%(platz)s';
  view.connect(ws, 'w3d', %(broadcast)s, mobil, -1,
               'https://cyberbotics.com/wwi/R2025a/images/loading/default_thumbnail.png');
}
customElements.whenDefined('webots-view').then(verbinden);

// --- Ansichten: Kamera nur hier im Browser umstellen (Webots selbst bleibt unberuehrt) ---
// Ziele in Weltkoordinaten (m, Ursprung Roboterfuss), passend zum Robotertisch
const ANSICHTEN = %(ansichten)s;   // um die Tischmitte der Welt, x zeigt von oben nach oben wie in iRC
function blick(pos, ziel, oben = [0, 0, 1]) {
  // Kamera bei pos, schaut auf ziel; Webots-Kamera: x = Blickrichtung, z = oben
  const sub = (a, b) => a.map((v, i) => v - b[i]), dot = (a, b) => a.reduce((s, v, i) => s + v * b[i], 0);
  const norm = a => { const l = Math.hypot(...a); return a.map(v => v / l); };
  const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
  const x = norm(sub(ziel, pos)), z = norm(sub(oben, x.map(v => v * dot(oben, x)))), y = cross(z, x);
  const m = [x[0], y[0], z[0], x[1], y[1], z[1], x[2], y[2], z[2]];
  const w = Math.acos(Math.max(-1, Math.min(1, (m[0] + m[4] + m[8] - 1) / 2)));
  let achse;
  if (w < 1e-6) achse = [0, 0, 1];
  else if (Math.PI - w < 1e-4) {
    const d = [m[0], m[4], m[8]], i = d.indexOf(Math.max(...d)), j = (i + 1) %% 3, k = (i + 2) %% 3;
    achse = [0, 0, 0];
    achse[i] = Math.sqrt((d[i] + 1) / 2); achse[j] = m[i * 3 + j] / (2 * achse[i]); achse[k] = m[i * 3 + k] / (2 * achse[i]);
  } else {
    const s = 2 * Math.sin(w);
    achse = [(m[7] - m[5]) / s, (m[2] - m[6]) / s, (m[3] - m[1]) / s];
  }
  const vp = WbWorld.instance.viewpoint;
  vp.position = new WbVector3(...pos);
  vp.orientation = new WbVector4(...achse, w);
  vp.updatePosition(); vp.updateOrientation();
  view._view.w3dScene.render();
}
let folgen = null;
async function greiferBlick() {
  // von der Seite auf den TCP, quer zur Bewegung der Backen: so sieht man den Spalt
  const auftrag = folgen;
  const d = await fetch('/api/platz/%(platz)s', {cache: 'no-store'}).then(r => r.json()).catch(() => ({}));
  // inzwischen eine andere Ansicht gewaehlt: diese Antwort nicht mehr anwenden
  if (auftrag !== folgen || !d.tcp || !WbWorld.instance) return;
  let q = [d.tcp_y[0], d.tcp_y[1]];
  const l = Math.hypot(...q);
  q = l > 0.2 ? q.map(v => v / l) : [0, -1];
  blick([d.tcp[0] + 0.24 * q[0], d.tcp[1] + 0.24 * q[1], d.tcp[2] + 0.05], [d.tcp[0], d.tcp[1], d.tcp[2] + 0.01]);
}
function ansicht(name) {
  clearInterval(folgen); folgen = null;
  document.querySelectorAll('[data-ansicht]').forEach(b => b.classList.toggle('an', b.dataset.ansicht === name));
  if (!WbWorld.instance) return;
  if (name === 'standard') { WbWorld.instance.viewpoint.resetViewpoint(); view._view.w3dScene.render(); }
  else if (name === 'greifer') { folgen = setInterval(greiferBlick, 250); greiferBlick(); }
  else blick(...ANSICHTEN[name]);
}
document.querySelectorAll('[data-ansicht]').forEach(b => b.onclick = () => ansicht(b.dataset.ansicht));
// wer selbst mit der Maus dreht, will nicht mehr mitgezogen werden
view.addEventListener('mousedown', () => {
  if (folgen) { clearInterval(folgen); folgen = null; }
  document.querySelectorAll('[data-ansicht]').forEach(b => b.classList.remove('an'));
});
</script>%(schueler_js)s%(ea_js)s</body></html>"""

# Zusatz fuer die Ansicht der Lehrkraft: Knoepfe fuer genau diesen Platz
LEHRER_VIEWER_JS = """<script>
async function lehrerAktion(aktion, frage) {
  if (frage && !confirm(frage)) return false;
  const r = await fetch('/api/lehrer/' + aktion, {method: 'POST', body: JSON.stringify({platz: '%%PLATZ%%'}),
    headers: {'Content-Type': 'application/json', 'X-Lehrer': '1'}});
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { alert(d.fehler || 'Das hat nicht geklappt.'); return false; }
  return true;
}
document.getElementById('l-teile').onclick = () => lehrerAktion('teile_zuruecksetzen');
document.getElementById('l-reset').onclick = () => lehrerAktion('zuruecksetzen', 'Simulation auf Platz %%PLATZ%% neu starten?');
document.getElementById('l-ende').onclick = async () => {
  if (await lehrerAktion('beenden', 'Simulation auf Platz %%PLATZ%% beenden?')) location.href = '/lehrer';
};
</script>"""

# Zusatz fuer die Schuelerseite: Zustand von iRC anzeigen und die Knoepfe bedienen
SCHUELER_JS = """<script>
async function api(was) {
  const r = await fetch('/api/' + was, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { alert(d.fehler || 'Das hat nicht geklappt.'); return false; }
  return true;
}
function aufgabeZeigen(an) {
  document.getElementById('aufgabe').hidden = !an;
  document.getElementById('knopf-aufgabe').classList.toggle('an', an);
  window.dispatchEvent(new Event('resize'));   // 3D-Ansicht an die neue Breite anpassen
}
document.getElementById('knopf-aufgabe').onclick = () => aufgabeZeigen(document.getElementById('aufgabe').hidden);
document.getElementById('aufgabe-zu').onclick = () => aufgabeZeigen(false);
document.getElementById('knopf-teile').onclick = () => api('teile_zuruecksetzen');
document.getElementById('knopf-reset').onclick = async () => {
  if (confirm('Simulation neu starten? Das dauert einige Sekunden. Nur nötig, wenn etwas hängt.')) await api('zuruecksetzen');
};
document.getElementById('knopf-ende').onclick = async () => {
  if (confirm('Simulation beenden?') && await api('beenden')) location.href = '/';
};
async function zustand() {
  try {
    const d = await (await fetch('/api/ich', {cache: 'no-store'})).json();
    if (!d.platz) { location.href = '/'; return; }
    const irc = document.getElementById('irc');
    irc.className = d.platz.farbe; irc.textContent = d.platz.text;
  } catch (e) { /* Server kurz weg, naechster Versuch */ }
}
zustand(); setInterval(zustand, 3000);
</script>"""

WAHL = """<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ReBeL-Simulation</title><style>%(stil)s
main { grid-template-columns:repeat(auto-fill,minmax(270px,1fr)); padding-top:12px; }
.welt { padding:0; overflow:hidden; }
.welt .bild { aspect-ratio:4/3; background:var(--bg) center/cover no-repeat; display:flex; align-items:center;
  justify-content:center; color:var(--grau); font-weight:700; border-bottom:1px solid var(--linie); }
.welt .text { padding:12px 14px 14px; display:flex; flex-direction:column; gap:6px; flex:1; }
.welt h2 { margin:0; font-size:1.15em; }
.welt a { color:var(--blau); font-size:.92em; }
.aufgabe { font-size:.92em; max-height:9.5em; overflow:auto; color:#333; }
button.knopf { margin-top:auto; background:var(--blau); color:#fff; border:0; padding:10px; border-radius:3px;
  font:inherit; font-weight:700; cursor:pointer; }
button.knopf:hover { background:var(--blau-dunkel); }
button.knopf:disabled { background:var(--grau-hell); color:var(--leise); cursor:default; }
.balken { margin:16px 20px 0; padding:12px 16px; border-radius:var(--ecke) 0 0 0; background:var(--karte);
  border:1px solid var(--linie); border-left:5px solid var(--blau); }
.balken.warnen { border-left-color:var(--gelb); }
.balken a { color:var(--blau); }
h1.abschnitt { margin:22px 20px 0; font-size:1.3em; color:var(--text); }
header .pc { margin-left:auto; font-size:.9em; color:var(--leise); }
header a.lehrer { color:var(--blau); text-decoration:none; font-weight:700; border:2px solid var(--blau); border-radius:3px;
  padding:6px 12px; font-size:.92em; white-space:nowrap; }
header a.lehrer:hover { background:var(--blau); color:#fff; }
</style></head>
<body><header>%(marke)s <span class="unter">Welt wählen</span><span class="pc">Dein PC: %(pc)s</span>
<a class="lehrer" href="/lehrer">%(lehrer)s</a></header>
%(balken)s
<h1 class="abschnitt">%(dreieck)sVerfügbare Welten</h1>
<main>%(karten)s</main>
<script>
document.querySelectorAll('button[data-welt]').forEach(b => b.onclick = async () => {
  document.querySelectorAll('button[data-welt]').forEach(x => x.disabled = true);
  b.textContent = 'startet ...';
  const r = await fetch('/api/starten', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                         body: JSON.stringify({welt: b.dataset.welt})});
  const d = await r.json().catch(() => ({}));
  if (r.ok) { location.href = '/'; return; }
  alert(d.fehler || 'Das hat nicht geklappt.');
  location.reload();
});
</script></body></html>"""


def anfrage_fuer_webots(methode, pfad_rest, kopfzeilen, port):
    """Baut die an Webots weitergeleitete Anfrage. WebSocket bleibt Upgrade,
    alles andere bekommt "Connection: close", damit der Browser fuer die
    naechste Datei wieder bei uns ankommt (und nicht direkt bei Webots)."""
    websocket = kopfzeilen.get("Upgrade", "").lower() == "websocket"
    zeilen = ["%s %s HTTP/1.1" % (methode, pfad_rest or "/"), "Host: %s:%d" % (STREAM_HOST, port)]
    for name, wert in kopfzeilen.items():
        if name.lower() not in OHNE_WEITERGABE:
            zeilen.append("%s: %s" % (name, wert))
    zeilen.append("Connection: Upgrade" if websocket else "Connection: close")
    return ("\r\n".join(zeilen) + "\r\n\r\n").encode("latin-1"), websocket


# --- WebSocket-Rahmen (nur Richtung Browser -> Webots, dort sind sie maskiert) ---

# Steuerbefehle, die eine Nur-Zuschauen-Ansicht (Schueler) nicht schicken darf
STEUERBEFEHLE = ("pause", "step", "reset", "reload", "real-time", "fast", "run", "load:", "quit")


def baue_rahmen(text):
    """Maskierter Text-Rahmen, wie ihn ein Browser an den Server schickt."""
    nutzlast = text.encode("utf-8")
    maske = os.urandom(4)
    kopf = bytearray([0x81])
    n = len(nutzlast)
    if n < 126:
        kopf.append(0x80 | n)
    elif n < 65536:
        kopf += bytes([0x80 | 126]) + n.to_bytes(2, "big")
    else:
        kopf += bytes([0x80 | 127]) + n.to_bytes(8, "big")
    return bytes(kopf) + maske + bytes(b ^ maske[i % 4] for i, b in enumerate(nutzlast))


def zerlege_rahmen(puffer):
    """Liefert (rohbytes, opcode, fin, nutzlast_klartext) fuer einen vollstaendigen
    Rahmen am Pufferanfang, sonst None."""
    if len(puffer) < 2:
        return None
    fin, opcode = puffer[0] & 0x80, puffer[0] & 0x0F
    maskiert, laenge = puffer[1] & 0x80, puffer[1] & 0x7F
    pos = 2
    if laenge == 126:
        if len(puffer) < 4:
            return None
        laenge, pos = int.from_bytes(puffer[2:4], "big"), 4
    elif laenge == 127:
        if len(puffer) < 10:
            return None
        laenge, pos = int.from_bytes(puffer[2:10], "big"), 10
    maske = b""
    if maskiert:
        if len(puffer) < pos + 4:
            return None
        maske, pos = puffer[pos:pos + 4], pos + 4
    if len(puffer) < pos + laenge:
        return None
    nutzlast = puffer[pos:pos + laenge]
    if maske:
        nutzlast = bytes(b ^ maske[i % 4] for i, b in enumerate(nutzlast))
    return puffer[:pos + laenge], opcode, bool(fin), nutzlast


def _pumpe_browser_zu_webots(browser, webots):
    """Wie _pumpe, wertet aber die Text-Nachrichten aus:
    - Nach der Anmeldung ("w3d" / "w3d;broadcast") pausiert Webots die Simulation und
      wartet, dass der Viewer "real-time" schickt. Die Nur-Zuschauen-Ansicht tut das
      nie, also schicken wir es selbst - sonst bleibt die Simulation stehen.
    - Nur-Zuschauen-Ansichten duerfen keine Steuerbefehle schicken."""
    puffer = b""
    nur_zuschauen = False
    try:
        while True:
            daten = browser.recv(65536)
            if not daten:
                break
            puffer += daten
            while True:
                rahmen = zerlege_rahmen(puffer)
                if rahmen is None:
                    break
                roh, opcode, fin, nutzlast = rahmen
                puffer = puffer[len(roh):]
                text = nutzlast.decode("utf-8", "replace") if opcode == 0x1 and fin else None
                if text is not None and nur_zuschauen and text.startswith(STEUERBEFEHLE):
                    continue
                webots.sendall(roh)
                if text is not None and text.split(";")[0].split(":")[0].strip() in ("w3d", "mjpeg"):
                    nur_zuschauen = "broadcast" in text
                    webots.sendall(baue_rahmen("real-time:-1"))
    except OSError:
        pass
    finally:
        for s in (browser, webots):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _pumpe(quelle, ziel):
    try:
        while True:
            daten = quelle.recv(65536)
            if not daten:
                break
            ziel.sendall(daten)
    except OSError:
        pass
    finally:
        for s in (quelle, ziel):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def kamera_ansichten(welt_datei):
    """Ziele der Ansichtsknoepfe (m): um die Tischmitte, bei Baukasten-Welten mit deren Tisch."""
    entwurf = entwuerfe().get(welt_datei[:-4] if welt_datei.endswith(".wbt") else welt_datei) or {}
    try:
        t = baukasten.tisch_werte(entwurf)
    except baukasten.BaukastenFehler:
        t = baukasten.tisch_werte({})
    m = baukasten.tisch_merkmale(t)["flaechen"][0]["mitte"]
    cx, cy, h = m[0] / 1000, m[1] / 1000, t["hoehe_mm"] / 1000
    return {"vorne": [[cx + 1.0, cy, h + 0.25], [cx, cy, h + 0.08]],
            "seite": [[cx, cy - 1.15, h + 0.25], [cx, cy, h + 0.08]],
            "oben": [[cx, cy, h + 1.3], [cx, cy, h], [1, 0, 0]]}


# Taster (links) und Leuchtmelder (rechts): Zustand aus der Statusdatei des Controllers
EA_JS = """<script>
const EA_STEUERN = %%STEUERN%%;
const SENSOR_FARBEN = {rot: '#d91f1a', gruen: '#26b33a', blau: '#1a66e6', gelb: '#f2d91a', weiss: '#f2f2f2', schwarz: '#141414'};
let eaKette = Promise.resolve();   // Druecken und Loslassen in der richtigen Reihenfolge schicken
function tasterSenden(nr, an) {
  if (!EA_STEUERN) return;
  eaKette = eaKette.then(() => fetch('/api/taster', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                     body: JSON.stringify({nr, an})})).catch(() => {});
}
document.querySelectorAll('.ea .taster').forEach(b => {
  const nr = Number(b.dataset.nr);
  let unten = false;
  b.addEventListener('pointerdown', e => {
    e.preventDefault(); unten = true; b.setPointerCapture(e.pointerId); b.classList.add('druck'); tasterSenden(nr, true);
  });
  const los = () => { if (!unten) return; unten = false; b.classList.remove('druck'); tasterSenden(nr, false); };
  ['pointerup', 'pointercancel', 'lostpointercapture'].forEach(n => b.addEventListener(n, los));
});
async function eaZustand() {
  const d = await fetch('/api/platz/%%PLATZ%%', {cache: 'no-store'}).then(r => r.json()).catch(() => null);
  if (!d) return;
  (d.taster || []).forEach((an, i) => document.querySelector('.ea .taster[data-nr="' + i + '"]')?.classList.toggle('an', !!an));
  (d.leuchten || []).forEach((an, i) => document.querySelector('.ea .leuchte[data-nr="' + i + '"]')?.classList.toggle('an', !!an));
  (d.sensoren || []).forEach((wert, i) => {   // true/false (belegt) oder Farbname (Farbsensor)
    const s = document.querySelector('.ea .sensor[data-nr="' + i + '"]');
    if (!s) return;
    s.classList.toggle('an', !!wert);
    s.querySelector('.lampe').style.background = typeof wert === 'string' ? (SENSOR_FARBEN[wert] || '') : '';
    s.querySelector('.lampe').title = typeof wert === 'string' ? wert : (wert ? 'belegt' : 'frei');
  });
  const m = document.getElementById('ea-meldung');
  if (m) { m.hidden = d.ea_ok !== false; m.textContent = d.ea_meldung || ''; }
}
eaZustand(); setInterval(eaZustand, 300);
</script>"""


def ea_von_welt(welt_datei):
    """Taster und Leuchtmelder einer Welt (aus worlds/<welt>.json), geprueft; sonst leer."""
    welt = next((w for w in verwalter.lese_welten(WELTEN_DIR) if w["datei"] == welt_datei), None)
    try:
        return baukasten.ea_werte({"ea": (welt or {}).get("ea") or {}})
    except baukasten.BaukastenFehler:
        return {"taster": [], "leuchten": [], "sensoren": []}


def sensor_text(s):
    """Kurzbeschreibung unter dem Sensornamen, z.B. "Farbsensor · rot→DIn25, blau→DIn26"."""
    if s["typ"] != "farbe":
        return "Sensor · DIn%d · %s" % (s["din"], "Öffner" if s["art"] == "oeffner" else "Schließer")
    if s["ausgabe"] == "bits":
        bits = "DIn%d" % s["din"] if s["bits"] == 1 else "DIn%d–%d" % (s["din"], s["din"] + s["bits"] - 1)
        return "Farbsensor · Code an %s: %s" % (bits, ", ".join("%s=%d" % (f["farbe"], f["code"]) for f in s["farben"]))
    return "Farbsensor · " + ", ".join("%s→DIn%d" % (f["farbe"], f["din"]) for f in s["farben"])


def ea_html(ea, steuern):
    """Taster unten links, Leuchtmelder unten rechts (leer, wenn die Welt keine hat)."""
    teile = []
    if ea["taster"] or ea["sensoren"]:
        knoepfe = "".join(
            '<button class="taster" data-nr="%d" title="%s"><span class="kappe"></span><span>%s<small>DIn%d · %s · %s</small>'
            '</span></button>' % (i, "Gedrückt halten" if t["modus"] == "tastend" else "Drücken schaltet um",
                                  html.escape(t["name"]), t["din"], "Öffner" if t["art"] == "oeffner" else "Schließer",
                                  t["modus"]) for i, t in enumerate(ea["taster"]))
        knoepfe += "".join(   # Sensorflaechen: nur Anzeige, gruen = belegt bzw. erkannte Farbe
            '<div class="sensor" data-nr="%d"><span class="lampe"></span><span>%s<small>%s</small></span></div>'
            % (i, html.escape(s["name"]), html.escape(sensor_text(s))) for i, s in enumerate(ea["sensoren"]))
        teile.append('<div class="ea links%s">%s<div id="ea-meldung" hidden></div></div>'
                     % ("" if steuern else " nur-sehen", knoepfe))
    if ea["leuchten"]:
        teile.append('<div class="ea rechts">%s</div>' % "".join(
            '<div class="leuchte" data-nr="%d" data-farbe="%s"><span class="lampe"></span><span>%s<small>DOut%d%s</small>'
            '</span></div>' % (i, l["farbe"], html.escape(l["name"]), l["dout"], " · umgekehrt" if l["umkehren"] else "")
            for i, l in enumerate(ea["leuchten"])))
    return "".join(teile)


def seite_viewer(platz, welt=None):
    """3D-Ansicht, immer nur zuschauend (den Roboter bewegt iRC, Webots hoert nur mit). Mit welt
    (Angaben aus lese_welten) ist es die Schuelerseite mit Aufgabe und Knoepfen, sonst die Ansicht
    fuer die Lehrkraft mit Teile zurueck, Neu starten und Beenden fuer diesen Platz."""
    if welt is None:
        titel = "Platz %s &middot; PC %s" % (html.escape(platz["platz"]), html.escape(platz["irc_host"]))
        knoepfe = ('<button id="l-teile" title="Alle Werkstücke zurück auf ihre Startlage">Teile zurück</button>'
                   '<button id="l-reset" title="Webots neu starten, wenn etwas hängt">Neu starten</button>'
                   '<button id="l-ende" class="warnen">Beenden</button>'
                   '<a href="/lehrer">Lehrerbereich</a>')
        aufgabe = ""
        schueler_js = LEHRER_VIEWER_JS.replace("%%PLATZ%%", html.escape(platz["platz"]))
    else:
        titel = html.escape(welt["titel"])
        knoepfe = ('<span id="irc">...</span>'
                   '<button id="knopf-aufgabe">Aufgabe</button>'
                   '<button id="knopf-teile" title="Alle Werkstücke zurück auf ihre Startlage">Teile zurück</button>'
                   '<button id="knopf-reset" title="Webots neu starten, wenn etwas hängt">Neu starten</button>'
                   '<a href="/?wechseln=1">Welt wechseln</a>'
                   '<button id="knopf-ende" class="warnen">Beenden</button>')
        pdf = ('<a class="pdf" href="/welten/%s" target="_blank">Arbeitsblatt (PDF) öffnen</a>' % html.escape(welt["pdf"])
               if welt["pdf"] else "")
        aufgabe = ('<aside id="aufgabe" hidden><div class="kopf"><h2>%s</h2><button id="aufgabe-zu" class="zu" '
                   'title="Aufgabe ausblenden">&times;</button></div><div class="md">%s</div>%s</aside>' % (
                       titel, aufgabe_md.zu_html(welt["aufgabe"] or "Keine Aufgabe hinterlegt."), pdf))
        schueler_js = SCHUELER_JS
    ea = ea_von_welt(platz["welt"])
    ea_teil = ea_html(ea, welt is not None)
    ea_js = (EA_JS.replace("%%STEUERN%%", "true" if welt is not None else "false")
             .replace("%%PLATZ%%", html.escape(platz["platz"]))) if ea_teil else ""
    return VIEWER % {"stil": STIL, "marke": marke(), "platz": html.escape(platz["platz"]), "titel": titel, "ea": ea_teil, "ea_js": ea_js,
                     "ansichten": json.dumps(kamera_ansichten(platz["welt"])),
                     "knoepfe": knoepfe, "aufgabe": aufgabe, "schueler_js": schueler_js,
                     "irc_host": html.escape(platz["irc_host"]), "stream_port": platz["stream_port"],
                     "welt": html.escape(platz["welt"]), "broadcast": "true",
                     "webots_js": WEBOTS_VIEW_JS, "wwi": WEBOTS_VIEW_JS.rsplit("/", 1)[0]}


def seite_wahl(pc, welten, eigener, frei, max_sim, meldung=None, lehrkraft=False):
    """Startseite: Weltenliste fuer den Schueler, Zugang zum Lehrerbereich. eigener: laufende Simulation (Welt wechseln)."""
    balken = []
    if meldung:
        balken.append('<div class="balken warnen">Deine Simulation wurde beendet: %s.</div>' % html.escape(meldung))
    if eigener is not None:
        titel = next((w["titel"] for w in welten if w["datei"] == eigener["welt"]), eigener["welt"])
        balken.append('<div class="balken">Deine Simulation läuft mit <b>%s</b>. Wähle eine andere Welt '
                      'oder <a href="/">gehe zurück</a>.</div>' % html.escape(titel))
    elif frei > 0:
        balken.append('<div class="balken">Starte zuerst iRC auf diesem PC, dann eine Welt. '
                      'Noch %d von %d Simulationen frei.</div>' % (frei, max_sim))
    else:
        balken.append('<div class="balken warnen">Alle %d Simulationen sind belegt. '
                      'Bitte später noch einmal versuchen.</div>' % max_sim)
    gesperrt = eigener is None and frei <= 0
    karten = []
    for w in welten:
        if w["bild"]:
            bild = '<div class="bild" style="background-image:url(\'/welten/%s\')"></div>' % html.escape(w["bild"])
        else:
            bild = '<div class="bild">%s</div>' % html.escape(w["datei"])
        pdf = ('<a href="/welten/%s" target="_blank">Arbeitsblatt (PDF)</a>' % html.escape(w["pdf"])
               if w["pdf"] else "")
        aktuell = eigener is not None and eigener["welt"] == w["datei"]
        knopf = ('<button class="knopf" data-welt="%s"%s>%s</button>'
                 % (html.escape(w["datei"]), " disabled" if gesperrt or aktuell else "",
                    "läuft gerade" if aktuell else ("Diese Welt nehmen" if eigener else "Simulation starten")))
        karten.append('<div class="platz welt">%s<div class="text"><h2>%s</h2><div class="aufgabe md">%s</div>%s%s'
                      '</div></div>' % (bild, html.escape(w["titel"]), aufgabe_md.zu_html(w["aufgabe"]), pdf, knopf))
    if not karten:
        karten.append('<div class="leer">Es sind noch keine Welten freigegeben.</div>')
    return WAHL % {"stil": STIL, "marke": marke(), "dreieck": DREIECK, "pc": html.escape(pc),
                   "lehrer": "Lehrerbereich" if lehrkraft else "Anmeldung Lehrkraft",
                   "balken": "\n".join(balken), "karten": "\n".join(karten)}


def _welten():
    return verwalter.lese_welten(WELTEN_DIR)


def _einstellungen():
    return verwalter.lade_einstellungen(KONFIG_DIR, ZUSTAND_DIR)


# Ein Verwalter fuer den ganzen Dienst. Die Funktionen lesen die Modulwerte erst
# beim Aufruf, damit Tests KONFIG_DIR usw. umstellen koennen.
VERWALTER = verwalter.Verwalter(
    plaetze_fn=lambda: lese_plaetze(), status_fn=lambda p: lese_status(p),
    aktiv_fn=lambda p: dienst_aktiv(p), einstellungen_fn=lambda: _einstellungen(),
    welten_fn=lambda: _welten(), log=lambda text: print(text, flush=True),
    befehl_fn=lambda platz, befehl: schreibe_befehl(platz, befehl))
SPERRE = lehrer.Sperre()


def vertraute_ip(pc):
    """Im Container selbst oder ein PC aus lehrer_ips: Admin auch ohne Anmeldung
    (zum Anlegen des ersten Kontos; danach lehrer_ips leeren)."""
    return pc in ("127.0.0.1", "::1") or pc in _einstellungen().get("lehrer_ips", [])


def wer(pc, token=None):
    """Angemeldeter Benutzer ({"name", "anzeige", "rolle", ...}) oder None."""
    benutzer = lehrer.sitzung_pruefen(ZUSTAND_DIR, token)
    if benutzer is not None:
        return benutzer
    if vertraute_ip(pc):
        return {"name": "pc-" + pc, "anzeige": "Lehrer-PC " + pc, "rolle": "admin",
                "muss_aendern": False, "vertraut": True}
    return None


def ist_lehrer(pc, token=None):
    return wer(pc, token) is not None


def darf_sehen(pc, platz, token=None):
    return platz["irc_host"] == pc or ist_lehrer(pc, token)


def zustand_fuer(pc):
    """JSON fuer /api/ich."""
    eigener = VERWALTER.platz_von(pc)
    ergebnis = {"pc": pc, "platz": None, "meldung": VERWALTER.meldungen.get(pc)}
    if eigener is not None:
        farbe, text = ampel(lese_status(eigener["platz"]), dienst_aktiv(eigener["platz"]))
        ergebnis["platz"] = {"platz": eigener["platz"], "welt": eigener["welt"], "farbe": farbe, "text": text}
    return ergebnis


def vorschau_erzeugen(datei):
    """Vorschaubild einer Welt mit Webots erzeugen (tools/vorschau.sh, dauert einige Sekunden)."""
    skript = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vorschau.sh")
    try:
        r = subprocess.run([skript, datei], capture_output=True, text=True, timeout=150)
    except (OSError, subprocess.SubprocessError) as e:
        raise lehrer.LehrerFehler("Vorschaubild ging nicht: %s" % e)
    if r.returncode != 0:
        raise lehrer.LehrerFehler("Vorschaubild ging nicht: %s" % (r.stdout.strip() or r.stderr.strip()))


DATEI_TYPEN = {"png": "image/png", "jpg": "image/jpeg", "pdf": "application/pdf"}
COOKIE = "rebel_lehrer"


# --- Teilebibliothek und Weltbaukasten (Phase 1/2: Schnittstellen, der 3D-Baukasten folgt) ---

def teile_liste():
    liste = []
    try:
        namen = sorted(os.listdir(TEILE_DIR))
    except OSError:
        return liste
    for name in namen:
        try:
            with open(os.path.join(TEILE_DIR, name, "teil.json"), encoding="utf-8") as f:
                t = json.load(f)
        except (OSError, ValueError):
            continue
        liste.append({"name": name, "quelle": t.get("quelle"), "groesse_mm": t.get("groesse_mm"),
                      "volumen_mm3": t.get("volumen_mm3"),
                      "anzahl": {k: len(t.get(k, [])) for k in ("flaechen", "bohrungen", "kanten", "ecken")},
                      "verwendet_in": [w for w, e in entwuerfe().items() if any(x.get("teil") == name for x in e["teile"])
                                       or (name == baukasten.BASISPLATTE and (e.get("tisch") or {}).get("art") == "profil")]
                      + ["Backensatz " + b["name"] for b in backensaetze_liste() if b["teil"] == name]
                      + (["Backensatz-Editor (Greifer)"] if name in baukasten.GREIFER_TEILE.values() else [])})
    return liste


def teil_einlesen(name, inhalt):
    """STEP aus dem Upload in die Teilebibliothek (eigener Prozess mit Speichergrenze, siehe teile.py)."""
    if not TEIL_NAME.fullmatch(name or ""):
        raise lehrer.LehrerFehler("Teilename: 1 bis 60 Zeichen, nur a-z, 0-9, - und _.")
    if not inhalt.lstrip()[:64].startswith(b"ISO-10303-21"):
        raise lehrer.LehrerFehler("Das ist keine STEP-Datei (.step/.stp).")
    os.makedirs(TEILE_DIR, exist_ok=True)
    step = os.path.join(TEILE_DIR, ".%s.upload.step" % name)
    with open(step, "wb") as f:
        f.write(inhalt)
    try:
        r = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "teile.py"),
                            "einlesen", step, name, "--teile", TEILE_DIR],
                           capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        raise lehrer.LehrerFehler("Das Einlesen dauerte zu lange (mehr als 5 Minuten).")
    finally:
        try:
            os.remove(step)
        except OSError:
            pass
    if r.returncode != 0:
        meldung = (r.stderr.strip().splitlines() or ["unbekannter Fehler"])[-1]
        if "MemoryError" in r.stderr or "bad_alloc" in r.stderr:
            meldung = "Das Teil ist zu gross oder zu fein (Speichergrenze). Ohne Gewinde und Kleinteile exportieren."
        raise lehrer.LehrerFehler("Einlesen fehlgeschlagen: %s" % meldung.replace("Fehler: ", ""))
    return json.loads(r.stdout.strip().splitlines()[-1])


# --- Backensaetze (Phase 4): Backe am Schunk EGP 40/25 im Baukasten, daraus Greifer-Variante egp40_<name> ---

PROJEKT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
BACKEN_DIR = os.environ.get("BACKEN_DIR", os.path.join(PROJEKT_DIR, "backen"))       # aus dem Baukasten (auch fuer den Generator)
FESTE_BACKEN_DIR = os.path.join(PROJEKT_DIR, "cad", "backen")                       # aus dem Repository
WERKZEUG_DIR = os.path.join(PROJEKT_DIR, "werkzeuge")
PROTO_DATEI = os.path.join(PROJEKT_DIR, "protos", "IgusRebel6DOF.proto")
IRC_DATEN = os.environ.get("IRC_DATEN", "/var/lib/rebel-sim/irc")
MODELL_SPERRE = threading.Lock()


def backensaetze_liste():
    """Alle Backensaetze: mitgelieferte (fest) und im Baukasten gebaute, mit Verwendung in Welten."""
    welten = {}
    for w in verwalter.lese_welten(WELTEN_DIR):
        try:
            with open(os.path.join(WELTEN_DIR, w["datei"]), encoding="utf-8", errors="replace") as f:
                m = re.search(r'\bgreifer\s+"([^"]+)"', f.read(20000))
            if m:
                welten.setdefault(m.group(1), []).append(w["datei"][:-4])
        except OSError:
            pass
    liste = []
    for ordner, fest in ((FESTE_BACKEN_DIR, True), (BACKEN_DIR, False)):
        try:
            namen = sorted(os.listdir(ordner))
        except OSError:
            continue
        for name in namen:
            try:
                with open(os.path.join(ordner, name, "backensatz.json"), encoding="utf-8") as f:
                    satz = json.load(f)
            except (OSError, ValueError):
                continue
            schluessel = "%s_%s" % (satz.get("basis", "egp25"), satz["name"])
            liste.append({"name": satz["name"], "titel": satz.get("titel", satz["name"]), "greifer": schluessel,
                          "fest": fest, "teil": satz.get("teil"), "kenndaten": satz.get("kenndaten"),
                          "basis": satz.get("basis", "egp25"),
                          "werkzeugpaket": os.path.isdir(os.path.join(WERKZEUG_DIR, schluessel)),
                          "verwendet_in": welten.get(schluessel, [])})
    return liste


def backensatz_lesen(name):
    """Gespeicherter Backensatz aus dem Baukasten (mit "entwurf") oder None."""
    if not baukasten.BACKENSATZ_NAME.fullmatch(name or ""):
        return None
    try:
        with open(os.path.join(BACKEN_DIR, name, "backensatz.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def greifer_liste():
    """Greifer-Varianten, die das erzeugte ReBeL-Modell kennt (Feld "greifer" im Proto)."""
    try:
        with open(PROTO_DATEI, encoding="utf-8") as f:
            m = re.search(r"field SFString\{([^}]*)\} greifer", f.read(5000))
    except OSError:
        m = None
    return re.findall(r'"([^"]+)"', m.group(1)) if m else ["egp40", "egp40_finger", "egp25", "egp25_finger", "vakuum", "kein"]


GREIFER_TEXTE = {"egp40": "Schunk EGP 40", "egp40_finger": "Schunk EGP 40 mit iRC-Fingern", "egp25": "Schunk EGP 25",
                 "egp25_finger": "Schunk EGP 25 mit iRC-Fingern", "vakuum": "Sauger", "kein": "kein Greifer"}


def greifer_titel_liste():
    """[{name, titel}] aller Greifer des Modells, Backensaetze mit ihrem Titel."""
    titel = {b["greifer"]: "EGP %s mit %s" % (b["basis"][3:], b["titel"]) for b in backensaetze_liste()}
    return [{"name": n, "titel": GREIFER_TEXTE.get(n) or titel.get(n) or n} for n in greifer_liste()]


def welten_fuer_lehrer():
    """Welten mit Greifer, Herkunft (Baukasten) und den Plaetzen, auf denen sie gerade laufen."""
    laeuft = {}
    for p in lese_plaetze():
        laeuft.setdefault(p["welt"], []).append(p["platz"])
    aus_baukasten = entwuerfe()
    return [dict(w, greifer=lehrer.welt_greifer(WELTEN_DIR, w["datei"]), baukasten=w["datei"][:-4] in aus_baukasten,
                 aufgabe_kurz=aufgabe_md.kurz(w["aufgabe"]),
                 laeuft=laeuft.get(w["datei"], [])) for w in _welten()]


def welt_exportieren(datei):
    """Welt mit Teilen und eigenem Backensatz als ZIP (welt_paket.exportieren)."""
    greifer = lehrer.welt_greifer(WELTEN_DIR, datei) if re.fullmatch(r"[a-z0-9_-]{1,50}\.wbt", datei or "") else None
    b = next((b for b in backensaetze_liste() if b["greifer"] == greifer and not b["fest"]), None)
    return welt_paket.exportieren(WELTEN_DIR, TEILE_DIR, datei, (b["name"], os.path.join(BACKEN_DIR, b["name"])) if b else None)


def welt_importieren(inhalt, name, ersetzen, wer_name):
    """Paket einspielen; bringt es einen neuen Backensatz mit, das Robotermodell neu erzeugen."""
    ziel = (name or "") + ".wbt"
    if ersetzen and name and any(p["welt"] == ziel for p in lese_plaetze()):
        raise welt_paket.PaketFehler("Die Welt %s läuft gerade. Erst die Simulation beenden." % name)
    bericht = welt_paket.importieren(inhalt, WELTEN_DIR, TEILE_DIR, BACKEN_DIR, neuer_name=name, ersetzen=ersetzen,
                                     besitzer="lehrer:%s" % wer_name,
                                     fest_backen=[b["name"] for b in backensaetze_liste() if b["fest"]])
    bericht["hinweise"] = []
    if bericht["backensatz_neu"]:
        try:
            modell_erzeugen()
        except baukasten.BaukastenFehler as e:
            bericht["hinweise"].append(str(e))
    if bericht["greifer"] and bericht["greifer"] not in greifer_liste():
        bericht["hinweise"].append("Den Greifer %s kennt das Robotermodell hier nicht; im Welt-Dialog einen anderen wählen."
                                   % bericht["greifer"])
    return bericht


def seite(text):
    """Gemeinsame Bausteine in eine Seitenvorlage einsetzen (Stil, Kopfzeile mit Logo)."""
    return text.replace("%%STIL%%", STIL).replace("%%MARKE%%", marke()).replace("%%DREIECK%%", DREIECK)


def modell_erzeugen():
    """ReBeL-Modell mit allen Backensaetzen neu erzeugen (tools/erzeuge_rebel_modell.py, braucht die iRC-Daten).
    Rueckgabe: {greifer: {"tcp_mm", "offen_mm", "hub_mm"}}."""
    if not os.path.isdir(os.path.join(IRC_DATEN, "Data")):
        raise baukasten.BaukastenFehler("Die iRC-Daten fehlen im Container (einmal rebel-sim-modell <irc-daten.zip>).")
    with MODELL_SPERRE:
        r = subprocess.run([sys.executable, os.path.join(PROJEKT_DIR, "tools", "erzeuge_rebel_modell.py"),
                            "--irc", IRC_DATEN, "--robot", "REBEL-6DOF-03"], capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise baukasten.BaukastenFehler("Modell konnte nicht erzeugt werden: %s" % (r.stderr or r.stdout).strip()[-300:])
    ergebnis = {m.group(1): {"tcp_mm": float(m.group(2)), "offen_mm": float(m.group(3)), "hub_mm": float(m.group(4))}
                for m in re.finditer(r"Greifer (\S+): TCP ([\d.]+) mm, Backen offen ([\d.]+) mm auseinander, "
                                     r"Hub je Backe ([\d.]+) mm", r.stdout)}
    ergebnis["_warnungen"] = re.findall(r"WARNUNG: (.*)", r.stdout)
    return ergebnis


def backensatz_speichern(name, entwurf, wer_name):
    """Backe am Greifer loesen, backensatz.json schreiben, Modell und Werkzeugpaket neu erzeugen."""
    if not isinstance(entwurf, dict):
        raise baukasten.BaukastenFehler("Kein Entwurf.")
    if any(b["fest"] and b["name"] == name for b in backensaetze_liste()):
        raise baukasten.BaukastenFehler("Den Namen %s hat schon ein mitgelieferter Backensatz." % name)
    entwurf = dict(entwurf, art="backensatz")
    lade = baukasten.teile_lader(TEILE_DIR)
    lagen = baukasten.loese(entwurf, lade)
    satz = baukasten.backensatz_aus_entwurf(name, entwurf, lagen, lade)
    alt = backensatz_lesen(name) or {}
    schluessel, alt_schluessel = "%s_%s" % (satz["basis"], name), "%s_%s" % (alt.get("basis", "egp25"), name)
    if alt and alt_schluessel != schluessel:
        # anderer Greifer: der alte Name verschwindet aus dem Modell, Welten mit ihm wuerden nicht mehr laden
        b = next((b for b in backensaetze_liste() if b["greifer"] == alt_schluessel and not b["fest"]), None)
        if b and b["verwendet_in"]:
            raise baukasten.BaukastenFehler("Der Greifer %s steckt noch in: %s. Dort erst einen anderen Greifer wählen "
                                            "oder den Backensatz unter neuem Namen speichern." % (alt_schluessel, ", ".join(b["verwendet_in"])))
    satz.update(entwurf=entwurf, besitzer=alt.get("besitzer") or "lehrer:%s" % wer_name)
    os.makedirs(os.path.join(BACKEN_DIR, name), exist_ok=True)
    datei = os.path.join(BACKEN_DIR, name, "backensatz.json")
    lehrer.schreibe_json(datei, satz)
    if alt and alt_schluessel != schluessel:
        import shutil
        shutil.rmtree(os.path.join(WERKZEUG_DIR, alt_schluessel), ignore_errors=True)
    modell = modell_erzeugen()
    kenndaten = modell.get(schluessel)
    satz["kenndaten"] = kenndaten
    lehrer.schreibe_json(datei, satz)
    if kenndaten is None:
        raise baukasten.BaukastenFehler("Gespeichert, aber das Robotermodell kennt den Greifer %s nicht: %s"
                                        % (schluessel, "; ".join(modell.get("_warnungen") or ["unbekannter Grund"])))
    return {"lagen": baukasten.zusammenfassung(entwurf, lagen), "greifer": schluessel, "kenndaten": kenndaten}


def backensatz_loeschen(name):
    b = next((b for b in backensaetze_liste() if b["name"] == name and not b["fest"]), None)
    if b is None:
        raise baukasten.BaukastenFehler("Den Backensatz %s gibt es nicht (mitgelieferte lassen sich nicht loeschen)." % name)
    if b["verwendet_in"]:
        raise baukasten.BaukastenFehler("Der Greifer %s steckt noch in: %s" % (b["greifer"], ", ".join(b["verwendet_in"])))
    import shutil
    shutil.rmtree(os.path.join(BACKEN_DIR, name))
    shutil.rmtree(os.path.join(WERKZEUG_DIR, b["greifer"]), ignore_errors=True)
    modell_erzeugen()


# Liegt im Werkzeugpaket: kopiert XML + OBJ in den Datenordner, den iRC V15 wirklich liest (%APPDATA%,
# nicht der Installationsordner). Zeilenenden CRLF fuer cmd.exe.
INSTALLIEREN_CMD = "\r\n".join([
    "@echo off",
    "rem Werkzeug fuer igus Robot Control V15 installieren (aus der ReBeL-Simulation)",
    "set ZIEL=%APPDATA%\\RobotControl\\V15\\Data\\Tools",
    "if not exist \"%ZIEL%\" (",
    "  echo iRC-Datenordner nicht gefunden: %ZIEL%",
    "  echo iRC V15 einmal starten und dieses Skript dann erneut ausfuehren.",
    "  pause",
    "  exit /b 1",
    ")",
    "copy /Y \"%~dp0*.xml\" \"%ZIEL%\" >nul && copy /Y \"%~dp0*.obj\" \"%ZIEL%\" >nul",
    "if errorlevel 1 (echo Kopieren ging nicht. & pause & exit /b 1)",
    "echo Fertig: Werkzeug nach %ZIEL% kopiert.",
    "echo iRC neu starten und unter Werkzeug \"%WERKZEUG%\" waehlen.",
    "pause",
    ""])


def werkzeugpaket_zip(name):
    """Werkzeug fuer iRC (XML + OBJ + installieren.cmd) als ZIP. Rueckgabe (dateiname, bytes)."""
    import io
    import zipfile
    b = next((b for b in backensaetze_liste() if b["name"] == name), None)
    ordner = os.path.join(WERKZEUG_DIR, b["greifer"]) if b else None
    if not ordner or not os.path.isdir(ordner):
        return None
    puffer = io.BytesIO()
    werkzeug = b["greifer"]
    with zipfile.ZipFile(puffer, "w", zipfile.ZIP_DEFLATED) as z:
        for datei in sorted(os.listdir(ordner)):
            z.write(os.path.join(ordner, datei), datei)
            if datei.endswith(".xml"):
                try:
                    with open(os.path.join(ordner, datei), encoding="utf-8") as f:
                        werkzeug = re.search(r'<INFO NAME="([^"]+)"', f.read()).group(1)
                except (OSError, AttributeError):
                    pass
        z.writestr("installieren.cmd", INSTALLIEREN_CMD.replace("%WERKZEUG%", re.sub(r"[^A-Za-z0-9 ._-]", "_", werkzeug)))
    return "Werkzeug_%s.zip" % b["greifer"], puffer.getvalue()


def entwuerfe():
    """Alle Baukasten-Entwuerfe: {welt: entwurf}."""
    ergebnis = {}
    try:
        namen = os.listdir(WELTEN_DIR)
    except OSError:
        return ergebnis
    for datei in namen:
        if datei.endswith(".baukasten.json"):
            try:
                with open(os.path.join(WELTEN_DIR, datei), encoding="utf-8") as f:
                    ergebnis[datei[:-len(".baukasten.json")]] = json.load(f)
            except (OSError, ValueError):
                pass
    return ergebnis


def baukasten_rechnen(entwurf):
    if not isinstance(entwurf, dict):
        raise baukasten.BaukastenFehler("Kein Entwurf.")
    lagen = baukasten.loese(entwurf, baukasten.teile_lader(TEILE_DIR))
    return baukasten.zusammenfassung(entwurf, lagen)


def baukasten_speichern(welt, entwurf, wer_name):
    """Entwurf loesen, als Welt schreiben (wbt + Entwurf + Titel/Aufgabe), Vorschaubild erzeugen."""
    if not WELT_NAME.fullmatch(welt or ""):
        raise baukasten.BaukastenFehler("Weltname: 1 bis 50 Zeichen, nur a-z, 0-9, - und _.")
    wbt = os.path.join(WELTEN_DIR, welt + ".wbt")
    entwurf_datei = os.path.join(WELTEN_DIR, welt + ".baukasten.json")
    if os.path.exists(wbt) and not os.path.exists(entwurf_datei):
        raise baukasten.BaukastenFehler("Die Welt %s wurde nicht mit dem Baukasten gebaut und bleibt unveraendert." % welt)
    lade = baukasten.teile_lader(TEILE_DIR)
    lagen = baukasten.loese(entwurf, lade)
    entwurf = dict(entwurf, besitzer=entwurf.get("besitzer") or "lehrer:%s" % wer_name,
                   ea=baukasten.ea_werte(entwurf))
    text = baukasten.welt_text(entwurf, lagen, lade, os.path.basename(entwurf_datei))
    lehrer.schreibe_json(entwurf_datei, entwurf)
    tmp = wbt + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, wbt)
    angaben_datei = os.path.join(WELTEN_DIR, welt + ".json")
    try:
        with open(angaben_datei, encoding="utf-8") as f:
            angaben = json.load(f)
    except (OSError, ValueError):   # neue Welt: erst freigeben, wenn sie fertig ist
        angaben = {"titel": entwurf.get("titel") or welt, "aufgabe": entwurf.get("aufgabe", ""), "sichtbar": False}
    # Taster, Leuchten und Sensorflaechen liest der Controller beim Start der Simulation
    angaben["ea"] = dict(entwurf["ea"], sensoren=baukasten.ea_sensoren(entwurf))
    lehrer.schreibe_json(angaben_datei, angaben)
    try:
        lehrer.loesche_weltdatei(WELTEN_DIR, welt + ".wbt", "bild")
        vorschau_erzeugen(welt + ".wbt")
    except lehrer.LehrerFehler:
        pass   # ohne Vorschaubild geht es auch
    return baukasten.zusammenfassung(entwurf, lagen)


class Handler(BaseHTTPRequestHandler):
    def _antwort(self, code, inhalt, typ="text/html; charset=utf-8", kopf=()):
        daten = inhalt if isinstance(inhalt, bytes) else inhalt.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", typ)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(daten)))
        for name, wert in kopf:
            self.send_header(name, wert)
        self.end_headers()
        self.wfile.write(daten)

    def _durchleiten(self, port, pfad_rest):
        try:
            webots = socket.create_connection((STREAM_HOST, port), timeout=5)
        except OSError:
            self._antwort(502, "Die Simulation fuer diesen Platz laeuft gerade nicht.", "text/plain; charset=utf-8")
            return
        webots.settimeout(None)
        anfrage, websocket = anfrage_fuer_webots(self.command, pfad_rest, self.headers, port)
        webots.sendall(anfrage)
        self.close_connection = True
        browser = self.connection
        browser.settimeout(None)
        hin = threading.Thread(target=_pumpe_browser_zu_webots if websocket else _pumpe,
                               args=(browser, webots), daemon=True)
        hin.start()
        _pumpe(webots, browser)
        hin.join(timeout=2)

    def _json(self, code, daten, kopf=()):
        self._antwort(code, json.dumps(daten, ensure_ascii=False), "application/json; charset=utf-8", kopf)

    def _text(self, code, text):
        self._antwort(code, text, "text/plain; charset=utf-8")

    def _pc(self):
        return self.client_address[0]

    def _token(self):
        for teil in self.headers.get("Cookie", "").split(";"):
            name, _, wert = teil.strip().partition("=")
            if name == COOKIE:
                return wert
        return None

    def _lehrer(self):
        return ist_lehrer(self._pc(), self._token())

    def _wer(self):
        return wer(self._pc(), self._token())

    def _platz(self, nummer):
        return next((p for p in lese_plaetze() if int(p["platz"]) == int(nummer)), None)

    def do_GET(self):  # noqa: N802
        pfad, _, abfrage = self.path.partition("?")
        pc = self._pc()
        sim = re.fullmatch(r"/sim/(\d{1,2})(/.*)?", pfad)
        if sim:
            platz = self._platz(sim.group(1))
            if platz is None:
                self._text(404, "Platz ist nicht angelegt.")
            elif not darf_sehen(pc, platz, self._token()):
                self._text(403, "Das ist nicht deine Simulation.")
            else:
                rest = (sim.group(2) or "/") + ("?" + abfrage if abfrage else "")
                self._durchleiten(platz["stream_port"], rest)
        elif pfad == "/":
            self._startseite(pc, "wechseln=1" in abfrage)
        elif pfad == "/api/ich":
            self._json(200, zustand_fuer(pc))
        elif re.fullmatch(r"/api/platz/\d{1,2}", pfad):
            platz = self._platz(pfad.rsplit("/", 1)[1])
            if platz is None:
                self._json(404, {"fehler": "Platz ist nicht angelegt."})
            elif not darf_sehen(pc, platz, self._token()):
                self._json(403, {"fehler": "Das ist nicht deine Simulation."})
            else:
                st = lese_status(platz["platz"]) or {}
                self._json(200, {k: st.get(k) for k in ("tcp", "tcp_y", "hilfe", "greifer_zu", "teil", "taster",
                                                        "leuchten", "sensoren", "ea_meldung", "ea_ok")})
        elif re.fullmatch(r"/welten/[A-Za-z0-9_-]{1,60}\.(png|jpg|pdf)", pfad):
            datei = os.path.join(WELTEN_DIR, pfad.rsplit("/", 1)[1])
            try:
                with open(datei, "rb") as f:
                    self._antwort(200, f.read(), DATEI_TYPEN[datei.rsplit(".", 1)[1]])
            except OSError:
                self._text(404, "nicht gefunden")
        elif pfad == "/logo" or re.fullmatch(r"/logos/[a-z0-9_-]{1,40}\.(png|jpg|svg)", pfad):
            name = lehrer.gewaehltes_logo(ZUSTAND_DIR) if pfad == "/logo" else pfad.rsplit("/", 1)[1]
            try:
                with open(os.path.join(lehrer.logo_dir(ZUSTAND_DIR), name or "-"), "rb") as f:
                    # SVG direkt geoeffnet: keine Skripte (sandbox), in <img> laufen sie ohnehin nie
                    self._antwort(200, f.read(), lehrer.LOGO_TYPEN[name.rsplit(".", 1)[1]],
                                  kopf=[("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; sandbox")])
            except (OSError, KeyError):
                self._text(404, "kein Logo")
        elif pfad == "/lehrer/baukasten":
            if not self._lehrer():
                self._antwort(302, "", kopf=[("Location", "/lehrer")])
            else:
                with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "baukasten_seite.html"),
                          encoding="utf-8") as f:
                    self._antwort(200, seite(f.read()))
        elif pfad == "/lehrer":
            if self._lehrer() and "anmelden=1" not in abfrage:
                self._antwort(200, seite(LEHRER_SEITE))
            else:
                self._antwort(200, seite(ANMELDUNG).replace(
                    "%%HINWEIS%%", "" if lehrer.hat_benutzer(ZUSTAND_DIR) else KEINE_BENUTZER))
        elif pfad == "/status.json" or pfad.startswith("/platz/") or pfad.startswith("/api/lehrer/"):
            if not self._lehrer():
                self._text(403, "Nur für die Lehrkraft.")
            elif pfad == "/status.json":
                titel = {w["datei"]: w["titel"] for w in _welten()}
                liste = gesamtstatus()
                for p in liste:
                    p["titel"] = titel.get(p["welt"], p["welt"])
                self._json(200, liste)
            elif pfad == "/api/lehrer/welten":
                self._json(200, welten_fuer_lehrer())
            elif pfad == "/api/lehrer/greifer_liste":
                self._json(200, greifer_titel_liste())
            elif pfad == "/api/lehrer/logos":
                self._json(200, {"logos": lehrer.logos(ZUSTAND_DIR), "gewaehlt": lehrer.gewaehltes_logo(ZUSTAND_DIR)})
            elif pfad == "/api/lehrer/ich":
                self._json(200, dict(self._wer(), benutzer_vorhanden=lehrer.hat_benutzer(ZUSTAND_DIR)))
            elif pfad == "/api/lehrer/protokoll":
                self._json(200, lehrer.lies_protokoll(ZUSTAND_DIR))
            elif pfad in ("/api/lehrer/einstellungen", "/api/lehrer/benutzer") and self._wer()["rolle"] != "admin":
                self._json(403, {"fehler": "Nur für Admins."})
            elif pfad == "/api/lehrer/einstellungen":
                e = _einstellungen()
                self._json(200, {k: e[k] for k in ("max_simulationen", "leerlauf_minuten", "ende_uhrzeiten")})
            elif pfad == "/api/lehrer/benutzer":
                self._json(200, lehrer.benutzer_liste(ZUSTAND_DIR))
            elif pfad == "/api/lehrer/teile":
                self._json(200, teile_liste())
            elif pfad == "/api/lehrer/teil" or pfad == "/api/lehrer/netz":
                name = urllib.parse.parse_qs(abfrage).get("name", [""])[0]
                datei = os.path.join(TEILE_DIR, name, "teil.json" if pfad.endswith("teil") else "netz.json")
                if not TEIL_NAME.fullmatch(name) or not os.path.exists(datei):
                    self._json(404, {"fehler": "Teil nicht gefunden."})
                else:
                    with open(datei, "rb") as f:
                        self._antwort(200, f.read(), "application/json; charset=utf-8")
            elif pfad == "/api/lehrer/baukasten":
                welt = urllib.parse.parse_qs(abfrage).get("welt", [""])[0]
                e = entwuerfe().get(welt)
                self._json(200 if e else 404, e or {"fehler": "Kein Baukasten-Entwurf %s." % welt})
            elif pfad == "/api/lehrer/entwuerfe":
                self._json(200, sorted(entwuerfe()))
            elif pfad == "/api/lehrer/backensaetze":
                self._json(200, backensaetze_liste())
            elif pfad == "/api/lehrer/backensatz":
                b = backensatz_lesen(urllib.parse.parse_qs(abfrage).get("name", [""])[0])
                self._json(200 if b else 404, b or {"fehler": "Backensatz nicht gefunden."})
            elif pfad == "/api/lehrer/greifer":
                self._json(200, greifer_liste())
            elif pfad == "/api/lehrer/welt_export":
                try:
                    name, daten = welt_exportieren(urllib.parse.parse_qs(abfrage).get("datei", [""])[0])
                except welt_paket.PaketFehler as e:
                    self._json(409, {"fehler": str(e)})
                else:
                    self._antwort(200, daten, "application/zip",
                                  kopf=[("Content-Disposition", 'attachment; filename="%s"' % name)])
            elif pfad == "/api/lehrer/werkzeugpaket":
                paket = werkzeugpaket_zip(urllib.parse.parse_qs(abfrage).get("name", [""])[0])
                if paket is None:
                    self._json(404, {"fehler": "Kein Werkzeugpaket (erst den Backensatz speichern)."})
                else:
                    self._antwort(200, paket[1], "application/zip",
                                  kopf=[("Content-Disposition", 'attachment; filename="%s"' % paket[0])])
            elif re.fullmatch(r"/platz/\d{1,2}", pfad):
                platz = self._platz(pfad.rsplit("/", 1)[1])
                if platz is None:
                    self._text(404, "Platz %s ist nicht angelegt." % pfad.rsplit("/", 1)[1])
                else:
                    self._antwort(200, seite_viewer(platz))
            else:
                self._text(404, "nicht gefunden")
        else:
            self._text(404, "nicht gefunden")

    def _startseite(self, pc, wechseln):
        eigener = VERWALTER.platz_von(pc)
        welten = _welten()
        if eigener is not None and not wechseln:
            welt = next((w for w in welten if w["datei"] == eigener["welt"]),
                        {"datei": eigener["welt"], "titel": eigener["welt"], "aufgabe": "", "pdf": None})
            self._antwort(200, seite_viewer(eigener, welt=welt))
            return
        max_sim = int(_einstellungen()["max_simulationen"])
        self._antwort(200, seite_wahl(pc, [w for w in welten if w["sichtbar"]], eigener,
                                      VERWALTER.frei(), max_sim, VERWALTER.meldungen.get(pc),
                                      lehrkraft=lehrer.sitzung_pruefen(ZUSTAND_DIR, self._token()) is not None))

    def do_POST(self):  # noqa: N802
        pfad, _, abfrage = self.path.partition("?")
        if pfad in ("/api/lehrer/upload", "/api/lehrer/teil_hochladen", "/api/lehrer/logo_hochladen", "/api/lehrer/welt_import"):
            self._hochladen(abfrage, art=pfad.rsplit("/", 1)[1])
            return
        try:
            laenge = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            laenge = -1
        if not 0 <= laenge <= 20000:
            self.close_connection = True
            self._json(413, {"fehler": "Anfrage zu gross."})
            return
        roh = self.rfile.read(laenge)   # immer ganz lesen, sonst bricht die Verbindung ab
        # nur JSON annehmen: ein fremdes Formular kann so keine Simulation starten oder beenden
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            self._json(415, {"fehler": "Bitte als JSON senden."})
            return
        try:
            daten = json.loads(roh or b"{}")
        except ValueError:
            self._json(400, {"fehler": "Ungültige Anfrage."})
            return
        if not isinstance(daten, dict):
            self._json(400, {"fehler": "Ungültige Anfrage."})
            return
        try:
            if pfad.startswith("/lehrer/"):
                self._anmeldung(pfad, daten)
            elif pfad.startswith("/api/lehrer/"):
                if not self._lehrer():
                    self._json(403, {"fehler": "Bitte zuerst anmelden."})
                elif self.headers.get("X-Lehrer") != "1":
                    # eigener Kopf: fremde Seiten koennen ihn nicht ohne Rueckfrage setzen
                    self._json(403, {"fehler": "Anfrage nicht von der Lehrerseite."})
                else:
                    self._neues_cookie = None
                    zusatz = self._lehrer_aktion(pfad.rsplit("/", 1)[1], daten) or {}
                    kopf = []
                    if self._neues_cookie:
                        kopf.append(("Set-Cookie", "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict"
                                     % (COOKIE, self._neues_cookie, lehrer.SITZUNG_S)))
                    self._json(200, dict({"ok": True}, **zusatz), kopf)
            elif pfad == "/api/starten":
                platz = VERWALTER.starten(self._pc(), str(daten.get("welt", "")))
                self._json(200, {"ok": True, "platz": platz})
            elif pfad == "/api/zuruecksetzen":
                VERWALTER.zuruecksetzen(self._pc())
                self._json(200, {"ok": True})
            elif pfad == "/api/teile_zuruecksetzen":
                VERWALTER.teile_zuruecksetzen(self._pc())
                self._json(200, {"ok": True})
            elif pfad == "/api/taster":
                VERWALTER.taster(self._pc(), daten.get("nr"), daten.get("an"))
                self._json(200, {"ok": True})
            elif pfad == "/api/beenden":
                VERWALTER.beenden_fuer(self._pc())
                self._json(200, {"ok": True})
            else:
                self._json(404, {"fehler": "nicht gefunden"})
        except (verwalter.VerwalterFehler, lehrer.LehrerFehler) as fehler:
            self._json(409, {"fehler": str(fehler)})

    def _cookie(self, token):
        return ("Set-Cookie", "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict"
                % (COOKIE, token, lehrer.SITZUNG_S if token else 0))

    def _anmeldung(self, pfad, daten):
        pc = self._pc()
        if pfad == "/lehrer/abmelden":
            self._json(200, {"ok": True}, [self._cookie("")])
        elif pfad == "/lehrer/anmelden":
            warten = SPERRE.wartezeit(pc)
            if warten > 0:
                self._json(429, {"fehler": "Zu viele falsche Versuche. Bitte %d s warten." % (warten + 1)})
                return
            try:
                token, benutzer = lehrer.anmelden(ZUSTAND_DIR, str(daten.get("name", "")),
                                                  str(daten.get("passwort", "")))
            except lehrer.LehrerFehler as fehler:
                SPERRE.fehlversuch(pc)
                self._json(403, {"fehler": str(fehler)})
                return
            SPERRE.erfolg(pc)
            lehrer.protokolliere(ZUSTAND_DIR, benutzer["name"], "angemeldet (PC %s)" % pc)
            self._json(200, {"ok": True, "muss_aendern": benutzer["muss_aendern"]}, [self._cookie(token)])
        else:
            self._json(404, {"fehler": "nicht gefunden"})

    # Was eine Lehrkraft darf; alles andere nur Admins
    FUER_LEHRKRAFT = {"zuruecksetzen", "teile_zuruecksetzen", "beenden", "alle_beenden", "welt", "welt_loeschen", "markdown",
                      "datei_loeschen", "vorschau", "passwort",
                      "teil_loeschen", "baukasten_rechnen", "baukasten_speichern", "backensatz_speichern",
                      "backensatz_loeschen"}

    def _lehrer_aktion(self, aktion, daten):
        """Aktionen der Lehrerseite. Gibt Zusatzdaten fuer die Antwort zurueck."""
        ich = self._wer()
        if ich.get("muss_aendern") and aktion != "passwort":
            raise lehrer.LehrerFehler("Bitte zuerst ein eigenes Passwort festlegen.")
        if aktion not in self.FUER_LEHRKRAFT and ich["rolle"] != "admin":
            raise lehrer.LehrerFehler("Das dürfen nur Admins.")
        protokoll = None
        zusatz = None
        if aktion == "zuruecksetzen":
            VERWALTER.zuruecksetzen_platz(daten.get("platz"))
            protokoll = "Platz %s zurueckgesetzt" % daten.get("platz")
        elif aktion == "teile_zuruecksetzen":
            VERWALTER.teile_zuruecksetzen_platz(daten.get("platz"))
            protokoll = "Platz %s: Teile auf Startlage" % daten.get("platz")
        elif aktion == "beenden":
            p = VERWALTER.platz(daten.get("platz"))
            VERWALTER.beenden_platz(p["platz"])
            protokoll = "Platz %s beendet (PC %s)" % (p["platz"], p["irc_host"])
        elif aktion == "alle_beenden":
            zusatz = {"anzahl": VERWALTER.alle_beenden()}
            protokoll = "alle Simulationen beendet (%d)" % zusatz["anzahl"]
        elif aktion == "welt":
            datei, greifer = daten.get("datei"), daten.get("greifer")
            w = lehrer.speichere_weltangaben(WELTEN_DIR, datei, daten.get("titel"),
                                             daten.get("aufgabe"), daten.get("sichtbar"))
            protokoll = "Welt %s gespeichert (%s)" % (datei, "freigegeben" if w["sichtbar"] else "gesperrt")
            if greifer and greifer != lehrer.welt_greifer(WELTEN_DIR, datei):
                lehrer.setze_greifer(WELTEN_DIR, datei, greifer, greifer_liste())
                protokoll += ", Greifer %s" % greifer
        elif aktion == "welt_loeschen":
            datei = str(daten.get("datei", ""))
            plaetze = [p["platz"] for p in lese_plaetze() if p["welt"] == datei]
            if plaetze:
                raise lehrer.LehrerFehler("Die Welt läuft gerade auf Platz %s. Erst die Simulation beenden." % ", ".join(plaetze))
            zusatz = {"dateien": lehrer.loesche_welt(WELTEN_DIR, datei)}
            protokoll = "Welt %s geloescht (Papierkorb: %s)" % (datei, ", ".join(zusatz["dateien"]))
        elif aktion == "markdown":
            zusatz = {"html": aufgabe_md.zu_html(str(daten.get("text", ""))[:5000])}
        elif aktion == "logo_waehlen":
            lehrer.waehle_logo(ZUSTAND_DIR, str(daten.get("name") or ""))
            protokoll = "Logo: %s" % (daten.get("name") or "keins")
        elif aktion == "logo_loeschen":
            lehrer.loesche_logo(ZUSTAND_DIR, str(daten.get("name", "")))
            protokoll = "Logo %s geloescht" % daten.get("name")
        elif aktion == "datei_loeschen":
            lehrer.loesche_weltdatei(WELTEN_DIR, daten.get("datei"), daten.get("art"))
            protokoll = "Welt %s: %s entfernt" % (daten.get("datei"), daten.get("art"))
        elif aktion == "vorschau":
            datei = str(daten.get("datei", ""))
            lehrer.loesche_weltdatei(WELTEN_DIR, datei, "bild")   # prueft auch den Namen
            vorschau_erzeugen(datei)
            protokoll = "Welt %s: Vorschaubild erzeugt" % datei
        elif aktion == "einstellungen":
            zusatz = {"einstellungen": lehrer.speichere_einstellungen(ZUSTAND_DIR, daten)}
            e = zusatz["einstellungen"]
            protokoll = "Einstellungen: hoechstens %d, Ende nach %d min, Stundenende %s" % (
                e["max_simulationen"], e["leerlauf_minuten"], ", ".join(e["ende_uhrzeiten"]) or "keins")
        elif aktion == "passwort":
            if ich.get("vertraut"):
                raise lehrer.LehrerFehler("Ohne eigenes Konto gibt es kein Passwort zu aendern.")
            lehrer.passwort_aendern(ZUSTAND_DIR, ich["name"], str(daten.get("alt", "")), str(daten.get("neu", "")))
            # neues Passwort beendet alle Anmeldungen; diese gleich erneuern
            self._neues_cookie = lehrer.sitzung_erzeugen(ZUSTAND_DIR, ich["name"])
            protokoll = "eigenes Passwort geaendert"
        elif aktion == "benutzer_anlegen":
            # das erste Konto legt man fuer sich selbst an; spaetere bekommen ein vorlaeufiges Passwort
            b = lehrer.benutzer_anlegen(ZUSTAND_DIR, daten.get("name"), daten.get("anzeige"), daten.get("rolle"),
                                        str(daten.get("passwort", "")), muss_aendern=lehrer.hat_benutzer(ZUSTAND_DIR))
            protokoll = "Benutzer %s angelegt (%s)" % (b["name"], b["rolle"])
        elif aktion == "benutzer_aendern":
            name = str(daten.get("name", ""))
            if name == ich["name"] and (daten.get("gesperrt") or daten.get("rolle") == "lehrkraft"):
                raise lehrer.LehrerFehler("Sich selbst sperren oder herabstufen geht nicht.")
            b = lehrer.benutzer_aendern(ZUSTAND_DIR, name, rolle=daten.get("rolle"), gesperrt=daten.get("gesperrt"),
                                        anzeige=daten.get("anzeige"))
            protokoll = "Benutzer %s: %s%s" % (b["name"], b["rolle"], ", gesperrt" if b["gesperrt"] else "")
        elif aktion == "benutzer_passwort":
            name = str(daten.get("name", ""))
            lehrer.passwort_setzen(ZUSTAND_DIR, name, str(daten.get("passwort", "")), muss_aendern=True)
            protokoll = "Passwort von %s zurueckgesetzt" % name
        elif aktion == "benutzer_loeschen":
            name = str(daten.get("name", ""))
            if name == ich["name"]:
                raise lehrer.LehrerFehler("Das eigene Konto kann man nicht loeschen.")
            lehrer.benutzer_loeschen(ZUSTAND_DIR, name)
            protokoll = "Benutzer %s geloescht" % name
        elif aktion == "teil_loeschen":
            name = str(daten.get("name", ""))
            t = next((t for t in teile_liste() if t["name"] == name), None)
            if t is None:
                raise lehrer.LehrerFehler("Das Teil %s gibt es nicht." % name)
            if t["verwendet_in"]:
                raise lehrer.LehrerFehler("Das Teil steckt noch in: %s" % ", ".join(t["verwendet_in"]))
            import shutil
            shutil.rmtree(os.path.join(TEILE_DIR, name))
            protokoll = "Teil %s geloescht" % name
        elif aktion == "baukasten_rechnen":
            zusatz = {"lagen": self._baukasten(baukasten_rechnen, daten.get("entwurf"))}
        elif aktion == "baukasten_speichern":
            welt = str(daten.get("welt", ""))
            zusatz = {"lagen": self._baukasten(baukasten_speichern, welt, daten.get("entwurf"), ich["name"])}
            protokoll = "Baukasten-Welt %s gespeichert" % welt
        elif aktion == "backensatz_speichern":
            name = str(daten.get("name", ""))
            zusatz = self._baukasten(backensatz_speichern, name, daten.get("entwurf"), ich["name"])
            protokoll = "Backensatz %s gespeichert, Greifer %s" % (name, zusatz["greifer"])
        elif aktion == "backensatz_loeschen":
            name = str(daten.get("name", ""))
            self._baukasten(backensatz_loeschen, name)
            protokoll = "Backensatz %s geloescht" % name
        else:
            raise lehrer.LehrerFehler("Unbekannte Aktion.")
        if protokoll:
            lehrer.protokolliere(ZUSTAND_DIR, ich["name"], protokoll)
        return zusatz

    @staticmethod
    def _baukasten(funktion, *argumente):
        """Baukasten-Fehler als Meldung fuer die Lehrerseite weitergeben."""
        try:
            return funktion(*argumente)
        except baukasten.BaukastenFehler as fehler:
            raise lehrer.LehrerFehler(str(fehler))

    def _hochladen(self, abfrage, art="upload"):
        teil = art == "teil_hochladen"
        ich = self._wer()

        def ablehnen(code, fehler):
            # erst den Rest der Anfrage lesen (bis zur Grenze), sonst bricht die Verbindung beim Browser ab
            try:
                rest = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                rest = -1
            if 0 <= rest <= welt_paket.MAX_PAKET:
                while rest > 0:
                    rest -= len(self.rfile.read(min(rest, 1 << 20)) or b"x" * rest)
            else:
                self.close_connection = True
            self._json(code, {"fehler": fehler})
        if ich is None or self.headers.get("X-Lehrer") != "1":
            ablehnen(403, "Nur für die Lehrkraft.")
            return
        if ich.get("muss_aendern"):
            ablehnen(409, "Bitte zuerst ein eigenes Passwort festlegen.")
            return
        datei = urllib.parse.parse_qs(abfrage).get("datei", [""])[0]
        try:
            laenge = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            laenge = -1
        grenze = welt_paket.MAX_PAKET if art == "welt_import" else lehrer.MAX_DATEI
        if not 0 < laenge <= grenze:
            self.close_connection = True
            self._json(413, {"fehler": "Die Datei fehlt oder ist groesser als %d MB." % (grenze // (1024 * 1024))})
            return
        try:
            if art == "welt_import":
                werte = urllib.parse.parse_qs(abfrage)
                try:
                    bericht = welt_importieren(self.rfile.read(laenge), werte.get("name", [""])[0] or None,
                                               werte.get("ersetzen", [""])[0] == "1", ich["name"])
                except welt_paket.WeltVorhanden as e:
                    self._json(409, {"fehler": str(e), "vorhanden": e.name, "vorschlag": e.vorschlag})
                    return
                lehrer.protokolliere(ZUSTAND_DIR, ich["name"], "Welt %s importiert (Teile neu: %s%s)" % (
                    bericht["welt"], ", ".join(bericht["teile_neu"]) or "keine",
                    ", Backensatz %s" % bericht["backensatz"] if bericht["backensatz_neu"] else ""))
                self._json(200, dict(bericht, ok=True))
                return
            if art == "logo_hochladen":
                inhalt = self.rfile.read(laenge)   # erst ganz lesen, dann pruefen (sonst bricht die Verbindung ab)
                if ich["rolle"] != "admin":
                    raise lehrer.LehrerFehler("Das Logo ändern nur Admins.")
                name = lehrer.speichere_logo(ZUSTAND_DIR, urllib.parse.parse_qs(abfrage).get("name", [""])[0], inhalt)
                lehrer.protokolliere(ZUSTAND_DIR, ich["name"], "Logo %s hochgeladen" % name)
                self._json(200, {"ok": True, "datei": name})
                return
            if teil:
                name = urllib.parse.parse_qs(abfrage).get("name", [""])[0]
                ergebnis = teil_einlesen(name, self.rfile.read(laenge))
                lehrer.protokolliere(ZUSTAND_DIR, ich["name"], "Teil %s eingelesen (%s mm)" % (
                    name, " x ".join("%g" % v for v in ergebnis["groesse_mm"])))
                self._json(200, dict(ergebnis, ok=True))
                return
            name = lehrer.speichere_weltdatei(WELTEN_DIR, datei, self.rfile.read(laenge))
            lehrer.protokolliere(ZUSTAND_DIR, ich["name"], "Welt %s: %s hochgeladen" % (datei, name))
            self._json(200, {"ok": True, "datei": name})
        except (lehrer.LehrerFehler, welt_paket.PaketFehler) as fehler:
            self._json(409, {"fehler": str(fehler)})

    def log_message(self, fmt, *args):
        pass


def ziel_umleitung(host_kopf, pfad, port):
    """Adresse auf dem Hauptport fuer eine Anfrage an einen alten Port (z.B. 8080 -> 80)."""
    host = (host_kopf or "").strip()
    if host.startswith("["):   # IPv6 [::1]:8080
        host = host[:host.find("]") + 1]
    else:
        host = host.split(":")[0]
    if not re.fullmatch(r"[A-Za-z0-9.\-\[\]:]{1,100}", host):
        host = "localhost"
    return "http://%s%s%s" % (host, "" if port == 80 else ":%d" % port, pfad if pfad.startswith("/") else "/")


class Umleitung(BaseHTTPRequestHandler):
    """Alter Port (8080): alles auf die Startseite am Hauptport weiterleiten."""
    def _weiter(self):
        laenge = self.headers.get("Content-Length")
        if laenge and laenge.isdigit() and int(laenge) <= 20000:
            self.rfile.read(int(laenge))
        self.send_response(301)
        self.send_header("Location", ziel_umleitung(self.headers.get("Host"), self.path, PORT))
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = do_HEAD = _weiter

    def log_message(self, fmt, *args):
        pass


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("Uebersicht auf Port %d (Konfiguration %s, Status %s, Welten %s)"
          % (PORT, KONFIG_DIR, STATUS_DIR, os.path.abspath(WELTEN_DIR)), flush=True)
    for alt in (int(p) for p in os.environ.get("UMLEITEN_VON", "").replace(",", " ").split() if p.isdigit()):
        try:
            umleitung = ThreadingHTTPServer(("0.0.0.0", alt), Umleitung)
        except OSError as e:
            print("Umleitung von Port %d geht nicht: %s" % (alt, e), flush=True)
            continue
        threading.Thread(target=umleitung.serve_forever, daemon=True).start()
        print("Port %d leitet auf Port %d um" % (alt, PORT), flush=True)
    threading.Thread(target=VERWALTER.laufen, daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
