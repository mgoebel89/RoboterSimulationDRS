"""Webseite der ReBeL-Simulation (laeuft im Container, Port 8080).

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
  /platz/07       3D-Ansicht von Platz 07 (?steuern=1: mit Pause/Reset)

Fuer alle, aber nur fuer die eigene Simulation (Lehrkraft: alle):
  /sim/07/...     Durchleitung zum Webots-Stream von Platz 07 (WebSocket und
                  Dateien wie Meshes). Noetig, weil aktuelle Browser
                  Verbindungen von einer http-Seite zu einem ANDEREN Port im
                  lokalen Netz sperren ("Local Network Access"). So kommt
                  alles vom selben Ursprung, und im Netz muss nur Port 8080
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

KONFIG_DIR = os.environ.get("KONFIG_DIR", "/etc/rebel-sim")
STATUS_DIR = os.environ.get("STATUS_DIR", "/run/rebel-sim")
WELTEN_DIR = os.environ.get("WELTEN_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "worlds"))
ZUSTAND_DIR = lehrer.ZUSTAND_DIR
TEILE_DIR = os.environ.get("TEILE_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "teile"))
WELT_NAME = re.compile(r"[a-z0-9_-]{1,50}")
TEIL_NAME = re.compile(r"[a-z0-9_-]{1,60}")
PORT = int(os.environ.get("UEBERSICHT_PORT", "8080"))
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


STIL = """
:root { --bg:#f4f5f7; --karte:#fff; --text:#1d2330; --leise:#5b6475; --orange:#f26b00;
        --gruen:#2e9e5b; --gelb:#e0a800; --rot:#d64545; }
* { box-sizing:border-box; }
body { margin:0; font-family:system-ui,Segoe UI,sans-serif; background:var(--bg); color:var(--text); }
header { background:#1d2330; color:#fff; padding:14px 20px; display:flex; gap:12px; align-items:center; }
header b { color:var(--orange); }
main { padding:20px; display:grid; grid-template-columns:repeat(auto-fill,minmax(230px,1fr)); gap:14px; }
.platz { background:var(--karte); border-radius:10px; padding:14px; box-shadow:0 1px 3px rgba(0,0,0,.12);
         display:flex; flex-direction:column; gap:6px; }
.kopf { display:flex; align-items:center; gap:10px; font-size:1.25em; font-weight:600; }
.punkt { width:16px; height:16px; border-radius:50%; flex:none; }
.gruen { background:var(--gruen); } .gelb { background:var(--gelb); } .rot { background:var(--rot); }
.leise { color:var(--leise); font-size:.9em; }
a.knopf { margin-top:6px; text-align:center; background:var(--orange); color:#fff; text-decoration:none;
          padding:8px; border-radius:6px; font-weight:600; }
.leer { padding:40px; color:var(--leise); grid-column:1/-1; }
"""

ANMELDUNG = """<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ReBeL - Lehrerbereich</title><style>%%STIL%%
.anmelden { max-width:340px; margin:60px auto; background:var(--karte); border-radius:10px; padding:22px;
  box-shadow:0 1px 3px rgba(0,0,0,.12); display:flex; flex-direction:column; gap:10px; }
.anmelden input { font:inherit; font-size:1.1em; padding:8px 10px; border:1px solid #c6ccd6; border-radius:6px; }
.anmelden button { font:inherit; font-weight:600; background:var(--orange); color:#fff; border:0; border-radius:6px;
  padding:10px; cursor:pointer; margin-top:6px; }
#fehler { color:var(--rot); min-height:1.2em; }
</style></head>
<body><header><b>ReBeL</b> Lehrerbereich</header>
<form class="anmelden" id="form">
  <label for="name">Benutzername</label>
  <input id="name" autocomplete="username" autocapitalize="none" autofocus>
  <label for="passwort">Passwort</label>
  <input id="passwort" type="password" autocomplete="current-password">
  <button>Anmelden</button>
  <div id="fehler"></div>%%HINWEIS%%
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
nav { display:flex; gap:6px; margin-left:16px; flex-wrap:wrap; }
nav button, header .rechts { color:#fff; background:#3a4256; border:0; border-radius:6px; padding:7px 12px;
  font:inherit; cursor:pointer; }
nav button.an { background:var(--orange); }
nav a.nav-link { color:#fff; background:#3a4256; border-radius:6px; padding:7px 12px; text-decoration:none; }
#ich { margin-left:auto; color:#cfd6e4; font-size:.9em; }
.leiste { display:flex; gap:12px; align-items:center; padding:16px 20px 0; flex-wrap:wrap; }
.knopfreihe { display:flex; gap:6px; flex-wrap:wrap; margin-top:6px; }
.knopfreihe a, .knopfreihe button, .leiste button, .feld button, label.knopf-datei, td button {
  font:inherit; font-size:.88em; border:0; border-radius:6px; padding:7px 10px; cursor:pointer;
  background:#e6e9ef; color:var(--text); text-decoration:none; }
button.haupt { background:var(--orange) !important; color:#fff !important; font-weight:600; }
button.warnen { background:var(--rot) !important; color:#fff !important; }
.hilfe { font-size:.88em; color:var(--leise); }
.welt { padding:0; overflow:hidden; }
.welt .bild { aspect-ratio:4/3; background:#dfe3ea center/cover no-repeat; display:flex; align-items:center;
  justify-content:center; color:var(--leise); font-weight:600; }
.welt .text { padding:12px 14px 14px; display:flex; flex-direction:column; gap:8px; }
.welt input[type=text], .welt textarea, .feld input, .feld select, td select { font:inherit; padding:6px 8px;
  border:1px solid #c6ccd6; border-radius:6px; width:100%; }
td select { width:auto; }
.welt textarea { min-height:8em; resize:vertical; }
.welt .datei { font-size:.8em; color:var(--leise); }
.spalte { padding:20px; display:flex; flex-direction:column; gap:16px; max-width:860px; }
.feld { background:var(--karte); border-radius:10px; padding:14px; box-shadow:0 1px 3px rgba(0,0,0,.12);
  display:flex; flex-direction:column; gap:8px; overflow-x:auto; }
.feld h2 { margin:0; font-size:1.05em; }
.zeile { display:flex; gap:8px; flex-wrap:wrap; } .zeile > * { flex:1; min-width:140px; }
table { border-collapse:collapse; width:100%; font-size:.92em; }
th, td { text-align:left; padding:6px 8px; border-bottom:1px solid #e6e9ef; vertical-align:middle; }
td.knoepfe { white-space:nowrap; }
.gesperrt { color:var(--rot); font-weight:600; }
#meldung { position:fixed; left:50%; bottom:20px; transform:translateX(-50%); background:#1d2330; color:#fff;
  padding:10px 16px; border-radius:8px; z-index:20; box-shadow:0 2px 10px rgba(0,0,0,.3); }
#meldung.fehler { background:var(--rot); }
[hidden] { display:none !important; }   /* sonst gewinnt z.B. display:flex gegen hidden */
#pw-dialog { position:fixed; inset:0; background:rgba(29,35,48,.6); z-index:15; display:flex; align-items:center;
  justify-content:center; padding:16px; }
#pw-dialog .feld { width:min(380px, 100%); }
input[type=file] { display:none; }
</style></head>
<body><header><b>ReBeL</b> Lehrerbereich
<nav><button data-tab="sim" class="an">Simulationen</button><button data-tab="welten">Welten</button>
<button data-tab="teile">Teile</button><a class="nav-link" href="/lehrer/baukasten">Baukasten</a><button data-tab="protokoll">Protokoll</button><button data-tab="einst" class="nur-admin">Einstellungen</button>
<button data-tab="benutzer" class="nur-admin">Benutzer</button></nav>
<span id="ich"></span><button class="rechts" id="pw-knopf">Passwort</button>
<button class="rechts" id="abmelden">Abmelden</button></header>
<div id="meldung" hidden></div>

<div id="pw-dialog" hidden><div class="feld"><h2 id="pw-titel">Passwort ändern</h2>
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
  <div class="leiste hilfe">Neue Welten als .wbt-Datei in /opt/rebel-sim/worlds ablegen. Nur freigegebene Welten
    sehen die Schüler. Änderungen gelten für neu gestartete Simulationen.</div>
  <main id="welten"></main>
</section>

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
  clearTimeout(meldungZeit); meldungZeit = setTimeout(() => m.hidden = true, fehler ? 6000 : 2500);
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
    + '<a href="/platz/' + esc(p.platz) + '?steuern=1" target="_blank" title="mit Pause und Reset von Webots">Steuern</a>'
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

// --- Welten ---
async function welten() {
  const liste = await (await fetch('/api/lehrer/welten', {cache: 'no-store'})).json();
  const ausBaukasten = await (await fetch('/api/lehrer/entwuerfe', {cache: 'no-store'})).json();
  const t = Date.now();
  $('welten').innerHTML = liste.map(w => '<div class="platz welt" data-datei="' + esc(w.datei) + '">'
    + (w.bild ? '<div class="bild" style="background-image:url(\\'/welten/' + esc(w.bild) + '?' + t + '\\')"></div>'
              : '<div class="bild">kein Vorschaubild</div>')
    + '<div class="text"><div class="datei">' + esc(w.datei) + '</div>'
    + '<label><input type="checkbox" class="w-sichtbar"' + (w.sichtbar ? ' checked' : '') + '> für Schüler freigegeben</label>'
    + '<input type="text" class="w-titel" maxlength="100" placeholder="Titel" value="' + esc(w.titel) + '">'
    + '<textarea class="w-aufgabe" maxlength="5000" placeholder="Aufgabentext">' + esc(w.aufgabe) + '</textarea>'
    + '<div class="knopfreihe"><button class="haupt w-speichern">Speichern</button>'
    + (ausBaukasten.includes(w.datei.replace(/[.]wbt$/, '')) ? '<a href="/lehrer/baukasten?welt=' + encodeURIComponent(w.datei.replace(/[.]wbt$/, ''))
       + '">Im Baukasten öffnen</a>' : '') + '</div>'
    + '<div class="knopfreihe"><button class="w-vorschau">Vorschaubild erzeugen</button>'
    + '<label class="knopf-datei">Bild hochladen<input type="file" class="w-datei" accept="image/png,image/jpeg"></label></div>'
    + '<div class="knopfreihe">' + (w.pdf ? '<a href="/welten/' + esc(w.pdf) + '" target="_blank">Arbeitsblatt ansehen</a>'
        + '<button class="w-pdf-weg">Arbeitsblatt entfernen</button>' : '')
    + '<label class="knopf-datei">Arbeitsblatt (PDF) ' + (w.pdf ? 'ersetzen' : 'hochladen')
    + '<input type="file" class="w-datei" accept="application/pdf"></label></div></div></div>').join('')
    || '<div class="leer">Keine Welten gefunden.</div>';
}
$('welten').onclick = async ev => {
  const karte = ev.target.closest('[data-datei]'), b = ev.target.closest('button');
  if (!karte || !b) return;
  const datei = karte.dataset.datei;
  if (b.classList.contains('w-speichern')) {
    await api('welt', {datei, titel: karte.querySelector('.w-titel').value, aufgabe: karte.querySelector('.w-aufgabe').value,
                       sichtbar: karte.querySelector('.w-sichtbar').checked});
    melden('Gespeichert.');
  } else if (b.classList.contains('w-vorschau')) {
    b.disabled = true; b.textContent = 'Webots rechnet ...';
    try { await api('vorschau', {datei}); melden('Vorschaubild erzeugt.'); } finally { welten(); }
  } else if (b.classList.contains('w-pdf-weg') && confirm('Arbeitsblatt entfernen?')) {
    await api('datei_loeschen', {datei, art: 'pdf'}); welten();
  }
};
$('welten').onchange = async ev => {
  if (!ev.target.classList.contains('w-datei') || !ev.target.files.length) return;
  const datei = ev.target.closest('[data-datei]').dataset.datei, f = ev.target.files[0];
  const r = await fetch('/api/lehrer/upload?datei=' + encodeURIComponent(datei), {method: 'POST', body: f,
    headers: {'Content-Type': f.type || 'application/octet-stream', 'X-Lehrer': '1'}});
  const d = await r.json().catch(() => ({}));
  if (r.ok) melden('Hochgeladen: ' + d.datei); else melden(d.fehler || 'Hochladen fehlgeschlagen.', true);
  welten();
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
}
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
.ansicht { flex:1; min-height:0; position:relative; }
webots-view { display:block; width:100%%; height:100%%; position:relative; }
#hinweis { font-size:.9em; color:#cfd6e4; }
.knoepfe { margin-left:auto; display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
.knoepfe a, .knoepfe button { color:#fff; background:#3a4256; border:0; border-radius:6px; padding:6px 10px;
  font:inherit; font-size:.9em; cursor:pointer; text-decoration:none; }
.knoepfe button.warnen { background:var(--rot); }
#irc { font-size:.9em; padding:3px 8px; border-radius:6px; background:#3a4256; }
#irc.gruen { background:var(--gruen); } #irc.gelb { background:var(--gelb); color:#1d2330; } #irc.rot { background:var(--rot); }
#aufgabe { position:absolute; top:10px; right:10px; z-index:5; width:min(380px, calc(100%% - 20px)); max-height:calc(100%% - 20px);
  overflow:auto; background:var(--karte); border-radius:10px; padding:14px; box-shadow:0 2px 10px rgba(0,0,0,.3);
  white-space:pre-line; }
#aufgabe h2 { margin:0 0 8px; font-size:1.1em; }
.ansichten { position:absolute; right:10px; bottom:52px; z-index:5; display:flex; gap:4px; flex-wrap:wrap;
  background:rgba(29,35,48,.75); padding:5px; border-radius:8px; }
.ansichten button { color:#fff; background:transparent; border:0; border-radius:5px; padding:5px 9px;
  font:inherit; font-size:.85em; cursor:pointer; }
.ansichten button.an { background:var(--orange); }
.ea { position:absolute; bottom:52px; z-index:5; display:flex; flex-direction:column; gap:5px; max-width:44%%;
  background:rgba(29,35,48,.78); padding:8px; border-radius:10px; color:#fff; font-size:.88em; }
.ea.links { left:10px; bottom:100px; } .ea.rechts { right:10px; bottom:100px; }   /* unten links steht die Einteach-Hilfe */
.ea .taster, .ea .leuchte { display:flex; align-items:center; gap:8px; background:transparent; color:#fff; border:0;
  font:inherit; padding:2px 4px; text-align:left; touch-action:none; user-select:none; -webkit-user-select:none; }
.ea .taster { cursor:pointer; border-radius:8px; } .ea .taster:hover { background:rgba(255,255,255,.08); }
.ea small { color:#aeb6c6; font-size:.82em; display:block; }
.ea .kappe { width:30px; height:30px; flex:none; border-radius:50%%; background:#c9ced8; border:3px solid #5d6577;
  box-shadow:0 3px 0 #2c3242; transition:transform .05s; }
.ea .taster.druck .kappe, .ea .taster.an .kappe { transform:translateY(2px); box-shadow:0 1px 0 #2c3242; }
.ea .taster.an .kappe { background:var(--orange); }
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
<body><header><b>ReBeL</b> %(titel)s <span id="hinweis">verbinde ...</span>
<div class="knoepfe">%(knoepfe)s</div></header>
<div class="ansicht">%(aufgabe)s<webots-view></webots-view>
<div class="ansichten" title="Blickrichtung. Mit der Maus: linke Taste drehen, rechte schieben, nach einem Klick Rad zoomen.">
<button data-ansicht="standard" class="an">Standard</button><button data-ansicht="vorne">Vorne</button>
<button data-ansicht="seite">Seite</button><button data-ansicht="oben">Oben</button>
<button data-ansicht="greifer" title="Kamera folgt dem Greifer, Blick quer zu den Backen">Greifer</button></div>%(ea)s</div>
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

# Zusatz fuer die Schuelerseite: Zustand von iRC anzeigen und die Knoepfe bedienen
SCHUELER_JS = """<script>
async function api(was) {
  const r = await fetch('/api/' + was, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { alert(d.fehler || 'Das hat nicht geklappt.'); return false; }
  return true;
}
document.getElementById('knopf-aufgabe').onclick = () => {
  const a = document.getElementById('aufgabe'); a.hidden = !a.hidden;
};
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
main { grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); }
.welt { padding:0; overflow:hidden; }
.welt .bild { aspect-ratio:4/3; background:#dfe3ea center/cover no-repeat; display:flex; align-items:center;
  justify-content:center; color:var(--leise); font-weight:600; }
.welt .text { padding:12px 14px 14px; display:flex; flex-direction:column; gap:6px; flex:1; }
.welt h2 { margin:0; font-size:1.15em; }
.aufgabe { white-space:pre-line; font-size:.92em; max-height:9.5em; overflow:auto; }
button.knopf { margin-top:auto; background:var(--orange); color:#fff; border:0; padding:9px; border-radius:6px;
  font:inherit; font-weight:600; cursor:pointer; }
button.knopf:disabled { background:#b9bfca; cursor:default; }
.balken { margin:16px 20px 0; padding:12px 14px; border-radius:10px; background:var(--karte);
  box-shadow:0 1px 3px rgba(0,0,0,.12); }
.balken.warnen { border-left:5px solid var(--gelb); }
header .pc { margin-left:auto; font-size:.9em; color:#cfd6e4; }
</style></head>
<body><header><b>ReBeL</b> Simulation &ndash; Welt wählen<span class="pc">Dein PC: %(pc)s</span></header>
%(balken)s
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


def seite_viewer(platz, steuern=False, welt=None):
    """3D-Ansicht. Mit welt (Angaben aus lese_welten) ist es die Schuelerseite
    mit Aufgabe und Knoepfen, sonst die Ansicht fuer die Lehrkraft."""
    if welt is None:
        titel = "Platz %s" % html.escape(platz["platz"])
        knoepfe = '<a href="/lehrer">Uebersicht</a>'
        aufgabe = schueler_js = ""
    else:
        titel = html.escape(welt["titel"])
        knoepfe = ('<span id="irc">...</span>'
                   '<button id="knopf-aufgabe">Aufgabe</button>'
                   '<button id="knopf-teile" title="Alle Werkstücke zurück auf ihre Startlage">Teile zurück</button>'
                   '<button id="knopf-reset" title="Webots neu starten, wenn etwas hängt">Neu starten</button>'
                   '<a href="/?wechseln=1">Welt wechseln</a>'
                   '<button id="knopf-ende" class="warnen">Beenden</button>')
        pdf = ('\n<a href="/welten/%s" target="_blank">Arbeitsblatt (PDF) öffnen</a>' % html.escape(welt["pdf"])
               if welt["pdf"] else "")
        aufgabe = '<div id="aufgabe"><h2>%s</h2>%s%s</div>' % (
            titel, html.escape(welt["aufgabe"] or "Keine Aufgabe hinterlegt."), pdf)
        schueler_js = SCHUELER_JS
    ea = ea_von_welt(platz["welt"])
    ea_teil = ea_html(ea, welt is not None)
    ea_js = (EA_JS.replace("%%STEUERN%%", "true" if welt is not None else "false")
             .replace("%%PLATZ%%", html.escape(platz["platz"]))) if ea_teil else ""
    return VIEWER % {"stil": STIL, "platz": html.escape(platz["platz"]), "titel": titel, "ea": ea_teil, "ea_js": ea_js,
                     "ansichten": json.dumps(kamera_ansichten(platz["welt"])),
                     "knoepfe": knoepfe, "aufgabe": aufgabe, "schueler_js": schueler_js,
                     "irc_host": html.escape(platz["irc_host"]), "stream_port": platz["stream_port"],
                     "welt": html.escape(platz["welt"]), "broadcast": "false" if steuern else "true",
                     "webots_js": WEBOTS_VIEW_JS, "wwi": WEBOTS_VIEW_JS.rsplit("/", 1)[0]}


def seite_wahl(pc, welten, eigener, frei, max_sim, meldung=None):
    """Weltenliste fuer den Schueler. eigener: laufende Simulation (Welt wechseln)."""
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
        karten.append('<div class="platz welt">%s<div class="text"><h2>%s</h2><div class="aufgabe">%s</div>%s%s'
                      '</div></div>' % (bild, html.escape(w["titel"]), html.escape(w["aufgabe"]), pdf, knopf))
    if not karten:
        karten.append('<div class="leer">Es sind noch keine Welten freigegeben.</div>')
    return WAHL % {"stil": STIL, "pc": html.escape(pc), "balken": "\n".join(balken), "karten": "\n".join(karten)}


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
                                       or (name == baukasten.BASISPLATTE and (e.get("tisch") or {}).get("art") == "profil")]})
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
        elif pfad == "/lehrer/baukasten":
            if not self._lehrer():
                self._antwort(302, "", kopf=[("Location", "/lehrer")])
            else:
                with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "baukasten_seite.html"),
                          encoding="utf-8") as f:
                    self._antwort(200, f.read().replace("%%STIL%%", STIL))
        elif pfad == "/lehrer":
            if self._lehrer() and "anmelden=1" not in abfrage:
                self._antwort(200, LEHRER_SEITE.replace("%%STIL%%", STIL))
            else:
                self._antwort(200, ANMELDUNG.replace("%%STIL%%", STIL).replace(
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
                self._json(200, _welten())
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
            elif re.fullmatch(r"/platz/\d{1,2}", pfad):
                platz = self._platz(pfad.rsplit("/", 1)[1])
                if platz is None:
                    self._text(404, "Platz %s ist nicht angelegt." % pfad.rsplit("/", 1)[1])
                else:
                    self._antwort(200, seite_viewer(platz, steuern="steuern=1" in abfrage))
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
                                      VERWALTER.frei(), max_sim, VERWALTER.meldungen.get(pc)))

    def do_POST(self):  # noqa: N802
        pfad, _, abfrage = self.path.partition("?")
        if pfad in ("/api/lehrer/upload", "/api/lehrer/teil_hochladen"):
            self._hochladen(abfrage, teil=pfad.endswith("teil_hochladen"))
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
    FUER_LEHRKRAFT = {"zuruecksetzen", "teile_zuruecksetzen", "beenden", "alle_beenden", "welt", "datei_loeschen", "vorschau", "passwort",
                      "teil_loeschen", "baukasten_rechnen", "baukasten_speichern"}

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
            w = lehrer.speichere_weltangaben(WELTEN_DIR, daten.get("datei"), daten.get("titel"),
                                             daten.get("aufgabe"), daten.get("sichtbar"))
            protokoll = "Welt %s gespeichert (%s)" % (daten.get("datei"), "freigegeben" if w["sichtbar"] else "gesperrt")
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

    def _hochladen(self, abfrage, teil=False):
        ich = self._wer()
        if ich is None or self.headers.get("X-Lehrer") != "1":
            self._json(403, {"fehler": "Nur für die Lehrkraft."})
            return
        if ich.get("muss_aendern"):
            self._json(409, {"fehler": "Bitte zuerst ein eigenes Passwort festlegen."})
            return
        datei = urllib.parse.parse_qs(abfrage).get("datei", [""])[0]
        try:
            laenge = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            laenge = -1
        if not 0 < laenge <= lehrer.MAX_DATEI:
            self.close_connection = True
            self._json(413, {"fehler": "Die Datei fehlt oder ist groesser als 20 MB."})
            return
        try:
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
        except lehrer.LehrerFehler as fehler:
            self._json(409, {"fehler": str(fehler)})

    def log_message(self, fmt, *args):
        pass


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("Uebersicht auf Port %d (Konfiguration %s, Status %s, Welten %s)"
          % (PORT, KONFIG_DIR, STATUS_DIR, os.path.abspath(WELTEN_DIR)), flush=True)
    threading.Thread(target=VERWALTER.laufen, daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
