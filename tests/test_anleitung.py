"""Anleitung als HTML (tools/anleitung_html.py)."""

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import anleitung_html as a  # noqa: E402


class TestAnleitung(unittest.TestCase):
    def test_echte_anleitung(self):
        with open(os.path.join(ROOT, "docs", "ANLEITUNG.md"), encoding="utf-8") as f:
            h = a.zu_html(f.read())
        ids = set(re.findall(r'id="([^"]+)"', h))
        self.assertEqual([l for l in re.findall(r'href="#([^"]+)"', h) if l not in ids], [])   # Inhaltsverzeichnis
        for bild in re.findall(r'src="/lehrer/anleitung/bilder/([^"]+)"', h):
            self.assertTrue(os.path.exists(os.path.join(ROOT, "docs", "bilder", bild)), bild)
        for rest in ("```", "![", "](", "<script"):
            self.assertNotIn(rest, h)

    def test_bausteine(self):
        h = a.zu_html("# Titel <b>\n\n1. Schritt mit `code`\n\n   ```bash\n   befehl --x\n   ```\n\n2. **Zwei**\n   - innen\n"
                      "\nText [Seite](https://x.de) und [Datei](../tools/x.py), ![Bild](bilder/a.jpg)\n\n"
                      "| A | B |\n|---|---|\n| 1 | *k* |\n\n> Hinweis\n\n---\n")
        self.assertIn('<h1 id="titel">Titel &lt;b&gt;</h1>', h)
        self.assertIn('<ol><li>Schritt mit <code>code</code><pre data-sprache="bash"><code>befehl --x</code></pre></li>'
                      '<li><strong>Zwei</strong><ul><li>innen</li></ul></li></ol>', h)
        self.assertIn('<a href="https://x.de" target="_blank" rel="noopener">Seite</a> und Datei, '
                      '<img src="/lehrer/anleitung/bilder/a.jpg" alt="Bild" loading="lazy">', h)
        self.assertIn("<thead><tr><th>A</th><th>B</th></tr></thead><tbody><tr><td>1</td><td><em>k</em></td></tr></tbody>", h)
        self.assertIn("<blockquote><p>Hinweis</p></blockquote><hr>", h)
        self.assertIn('<ol start="3"><li>drei</li></ol>', a.zu_html("3. drei\n"))


if __name__ == "__main__":
    unittest.main()
