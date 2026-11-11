#!/usr/bin/env python3
"""Build world snow-cover images for the Snowmap app from NOAA GFS.

For today and the next seven days this downloads the GFS 0.25° snow depth
field (one small GRIB message per day, fetched by byte range from NOAA's
public archive), reprojects it to Web Mercator and writes a transparent PNG
where white means snow on the ground. A manifest (latest.json) tells the app
which file belongs to which date.

GFS data is produced by NOAA/NCEP and is in the public domain.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import requests
from PIL import Image

BUCKET = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
DAYS = 8                 # today + 7
IMAGE_SIZE = 2048        # pixels per side, Web Mercator square
MAX_LATITUDE = 85.05112878   # Web Mercator limit; matches MapKit's world rect
SESSION = requests.Session()
SESSION.headers["User-Agent"] = "snowmap-data (github.com/staylowmedia/snowmap-data)"


def grib_url(run: dt.datetime, hour: int) -> str:
    return (f"{BUCKET}/gfs.{run:%Y%m%d}/{run:%H}/atmos/"
            f"gfs.t{run:%H}z.pgrb2.0p25.f{hour:03d}")


def get(url: str, **kwargs) -> requests.Response:
    """GET with a few retries; NOAA's archive occasionally hiccups."""
    last: Exception | None = None
    for attempt in range(4):
        try:
            response = SESSION.get(url, timeout=60, **kwargs)
            if response.status_code in (200, 206, 404):
                return response
            last = RuntimeError(f"HTTP {response.status_code} for {url}")
        except requests.RequestException as error:
            last = error
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Could not fetch {url}: {last}")


def forecast_hours(run: dt.datetime, first_day: dt.date) -> list[int]:
    """Forecast hour closest to 12 UTC for each day, as offered by GFS."""
    hours = []
    for d in range(DAYS):
        target = dt.datetime.combine(first_day + dt.timedelta(days=d), dt.time(12), dt.timezone.utc)
        hour = max(0, round((target - run).total_seconds() / 3600))
        if hour > 120:                 # hourly up to 120 h, then every 3 h
            hour = 3 * round(hour / 3)
        hours.append(min(hour, 384))
    return hours


def latest_run(now: dt.datetime, first_day: dt.date) -> dt.datetime:
    """Newest GFS run whose files for all the days we need are published."""
    base = now.replace(minute=0, second=0, microsecond=0)
    base -= dt.timedelta(hours=base.hour % 6)
    for back in range(8):
        run = base - dt.timedelta(hours=6 * back)
        last_hour = forecast_hours(run, first_day)[-1]
        if get(grib_url(run, last_hour) + ".idx").status_code == 200:
            return run
    raise RuntimeError("No complete GFS run found in the last 48 hours")


def snow_depth_message(run: dt.datetime, hour: int) -> bytes:
    """Download only the surface snow depth (SNOD) message of one GFS file."""
    url = grib_url(run, hour)
    index = get(url + ".idx")
    if index.status_code != 200:
        raise RuntimeError(f"Missing index for {url}")
    lines = [line.split(":") for line in index.text.splitlines() if line.strip()]
    for i, parts in enumerate(lines):
        if len(parts) > 4 and parts[3] == "SNOD" and parts[4] == "surface":
            start = int(parts[1])
            end = int(lines[i + 1][1]) - 1 if i + 1 < len(lines) else None
            byte_range = f"bytes={start}-{end}" if end is not None else f"bytes={start}-"
            response = get(url, headers={"Range": byte_range})
            if response.status_code != 206:
                raise RuntimeError(f"Range request failed ({response.status_code}) for {url}")
            return response.content
    raise RuntimeError(f"No SNOD:surface record in {url}.idx")


def decode(message: bytes) -> np.ndarray:
    """Snow depth in metres as a (721, 1440) array: north→south, lon 0→360."""
    import eccodes

    gid = eccodes.codes_new_from_message(message)
    try:
        eccodes.codes_set(gid, "missingValue", -9999.0)
        ni = eccodes.codes_get(gid, "Ni")
        nj = eccodes.codes_get(gid, "Nj")
        values = eccodes.codes_get_values(gid).reshape(nj, ni)
        first_lat = eccodes.codes_get(gid, "latitudeOfFirstGridPointInDegrees")
        last_lat = eccodes.codes_get(gid, "latitudeOfLastGridPointInDegrees")
        first_lon = eccodes.codes_get(gid, "longitudeOfFirstGridPointInDegrees")
        name = eccodes.codes_get(gid, "shortName")
    finally:
        eccodes.codes_release(gid)
    if (nj, ni) != (721, 1440) or abs(first_lon) > 1e-6:
        raise RuntimeError(f"Unexpected grid {nj}x{ni}, first longitude {first_lon}")
    if first_lat < last_lat:           # stored south→north: flip to north→south
        values = values[::-1]
    values = np.where(values < -1000, 0.0, values)
    print(f"    decoded {name}: max {values.max():.2f} m, "
          f"{(values > 0.01).mean() * 100:.1f} % of grid points with snow")
    return values.astype(np.float32)


