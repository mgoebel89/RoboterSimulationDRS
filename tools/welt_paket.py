"""Welten als Paket (ZIP) exportieren und in einer anderen Instanz importieren,
z.B. zuhause im Baukasten fertig bauen und in der Schule einspielen.

Ein Paket enthaelt alles, was die Welt braucht:
  rebel-welt.json                                   Kennung, Version, Inhalt
  welt/<name>.wbt .json .baukasten.json .png|.jpg .pdf
  teile/<teil>/teil.json netz.json netz.obj quelle.step   nur die Teile, die die Welt verwendet
  backen/<satz>/backensatz.json                     eigener Backensatz, falls der Greifer der Welt einer ist
Nicht enthalten sind das Robotermodell und die iRC-Werkzeuge (igus-Daten): die erzeugt jede Instanz selbst.

Beim Import:
  - gleichnamige, inhaltsgleiche Teile/Backensaetze werden weiterverwendet; unterscheiden sie sich,
    kommt das neue unter einem freien Namen (teil-2, ...) dazu und die Welt verweist darauf,
  - eine vorhandene Welt wird nur mit "ersetzen" ueberschrieben (die alte geht in den Papierkorb),
    sonst unter einem neuen Namen angelegt,
  - die Welt ist danach fuer Schueler gesperrt, bis die Lehrkraft sie freigibt.
Nur Python-Standardbibliothek.
"""

import hashlib
import io
import json
import os
import re
import time
import zipfile

import lehrer

FORMAT = "rebel-sim-welt"
VERSION = 1
MANIFEST = "rebel-welt.json"
WELT_NAME = re.compile(r"[a-z0-9_-]{1,50}")
TEIL_NAME = re.compile(r"[a-z0-9_-]{1,60}")
SATZ_NAME = re.compile(r"[a-z][a-z0-9_]{0,19}")
TEIL_DATEIEN = ("teil.json", "netz.json", "netz.obj", "quelle.step", "vorschau.png")
WELT_ENDUNGEN = lehrer.WELT_ENDUNGEN
MAX_PAKET = 100 * 1024 * 1024         # hochgeladene ZIP-Datei
MAX_ENTPACKT = 400 * 1024 * 1024      # Summe der Dateien im Paket (Schutz vor "ZIP-Bomben")
TEIL_VERWEIS = re.compile(r'"\.\./teile/([a-z0-9_-]{1,60})/netz\.obj"')
# Zeichenketten in der Welt, die auf Dateien zeigen: nur diese sind erlaubt
ERLAUBTE_VERWEISE = (re.compile(r"\.\./protos/[A-Za-z0-9_]+\.proto"), re.compile(r"\.\./teile/[a-z0-9_-]{1,60}/netz\.obj"),
                     re.compile(r"webots://[^\"]*"), re.compile(r"https://raw\.githubusercontent\.com/cyberbotics/webots/[^\"]*"))


class PaketFehler(Exception):
    """Fehler mit einer Meldung fuer die Lehrerseite."""


class WeltVorhanden(PaketFehler):
    def __init__(self, name, vorschlag):
        super().__init__("Die Welt %s gibt es hier schon." % name)
        self.name, self.vorschlag = name, vorschlag


def verwendete_teile(wbt_text, entwurf=None):
    """Namen der Teile aus der Teilebibliothek, die eine Welt braucht."""
    namen = set(TEIL_VERWEIS.findall(wbt_text))
    for t in (entwurf or {}).get("teile", []):
        if t.get("teil") and TEIL_NAME.fullmatch(str(t["teil"])):
            namen.add(t["teil"])
    return sorted(namen)


def _lies(pfad, modus="r"):
    with open(pfad, modus, **({} if "b" in modus else {"encoding": "utf-8"})) as f:
        return f.read()


