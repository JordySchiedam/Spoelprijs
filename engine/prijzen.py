#!/usr/bin/env python3
"""
Spoelprijs prijzenmotor
=======================

Haalt filamentprijzen op bij Nederlandse webshops en schrijft data.json voor de site.

Bronnen per winkel (zie config/winkels.json):
  - "feed"    : productfeed van een affiliate-netwerk (Awin, Daisycon) als CSV of CSV.gz.
                Prijs, voorraad en affiliate-link komen rechtstreeks uit de feed.
  - "html"    : productpagina uitlezen. Eerst schema.org JSON-LD, daarna meta-tags.
  - "handmatig": prijs staat in config/producten.json (bijv. Amazon, totdat je API-toegang hebt).

Gebruik:
  python engine/prijzen.py                  # normaal draaien
  python engine/prijzen.py --droog          # niets wegschrijven, alleen tonen
  python engine/prijzen.py --uit data.json  # ander uitvoerbestand

Alleen de standaardbibliotheek van Python 3.9+ is nodig.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.robotparser
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

HIER = Path(__file__).resolve().parent
ROOT = HIER.parent
TZ = timezone(timedelta(hours=1))  # Europe/Amsterdam (wintertijd); alleen voor weergave

USER_AGENT = "SpoelprijsBot/1.0 (+https://spoelprijs.nl/bot; prijsvergelijker filament)"
TIMEOUT = 25
MAX_DAGEN_OUD = 3  # zo lang mag een oude prijs blijven staan als ophalen mislukt


# --------------------------------------------------------------------------------------
# Hulpfuncties
# --------------------------------------------------------------------------------------

def parse_prijs(waarde) -> float | None:
    """Zet '€ 1.234,56', '16,49', '16.49' of 16.49 om naar een float."""
    if waarde is None:
        return None
    if isinstance(waarde, (int, float)):
        return round(float(waarde), 2)
    s = re.sub(r"[^\d,.\-]", "", str(waarde))
    if not s or not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        dui = "." if dec == "," else ","
        s = s.replace(dui, "").replace(dec, ".")
    elif "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
        s = s.replace(".", "")
    try:
        p = round(float(s), 2)
    except ValueError:
        return None
    return p if p > 0 else None


def parse_voorraad(waarde) -> bool | None:
    if waarde is None:
        return None
    s = str(waarde).strip().lower()
    if not s:
        return None
    if any(w in s for w in ("outofstock", "out of stock", "soldout", "discontinued", "niet op voorraad", "uitverkocht")):
        return False
    if any(w in s for w in ("instock", "in stock", "limitedavailability", "onlineonly", "op voorraad")):
        return True
    if s in ("1", "yes", "ja", "true", "y"):
        return True
    if s in ("0", "no", "nee", "false", "n"):
        return False
    return None


class Ophaler:
    """Haalt URL's op met een nette user-agent, robots.txt-check en vertraging per host."""

    def __init__(self, vertraging: float = 3.0):
        self.vertraging = vertraging
        self._laatst: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def _mag(self, url: str) -> bool:
        p = urllib.parse.urlparse(url)
        if p.scheme not in ("http", "https"):
            return True
        basis = f"{p.scheme}://{p.netloc}"
        if basis not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                req = urllib.request.Request(basis + "/robots.txt", headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    rp.parse(r.read().decode("utf-8", "replace").splitlines())
                self._robots[basis] = rp
            except Exception:
                self._robots[basis] = None  # geen robots.txt bereikbaar: toegestaan
        rp = self._robots[basis]
        return True if rp is None else rp.can_fetch(USER_AGENT, url)

    def haal(self, url: str, robots: bool = True) -> bytes:
        if robots and not self._mag(url):
            raise PermissionError(f"robots.txt staat {url} niet toe")
        host = urllib.parse.urlparse(url).netloc
        wacht = self.vertraging - (time.time() - self._laatst.get(host, 0))
        if host and wacht > 0:
            time.sleep(wacht)
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,text/csv,*/*;q=0.8",
            "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.5",
        })
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                data = r.read()
        finally:
            self._laatst[host] = time.time()
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        return data


# --------------------------------------------------------------------------------------
# Bron 1: productfeeds (Awin, Daisycon, ...)
# --------------------------------------------------------------------------------------

def lees_feed(ruw: bytes, kolommen: dict) -> dict[str, dict]:
    """Leest een CSV-feed en geeft {sleutel: aanbieding} terug, per EAN én per winkel-ID."""
    tekst = ruw.decode("utf-8-sig", "replace")
    try:
        dialect = csv.Sniffer().sniff(tekst[:5000], delimiters=",;|\t")
    except csv.Error:
        dialect = csv.excel
    rijen = csv.DictReader(io.StringIO(tekst), dialect=dialect)
    index: dict[str, dict] = {}
    for rij in rijen:
        prijs = parse_prijs(rij.get(kolommen.get("prijs", ""), ""))
        if prijs is None:
            continue
        aanbod = {
            "price": prijs,
            "stock": parse_voorraad(rij.get(kolommen.get("voorraad", ""), "")),
            "url": (rij.get(kolommen.get("url", ""), "") or "").strip() or None,
            "ship": parse_prijs(rij.get(kolommen.get("verzending", ""), "")) if kolommen.get("verzending") else None,
            "title": (rij.get(kolommen.get("naam", ""), "") or "").strip(),
            # Extra velden voor de automatische catalogus
            "ean": (rij.get(kolommen.get("ean", ""), "") or "").strip().split(";")[0].split(",")[0].strip(),
            "feed_id": (rij.get(kolommen.get("id", ""), "") or "").strip(),
            "brand": (rij.get(kolommen.get("merk", ""), "") or "").strip(),
            "kleur": (rij.get(kolommen.get("kleur", ""), "") or "").strip(),
            "categorie": (rij.get(kolommen.get("categorie", ""), "") or "").strip(),
        }
        for veld in ("ean", "id"):
            k = kolommen.get(veld)
            if k and rij.get(k):
                for waarde in re.split(r"[;,|\s]+", rij[k].strip()):
                    if waarde:
                        index[f"{veld}:{waarde.lstrip('0')}"] = aanbod
    return index


# --------------------------------------------------------------------------------------
# Automatische catalogus: elk filament uit een feed wordt een product op de site
# --------------------------------------------------------------------------------------

MATERIALEN = [  # volgorde is belangrijk: specifieker eerst
    # PETG/PLA mogen in een woord staan (easyPETG, ecoPLA), maar niet gevolgd door een letter (Plastic)
    (r"PETG(?![a-z])", "PETG"), (r"PLA(?![a-z])", "PLA"), (r"TPU(?![a-z])", "TPU"),
    (r"\bTPE\b", "TPE"), (r"\bABS\b", "ABS"), (r"\bASA\b", "ASA"),
    (r"\b(PA|PA6|PA12|NYLON)\b", "PA"), (r"\bPC\b", "PC"),
]
KLEUREN = [  # (zoekwoorden, hex) — voor het spoeltje op de site
    (("transparant", "transparent", "clear", "natural", "naturel", "neutraal"), "#d9e6ea"),
    (("zwart", "black", "charcoal", "jet"), "#1b1b1b"),
    (("wit", "white", "ivory", "bone"), "#f2f2ef"),
    (("grijs", "grey", "gray", "silver", "zilver", "antraciet", "anthracite"), "#8d9196"),
    (("rood", "red"), "#d23b2f"), (("oranje", "orange"), "#ef7d22"),
    (("geel", "yellow"), "#f2c230"), (("groen", "green", "olive"), "#3c9a4a"),
    (("blauw", "blue", "navy", "teal", "cyan"), "#2f6fd6"), (("paars", "purple", "violet", "lavender"), "#7d4fc4"),
    (("roze", "pink", "magenta"), "#e86aa6"), (("bruin", "brown", "wood", "coffee", "beige", "skin"), "#8a5a3b"),
    (("goud", "gold", "brons", "bronze", "copper", "koper"), "#c49a3a"),
]


def _gewicht_gram(tekst: str) -> int | None:
    t = tekst.lower().replace(",", ".")
    m = re.search(r"(\d+)\s*[x×]\s*(\d+(?:\.\d+)?)\s*(kg|g)\b", t)
    if m:
        n, w, eenheid = int(m.group(1)), float(m.group(2)), m.group(3)
        return int(round(n * (w * 1000 if eenheid == "kg" else w)))
    treffers = re.findall(r"(\d+(?:\.\d+)?)\s*(kg|g)\b(?!\s*/\s*cm)", t)
    if not treffers:
        return None
    w, eenheid = treffers[-1]
    g = float(w) * 1000 if eenheid == "kg" else float(w)
    return int(round(g)) if 100 <= g <= 10000 else None


