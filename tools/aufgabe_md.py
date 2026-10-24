"""Aufgabentexte der Welten als Markdown (kleine, sichere Teilmenge; nur Standardbibliothek).

Der Text wird zuerst vollstaendig HTML-maskiert; danach entstehen nur diese Elemente:
  # / ## / ### Ueberschrift      Absatz (Leerzeile trennt, einfacher Zeilenumbruch bleibt einer)
  - oder * Aufzaehlung          1. nummerierte Liste      > Hinweis (Zitat)      --- Trennlinie
  **fett**   *kursiv* / _kursiv_   `Code`   [Text](https://...) Link   | Tabelle | mit | Kopfzeile |
Bisherige Aufgaben ohne Markdown sehen damit aus wie vorher (Absaetze und Zeilenumbrueche bleiben).
"""

import html
import re

LINK = re.compile(r"\[([^\]\n]+)\]\(((?:https?://|/welten/)[^\s)]+)\)")


def _inline(text):
    """Auszeichnungen in einer Zeile; text ist schon maskiert."""
    code = []

    def merken(m):
        code.append(m.group(1))
        return "\x00%d\x00" % (len(code) - 1)
    text = re.sub(r"`([^`\n]+)`", merken, text)
    text = LINK.sub(lambda m: '<a href="%s" target="_blank" rel="noopener">%s</a>' % (m.group(2), m.group(1)), text)
    text = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])", r"<em>\1</em>", text)
    text = re.sub(r"(?<![\w&#])_(?=\S)(.+?)(?<=\S)_(?!\w)", r"<em>\1</em>", text)
    return re.sub("\x00(\\d+)\x00", lambda m: "<code>%s</code>" % code[int(m.group(1))], text)


def _zellen(zeile):
    zeile = zeile.strip()
    if zeile.startswith("|"):
        zeile = zeile[1:]
    if zeile.endswith("|"):
        zeile = zeile[:-1]
    return [z.strip() for z in zeile.split("|")]


def zu_html(text):
    """Markdown -> HTML-Schnipsel (ohne umschliessendes Element)."""
    zeilen = html.escape(text or "", quote=True).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    aus, absatz, zitat = [], [], []
    liste = None   # [art, [Eintraege]]

    def absatz_ende():
        if absatz:
            aus.append("<p>%s</p>" % "<br>".join(_inline(z) for z in absatz))
            absatz.clear()
        if zitat:
            aus.append("<blockquote>%s</blockquote>" % "<br>".join(_inline(z) for z in zitat))
            zitat.clear()

    def liste_ende():
        nonlocal liste
        if liste:
            aus.append("<%s>%s</%s>" % (liste[0], "".join("<li>%s</li>" % _inline(e) for e in liste[1]), liste[0]))
            liste = None

    i = 0
    while i < len(zeilen):
        z = zeilen[i]
        s = z.strip()
        i += 1
        if not s:
            absatz_ende()
            liste_ende()
            continue
        kopf = re.match(r"(#{1,3})\s+(.*?)\s*#*$", s)
        ul = re.match(r"[-*+]\s+(.*)", s)
        ol = re.match(r"\d{1,3}[.)]\s+(.*)", s)
        if kopf:
            absatz_ende()
            liste_ende()
            stufe = len(kopf.group(1)) + 2   # h3..h5: der Titel der Welt ist schon die Hauptueberschrift
            aus.append("<h%d>%s</h%d>" % (stufe, _inline(kopf.group(2)), stufe))
        elif re.fullmatch(r"(-\s*){3,}|(\*\s*){3,}|(_\s*){3,}", s):
            absatz_ende()
            liste_ende()
            aus.append("<hr>")
        elif s.startswith("|") and i < len(zeilen) and re.fullmatch(r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?", zeilen[i].strip()):
            absatz_ende()
            liste_ende()
            kopfzellen = _zellen(s)
            i += 1
            reihen = []
            while i < len(zeilen) and zeilen[i].strip().startswith("|"):
                reihen.append(_zellen(zeilen[i]))
                i += 1
            aus.append("<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" % (
                "".join("<th>%s</th>" % _inline(c) for c in kopfzellen),
                "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % _inline(c) for c in r) for r in reihen)))
        elif ul or ol:
            if absatz or zitat:
                absatz_ende()
            art = "ul" if ul else "ol"
            if liste and liste[0] != art:
                liste_ende()
            if not liste:
                liste = [art, []]
            liste[1].append((ul or ol).group(1))
        elif s.startswith("&gt;"):
            liste_ende()
            if absatz:
                absatz_ende()
            zitat.append(s[4:].strip())
        elif liste and z[:1] in (" ", "\t"):
            liste[1][-1] += " " + s   # eingerueckte Folgezeile gehoert zum Listeneintrag
        else:
            liste_ende()
            if zitat:
                absatz_ende()
            absatz.append(s)
    absatz_ende()
    liste_ende()
    return "\n".join(aus)


def kurz(text, laenge=160):
    """Klartext ohne Markdown-Zeichen fuer Listen und Vorschauen (eine Zeile)."""
    t = LINK.sub(r"\1", text or "")
    t = re.sub(r"^\s*(#{1,3}|[-*+]|\d{1,3}[.)]|>)\s+", "", t, flags=re.M)
    t = re.sub(r"[*_`|]", "", t)
    t = " ".join(t.split())
    return t if len(t) <= laenge else t[:laenge - 1].rstrip() + "…"