def to_mercator(depth: np.ndarray, size: int = IMAGE_SIZE) -> np.ndarray:
    """Resample the lat/lon grid to a Web Mercator square (bilinear)."""
    nj, ni = depth.shape
    step = 360.0 / ni
    # Pixel centres → longitude / latitude.
    lon = -180.0 + (np.arange(size) + 0.5) / size * 360.0
    y = (np.arange(size) + 0.5) / size
    lat = np.degrees(np.arctan(np.sinh(math.pi * (1 - 2 * y))))
    col = (lon % 360.0) / step
    row = (90.0 - lat) / step
    c0 = np.floor(col).astype(int)
    r0 = np.clip(np.floor(row).astype(int), 0, nj - 2)
    fc = (col - c0)[None, :]
    fr = (row - r0)[:, None]
    c0, c1 = c0 % ni, (c0 + 1) % ni            # wrap around the date line
    top = depth[r0][:, c0] * (1 - fc) + depth[r0][:, c1] * fc
    bottom = depth[r0 + 1][:, c0] * (1 - fc) + depth[r0 + 1][:, c1] * fc
    return top * (1 - fr) + bottom * fr


def to_image(depth_m: np.ndarray) -> Image.Image:
    """White where snow lies; opacity grows with depth, soft edge around 1 cm."""
    cm = depth_m * 100.0
    edge = np.clip((cm - 0.5) / 1.0, 0.0, 1.0)
    alpha = edge * (0.5 + np.clip(cm / 30.0, 0.0, 1.0) * 0.42)
    rgba = np.empty(depth_m.shape + (4,), dtype=np.uint8)
    rgba[..., :3] = 255
    rgba[..., 3] = np.round(alpha * 255).astype(np.uint8)
    return Image.fromarray(rgba, "RGBA")


PREVIEW = """<!doctype html>
<html lang="nb"><meta charset="utf-8"><title>Snowmap – snødata</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body{{margin:0;padding:24px;background:#0b1f2e;color:#eaf2f7;font:16px system-ui,sans-serif}}
 main{{max-width:1100px;margin:auto}} figure{{margin:0 0 24px}}
 img{{width:100%;background:#12324a;border-radius:12px;display:block}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:16px}}
 a{{color:#9fd0e8}} figcaption{{margin-top:6px;color:#9fd0e8}}
</style>
<main>
<h1>Snowmap – snødata</h1>
<p>Snødybde fra NOAA GFS 0,25°, kjøring {run} UTC. Laget {generated} UTC.
Hvitt betyr snø på bakken. <a href="snow/latest.json">latest.json</a></p>
<div class="grid">{figures}</div>
</main></html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="site", help="output folder")
    args = parser.parse_args()

    now = dt.datetime.now(dt.timezone.utc)
    first_day = now.date()
    run = latest_run(now, first_day)
    hours = forecast_hours(run, first_day)
    run_id = f"{run:%Y%m%d%H}"
    print(f"GFS run {run:%Y-%m-%d %H} UTC, forecast hours {hours}")

    out = Path(args.out)
    snow_dir = out / "snow"
    snow_dir.mkdir(parents=True, exist_ok=True)

    days = []
    for d, hour in enumerate(hours):
        date = first_day + dt.timedelta(days=d)
        print(f"  {date} (f{hour:03d})")
        depth = decode(snow_depth_message(run, hour))
        image = to_image(to_mercator(depth))
        name = f"snow_{run_id}_d{d}.png"
        image.save(snow_dir / name, optimize=True)
        print(f"    wrote {name}, {(snow_dir / name).stat().st_size / 1024:.0f} kB")
        days.append({"date": date.isoformat(), "file": name, "forecastHour": hour})

    manifest = {
        "generated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run": run_id,
        "source": "NOAA GFS 0.25° snow depth",
        "projection": "web-mercator",
        "maxLatitude": MAX_LATITUDE,
        "days": days,
    }
    (snow_dir / "latest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    figures = "".join(
        f'<figure><img src="snow/{day["file"]}" alt="Snø {day["date"]}">'
        f'<figcaption>{day["date"]}</figcaption></figure>' for day in days)
    (out / "index.html").write_text(PREVIEW.format(
        run=f"{run:%Y-%m-%d %H}", generated=f"{now:%Y-%m-%d %H:%M}", figures=figures))
    (out / ".nojekyll").write_text("")
    print(f"Done: {len(days)} days in {snow_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
