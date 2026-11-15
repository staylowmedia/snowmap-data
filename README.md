# snowmap-data

Snøbilder til appen Snowmap. En planlagt jobb henter snødybde for hele verden
fra den amerikanske værmodellen GFS og publiserer ferdige bilder som appen
legger over kartet.

## Slik virker det

1. GitHub Actions kjører `scripts/build_snow.py` fire ganger i døgnet.
2. Skriptet laster ned snødybde (0,25° rutenett) for i dag og sju dager frem
   fra NOAAs åpne arkiv, én liten fil per dag.
3. Hver dag blir et gjennomsiktig PNG-bilde i Web Mercator, 2048 × 2048
   piksler, der hvitt betyr snø på bakken.
4. Bildene og `snow/latest.json` publiseres på GitHub Pages.
5. Samme kjøring skriver `snow/ai_sentences.json`: én kort snøsetning per
   5°-rute og språk. Uten API-nøkkel blir alle setninger regeltekst.
   Feiler AI-steget, publiseres bildene og `latest.json` likevel.

## Filer appen bruker

- `snow/latest.json` – hvilken fil som hører til hvilken dato. Formatet er uendret.
- `snow/snow_<kjøring>_d<dag>.png` – ett bilde per dag. Uendret.
- `snow/ai_sentences.json` – setninger og `app_config`, beskrevet under.

## Snøsetninger

Filen ligger ved siden av `latest.json` og bruker samme `generated` og `run`.
Setningen beskriver snødybde i en 5°-rute, ikke et punkt, og den inneholder
ikke stedsnavn. Appen kan bytte ut `{place}` med stedsnavnet selv.

Eksempel, forkortet (filen har alle 2592 rutene, ti språk i hver):

```json
{
  "schema_version": 1,
  "generated": "2026-11-15T06:25:00Z",
  "run": "2026111500",
  "model": "rule-based",
  "languages": ["en", "nb", "sv", "de", "fr", "es", "it", "pt-BR", "ja", "zh-Hans"],
  "placeholder": "{place}",
  "max_age_hours": 30,
  "app_config": { "live_ai_daily_cap": 5 },
  "window": { "first_date": "2026-11-15", "last_date": "2026-11-22" },
  "grid": {
    "resolution_deg": 5.0,
    "lat_south": -90.0,
    "lon_west": -180.0,
    "n_lat": 36,
    "n_lon": 72,
    "index": "i * n_lon + j"
  },
  "ai_calls": 0,
  "cells": [
    { "text": { "en": "Little or no snow on the ground in this area.", "nb": "…" },
      "source": { "en": "rule", "nb": "rule" } }
  ]
}
```

Ukjente felt skal ignoreres. `schema_version` er 1.

| Felt | Betydning |
|---|---|
| `generated`, `run` | Samme strenger som i `latest.json` fra samme kjøring. |
| `model` | Modell-id når minst én setning ble godkjent, ellers `"rule-based"`. |
| `languages` | Kodene appen bruker: `en`, `nb`, `sv`, `de`, `fr`, `es`, `it`, `pt-BR`, `ja`, `zh-Hans`. |
| `placeholder` | Alltid `{place}`. Regelteksten i denne versjonen bruker den ikke. |
| `max_age_hours` | 30. Appen bruker tallet i ferskhetsregelen under. |
| `app_config.live_ai_daily_cap` | Heltall ≥ 0. Tak for **brukerutløste** direkte modellkall per enhet per lokal dag. Forhåndsberegnede setninger teller ikke. Standard 5. |
| `window` | Datoene setningen gjelder, lik første og siste dato i `latest.json`. |
| `ai_calls` | Antall modellkall i kjøringen. Appen trenger det ikke. |
| `cells` | 2592 objekter, radvis. `text` og `source` har alle språkene. `source` er `"ai"` eller `"rule"`. |

`app_config` settes ett sted. Standarden er `LIVE_AI_DAILY_CAP` i
`scripts/snow_sentences.py` (5). Actions-variabelen `SNOWMAP_LIVE_AI_DAILY_CAP`
overstyrer den. Tom eller ugyldig verdi (ikke et heltall ≥ 0) gir 5, og
jobben fortsetter. Mangler feltet, eller er filen uleselig, bruker appen 5.
Setningenes ferskhetsregler hever ikke taket.

### Rute

Les tallene fra `grid`, ikke hardkod dem.