def herken_filament(titel: str, categorie: str = "", merk: str = "", kleur: str = "") -> dict | None:
    """Maakt van een feedregel een productbeschrijving, of None als het geen 1,75 mm-filamentrol is."""
    tekst = f"{titel} {categorie}"
    laag = tekst.lower()
    if "filament" not in laag:
        return None
    if re.search(r"2[.,]85|3[.,]0+\s*mm|\b3\s*mm\b", laag):
        return None  # andere diameter
    if re.search(r"\b(printer|nozzle|dryer|droger|hotend|extruder|pen|spoolholder|spoelhouder|sample|proef)\b", laag) and not re.search(r"\d\s*(kg|g)\b", laag):
        return None
    gewicht = _gewicht_gram(titel)
    if not gewicht:
        return None
    materiaal = next((m for patroon, m in MATERIALEN if re.search(patroon, titel, re.I)), None)
    if not materiaal:
        return None
    mat = bool(re.search(r"\bmat(t|te)?\b", titel, re.I))
    shore = re.search(r"\b(\d{2})\s*A\b", titel)
    kleurnaam = kleur.strip()
    zoek = (kleurnaam or titel).lower()
    hex_ = next((h for woorden, h in KLEUREN if any(w in zoek for w in woorden)), "#9aa0a6")

    naam = titel.split(",")[0]
    if merk and naam.lower().startswith(merk.lower()):
        naam = naam[len(merk):]
    naam = re.sub(r"\b1[.,]75\s*mm\b|\d+(?:[.,]\d+)?\s*(kg|g)\b|/", " ", naam, flags=re.I)
    if kleurnaam:
        naam = re.sub(re.escape(kleurnaam) + r"\s*$", "", naam.strip(), flags=re.I)
    naam = re.sub(r"\s+", " ", naam).strip(" -–") or materiaal
    if not kleurnaam:
        kleurnaam = "Zie winkel"

    p = {"brand": merk or titel.split()[0], "name": naam, "material": materiaal,
         "color": hex_, "colorName": kleurnaam, "weight": gewicht}
    if mat:
        p["matte"] = True
    if shore and materiaal in ("TPU", "TPE"):
        p["shore"] = shore.group(1) + "A"
    return p