def exportieren(welten_dir, teile_dir, datei, backensatz=None, jetzt=None):
    """ZIP einer Welt. backensatz: (name, ordner) des eigenen Backensatzes der Welt oder None.
    Rueckgabe: (Dateiname, Inhalt)."""
    if not re.fullmatch(r"[a-z0-9_-]{1,50}\.wbt", datei or "") or not os.path.isfile(os.path.join(welten_dir, datei)):
        raise PaketFehler("Diese Welt gibt es nicht.")
    name = datei[:-4]
    basis = os.path.join(welten_dir, name)
    wbt = _lies(basis + ".wbt")
    entwurf = lehrer._lies_json(basis + ".baukasten.json") if os.path.exists(basis + ".baukasten.json") else None
    teile = set(verwendete_teile(wbt, entwurf))
    satz = None
    if backensatz:
        satz = json.loads(_lies(os.path.join(backensatz[1], "backensatz.json")))
        if satz.get("teil"):
            teile.add(satz["teil"])
    fehlend = [t for t in sorted(teile) if not os.path.isfile(os.path.join(teile_dir, t, "teil.json"))]
    if fehlend:
        raise PaketFehler("Die Welt braucht Teile, die in der Teilebibliothek fehlen: %s" % ", ".join(fehlend))
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(MANIFEST, json.dumps({
            "format": FORMAT, "version": VERSION, "welt": name, "titel": lehrer._lies_json(basis + ".json").get("titel", name),
            "erstellt": time.strftime("%Y-%m-%d %H:%M", time.localtime(jetzt)), "teile": sorted(teile),
            "backensatz": backensatz[0] if backensatz else None}, ensure_ascii=False, indent=2))
        for e in WELT_ENDUNGEN:
            if os.path.exists(basis + e):
                z.write(basis + e, "welt/" + name + e)
        for t in sorted(teile):
            for d in TEIL_DATEIEN:
                if os.path.exists(os.path.join(teile_dir, t, d)):
                    z.write(os.path.join(teile_dir, t, d), "teile/%s/%s" % (t, d))
        if satz is not None:
            # Kenndaten erzeugt die Zielinstanz neu; Besitzer gilt nur in dieser Instanz
            z.writestr("backen/%s/backensatz.json" % backensatz[0], json.dumps(
                {k: v for k, v in satz.items() if k not in ("kenndaten", "besitzer")}, ensure_ascii=False, indent=2))
    return "rebel-welt-%s.zip" % name, puffer.getvalue()


def _paket_lesen(inhalt):
    """Paket pruefen: Manifest, erlaubte Dateinamen, Groessen. Rueckgabe (manifest, {name: bytes})."""
    if len(inhalt) > MAX_PAKET:
        raise PaketFehler("Das Paket ist groesser als 100 MB.")
    try:
        z = zipfile.ZipFile(io.BytesIO(inhalt))
    except zipfile.BadZipFile:
        raise PaketFehler("Das ist keine ZIP-Datei (Welt-Paket aus „Exportieren“).")
    with z:
        try:
            manifest = json.loads(z.read(MANIFEST).decode("utf-8"))
        except (KeyError, ValueError, UnicodeDecodeError):
            raise PaketFehler("Im ZIP fehlt %s – ist es ein Welt-Paket aus „Exportieren“?" % MANIFEST)
        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
            raise PaketFehler("Das ZIP ist kein Welt-Paket der ReBeL-Simulation.")
        if not isinstance(manifest.get("version"), int) or manifest["version"] > VERSION:
            raise PaketFehler("Das Paket stammt aus einer neueren Version; bitte diese Instanz aktualisieren (rebel-sim-update).")
        name = str(manifest.get("welt", ""))
        if not WELT_NAME.fullmatch(name):
            raise PaketFehler("Ungueltiger Weltname im Paket.")
        muster = re.compile(r"(?:welt/%s(?:%s)|teile/[a-z0-9_-]{1,60}/(?:%s)|backen/[a-z][a-z0-9_]{0,19}/backensatz\.json|%s)"
                            % (re.escape(name), "|".join(re.escape(e) for e in WELT_ENDUNGEN),
                               "|".join(re.escape(d) for d in TEIL_DATEIEN), re.escape(MANIFEST)))
        summe, dateien = 0, {}
        for info in z.infolist():
            if info.is_dir():
                continue
            if not muster.fullmatch(info.filename):
                raise PaketFehler("Unerwartete Datei im Paket: %s" % info.filename[:80])
            summe += info.file_size
            if summe > MAX_ENTPACKT:
                raise PaketFehler("Das Paket ist entpackt zu gross.")
            dateien[info.filename] = z.read(info)
    if "welt/%s.wbt" % name not in dateien:
        raise PaketFehler("Im Paket fehlt die Welt-Datei %s.wbt." % name)
    return manifest, dateien


