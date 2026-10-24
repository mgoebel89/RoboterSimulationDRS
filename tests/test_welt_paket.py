"""Tests fuer Welt-Pakete (tools/welt_paket.py) und Markdown-Aufgaben (tools/aufgabe_md.py)."""

import io
import json
import os
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import aufgabe_md  # noqa: E402
import lehrer  # noqa: E402
import welt_paket as wp  # noqa: E402

WBT = ('#VRML_SIM R2025a utf8\n# Erzeugt vom Weltbaukasten (tools/baukasten.py) aus lager.baukasten.json\n'
       'EXTERNPROTO "../protos/IgusRebel6DOF.proto"\nEXTERNPROTO "../protos/Aufbau.proto"\n'
       'WorldInfo { title "Lager 1/2" }\nDEF REBEL IgusRebel6DOF {\n  greifer "egp40_robi3"\n}\n'
       'Aufbau {\n  modell [ "../teile/magazin/netz.obj" ]\n}\nWerkstueck {\n  modell [ "../teile/klotz/netz.obj" ]\n}\n')


class Instanz:
    """Ordner einer Instanz: worlds, teile, backen."""

    def __init__(self, wurzel):
        self.welten, self.teile, self.backen = (os.path.join(wurzel, d) for d in ("worlds", "teile", "backen"))
        for d in (self.welten, self.teile, self.backen):
            os.makedirs(d)

    def teil(self, name, inhalt="v 0 0 0\n"):
        os.makedirs(os.path.join(self.teile, name))
        for datei, daten in (("teil.json", '{"box": [0, 0, 0, 1, 1, 1]}'), ("netz.json", "{}"), ("netz.obj", inhalt),
                             ("quelle.step", "ISO-10303-21;")):
            with open(os.path.join(self.teile, name, datei), "w", encoding="utf-8") as f:
                f.write(daten)

    def backensatz(self, name, **werte):
        os.makedirs(os.path.join(self.backen, name))
        lehrer.schreibe_json(os.path.join(self.backen, name, "backensatz.json"), dict(
            {"name": name, "basis": "egp40", "teil": "backe", "matrix": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
             "verschiebung_mm": [-8, 0, 100], "tcp": "spitze", "tcp_versatz_mm": 0, "kenndaten": {"tcp_mm": 120},
             "besitzer": "lehrer:gob"}, **werte))

    def welt(self, name="lager", wbt=WBT):
        with open(os.path.join(self.welten, name + ".wbt"), "w", encoding="utf-8") as f:
            f.write(wbt)
        lehrer.schreibe_json(os.path.join(self.welten, name + ".json"), {"titel": "Lager", "aufgabe": "# Ziel", "sichtbar": True})
        lehrer.schreibe_json(os.path.join(self.welten, name + ".baukasten.json"), {
            "titel": "Lager", "greifer": "egp40_robi3", "besitzer": "lehrer:gob",
            "teile": [{"id": "m", "teil": "magazin"}, {"id": "k", "teil": "klotz"}]})
        with open(os.path.join(self.welten, name + ".png"), "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\nbild")