def auto_producten(feeds: dict[str, dict], winkels: dict, bestaand: list[dict]) -> list[dict]:
    """Voegt alle filamenten uit feeds met "auto_catalogus" toe die nog niet in de catalogus staan."""
    gezien = {p["ean"].lstrip("0") for p in bestaand if p.get("ean")}
    nieuw: list[dict] = []
    for sleutel, index in feeds.items():
        w = winkels.get(sleutel, {})
        if not w.get("auto_catalogus"):
            continue
        uniek = {id(a): a for a in index.values()}.values()
        teller = 0
        for a in uniek:
            ean = a.get("ean", "").lstrip("0")
            if ean and ean in gezien:
                continue
            p = herken_filament(a.get("title", ""), a.get("categorie", ""), a.get("brand", ""), a.get("kleur", ""))
            if not p:
                continue
            p["id"] = re.sub(r"[^a-z0-9]+", "-", f"auto-{sleutel}-{ean or a.get('feed_id')}".lower()).strip("-")
            p["auto"] = True
            if ean:
                p["ean"] = ean
                gezien.add(ean)
            # Ook bij andere feeds aanbieden als het EAN daar voorkomt
            p["winkels"] = {sleutel: ({} if ean else {"id": a.get("feed_id")})}
            for ander, idx in feeds.items():
                if ander != sleutel and ean and f"ean:{ean}" in idx:
                    p["winkels"][ander] = {}
            nieuw.append(p)
            teller += 1
            if teller >= w.get("auto_max", 2000):
                break
    return nieuw


# --------------------------------------------------------------------------------------
# Bron 2: productpagina's (JSON-LD / meta-tags)
# --------------------------------------------------------------------------------------

