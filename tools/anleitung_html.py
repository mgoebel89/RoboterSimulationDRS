"""docs/ANLEITUNG.md als HTML fuer den Lehrerbereich (/lehrer/anleitung), nur Standardbibliothek.

Kann, was die Anleitung braucht: Ueberschriften mit Ankern wie auf GitHub (Inhaltsverzeichnis), Absaetze,
verschachtelte Listen, Codebloecke (auch in Listen), Tabellen, Zitate, Trennlinien, Bilder (docs/bilder),
Links, **fett**, *kursiv*, `Code`. Der Text wird zuerst HTML-maskiert, eigenes HTML in der Anleitung wirkt nicht.
Links auf Dateien im Repository werden zu Text (der Container liefert keine Repository-Dateien aus).
"""

import html
import re

BILD_PFAD = "/lehrer/anleitung/bilder/"
SEITEN = {"testablauf_schule.html": "/lehrer/testablauf", "ANLEITUNG.md": "/lehrer/anleitung"}
LISTE = re.compile(r"( *)([-*]|\d+\.) +(.*)")
UEBERSCHRIFT = re.compile(r"(#{1,6}) +(.*?) *#*$")


def anker(text):
    """Anker wie GitHub: klein, Satzzeichen weg, Leerzeichen zu '-' (Umlaute bleiben)."""
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[^\w\- ]", "", text.strip().lower())
    return text.replace(" ", "-")


def _ziel(href):
    if href.startswith(("http://", "https://", "#")):
        return href
    if href.startswith("bilder/"):
        return BILD_PFAD + href[len("bilder/"):]
    return SEITEN.get(href.split("#")[0])


def _inline(text):
    """Auszeichnungen in einer Zeile; text ist noch nicht maskiert."""
    text = html.escape(text, quote=False)
    code = []

    def merken(inhalt):
        code.append(inhalt)
        return "\x00%d\x00" % (len(code) - 1)
    text = re.sub(r"`([^`\n]+)`", lambda m: merken("<code>%s</code>" % m.group(1)), text)

    def bild(m):
        ziel = _ziel(m.group(2))
        if not ziel:
            return m.group(1)
        return merken('<img src="%s" alt="%s" loading="lazy">' % (html.escape(ziel), html.escape(m.group(1))))
    text = re.sub(r"!\[([^\]\n]*)\]\(([^\s)]+)\)", bild, text)

    def link(m):
        ziel = _ziel(m.group(2))
        if not ziel:
            return m.group(1)
        extern = ziel.startswith("http") or ziel == SEITEN["testablauf_schule.html"]
        return '<a href="%s"%s>%s</a>' % (html.escape(ziel), ' target="_blank" rel="noopener"' if extern else "", m.group(1))
    text = re.sub(r"\[([^\]\n]+)\]\(([^\s)]+)\)", link, text)
    text = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])", r"<em>\1</em>", text)
    text = re.sub(r"(?<![\w&#/])_(?=\S)(.+?)(?<=\S)_(?![\w/])", r"<em>\1</em>", text)
    for _ in range(2):   # Code in Links bzw. Bilder in Tabellen
        text = re.sub("\x00(\\d+)\x00", lambda m: code[int(m.group(1))], text)
    return text


def _einzug(zeile):
    return len(zeile) - len(zeile.lstrip(" "))


def _beginnt_block(zeile):
    s = zeile.strip()
    return (s.startswith(("```", "|", ">", "#")) or s in ("---", "***") or bool(LISTE.fullmatch(zeile.rstrip())))


def _zellen(zeile):
    zeile = zeile.strip().strip("|")
    return [z.strip() for z in zeile.split("|")]


def _liste(zeilen, i):
    """Liste ab Zeile i; Rueckgabe (html, naechste Zeile)."""
    m = LISTE.fullmatch(zeilen[i].rstrip())
    basis, nummeriert = len(m.group(1)), m.group(2)[0].isdigit()
    eintraege = []
    while i < len(zeilen):
        z = zeilen[i]
        m = LISTE.fullmatch(z.rstrip())
        if not z.strip():
            j = i
            while j < len(zeilen) and not zeilen[j].strip():
                j += 1
            weiter = j < len(zeilen) and (_einzug(zeilen[j]) > basis or (
                LISTE.fullmatch(zeilen[j].rstrip()) and _einzug(zeilen[j]) == basis))
            if not weiter:
                break
            if eintraege:
                eintraege[-1][1].append("")
            i += 1
            continue
        if m and len(m.group(1)) == basis:
            if m.group(2)[0].isdigit() != nummeriert:
                break
            inhalt_einzug = basis + len(m.group(2)) + 1
            eintraege.append((inhalt_einzug, [m.group(3)], m.group(2)))
        elif eintraege and (_einzug(z) > basis or not _beginnt_block(z)):
            if _einzug(z) <= basis and eintraege[-1][1] and not eintraege[-1][1][-1].strip():
                break   # nach einer Leerzeile ohne Einzug: neuer Absatz ausserhalb der Liste
            weg = min(_einzug(z), eintraege[-1][0])
            eintraege[-1][1].append(z[weg:])
        else:
            break
        i += 1
    teile = []
    for _einz, inhalt, _marke in eintraege:
        innen = bloecke(inhalt)
        if innen.startswith("<p>"):   # erster Absatz ohne Abstand, wie bei kurzen Listen ueblich
            ende = innen.index("</p>")
            innen = innen[3:ende] + innen[ende + 4:]
        teile.append("<li>%s</li>" % innen)
    start = ""
    if nummeriert and eintraege and eintraege[0][2] != "1.":
        start = ' start="%d"' % int(eintraege[0][2][:-1])
    tag = "ol" if nummeriert else "ul"
    return "<%s%s>%s</%s>" % (tag, start, "".join(teile), tag), i


