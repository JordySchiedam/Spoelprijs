"""Tests voor de prijzenmotor. Draaien met:  python -m unittest discover -s engine/tests"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import prijzen as pz  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"


def furl(naam):
    return (FIX / naam).as_uri()


class TestParsers(unittest.TestCase):
    def test_prijs(self):
        self.assertEqual(pz.parse_prijs("€ 1.234,56"), 1234.56)
        self.assertEqual(pz.parse_prijs("16,49"), 16.49)
        self.assertEqual(pz.parse_prijs("16.49"), 16.49)
        self.assertEqual(pz.parse_prijs("1,234.50"), 1234.5)
        self.assertEqual(pz.parse_prijs("1.299"), 1299.0)
        self.assertEqual(pz.parse_prijs(20), 20.0)
        self.assertIsNone(pz.parse_prijs("gratis"))
        self.assertIsNone(pz.parse_prijs("0,00"))

    def test_voorraad(self):
        self.assertTrue(pz.parse_voorraad("https://schema.org/InStock"))
        self.assertFalse(pz.parse_voorraad("https://schema.org/OutOfStock"))
        self.assertFalse(pz.parse_voorraad("Niet op voorraad"))
        self.assertTrue(pz.parse_voorraad("1"))
        self.assertIsNone(pz.parse_voorraad(""))

    def test_pagina_jsonld(self):
        r = pz.lees_pagina((FIX / "pagina_jsonld.html").read_text(), "6933582309633")
        self.assertEqual((r["price"], r["stock"], r["bron"]), (20.10, True, "json-ld"))

    def test_pagina_meta(self):
        r = pz.lees_pagina((FIX / "pagina_meta.html").read_text())
        self.assertEqual((r["price"], r["stock"], r["bron"]), (19.95, False, "meta"))

    def test_pagina_kapot(self):
        with self.assertRaises(ValueError):
            pz.lees_pagina((FIX / "pagina_kapot.html").read_text())

    def test_feed_gz(self):
        idx = pz.lees_feed(pz.Ophaler(0).haal(furl("awin_feed.csv.gz")), {
            "ean": "ean", "id": "merchant_product_id", "prijs": "search_price",
            "voorraad": "in_stock", "url": "aw_deep_link"})
        self.assertEqual(idx["ean:6933582309633"]["price"], 15.99)
        self.assertEqual(idx["ean:6933582399999"]["price"], 24.49)
        self.assertFalse(idx["id:SUN-TPU-GN"]["stock"])

    def test_affiliate(self):
        w = {"affiliate": True, "affiliate_sjabloon": "{url_raw}?tag={tag}", "affiliate_env": {"tag": "TEST_TAG"}}
        os.environ.pop("TEST_TAG", None)
        self.assertEqual(pz.affiliate_url("https://a.nl/dp/X", w), "https://a.nl/dp/X")
        os.environ["TEST_TAG"] = "spoel-21"
        self.assertEqual(pz.affiliate_url("https://a.nl/dp/X", w), "https://a.nl/dp/X?tag=spoel-21")


class TestDraai(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.winkels = {
            "jake": {"naam": "3DJake", "type": "feed", "affiliate": True, "feed_url": furl("awin_feed.csv.gz"),
                     "kolommen": {"ean": "ean", "id": "merchant_product_id", "prijs": "search_price",
                                  "voorraad": "in_stock", "url": "aw_deep_link"},
                     "verzending": 4.95, "gratis_vanaf": 49},
            "pro": {"naam": "Proshop", "type": "html", "affiliate": False, "verzending": 6.99, "gratis_vanaf": None},
            "amz": {"naam": "Amazon.nl", "type": "handmatig", "affiliate": True, "max_dagen_handmatig": 1,
                    "affiliate_sjabloon": "{url_raw}?tag={tag}", "affiliate_env": {"tag": "TEST_TAG"},
                    "verzending": 3.99, "gratis_vanaf": 20},
        }
        self.producten = [
            {"id": "matte-wit", "brand": "SUNLU", "name": "PLA Matte", "material": "PLA", "color": "#fff",
             "colorName": "Wit", "weight": 1000, "ean": "6933582309633",
             "winkels": {"jake": {}, "pro": {"url": furl("pagina_jsonld.html")},
                         "amz": {"url": "https://www.amazon.nl/dp/X", "prijs": 17.49, "gecontroleerd": "2026-11-17"}}},
            {"id": "kapot", "brand": "X", "name": "Y", "material": "PLA", "color": "#000", "colorName": "Zwart",
             "weight": 1000, "winkels": {"pro": {"url": furl("pagina_kapot.html")}}},
        ]
        (self.tmp / "w.json").write_text(json.dumps(self.winkels))
        (self.tmp / "p.json").write_text(json.dumps(self.producten))
        os.environ["TEST_TAG"] = "spoel-21"
        self.nu = datetime(2026, 11, 17, 22, 0, tzinfo=pz.TZ)

    def run_(self, nu=None):
        return pz.draai(self.tmp / "w.json", self.tmp / "p.json", self.tmp / "data.json", self.tmp / "h.csv",
                        ophaler=pz.Ophaler(0), nu=nu or self.nu)

    def test_volledige_run(self):
        res = self.run_()
        data = json.loads((self.tmp / "data.json").read_text())
        self.assertEqual(len(data["products"]), 2)  # kapot product blijft staan, zonder prijs
        self.assertEqual(data["products"][1]["offers"], [])
        offers = {o["shop"]: o for o in data["products"][0]["offers"]}
        self.assertEqual(offers["jake"]["price"], 15.99)
        self.assertTrue(offers["jake"]["url"].startswith("https://www.awin1.com"))
        self.assertEqual(offers["pro"]["price"], 20.10)
        self.assertEqual(offers["amz"]["url"], "https://www.amazon.nl/dp/X?tag=spoel-21")
        self.assertEqual(res["_rapport"]["ok"], 3)
        self.assertEqual(len(res["_rapport"]["mislukt"]), 1)
        self.assertEqual(len((self.tmp / "h.csv").read_text().strip().splitlines()), 4)  # kop + 3

    def test_oude_prijs_bij_storing(self):
        self.run_()
        # Proshop-pagina 'valt weg'
        self.producten[0]["winkels"]["pro"]["url"] = furl("bestaat_niet.html")
        (self.tmp / "p.json").write_text(json.dumps(self.producten))
        res = self.run_(self.nu + timedelta(days=1))
        pro = [o for o in res["products"][0]["offers"] if o["shop"] == "pro"][0]
        self.assertTrue(pro["stale"])
        self.assertEqual(pro["price"], 20.10)
        # na 4 dagen verdwijnt hij
        res = self.run_(self.nu + timedelta(days=4))
        self.assertFalse([o for o in res["products"][0]["offers"] if o["shop"] == "pro"])

    def test_handmatige_prijs_te_oud(self):
        res = self.run_(self.nu + timedelta(days=3))
        self.assertFalse([o for o in res["products"][0]["offers"] if o["shop"] == "amz"])


if __name__ == "__main__":
    unittest.main()