class _PaginaParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.jsonld: list[str] = []
        self.meta: dict[str, str] = {}
        self._in_ld = False
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self._in_ld, self._buf = True, []
        elif tag == "meta":
            sleutel = a.get("property") or a.get("itemprop") or a.get("name")
            if sleutel and "content" in a:
                self.meta.setdefault(sleutel.lower(), a["content"])
        elif a.get("itemprop") in ("price", "availability"):
            waarde = a.get("content") or a.get("href")
            if waarde:
                self.meta.setdefault(a["itemprop"], waarde)

    def handle_data(self, data):
        if self._in_ld:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._in_ld:
            self.jsonld.append("".join(self._buf))
            self._in_ld = False


def _producten_in(obj):
    if isinstance(obj, list):
        for x in obj:
            yield from _producten_in(x)
    elif isinstance(obj, dict):
        t = obj.get("@type")
        typen = t if isinstance(t, list) else [t]
        if "Product" in typen or "ProductGroup" in typen:
            yield obj
        for k in ("@graph", "hasVariant", "mainEntity"):
            if k in obj:
                yield from _producten_in(obj[k])


def _aanbiedingen(product: dict):
    offers = product.get("offers")
    if isinstance(offers, dict):
        offers = [offers]
    for o in offers or []:
        if not isinstance(o, dict):
            continue
        if o.get("@type") == "AggregateOffer" and "price" not in o:
            yield {"price": o.get("lowPrice"), "availability": o.get("availability")}
        else:
            prijs = o.get("price")
            if prijs is None and isinstance(o.get("priceSpecification"), dict):
                prijs = o["priceSpecification"].get("price")
            yield {"price": prijs, "availability": o.get("availability"), "currency": o.get("priceCurrency")}


def lees_pagina(html: str, ean: str | None = None) -> dict:
    """Haalt prijs en voorraad uit een productpagina. Gooit ValueError als er geen prijs is."""
    p = _PaginaParser()
    p.feed(html)
    kandidaten = []
    for blok in p.jsonld:
        try:
            data = json.loads(blok.strip())
        except json.JSONDecodeError:
            continue
        for prod in _producten_in(data):
            gtin = str(prod.get("gtin13") or prod.get("gtin") or prod.get("ean") or "").lstrip("0")
            for o in _aanbiedingen(prod):
                prijs = parse_prijs(o.get("price"))
                if prijs and (o.get("currency") in (None, "", "EUR")):
                    kandidaten.append((gtin, prijs, parse_voorraad(o.get("availability"))))
    if kandidaten:
        if ean:
            passend = [k for k in kandidaten if k[0] == ean.lstrip("0")]
            if passend:
                kandidaten = passend
        _, prijs, voorraad = min(kandidaten, key=lambda k: k[1])
        return {"price": prijs, "stock": voorraad, "bron": "json-ld"}

    prijs = parse_prijs(p.meta.get("product:price:amount") or p.meta.get("og:price:amount") or p.meta.get("price"))
    if prijs:
        voorraad = parse_voorraad(p.meta.get("product:availability") or p.meta.get("og:availability") or p.meta.get("availability"))
        return {"price": prijs, "stock": voorraad, "bron": "meta"}
    raise ValueError("geen prijs gevonden op de pagina")


# --------------------------------------------------------------------------------------
# Affiliate-links
# --------------------------------------------------------------------------------------

def affiliate_url(url: str, winkel: dict) -> str:
    """Zet een gewone winkel-URL om in een affiliate-link volgens winkels.json."""
    sjabloon = winkel.get("affiliate_sjabloon")
    if not url or not sjabloon or not winkel.get("affiliate"):
        return url
    waarden = {k: os.environ.get(v, "") for k, v in winkel.get("affiliate_env", {}).items()}
    if any(not v for v in waarden.values()):
        return url  # nog geen ID ingesteld: gewone link
    waarden["url"] = urllib.parse.quote(url, safe="")
    waarden["url_raw"] = url
    return sjabloon.format(**waarden)


# --------------------------------------------------------------------------------------
# Hoofdprogramma
# --------------------------------------------------------------------------------------