def bloecke(zeilen):
    """Liste von Zeilen (Einzug schon entfernt) -> HTML."""
    raus, i = [], 0
    while i < len(zeilen):
        z = zeilen[i]
        s = z.strip()
        if not s:
            i += 1
            continue
        if s.startswith("```"):
            einzug, sprache, code = _einzug(z), s[3:].strip(), []
            i += 1
            while i < len(zeilen) and not zeilen[i].strip().startswith("```"):
                code.append(zeilen[i][min(einzug, _einzug(zeilen[i])):])
                i += 1
            i += 1
            raus.append('<pre%s><code>%s</code></pre>' % (' data-sprache="%s"' % html.escape(sprache) if sprache else "",
                                                          html.escape("\n".join(code), quote=False)))
            continue
        m = UEBERSCHRIFT.fullmatch(s)
        if m:
            stufe, text = len(m.group(1)), _inline(m.group(2))
            raus.append('<h%d id="%s">%s</h%d>' % (stufe, html.escape(anker(m.group(2))), text, stufe))
            i += 1
            continue
        if s in ("---", "***"):
            raus.append("<hr>")
            i += 1
            continue
        if s.startswith("|"):
            tabelle = []
            while i < len(zeilen) and zeilen[i].strip().startswith("|"):
                tabelle.append(_zellen(zeilen[i]))
                i += 1
            kopf, rest = tabelle[0], tabelle[1:]
            if rest and all(re.fullmatch(r":?-{2,}:?", c) for c in rest[0] if c):
                rest = rest[1:]
            zeile = lambda zellen, tag: "<tr>%s</tr>" % "".join("<%s>%s</%s>" % (tag, _inline(c), tag) for c in zellen)  # noqa: E731
            leer = all(not c for c in kopf)
            raus.append('<div class="tabelle"><table>%s<tbody>%s</tbody></table></div>' % (
                "" if leer else "<thead>%s</thead>" % zeile(kopf, "th"), "".join(zeile(r, "td") for r in rest)))
            continue
        if s.startswith(">"):
            zitat = []
            while i < len(zeilen) and zeilen[i].strip().startswith(">"):
                zitat.append(re.sub(r"^\s*> ?", "", zeilen[i]))
                i += 1
            raus.append("<blockquote>%s</blockquote>" % bloecke(zitat))
            continue
        if LISTE.fullmatch(z.rstrip()):
            teil, i = _liste(zeilen, i)
            raus.append(teil)
            continue
        absatz = [s]
        i += 1
        while i < len(zeilen) and zeilen[i].strip() and not _beginnt_block(zeilen[i]):
            absatz.append(zeilen[i].strip())
            i += 1
        raus.append("<p>%s</p>" % _inline(" ".join(absatz)))
    return "".join(raus)


def zu_html(text):
    return bloecke(text.replace("\r\n", "\n").split("\n"))


STIL_ANLEITUNG = """
.anleitung { max-width:900px; margin:0 auto; padding:20px 24px 60px; line-height:1.55; }
.anleitung h1 { font-size:1.7em; margin:.2em 0 .6em; }
.anleitung h2 { margin-top:1.8em; padding-bottom:.25em; border-bottom:1px solid var(--linie); color:var(--blau-dunkel); }
.anleitung h3 { margin-top:1.4em; }
.anleitung pre { background:#f2f4f7; border:1px solid var(--linie); border-radius:var(--r); padding:10px 12px;
  overflow-x:auto; font-size:.88em; line-height:1.4; }
.anleitung code { font-family:Consolas, "Courier New", monospace; font-size:.92em; }
.anleitung :not(pre) > code { background:#f2f4f7; padding:1px 4px; border-radius:4px; overflow-wrap:anywhere; }
.anleitung img { max-width:100%; height:auto; border:1px solid var(--linie); border-radius:var(--r); }
.anleitung .tabelle { overflow-x:auto; margin:.8em 0; }
.anleitung table { border-collapse:collapse; }
.anleitung th, .anleitung td { border:1px solid var(--linie); padding:5px 8px; vertical-align:top; text-align:left; }
.anleitung th { background:var(--bg); }
.anleitung td img { max-width:280px; }
.anleitung blockquote { margin:.8em 0; padding:6px 14px; border-left:4px solid var(--blau-mittel); background:var(--blau-hell); }
.anleitung li { margin:.2em 0; }
.anleitung hr { border:0; border-top:1px solid var(--linie); margin:1.5em 0; }
.anleitung-kopf { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:6px; }
@media print { header, .anleitung-kopf { display:none; } .anleitung { padding:0; } }
@media (max-width:600px) { .anleitung { padding:12px 16px 40px; } }
"""


def seite(text):
    """Ganze Seite; %%STIL%% und %%MARKE%% setzt uebersicht.seite() ein."""
    return ("""<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ReBeL - Anleitung</title><style>%%STIL%%""" + STIL_ANLEITUNG + """</style></head>
<body><header>%%MARKE%% <span class="unter">Anleitung</span></header>
<main class="anleitung"><div class="anleitung-kopf"><a class="btn" href="/lehrer">Zurück zum Lehrerbereich</a>
<a class="btn" href="/lehrer/testablauf" target="_blank" rel="noopener">Testablauf Schule (Druckvorlage)</a>
<button onclick="print()">Drucken</button></div>
""" + zu_html(text) + "</main></body></html>")