def _pruefe_wbt(text):
    if not text.startswith("#VRML_SIM"):
        raise PaketFehler("Die Welt-Datei ist keine Webots-Welt.")
    # nur Zeichenketten, die nach Datei oder Adresse aussehen (Titel und Aufgabe duerfen "/" enthalten)
    for wert in re.findall(r'"([^"\n]*(?:://|\.(?:proto|obj|stl|dae|png|jpe?g|hdr|wbt|wav|mp3))[^"\n]*)"', text, re.I):
        if not any(m.fullmatch(wert) for m in ERLAUBTE_VERWEISE):
            raise PaketFehler("Die Welt verweist auf eine nicht erlaubte Datei: %s" % wert[:80])
    for wert in re.findall(r'\bcontroller\s+"([^"]*)"', text):
        if wert not in ("irc_bridge", "<none>"):
            raise PaketFehler("Die Welt nutzt einen fremden Controller (%s)." % wert[:40])


def _frei(name, belegt, muster, laenge, trenner="-"):
    """Freier Name wie name-2 (Welten, Teile) bzw. name_2 (Backensaetze: ohne Bindestrich)."""
    for n in range(2, 1000):
        kandidat = "%s%s%d" % (name[:laenge - len(trenner) - len(str(n))], trenner, n)
        if muster.fullmatch(kandidat) and kandidat not in belegt:
            return kandidat
    raise PaketFehler("Kein freier Name fuer %s." % name)


def _hash(dateien):
    h = hashlib.sha256()
    for name, daten in sorted(dateien.items()):
        h.update(name.encode() + b"\0" + daten + b"\0")
    return h.hexdigest()


SATZ_VERGLEICH = ("basis", "teil", "matrix", "verschiebung_mm", "tcp", "tcp_versatz_mm", "hub_mm")