def laad_json(pad: Path, standaard=None):
    try:
        return json.loads(pad.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return standaard


def draai(winkels_pad: Path, producten_pad: Path, uit_pad: Path, history_pad: Path | None,
          droog: bool = False, ophaler: Ophaler | None = None, nu: datetime | None = None) -> dict:
    winkels = laad_json(winkels_pad)
    producten = laad_json(producten_pad)
    vorige = laad_json(uit_pad, {}) or {}
    ophaler = ophaler or Ophaler()
    nu = nu or datetime.now(TZ)
    nu_iso = nu.isoformat(timespec="seconds")

    # Oude prijzen bewaren voor als een winkel tijdelijk niet bereikbaar is
    oud: dict[tuple[str, str], dict] = {}
    for prod in vorige.get("products", []):
        for o in prod.get("offers", []):
            oud[(prod["id"], o["shop"])] = o

    # Feeds één keer per winkel inladen
    feeds: dict[str, dict] = {}
    rapport = {"ok": 0, "oud": 0, "mislukt": [], "feeds": {}}
    for sleutel, w in winkels.items():
        if w.get("type") != "feed" or w.get("actief") is False:
            continue
        url = os.environ.get(w.get("feed_env", ""), "") or w.get("feed_url", "")
        if not url:
            rapport["feeds"][sleutel] = "geen feed-URL ingesteld (" + w.get("feed_env", "?") + ")"
            continue
        try:
            feeds[sleutel] = lees_feed(ophaler.haal(url, robots=False), w["kolommen"])
            rapport["feeds"][sleutel] = f"{len(feeds[sleutel])} regels"
        except Exception as e:  # noqa: BLE001
            rapport["feeds"][sleutel] = f"mislukt: {e}"

    # Catalogusproducten met een EAN ook vergelijken bij elke feed waarin dat EAN voorkomt
    for prod in producten:
        ean = (prod.get("ean") or "").lstrip("0")
        if not ean:
            continue
        for sleutel, idx in feeds.items():
            if sleutel not in prod.setdefault("winkels", {}) and f"ean:{ean}" in idx:
                prod["winkels"][sleutel] = {}

    # Alle overige filamenten uit de feeds automatisch toevoegen
    auto = auto_producten(feeds, winkels, producten)
    rapport["auto"] = len(auto)
    producten = producten + auto

    uit_producten = []
    history_rijen = []
    for prod in producten:
        aanbiedingen = []
        for sleutel, cfg in prod.get("winkels", {}).items():
            w = winkels.get(sleutel)
            if not w or w.get("actief") is False:
                continue
            try:
                if w["type"] == "feed":
                    if sleutel not in feeds:
                        raise RuntimeError("feed niet beschikbaar")
                    zoek = [f"id:{cfg['id']}"] if cfg.get("id") else []
                    if prod.get("ean"):
                        zoek.append(f"ean:{prod['ean'].lstrip('0')}")
                    treffer = next((feeds[sleutel][k] for k in zoek if k in feeds[sleutel]), None)
                    if not treffer:
                        raise LookupError("product niet in feed")
                    gevonden = dict(treffer)
                    url = gevonden.get("url") or cfg.get("url")
                    # Feed-links van Awin/Daisycon zijn al affiliate-links
                    if url and not w.get("feed_is_affiliate", True):
                        url = affiliate_url(url, w)
                elif w["type"] == "html":
                    html = ophaler.haal(cfg["url"]).decode("utf-8", "replace")
                    gevonden = lees_pagina(html, prod.get("ean"))
                    url = affiliate_url(cfg["url"], w)
                elif w["type"] == "handmatig":
                    gevonden = {"price": parse_prijs(cfg.get("prijs")), "stock": cfg.get("voorraad", True)}
                    if not gevonden["price"]:
                        raise ValueError("geen handmatige prijs")
                    url = affiliate_url(cfg.get("url", ""), w)
                    gecontroleerd = cfg.get("gecontroleerd")
                    if gecontroleerd:
                        leeftijd = (nu.date() - datetime.fromisoformat(gecontroleerd).date()).days
                        if leeftijd > w.get("max_dagen_handmatig", 7):
                            raise ValueError(f"handmatige prijs is {leeftijd} dagen oud")
                else:
                    raise ValueError(f"onbekend type {w['type']}")

                aanbod = {
                    "shop": sleutel,
                    "price": gevonden["price"],
                    "stock": gevonden.get("stock") if gevonden.get("stock") is not None else True,
                    "url": url,
                    "checked": nu_iso,
                }
                aanbiedingen.append(aanbod)
                rapport["ok"] += 1
                history_rijen.append([nu.date().isoformat(), prod["id"], sleutel, f"{aanbod['price']:.2f}", int(aanbod["stock"])])
            except Exception as e:  # noqa: BLE001
                vorig = oud.get((prod["id"], sleutel))
                if vorig and vorig.get("checked"):
                    leeftijd = nu - datetime.fromisoformat(vorig["checked"])
                    if leeftijd <= timedelta(days=MAX_DAGEN_OUD):
                        aanbiedingen.append({**vorig, "stale": True})
                        rapport["oud"] += 1
                        rapport["mislukt"].append(f"{prod['id']} @ {sleutel}: {e} (oude prijs gebruikt)")
                        continue
                rapport["mislukt"].append(f"{prod['id']} @ {sleutel}: {e}")

        # Ook producten zonder prijs gaan mee; de site toont die onderaan als "nog geen prijs"
        uit_producten.append({
            k: prod[k] for k in ("id", "brand", "name", "material", "color", "colorName", "weight", "matte", "shore", "ean", "auto")
            if k in prod
        } | {"offers": aanbiedingen})

    uitvoer = {
        "updated": nu_iso,
        "demo": False,
        "shops": {
            k: {"name": w["naam"], "affiliate": bool(w.get("affiliate")), "ship": w.get("verzending", 0), "freeFrom": w.get("gratis_vanaf")}
            for k, w in winkels.items() if w.get("actief") is not False
        },
        "products": uit_producten,
    }

    if not droog:
        if rapport["ok"] == 0 and vorige.get("products"):
            print("Niets opgehaald: bestaande data.json blijft staan.", file=sys.stderr)
        else:
            tmp = uit_pad.with_suffix(".tmp")
            tmp.write_text(json.dumps(uitvoer, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(uit_pad)
        if history_pad and history_rijen:
            nieuw = not history_pad.exists()
            with history_pad.open("a", newline="", encoding="utf-8") as f:
                schrijver = csv.writer(f)
                if nieuw:
                    schrijver.writerow(["datum", "product", "winkel", "prijs", "voorraad"])
                schrijver.writerows(history_rijen)

    uitvoer["_rapport"] = rapport
    return uitvoer


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Spoelprijs prijzenmotor")
    ap.add_argument("--winkels", default=str(HIER / "config" / "winkels.json"))
    ap.add_argument("--producten", default=str(HIER / "config" / "producten.json"))
    ap.add_argument("--uit", default=str(ROOT / "data.json"))
    ap.add_argument("--history", default=str(ROOT / "history.csv"))
    ap.add_argument("--droog", action="store_true", help="niets wegschrijven")
    ap.add_argument("--vertraging", type=float, default=3.0, help="seconden tussen verzoeken naar dezelfde winkel")
    a = ap.parse_args(argv)

    res = draai(Path(a.winkels), Path(a.producten), Path(a.uit), Path(a.history), a.droog, Ophaler(a.vertraging))
    r = res["_rapport"]
    print(f"Prijzen opgehaald: {r['ok']}  ·  oude prijs gebruikt: {r['oud']}  ·  producten: {len(res['products'])}")
    for k, v in r["feeds"].items():
        print(f"  feed {k}: {v}")
    if r.get("auto"):
        print(f"  + {r['auto']} filamenten automatisch toegevoegd uit de feeds")
    # Meldingen over een ontbrekende feed samenvatten in één regel per winkel
    zonder_feed: dict[str, int] = {}
    for m in r["mislukt"]:
        if m.endswith(": feed niet beschikbaar"):
            winkel = m.split(" @ ")[1].split(":")[0]
            zonder_feed[winkel] = zonder_feed.get(winkel, 0) + 1
        else:
            print(f"  ! {m}")
    for winkel, n in zonder_feed.items():
        print(f"  - {winkel}: {n} producten overgeslagen (nog geen feed)")
    return 0 if r["ok"] or r["oud"] else 1


if __name__ == "__main__":
    sys.exit(main())