```
lonC = ((lon + 180) mod 360) - 180    # resten skal være i [0, 360)
if lonC == 180: lonC = -180           # 180 og −180 er samme rute
latC = clamp(lat, -90, 90)
i = floor((latC - lat_south) / resolution_deg)
j = floor((lonC - lon_west) / resolution_deg)
if i == n_lat: i = n_lat - 1          # breddegrad 90
if j == n_lon: j = n_lon - 1
i = clamp(i, 0, n_lat - 1)
j = clamp(j, 0, n_lon - 1)
index = i * n_lon + j
```

I Swift gir `truncatingRemainder` fortegn; bruk en modulo som gir ikke-negativ
rest. Rute `(i, j)` dekker bredde `[lat_south + i·oppløsning, lat_south + (i+1)·oppløsning)`
og lengde `[lon_west + j·oppløsning, lon_west + (j+1)·oppløsning)`. Den nordligste
ruten inkluderer også 90°N.

Oslo (59,91°N, 10,75°Ø) → `i = 29`, `j = 38`, indeks `29 * 72 + 38 = 2126`,
ruten `[55, 60) × [10, 15)`. Tromsø (69,65°N, 18,96°Ø) → `i = 31`, `j = 39`.

Et GFS-punkt regnes som snø når dybden er over 1 cm. Snitt og maks er over
alle 0,25°-punktene i ruten, også hav. Setningen gjelder `window.first_date`
(ca. kl. 12 UTC) og sier fra hvis snittet ventes å stige eller synke med
minst 5 cm innen `window.last_date`. Det er én setning for hele vinduet,
ikke én per dag. Temperatur, nedbør og tiårshistorikk er ikke med.

`{place}` kan stå høyst én gang. Appen bytter den ut med stedsnavnet som
ren tekst. Uten navn fjernes tokenet og doble mellomrom slås sammen.
Mer enn ett punktum, URL, HTML, markdown eller emoji blir forkastet før
publisering, og den språklinjen faller tilbake til regelen.

### Når appen skal bruke filen

Bruk setningen bare når alt dette holder:

1. `schema_version` er 1, og `len(cells)` er `n_lat * n_lon`.
2. `run` og `generated` er lik de samme feltene i `latest.json`.
3. `generated` er `ÅÅÅÅ-MM-DDTHH:MM:SSZ`. Alder = nå (UTC) minus `generated`.
   Godta alder fra −15 minutter til `max_age_hours` (30), inklusivt.
4. Valgt kalenderdato ligger i `window.first_date` … `window.last_date`, inklusivt.
5. Språkkoden finnes som nøkkel. Bruk kodene over, ikke `nb-NO` eller `zh`.

Ellers brukes regelsetningen i appen (`SnowAI.fallback`). En fersk fil kan
erstatte det direkte modellkallet. Datoer utenfor vinduet, for eksempel jul
når jul er mer enn sju dager unna, dekkes ikke.

`live_ai_daily_cap` leses også fra en fil med `schema_version` 1 selv om
setningene er for gamle. Mangler filen, brukes 5.

### Hemmeligheter

AI kjøres bare når alle tre er satt. Det er ingen standardleverandør.
Nøkkelen sendes som `Authorization: Bearer` og skrives ikke til filen, URL-en
eller loggen.

| Navn | Type | |
|---|---|---|
| `SNOWMAP_AI_API_KEY` | Actions-hemmelighet | Påkrevd for modellkall. |
| `SNOWMAP_AI_BASE_URL` | Actions-variabel | Påkrevd. `https`, uten brukerinfo, spørrestreng eller fragment. `/chat/completions` legges til om den mangler. |
| `SNOWMAP_AI_MODEL` | Actions-variabel | Påkrevd. For eksempel `gpt-4.1-mini`. |
| `SNOWMAP_AI_MAX_CALLS` | Actions-variabel | Valgfri. Standard og hardt tak 16. `0` gir bare regler. |
| `SNOWMAP_LIVE_AI_DAILY_CAP` | Actions-variabel | Valgfri. Overstyrer 5. Se over. |

Hvert kall tar inntil 8 ruter og alle ti språk, med 12 sekunders tidsavbrudd
og 60 sekunders tak for hele AI-steget. Manglende nøkkel, HTTP-feil, avkuttet
svar eller en setning som ikke består sjekken, gir regeltekst for den linjen.

## Kjøre lokalt

```
pip install -r requirements.txt
PYTHONPATH=scripts python -m unittest discover -s tests -v
python scripts/build_snow.py --out site
```

## Datakilde

NOAA/NCEP Global Forecast System (GFS). Dataene er offentlig eiendom.
