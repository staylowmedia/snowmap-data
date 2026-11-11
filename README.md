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

## Filer appen bruker

- `snow/latest.json` – hvilken fil som hører til hvilken dato
- `snow/snow_<kjøring>_d<dag>.png` – ett bilde per dag

## Kjøre lokalt

```
pip install -r requirements.txt
python scripts/build_snow.py --out site
```

## Datakilde

NOAA/NCEP Global Forecast System (GFS). Dataene er offentlig eiendom.