class TestWeltPaket(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.zuhause = Instanz(os.path.join(self.tmp.name, "zuhause"))
        self.schule = Instanz(os.path.join(self.tmp.name, "schule"))
        z = self.zuhause
        z.teil("magazin"), z.teil("klotz"), z.teil("backe"), z.teil("unbenutzt")
        z.backensatz("robi3")
        z.welt()

    def tearDown(self):
        self.tmp.cleanup()

    def paket(self):
        return wp.exportieren(self.zuhause.welten, self.zuhause.teile, "lager.wbt",
                              ("robi3", os.path.join(self.zuhause.backen, "robi3")), jetzt=0)

    def einspielen(self, inhalt, **optionen):
        s = self.schule
        return wp.importieren(inhalt, s.welten, s.teile, s.backen, besitzer="lehrer:mueller", **optionen)

    def test_export_enthaelt_alles_was_die_welt_braucht(self):
        name, inhalt = self.paket()
        self.assertEqual(name, "rebel-welt-lager.zip")
        z = zipfile.ZipFile(io.BytesIO(inhalt))
        namen = set(z.namelist())
        for erwartet in ("rebel-welt.json", "welt/lager.wbt", "welt/lager.json", "welt/lager.baukasten.json", "welt/lager.png",
                         "teile/magazin/netz.obj", "teile/klotz/quelle.step", "teile/backe/teil.json", "backen/robi3/backensatz.json"):
            self.assertIn(erwartet, namen)
        self.assertNotIn("teile/unbenutzt/teil.json", namen)
        manifest = json.loads(z.read("rebel-welt.json"))
        self.assertEqual((manifest["format"], manifest["welt"], manifest["teile"], manifest["backensatz"]),
                         ("rebel-sim-welt", "lager", ["backe", "klotz", "magazin"], "robi3"))
        satz = json.loads(z.read("backen/robi3/backensatz.json"))
        self.assertNotIn("kenndaten", satz)   # erzeugt die Zielinstanz neu
        self.assertNotIn("besitzer", satz)
        with self.assertRaises(wp.PaketFehler):
            wp.exportieren(self.zuhause.welten, self.zuhause.teile, "../lager.wbt")
        os.remove(os.path.join(self.zuhause.teile, "klotz", "teil.json"))
        with self.assertRaises(wp.PaketFehler):   # fehlendes Teil faellt beim Export auf, nicht erst in der Schule
            self.paket()

    def test_import_in_leere_instanz(self):
        b = self.einspielen(self.paket()[1])
        self.assertEqual((b["welt"], b["teile_neu"], b["teile_umbenannt"], b["backensatz"], b["backensatz_neu"], b["greifer"]),
                         ("lager.wbt", ["backe", "klotz", "magazin"], {}, "robi3", True, "egp40_robi3"))
        s = self.schule
        with open(os.path.join(s.welten, "lager.wbt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), WBT)
        angaben = lehrer._lies_json(os.path.join(s.welten, "lager.json"))
        self.assertEqual((angaben["titel"], angaben["aufgabe"], angaben["sichtbar"]), ("Lager", "# Ziel", False))   # erst freigeben
        self.assertEqual(lehrer._lies_json(os.path.join(s.welten, "lager.baukasten.json"))["besitzer"], "lehrer:mueller")
        self.assertEqual(lehrer._lies_json(os.path.join(s.backen, "robi3", "backensatz.json"))["besitzer"], "lehrer:mueller")
        self.assertTrue(os.path.exists(os.path.join(s.teile, "klotz", "quelle.step")))
        self.assertTrue(os.path.exists(os.path.join(s.welten, "lager.png")))

    def test_gleiche_teile_bleiben_abweichende_bekommen_neuen_namen(self):
        s = self.schule
        s.teil("magazin")                           # gleich wie zuhause
        s.teil("klotz", inhalt="v 9 9 9\n")         # anderer Klotz mit gleichem Namen
        s.teil("klotz-2", inhalt="v 5 5 5\n")       # belegt den naechsten Namen
        s.backensatz("robi3", verschiebung_mm=[-9, 0, 100])   # anderer Backensatz gleichen Namens
        b = self.einspielen(self.paket()[1])
        self.assertEqual((b["teile_gleich"], b["teile_umbenannt"], b["backensatz"], b["greifer"]),
                         (["magazin"], {"klotz": "klotz-3"}, "robi3_2", "egp40_robi3_2"))
        with open(os.path.join(s.welten, "lager.wbt"), encoding="utf-8") as f:
            wbt = f.read()
        self.assertIn('"../teile/klotz-3/netz.obj"', wbt)
        self.assertIn('"../teile/magazin/netz.obj"', wbt)
        self.assertIn('greifer "egp40_robi3_2"', wbt)
        with open(os.path.join(s.teile, "klotz", "netz.obj"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "v 9 9 9\n")   # vorhandenes Teil unberuehrt
        entwurf = lehrer._lies_json(os.path.join(s.welten, "lager.baukasten.json"))
        self.assertEqual(([t["teil"] for t in entwurf["teile"]], entwurf["greifer"]), (["magazin", "klotz-3"], "egp40_robi3_2"))
        self.assertEqual(lehrer._lies_json(os.path.join(s.backen, "robi3", "backensatz.json"))["verschiebung_mm"], [-9, 0, 100])
        # derselbe Backensatz ein zweites Mal: wird weiterverwendet
        self.zuhause.welt("lager2")
        _, inhalt = wp.exportieren(self.zuhause.welten, self.zuhause.teile, "lager2.wbt",
                                   ("robi3", os.path.join(self.zuhause.backen, "robi3")))
        b = self.einspielen(inhalt)
        # ... ebenso der frueher als klotz-3 uebernommene Klotz
        self.assertEqual((b["backensatz"], b["backensatz_neu"], b["teile_gleich"], b["teile_neu"]),
                         ("robi3_2", False, ["backe", "klotz-3", "magazin"], []))
        with open(os.path.join(s.welten, "lager2.wbt"), encoding="utf-8") as f:
            self.assertIn('"../teile/klotz-3/netz.obj"', f.read())

    def test_vorhandene_welt_neuer_name_oder_ersetzen(self):
        inhalt = self.paket()[1]
        self.einspielen(inhalt)
        with self.assertRaises(wp.WeltVorhanden) as e:
            self.einspielen(inhalt)
        self.assertEqual((e.exception.name, e.exception.vorschlag), ("lager", "lager-2"))
        b = self.einspielen(inhalt, neuer_name="lager_neu")
        self.assertEqual(b["welt"], "lager_neu.wbt")
        s = self.schule
        self.assertTrue(os.path.exists(os.path.join(s.welten, "lager_neu.png")))
        with open(os.path.join(s.welten, "lager_neu.wbt"), encoding="utf-8") as f:
            self.assertIn("aus lager_neu.baukasten.json", f.read())
        b = self.einspielen(inhalt, ersetzen=True)
        self.assertEqual(b["welt"], "lager.wbt")
        korb = os.listdir(os.path.join(s.welten, "papierkorb"))
        self.assertEqual(len(korb), 1)
        with self.assertRaises(wp.PaketFehler):
            self.einspielen(inhalt, neuer_name="Böse Welt")

    def zip(self, dateien):
        puffer = io.BytesIO()
        with zipfile.ZipFile(puffer, "w") as z:
            for name, daten in dateien.items():
                z.writestr(name, daten)
        return puffer.getvalue()

    def test_manipulierte_pakete(self):
        manifest = json.dumps({"format": "rebel-sim-welt", "version": 1, "welt": "x"})
        gut = {"rebel-welt.json": manifest, "welt/x.wbt": WBT.replace("egp40_robi3", "egp40")}
        self.assertEqual(self.einspielen(self.zip(gut))["welt"], "x.wbt")
        for name, dateien in (
                ("kein zip", None),
                ("ohne manifest", {"welt/x.wbt": WBT}),
                ("fremdes format", dict(gut, **{"rebel-welt.json": '{"format": "anders", "version": 1, "welt": "y"}'})),
                ("neuere version", dict(gut, **{"rebel-welt.json": '{"format": "rebel-sim-welt", "version": 99, "welt": "y"}'})),
                ("pfad nach aussen", dict(gut, **{"../../etc/passwd": "x"})),
                ("andere datei", dict(gut, **{"teile/a/boese.sh": "x"})),
                ("fremder controller", {"rebel-welt.json": manifest.replace('"x"', '"y"'),
                                        "welt/y.wbt": WBT + 'Robot { controller "boese" }\n'}),
                ("fremdes proto", {"rebel-welt.json": manifest.replace('"x"', '"z"'),
                                   "welt/z.wbt": WBT + 'EXTERNPROTO "https://boese.example/a.proto"\n'}),
                ("teil unvollstaendig", {"rebel-welt.json": manifest.replace('"x"', '"w"'), "welt/w.wbt": WBT,
                                         "teile/klotz/netz.obj": "v 0 0 0\n"})):
            with self.assertRaises(wp.PaketFehler, msg=name):
                self.einspielen(b"PK nix" if dateien is None else self.zip(dateien))
        self.assertFalse(os.path.exists(os.path.join(self.schule.welten, "y.wbt")))
        self.assertFalse(os.path.exists(os.path.join(self.schule.teile, "klotz")))


class TestMarkdown(unittest.TestCase):
    def test_alte_aufgaben_bleiben_lesbar(self):
        self.assertEqual(aufgabe_md.zu_html("Setze den Stift.\n\nGreifer: EGP 40.\nDOut32 schliesst."),
                         "<p>Setze den Stift.</p>\n<p>Greifer: EGP 40.<br>DOut32 schliesst.</p>")

    def test_auszeichnungen(self):
        h = aufgabe_md.zu_html("# Ziel\n**fett** und *kursiv*, `DOut31`, snake_case_name\n\n- a\n- b\n  weiter\n\n1. x\n\n"
                               "> Achtung\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n---")
        for teil in ("<h3>Ziel</h3>", "<strong>fett</strong>", "<em>kursiv</em>", "<code>DOut31</code>", "snake_case_name",
                     "<ul><li>a</li><li>b weiter</li></ul>", "<ol><li>x</li></ol>", "<blockquote>Achtung</blockquote>",
                     "<th>A</th>", "<td>2</td>", "<hr>"):
            self.assertIn(teil, h)

    def test_sicher(self):
        h = aufgabe_md.zu_html('<script>alert(1)</script> [x](javascript:alert(1)) [ok](https://a.de/?a=1&b="2") '
                               '<img src=x onerror=alert(1)>')
        self.assertNotIn("<script", h)
        self.assertNotIn("<img", h)
        self.assertNotIn('href="javascript', h)
        self.assertIn('<a href="https://a.de/?a=1&amp;b=&quot;2&quot;"', h)

    def test_kurz(self):
        self.assertEqual(aufgabe_md.kurz("# Ziel\n**Stift** auf [Marke](https://x)\n- a"), "Ziel Stift auf Marke a")
        self.assertEqual(len(aufgabe_md.kurz("x" * 500)), 160)


if __name__ == "__main__":
    unittest.main()