def importieren(inhalt, welten_dir, teile_dir, backen_dir, neuer_name=None, ersetzen=False, besitzer=None,
                fest_backen=()):
    """Paket einspielen. fest_backen: Namen mitgelieferter Backensaetze (nie ueberschreiben).
    Rueckgabe: Bericht {welt, teile_neu, teile_gleich, teile_umbenannt, backensatz, backensatz_neu, greifer}."""
    manifest, dateien = _paket_lesen(inhalt)
    alt_name = manifest["welt"]
    name = neuer_name or alt_name
    if not WELT_NAME.fullmatch(name):
        raise PaketFehler("Weltname: 1 bis 50 Zeichen, nur a-z, 0-9, - und _.")
    vorhanden = set(n[:-4] for n in os.listdir(welten_dir) if n.endswith(".wbt")) if os.path.isdir(welten_dir) else set()
    if name in vorhanden and not ersetzen:
        raise WeltVorhanden(name, _frei(name, vorhanden, re.compile(r"[a-z0-9_-]{1,50}"), 50))
    wbt = dateien["welt/%s.wbt" % alt_name].decode("utf-8", "replace")
    _pruefe_wbt(wbt)
    entwurf = None
    if "welt/%s.baukasten.json" % alt_name in dateien:
        try:
            entwurf = json.loads(dateien["welt/%s.baukasten.json" % alt_name].decode("utf-8"))
        except ValueError:
            raise PaketFehler("Der Baukasten-Entwurf im Paket ist kaputt.")
    angaben = {}
    if "welt/%s.json" % alt_name in dateien:
        try:
            angaben = json.loads(dateien["welt/%s.json" % alt_name].decode("utf-8"))
        except ValueError:
            angaben = {}

    # Teile: gleiche weiterverwenden, abweichende unter freiem Namen
    im_paket = {}
    for pfad, daten in dateien.items():
        if pfad.startswith("teile/"):
            _, teil, datei = pfad.split("/")
            im_paket.setdefault(teil, {})[datei] = daten
    for teil, inhalt_teil in im_paket.items():
        if "teil.json" not in inhalt_teil or "netz.obj" not in inhalt_teil or "netz.json" not in inhalt_teil:
            raise PaketFehler("Teil %s ist im Paket unvollstaendig." % teil)
        try:
            json.loads(inhalt_teil["teil.json"].decode("utf-8"))
            json.loads(inhalt_teil["netz.json"].decode("utf-8"))
        except ValueError:
            raise PaketFehler("Teil %s ist im Paket kaputt." % teil)
    belegt = set(os.listdir(teile_dir)) if os.path.isdir(teile_dir) else set()

    def hash_hier(name):
        ordner = os.path.join(teile_dir, name)
        return _hash({d: _lies(os.path.join(ordner, d), "rb") for d in ("teil.json", "netz.obj")
                      if os.path.exists(os.path.join(ordner, d))})
    umbenannt, neu, gleich, schon_da = {}, [], [], set()   # schon_da: Teile des Pakets, die nicht geschrieben werden
    for teil in sorted(im_paket):
        if teil in belegt:
            h = _hash({d: im_paket[teil][d] for d in ("teil.json", "netz.obj")})
            if hash_hier(teil) == h:
                gleich.append(teil)
                schon_da.add(teil)
                continue
            # schon einmal unter anderem Namen importiert (klotz-2, ...)? dann das weiterverwenden
            frueher = next((n for n in sorted(belegt) if n.startswith(teil + "-") and os.path.isdir(os.path.join(teile_dir, n))
                            and hash_hier(n) == h), None)
            if frueher:
                umbenannt[teil] = frueher
                gleich.append(frueher)
                schon_da.add(teil)
                continue
            umbenannt[teil] = _frei(teil, belegt, TEIL_NAME, 60)
            belegt.add(umbenannt[teil])
        neu.append(umbenannt.get(teil, teil))

    # Backensatz: gleich -> weiterverwenden, sonst unter freiem Namen
    satz, satz_name, satz_neu, greifer_alt, greifer_neu = None, None, False, None, None
    pfade = [p for p in dateien if p.startswith("backen/")]
    if pfade:
        alt_satz = pfade[0].split("/")[1]
        try:
            satz = json.loads(dateien[pfade[0]].decode("utf-8"))
        except ValueError:
            raise PaketFehler("Der Greifer im Paket ist kaputt.")
        if satz.get("name") != alt_satz or not SATZ_NAME.fullmatch(alt_satz):
            raise PaketFehler("Der Greifer im Paket ist kaputt.")
        if satz.get("teil") in umbenannt:
            satz["teil"] = umbenannt[satz["teil"]]
        for t in (satz.get("entwurf") or {}).get("teile", []):
            if t.get("teil") in umbenannt:
                t["teil"] = umbenannt[t["teil"]]
        greifer_alt = "%s_%s" % (satz.get("basis", "egp25"), alt_satz)
        belegte_saetze = (set(os.listdir(backen_dir)) if os.path.isdir(backen_dir) else set()) | set(fest_backen)
        satz_name = alt_satz

        def gleicher(name):
            if name in fest_backen:
                return False
            hier = lehrer._lies_json(os.path.join(backen_dir, name, "backensatz.json"))
            return bool(hier) and all(hier.get(k) == satz.get(k) for k in SATZ_VERGLEICH)
        # derselbe Backensatz ist schon da (unter seinem Namen oder frueher umbenannt als robi3_2, ...)
        da = next((n for n in [alt_satz] + sorted(n for n in belegte_saetze if n.startswith(alt_satz + "_"))
                   if n in belegte_saetze and gleicher(n)), None)
        if da:
            satz_name = da
        else:
            if alt_satz in belegte_saetze:
                satz_name = _frei(alt_satz, belegte_saetze, SATZ_NAME, 20, "_")
            satz_neu = True
        satz["name"] = satz_name
        greifer_neu = "%s_%s" % (satz.get("basis", "egp25"), satz_name)

    # Welt umschreiben: Verweise auf umbenannte Teile und den Backensatz, eigener Name
    for alt, neu_teil in umbenannt.items():
        wbt = wbt.replace('"../teile/%s/netz.obj"' % alt, '"../teile/%s/netz.obj"' % neu_teil)
    wbt = wbt.replace("%s.baukasten.json" % alt_name, "%s.baukasten.json" % name)
    if greifer_alt and greifer_neu != greifer_alt:
        wbt = re.sub(r'(\bgreifer\s+")' + re.escape(greifer_alt) + '"', lambda m: m.group(1) + greifer_neu + '"', wbt)
    if entwurf is not None:
        for t in entwurf.get("teile", []):
            if t.get("teil") in umbenannt:
                t["teil"] = umbenannt[t["teil"]]
        if greifer_alt and entwurf.get("greifer") == greifer_alt:
            entwurf["greifer"] = greifer_neu
        if besitzer:
            entwurf["besitzer"] = besitzer
    angaben["sichtbar"] = False   # erst ansehen, dann freigeben

    # Schreiben: erst Teile und Backensatz, dann die Welt (ersetzt: alte Welt in den Papierkorb)
    os.makedirs(teile_dir, exist_ok=True)
    for teil in sorted(im_paket):
        ziel_name = umbenannt.get(teil, teil)
        if teil in schon_da:
            continue
        ziel = os.path.join(teile_dir, ziel_name)
        os.makedirs(ziel + ".neu", exist_ok=True)
        for d, daten in im_paket[teil].items():
            with open(os.path.join(ziel + ".neu", d), "wb") as f:
                f.write(daten)
        os.replace(ziel + ".neu", ziel)
    if satz is not None and satz_neu:
        os.makedirs(os.path.join(backen_dir, satz_name), exist_ok=True)
        if besitzer:
            satz["besitzer"] = besitzer
        lehrer.schreibe_json(os.path.join(backen_dir, satz_name, "backensatz.json"), satz)
    if name in vorhanden:
        lehrer.loesche_welt(welten_dir, name + ".wbt")
    for e in WELT_ENDUNGEN:
        quelle = "welt/%s%s" % (alt_name, e)
        if quelle not in dateien:
            continue
        ziel = os.path.join(welten_dir, name + e)
        if e == ".wbt":
            daten = wbt.encode("utf-8")
        elif e == ".baukasten.json" and entwurf is not None:
            daten = json.dumps(entwurf, ensure_ascii=False, indent=2).encode("utf-8")
        elif e == ".json":
            daten = json.dumps(angaben, ensure_ascii=False, indent=2).encode("utf-8")
        else:
            daten = dateien[quelle]
        with open(ziel + ".tmp", "wb") as f:
            f.write(daten)
        os.replace(ziel + ".tmp", ziel)
    if "welt/%s.json" % alt_name not in dateien:
        lehrer.schreibe_json(os.path.join(welten_dir, name + ".json"), angaben)
    greifer = lehrer.welt_greifer(welten_dir, name + ".wbt")
    return {"welt": name + ".wbt", "titel": angaben.get("titel") or name, "teile_neu": neu, "teile_gleich": gleich,
            "teile_umbenannt": umbenannt, "backensatz": satz_name, "backensatz_neu": satz_neu, "greifer": greifer}
