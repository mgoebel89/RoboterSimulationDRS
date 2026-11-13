"""Tests fuer das Update aus dem Lehrerbereich (tools/updates.py, Einstellungen -> Version und Update).
Die Schnittstelle dazu testet test_lehrer.py (TestLehrerSeiten.test_update_*)."""

import json
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import updates  # noqa: E402


class UpdateGrundlage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stand_dir = os.path.join(self.tmp.name, "stand")
        self.zustand = os.path.join(self.tmp.name, "lib")
        os.makedirs(self.stand_dir)
        os.makedirs(self.zustand)
        self.einheit = os.path.join(self.tmp.name, "rebel-sim-update-ausfuehren.path")
        open(self.einheit, "w").close()
        self.alt = (updates.STAND_DIR, updates.EINHEIT)
        updates.STAND_DIR, updates.EINHEIT = self.stand_dir, self.einheit
        self.head = updates.installiert(ROOT)

    def tearDown(self):
        updates.STAND_DIR, updates.EINHEIT = self.alt
        self.tmp.cleanup()

    def schreibe(self, name, daten):
        with open(os.path.join(self.stand_dir, name), "w", encoding="utf-8") as f:
            f.write(daten if isinstance(daten, str) else json.dumps(daten))

    def pruefung(self, neu=2, stand=None):
        self.schreibe("stand.json", {"zeit": "2026-11-13T10:00:00", "fehler": None,
                                     "installiert": {"stand": stand or self.head["stand"]},
                                     "verfuegbar": {"stand": "abc1234"}, "neu": neu,
                                     "aenderungen": [{"stand": "abc1234", "datum": "13.11.2026", "text": "Neu"}] * neu})


class TestStand(UpdateGrundlage):
    def test_installiert_aus_git(self):
        self.assertIsNotNone(self.head)
        self.assertRegex(self.head["stand"], r"^[0-9a-f]{7,}$")
        self.assertRegex(self.head["datum"], r"^\d\d\.\d\d\.\d{4} \d\d:\d\d$")
        self.assertIsNone(updates.installiert(self.tmp.name))   # kein Git-Klon

    def test_ohne_pruefung(self):
        s = updates.stand(self.zustand, ROOT)
        self.assertTrue(s["eingerichtet"])
        self.assertIsNone(s["pruefung"])
        self.assertFalse(s["laeuft"])
        self.assertEqual(s["wartet"], {"pruefen": False, "ausfuehren": False})
        self.assertEqual(s["protokoll"], "")
        self.assertEqual(updates.kurz(ROOT), 0)

    def test_neuer_stand(self):
        self.pruefung(neu=2)
        s = updates.stand(self.zustand, ROOT)
        self.assertEqual(s["pruefung"]["neu"], 2)
        self.assertEqual(updates.kurz(ROOT), 2)

    def test_pruefung_veraltet_nach_update_von_hand(self):
        self.pruefung(neu=3, stand="0000000")
        s = updates.stand(self.zustand, ROOT)
        self.assertTrue(s["pruefung"]["veraltet"])
        self.assertIsNone(s["pruefung"]["neu"])
        self.assertEqual(updates.kurz(ROOT), 0)

    def test_nicht_eingerichtet(self):
        os.remove(self.einheit)
        self.pruefung(neu=2)
        self.assertFalse(updates.stand(self.zustand, ROOT)["eingerichtet"])
        self.assertEqual(updates.kurz(ROOT), 0)
        with self.assertRaises(updates.UpdateFehler):
            updates.anfordern(self.zustand, "pruefen")

    def test_lauf_und_protokoll(self):
        self.schreibe("lauf.json", {"beginn": "2026-11-13T10:00:00", "ende": None, "ergebnis": None})
        self.schreibe("protokoll.txt", "".join("Zeile %d\n" % i for i in range(300)))
        s = updates.stand(self.zustand)
        self.assertTrue(s["laeuft"])
        self.assertEqual(s["protokoll"].count("\n"), 200)   # nur das Ende
        self.assertTrue(s["protokoll"].endswith("Zeile 299\n"))
        self.schreibe("lauf.json", "kaputt")
        self.assertFalse(updates.stand(self.zustand)["laeuft"])


class TestAnfordern(UpdateGrundlage):
    def test_pruefen_legt_anfrage_ab(self):
        updates.anfordern(self.zustand, "pruefen")
        self.assertTrue(os.path.exists(os.path.join(self.zustand, "update", "pruefen")))
        self.assertTrue(updates.stand(self.zustand)["wartet"]["pruefen"])

    def test_ausfuehren_nur_mit_neuem_stand(self):
        with self.assertRaises(updates.UpdateFehler):
            updates.anfordern(self.zustand, "ausfuehren")
        self.pruefung(neu=1)
        updates.anfordern(self.zustand, "ausfuehren")
        self.assertTrue(updates.stand(self.zustand, ROOT)["wartet"]["ausfuehren"])
        with self.assertRaises(updates.UpdateFehler) as f:   # nicht zweimal
            updates.anfordern(self.zustand, "ausfuehren")
        self.assertIn("schon", str(f.exception))

    def test_nicht_waehrend_eines_laufs(self):
        self.pruefung(neu=1)
        self.schreibe("lauf.json", {"beginn": "2026-11-13T10:00:00", "ende": None, "ergebnis": None})
        for art in updates.ARTEN:
            with self.assertRaises(updates.UpdateFehler):
                updates.anfordern(self.zustand, art)

    def test_alte_anfrage_verfaellt(self):
        updates.anfordern(self.zustand, "pruefen")
        alt = time.time() - updates.ANFRAGE_VERFALLEN_S - 5
        os.utime(os.path.join(self.zustand, "update", "pruefen"), (alt, alt))
        self.assertFalse(updates.stand(self.zustand)["wartet"]["pruefen"])

    def test_unbekannte_aktion(self):
        for art in ("", "loeschen", "../x"):
            with self.assertRaises(updates.UpdateFehler):
                updates.anfordern(self.zustand, art)
        self.assertFalse(os.path.exists(os.path.join(self.zustand, "update")))


class TestDeploy(unittest.TestCase):
    """Units und Skript passen zusammen (Pfade der Anfrage-Dateien, installierte Namen)."""

    def lies(self, *teile):
        with open(os.path.join(ROOT, *teile), encoding="utf-8") as f:
            return f.read()

    def test_pfade(self):
        for art in updates.ARTEN:
            einheit = self.lies("deploy", "rebel-sim-update-%s.path" % art)
            self.assertIn("PathExists=/var/lib/rebel-sim/update/%s" % art, einheit)
            self.assertIn("rebel-sim-selbstupdate %s" % art, self.lies("deploy", "rebel-sim-update-%s.service" % art))
            self.assertIn('rm -f "$ANFRAGE/%s"' % art, self.lies("deploy", "selbstupdate.sh"))
        install = self.lies("deploy", "install.sh")
        for name in ("rebel-sim-update-pruefen.timer", "rebel-sim-update-ausfuehren.path", "selbstupdate.sh"):
            self.assertIn(name, install)
        self.assertEqual(os.path.basename(updates.EINHEIT if updates.EINHEIT.endswith(".path") else ""),
                         "rebel-sim-update-ausfuehren.path")


if __name__ == "__main__":
    unittest.main()
