# Spoelprijs

Prijsvergelijker voor 3D-printer filament bij Nederlandse webshops, met affiliate-links.

```
index.html                      de website (laadt data.json, valt terug op voorbeelddata)
data.json                       wordt elke dag gemaakt door de prijzenmotor
history.csv                     prijsgeschiedenis (voor latere grafieken en alerts)
engine/prijzen.py               de prijzenmotor (alleen standaard Python 3.9+)
engine/config/winkels.json      winkels: type bron, verzendkosten, affiliate-instellingen
engine/config/producten.json    jouw productcatalogus
engine/tests/                   tests + voorbeeldbestanden
.github/workflows/prijzen.yml   draait de motor elke dag via GitHub Actions
```

## Zo werkt het

Per winkel kies je in `winkels.json` één van drie bronnen:

| type | wanneer | wat je nodig hebt |
|---|---|---|
| `feed` | winkel met affiliate-programma (3DJake via Awin, FEPshop via Daisycon) | de feed-URL uit je Awin/Daisycon-account. De links in de feed zijn al affiliate-links. |
| `html` | winkel zonder programma (Proshop, 123-3D, MakerPoint) | de URL van de productpagina in `producten.json` |
| `handmatig` | Amazon.nl | prijs + datum `gecontroleerd` in `producten.json` |

**Producten koppelen:** zet bij elk product het **EAN** (de barcode, staat vaak op de productpagina of in de feed). Daarmee vindt de motor het product automatisch in elke feed. Zonder EAN kun je per winkel een `"id"` opgeven (het artikelnummer uit de feed).

**Als een winkel tijdelijk niet werkt** blijft de laatste prijs maximaal 3 dagen staan (gemarkeerd als oud). Daarna verdwijnt de winkel bij dat product tot hij weer werkt.

## Amazon: let op

Amazon heeft strenge regels voor het tonen van prijzen op affiliate-sites. Scrape Amazon niet (dat mag niet en wordt geblokkeerd). Zet de prijzen handmatig in `producten.json` en lees de regels van het Amazon PartnerNet-programma na. Zodra je toegang hebt tot hun product-API, kan er een `amazon`-bron bij die het automatisch doet. Een handmatige prijs ouder dan `max_dagen_handmatig` (standaard 1 dag) wordt niet getoond.

## Lokaal testen

```bash
python -m unittest discover -s engine/tests     # tests
python engine/prijzen.py --droog                # proefdraaien zonder iets op te slaan
python engine/prijzen.py                        # echt draaien, schrijft data.json
python -m http.server 8000                      # site bekijken op http://localhost:8000
```

Feed-URL's en je Amazon-tag geef je mee als omgevingsvariabele:

```bash
export AWIN_3DJAKE_FEED_URL="https://productdata.awin.com/datafeed/download/apikey/..."
export AMAZON_TAG="jouwtag-21"
```

## Live zetten (gratis)

1. Maak een GitHub-repository en zet deze map erin.
2. **Settings → Secrets and variables → Actions**: voeg `AWIN_3DJAKE_FEED_URL`, `DAISYCON_FEPSHOP_FEED_URL` en `AMAZON_TAG` toe zodra je ze hebt.
3. **Settings → Pages**: kies "Deploy from a branch", branch `main`, map `/ (root)`.
4. Koppel je eigen domein (bijv. spoelprijs.nl) onder Pages → Custom domain.
5. Tabblad **Actions → Prijzen bijwerken → Run workflow** om de eerste keer te draaien. Daarna gaat het elke ochtend vanzelf.

## Checklist voor livegang

- [ ] Naam en domein vastleggen
- [ ] Verzendkosten per winkel controleren in `winkels.json`
- [ ] Catalogus vullen: minimaal 20–30 populaire filamenten met EAN
- [ ] Aanmelden bij Awin (3DJake), Daisycon (FEPshop) en Amazon PartnerNet
- [ ] Feed-URL's en tag als GitHub-secrets toevoegen
- [ ] Privacyverklaring en cookievermelding toevoegen (affiliate-links plaatsen cookies bij de winkel)
- [ ] Bij winkels zonder programma: hun voorwaarden en robots.txt respecteren (de motor checkt robots.txt en wacht 3 seconden tussen verzoeken)
