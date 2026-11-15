#!/usr/bin/env python3
"""Precompute short snow sentences for the Snowmap app.

The image build already has a global GFS snow-depth grid. This module turns
that grid into one short sentence per 5° cell per language and writes
``site/snow/ai_sentences.json`` next to ``latest.json``.

No place name and no user coordinate is sent anywhere. With no API key, or
when a call fails or the answer fails validation, the sentence is a
deterministic rule. A failure here must not stop the image publish.
"""
from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Mapping

# App languages, same codes as LanguageManager.languages in snowmap-242.
LANGUAGES = ("en", "nb", "sv", "de", "fr", "es", "it", "pt-BR", "ja", "zh-Hans")
CJK = frozenset({"ja", "zh-Hans"})

SCHEMA_VERSION = 1
RESOLUTION_DEG = 5.0
LAT_SOUTH = -90.0
LON_WEST = -180.0
N_LAT = 36
N_LON = 72
GFS_ROWS = 721
GFS_COLS = 1440
GFS_STEP = 0.25
SNOW_M = 0.01          # 1 cm; same soft edge the map images use
TREND_CM = 5.0
MAX_AGE_HOURS = 30
# Single default for app_config.live_ai_daily_cap. The workflow can override
# it with the SNOWMAP_LIVE_AI_DAILY_CAP variable; nothing else should hardcode it.
# Published values are clamped to 0..LIVE_AI_DAILY_CAP_MAX. Above the max becomes
# the max; below 0 or not an integer becomes the default.
LIVE_AI_DAILY_CAP = 5
LIVE_AI_DAILY_CAP_MAX = 20
PLACEHOLDER = "{place}"

BATCH_SIZE = 8
HARD_MAX_CALLS = 16
REQUEST_TIMEOUT_S = 12   # wall-clock seconds for one call, not an idle timeout
AI_BUDGET_S = 60          # wall-clock seconds for the whole AI step
# A full batch at the length cap is about 4,923 tokens. 6144 leaves headroom
# so that reply is not truncated and discarded.
MAX_TOKENS = 6144
MAX_RESPONSE_BYTES = 512_000

MAX_CHARS = 180
MAX_WORDS = 18
MIN_WORDS = 3
MAX_CJK_CHARS = 90
MIN_CJK_CHARS = 4
MIN_CHARS = 8

USER_AGENT = "snowmap-data (github.com/staylowmedia/snowmap-data)"
SAFE_SENTENCE = "Around {place} and the wider region, there is little or no snow."

_MODEL_RE = re.compile(r"[A-Za-z0-9._:/-]{1,80}")
# Underscore is excluded on purpose: \w would otherwise allow markdown _emphasis_.
# En dash is allowed so "2–4 cm" is not rejected. Underscore stays out.
_ALLOWED_FULL_RE = re.compile(r"(?:[^\W_]|[\s.,;:'’\"()%，、。！？\-\u2013])+$", re.UNICODE)
_TERMINATORS = set(".!?。！？")
_SCHEME_RE = re.compile(r"(?i)(?:javascript|data|vbscript|mailto|https?|file|tel)\s*:|://")
# ASCII-only edges. \w would treat CJK as a boundary and miss "雪snowdeals.com".
_NOT_ASCII_BEFORE = r"(?<![A-Za-z0-9-])"
_NOT_ASCII_AFTER = r"(?![A-Za-z0-9-])"
# Each label is at least two characters, so "t.ex" and "z.B" are not domains.
_DOMAIN_RE = re.compile(
    _NOT_ASCII_BEFORE
    + r"(?:[A-Za-z0-9][A-Za-z0-9-]{0,61}[A-Za-z0-9]\.)+[A-Za-z]{2,}"
    + _NOT_ASCII_AFTER
)
_DOT_WORD_RE = re.compile(
    _NOT_ASCII_BEFORE
    + r"[A-Za-z0-9]{2,}(?:-[A-Za-z0-9]+)*"
    + r"\s*(?:\(\s*)?(?:dot|d0t|punktum|punkt|point)(?:\s*\))?\s+"
    + r"([A-Za-z]{2,})"
    + _NOT_ASCII_AFTER,
    re.IGNORECASE,
)
_SPACED_DOT_RE = re.compile(
    _NOT_ASCII_BEFORE
    + r"[A-Za-z0-9][A-Za-z0-9-]{2,61}[A-Za-z0-9]\.\s+[A-Za-z]{2,}"
    + _NOT_ASCII_AFTER
)
_DOTCOM_RE = re.compile(_NOT_ASCII_BEFORE + r"[A-Za-z0-9]{2,}dotcom" + _NOT_ASCII_AFTER, re.IGNORECASE)
_DOT_TLD_SKIP = frozenset({"is", "the", "of", "to", "an", "or", "cm", "mm", "and"})
# Five digits in a row is a phone, not a snow depth we print. 55501 is rejected; 9999 cm is not.
_PHONE_RUN_RE = re.compile(r"\d{5,}")
_ABBREV_RE = re.compile(r"(?i)\b(?:ca|mr|mrs|dr|z\.b|t\.ex|e\.g|i\.e)\.")
_MARKDOWN_CHARS = set("_*~`#[]<>|")


@dataclass(frozen=True)
class CellStats:
    i: int
    j: int
    mean_cm: float
    max_cm: float
    fraction: float
    later_mean_cm: float
    n: int

    @property
    def id(self) -> str:
        return f"{self.i},{self.j}"


@dataclass(frozen=True)
class AIConfig:
    """Holds the key in memory for the request header only. ``repr`` hides it."""

    api_key: str
    base_url: str
    model: str
    max_calls: int
    enabled: bool
    reason: str

    def __repr__(self) -> str:
        return (
            f"AIConfig(key_set={bool(self.api_key)}, enabled={self.enabled}, "
            f"model={self.model!r}, max_calls={self.max_calls}, reason={self.reason!r})"
        )


def cell_index(lat: float, lon: float) -> tuple[int, int]:
    """Map a point to ``(i, j)`` on the published grid.

    ``i`` grows northwards from ``LAT_SOUTH``. ``j`` grows eastwards from
    ``LON_WEST``. Latitude 90 lands in the northernmost cell. Longitude 180
    and −180 both land in column 0.
    """
    if not math.isfinite(lat) or not math.isfinite(lon):
        raise ValueError("lat and lon must be finite")
    lat_c = min(90.0, max(-90.0, lat))
    lon_c = _norm_lon(lon)
    i = math.floor((lat_c - LAT_SOUTH) / RESOLUTION_DEG)
    j = math.floor((lon_c - LON_WEST) / RESOLUTION_DEG)
    if i == N_LAT:          # lat == 90 exactly
        i = N_LAT - 1
    if j == N_LON:          # should not happen after normalisation
        j = N_LON - 1
    i = min(max(int(i), 0), N_LAT - 1)
    j = min(max(int(j), 0), N_LON - 1)
    return i, j


def cell_offset(i: int, j: int) -> int:
    return i * N_LON + j


def _norm_lon(lon: float) -> float:
    """Longitude in ``[-180, 180)``. 180 maps to −180."""
    x = (lon + 180.0) % 360.0
    if x < 0:               # defensive; Python's % is already non-negative
        x += 360.0
    lon_c = x - 180.0
    if lon_c == 180.0:
        lon_c = -180.0
    return lon_c


def _lon360(lon: float) -> float:
    x = lon % 360.0
    return x + 360.0 if x < 0 else x


def row_span(i: int) -> tuple[int, int]:
    """Row slice into a 721-row grid, north at row 0, 0.25° steps.

    The southern edge of the cell is included. The northern edge belongs to
    the next cell, except the northernmost cell, which also includes 90°N.
    """
    if not 0 <= i < N_LAT:
        raise ValueError(f"row {i} outside the sentence grid")
    south = LAT_SOUTH + i * RESOLUTION_DEG
    r1 = int(round((90.0 - south) / GFS_STEP)) + 1
    if i == N_LAT - 1:
        r0 = 0
    else:
        north = south + RESOLUTION_DEG
        r0 = int(round((90.0 - north) / GFS_STEP)) + 1
    r1 = min(max(r1, 0), GFS_ROWS)
    r0 = min(max(r0, 0), r1)
    return r0, r1


def col_span(j: int) -> tuple[int, int]:
    """Column slice into a 1440-column grid, longitude 0 at column 0."""
    if not 0 <= j < N_LON:
        raise ValueError(f"column {j} outside the sentence grid")
    west = LON_WEST + j * RESOLUTION_DEG
    east = west + RESOLUTION_DEG
    c0 = int(math.floor(_lon360(west) / GFS_STEP + 1e-6))
    # Exclusive edges that normalise to 0° or 360° must stay at the end of
    # the previous range: 180° is column 720, and 0° approached from the west
    # is column 1440 rather than column 0.
    if abs(east - 180.0) < 1e-6:
        c1 = int(math.floor(180.0 / GFS_STEP + 1e-6))
    elif abs(east) < 1e-6:
        c1 = GFS_COLS
    else:
        c1 = int(math.floor(_lon360(east) / GFS_STEP + 1e-6))
    if c1 <= c0 or c1 > GFS_COLS:
        raise RuntimeError(f"bad column span {c0}:{c1} for cell column {j}")
    return c0, c1


def _clip0(values):
    if hasattr(values, "clip"):
        return values.clip(min=0)
    return values


def block_stats(block) -> tuple[float, float, float, int]:
    """Return mean metres, max metres, fraction deeper than 1 cm, and count."""
    if hasattr(block, "ravel"):
        values = _clip0(block.ravel())
        n = int(values.size)
        if n == 0:
            return 0.0, 0.0, 0.0, 0
        finite = values == values
        if not bool(finite.all()):
            values = values.copy()
            values[finite == False] = 0.0  # noqa: E712 — numpy mask
        mean = float(values.mean())
        maximum = float(values.max())
        fraction = float((values > SNOW_M).sum()) / n
        return mean, maximum, fraction, n
    total = 0.0
    maximum = 0.0
    snow = 0
    n = 0
    for row in block:
        for raw in row:
            value = float(raw)
            if value != value or value < 0:  # NaN or a fill below zero
                value = 0.0
            n += 1
            total += value
            if value > maximum:
                maximum = value
            if value > SNOW_M:
                snow += 1
    if n == 0:
        return 0.0, 0.0, 0.0, 0
    return total / n, maximum, snow / n, n


def summarize(today, later) -> list[CellStats]:
    """Summarise day-0 and the last forecast day onto the 5° grid."""
    if getattr(today, "shape", None) != (GFS_ROWS, GFS_COLS):
        raise ValueError(f"expected a {(GFS_ROWS, GFS_COLS)} snow grid")
    if getattr(later, "shape", None) != (GFS_ROWS, GFS_COLS):
        raise ValueError(f"expected a {(GFS_ROWS, GFS_COLS)} snow grid")
    cells: list[CellStats] = []
    for i in range(N_LAT):
        r0, r1 = row_span(i)
        for j in range(N_LON):
            c0, c1 = col_span(j)
            mean_m, max_m, fraction, n = block_stats(today[r0:r1, c0:c1])
            later_m, _, _, _ = block_stats(later[r0:r1, c0:c1])
            cells.append(CellStats(
                i=i, j=j,
                mean_cm=mean_m * 100.0,
                max_cm=max_m * 100.0,
                fraction=fraction,
                later_mean_cm=later_m * 100.0,
                n=n,
            ))
    return cells


def shown_cm(value: float) -> int:
    if not math.isfinite(value) or value <= 0:
        return 0
    return int(math.floor(value + 0.5))


def kind_of(cell: CellStats) -> str:
    """Rule bucket. Trend compares today with the last day of the forecast."""
    shown = shown_cm(cell.mean_cm)
    bare = cell.fraction < 0.02 and cell.max_cm < 1.0
    if bare:
        return "incoming" if cell.later_mean_cm >= TREND_CM else "none"
    solid = cell.fraction >= 0.50 and cell.mean_cm >= 10.0
    if not solid and shown <= 0:
        return "incoming" if cell.later_mean_cm >= TREND_CM else "dusting"
    delta = cell.later_mean_cm - cell.mean_cm
    trend = ""
    if delta >= TREND_CM:
        trend = "_up"
    elif delta <= -TREND_CM:
        trend = "_down"
    return ("solid" if solid else "patchy") + trend


def interest(cell: CellStats) -> float:
    """Higher means the rule is a poorer description, so the cell is worth a call.

    Empty cells and deep, unchanging snow stay on the rule. Mixed cover and
    a real change over the week go first. The order is deterministic.
    """
    change = abs(cell.later_mean_cm - cell.mean_cm)
    bare = cell.fraction < 0.02 and cell.max_cm < 1.0
    if bare and change < TREND_CM:
        return 0.0
    if cell.fraction >= 0.90 and cell.mean_cm >= 30.0 and change < TREND_CM:
        return 0.0
    mixed = max(0.0, 1.0 - abs(cell.fraction - 0.5) * 2.0)
    trend = min(change / 20.0, 1.5)
    return mixed + trend


def select_for_ai(cells: list[CellStats], max_cells: int) -> list[CellStats]:
    ranked = sorted(cells, key=lambda cell: (-interest(cell), cell.i, cell.j))
    return [cell for cell in ranked if interest(cell) > 0][:max(0, max_cells)]


def _terminator_count(text: str) -> int:
    """Sentence endings. Decimals and a few abbreviations are not endings."""
    stripped = re.sub(r"\d+\.\d+", "", text)
    stripped = _ABBREV_RE.sub("", stripped)
    return sum(stripped.count(ch) for ch in _TERMINATORS)


def _cjk_count(text: str) -> int:
    count = 0
    for ch in text:
        code = ord(ch)
        if 0x3040 <= code <= 0x30FF or 0x3400 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF:
            count += 1
    return count


def _has_confusable(text: str) -> bool:
    """Cyrillic, Greek, fullwidth, and math alphanumeric lookalikes."""
    for ch in text:
        code = ord(ch)
        if 0x0370 <= code <= 0x03FF or 0x0400 <= code <= 0x052F or 0x1F00 <= code <= 0x1FFF:
            return True
        # Fullwidth Latin letters and digits, not CJK punctuation such as "，".
        if 0xFF10 <= code <= 0xFF19 or 0xFF21 <= code <= 0xFF3A or 0xFF41 <= code <= 0xFF5A:
            return True
        if 0x1D400 <= code <= 0x1D7FF:
            return True
    return False


def _normalize_dots(text: str) -> str:
    return text.replace("。", ".").replace("．", ".").replace("｡", ".")


def _is_short_decimal(groups: list[str], separators: list[str]) -> bool:
    """``12.5`` and ``15.11``. Not ``555.0100``."""
    if separators != ["."]:
        return False
    if len(groups) != 2:
        return False
    whole, fraction = groups
    return 1 <= len(whole) <= 4 and 1 <= len(fraction) <= 2


def _is_thousands_group(groups: list[str], separators: list[str]) -> bool:
    """``1 234 567`` only. ``123 456 789`` and ``1 800 555 010`` are phones."""
    if any(separator != " " for separator in separators):
        return False
    if not 2 <= len(groups) <= 3:
        return False
    if not 1 <= len(groups[0]) <= 2:
        return False
    return all(len(group) == 3 for group in groups[1:])


def _looks_like_phone(body: str) -> bool:
    """Phone-like numbers. Depths, short decimals, and one thousands group stay."""
    if _PHONE_RUN_RE.search(body):
        return True
    if re.search(r"\(\d{2,}\)", body):
        return True
    if "+" in body and len(re.findall(r"\d", body)) >= 7:
        return True
    for match in re.finditer(r"\d+(?:[ .\-]\d+)+", body):
        chunk = match.group(0)
        groups = re.findall(r"\d+", chunk)
        separators = re.findall(r"[ .\-]", chunk)
        if _is_short_decimal(groups, separators) or _is_thousands_group(groups, separators):
            continue
        return True
    return False


def _unsafe_prose(body: str) -> bool:
    """Reject URLs, bare domains, phone numbers, schemes, and markdown.

    Domain and phone checks look at tokens, not a single substring such as
    ``http``. Ideographic dots and the word "dot" count as domain separators.
    """
    if any(ch in body for ch in _MARKDOWN_CHARS):
        return True
    if _has_confusable(body):
        return True
    if _SCHEME_RE.search(body):
        return True
    normalized = _normalize_dots(body)
    if _DOMAIN_RE.search(normalized) or _SPACED_DOT_RE.search(normalized) or _DOTCOM_RE.search(body):
        return True
    if any(match.group(1).lower() not in _DOT_TLD_SKIP for match in _DOT_WORD_RE.finditer(body)):
        return True
    if _looks_like_phone(body):
        return True
    return False


def _non_latin_dominates(text: str) -> bool:
    latin = 0
    other = 0
    for ch in text:
        if not ch.isalpha():
            continue
        code = ord(ch)
        # Latin, Latin-1 supplement, and Latin Extended-A/B cover nb/sv/de/fr/es/it/pt.
        if code <= 0x024F:
            latin += 1
        else:
            other += 1
    if latin + other == 0:
        return True
    return other > latin


def script_ok(text: str, lang: str, english: str | None) -> bool:
    """Language filter applied after the shared prose checks.

    Japanese and Chinese must contain CJK. A spaced language must not be mostly
    another script. A non-English line that copies the English line is rejected.
    """
    if lang != "en" and english is not None and text == english:
        return False
    if lang in CJK:
        return _cjk_count(text) >= MIN_CJK_CHARS
    return not _non_latin_dominates(text)


def validate_sentence(text: str, lang: str) -> str | None:
    """Return a cleaned sentence, or None when it must not be published."""
    if not isinstance(text, str):
        return None
    cleaned = " ".join(text.split())
    if not cleaned or len(cleaned) < MIN_CHARS or len(cleaned) > MAX_CHARS:
        return None
    if cleaned.count(PLACEHOLDER) != 1:
        return None
    body = cleaned.replace(PLACEHOLDER, " ")
    if "{" in body or "}" in body:
        return None
    if _unsafe_prose(body):
        return None
    if not _ALLOWED_FULL_RE.fullmatch(body.strip()):
        return None
    if _terminator_count(cleaned) > 1:
        return None
    if lang in CJK:
        if len(cleaned) > MAX_CJK_CHARS:
            return None
    else:
        words = cleaned.replace(PLACEHOLDER, "place").split()
        if len(words) < MIN_WORDS or len(words) > MAX_WORDS:
            return None
    return cleaned


def accept_sentence(text: str, lang: str, english: str | None) -> str | None:
    cleaned = validate_sentence(text, lang)
    if cleaned is None or not script_ok(cleaned, lang, english):
        return None
    return cleaned


# {n} is whole centimetres. incoming uses the later day.
# Every line names the wider region around {place}. A 5° cell is about 555 km
# across, so the sentence must not sound like a pinpoint forecast.
_RULES: dict[str, dict[str, str]] = {
    "none": {
        "en": "Around {place} and the wider region, there is little or no snow.",
        "nb": "I regionen rundt {place} er det lite eller ingen snø.",
        "sv": "I regionen runt {place} är det lite eller ingen snö.",
        "de": "In der weiteren Region um {place} liegt kaum oder kein Schnee.",
        "fr": "Dans la région autour de {place}, il y a peu ou pas de neige.",
        "es": "En la región alrededor de {place} hay poca o ninguna nieve.",
        "it": "Nella regione intorno a {place} c'è poca o nessuna neve.",
        "pt-BR": "Na região ao redor de {place} há pouca ou nenhuma neve.",
        "ja": "{place}周辺の広い地域では、雪はほとんどありません。",
        "zh-Hans": "在{place}周边的更大范围内，地面几乎没有积雪。",
    },
    "incoming": {
        "en": "Around {place} and the wider region, about {n} cm of snow is expected this week.",
        "nb": "I regionen rundt {place} ventes omtrent {n} cm snø i løpet av uken.",
        "sv": "I regionen runt {place} väntas ungefär {n} cm snö inom en vecka.",
        "de": "In der weiteren Region um {place} werden etwa {n} cm Schnee diese Woche erwartet.",
        "fr": "Dans la région autour de {place}, environ {n} cm de neige sont attendus cette semaine.",
        "es": "En la región alrededor de {place} se esperan unos {n} cm de nieve esta semana.",
        "it": "Nella regione intorno a {place} sono attesi circa {n} cm di neve questa settimana.",
        "pt-BR": "Na região ao redor de {place} esperam-se cerca de {n} cm de neve nesta semana.",
        "ja": "{place}周辺の広い地域では、1週間以内に約{n}cmの雪が見込まれます。",
        "zh-Hans": "在{place}周边的更大范围内，一周内预计约有{n}厘米积雪。",
    },
    "dusting": {
        "en": "Around {place} and the wider region, there is only a thin dusting of snow.",
        "nb": "I regionen rundt {place} er det bare et tynt dryss av snø.",
        "sv": "I regionen runt {place} finns bara ett tunt lager snö.",
        "de": "In der weiteren Region um {place} liegt nur eine dünne Schneeschicht.",
        "fr": "Dans la région autour de {place}, il n'y a qu'une fine couche de neige.",
        "es": "En la región alrededor de {place} solo hay una capa muy fina de nieve.",
        "it": "Nella regione intorno a {place} c'è solo un velo sottile di neve.",
        "pt-BR": "Na região ao redor de {place} há apenas uma camada fina de neve.",
        "ja": "{place}周辺の広い地域では、雪はごく薄い程度です。",
        "zh-Hans": "在{place}周边的更大范围内，只有很薄的一层雪。",
    },
    "patchy": {
        "en": "Around {place} and the wider region, snow is patchy, about {n} cm on average.",
        "nb": "I regionen rundt {place} er snøen flekkvis, omtrent {n} cm i snitt.",
        "sv": "I regionen runt {place} är snön fläckvis, ungefär {n} cm i snitt.",
        "de": "In der weiteren Region um {place} liegt der Schnee fleckenweise, etwa {n} cm.",
        "fr": "Dans la région autour de {place}, la neige est éparse, environ {n} cm en moyenne.",
        "es": "En la región alrededor de {place} la nieve es irregular, unos {n} cm de media.",
        "it": "Nella regione intorno a {place} la neve è a chiazze, circa {n} cm in media.",
        "pt-BR": "Na região ao redor de {place} a neve é irregular, cerca de {n} cm em média.",
        "ja": "{place}周辺の広い地域では、雪はまだらで平均約{n}cmです。",
        "zh-Hans": "在{place}周边的更大范围内，积雪不均匀，平均约{n}厘米。",
    },
    "patchy_up": {
        "en": "Around {place} and the wider region, patchy snow averages {n} cm, with more expected.",
        "nb": "I regionen rundt {place} er snøen flekkvis, omtrent {n} cm, og mer ventes.",
        "sv": "I regionen runt {place} är snön fläckvis, ungefär {n} cm, och mer väntas.",
        "de": "In der weiteren Region um {place} liegt fleckenweise etwa {n} cm Schnee, und es wird mehr.",
        "fr": "Dans la région autour de {place}, neige éparse d'environ {n} cm, et davantage est attendu.",
        "es": "En la región alrededor de {place} hay nieve irregular, unos {n} cm, y se espera más.",
        "it": "Nella regione intorno a {place} c'è neve a chiazze, circa {n} cm, e ne arriva altra.",
        "pt-BR": "Na região ao redor de {place} a neve irregular média é de {n} cm, com mais a caminho.",
        "ja": "{place}周辺の広い地域では、まだらの雪は平均約{n}cmで、さらに増える見込みです。",
        "zh-Hans": "在{place}周边的更大范围内，不均匀积雪平均约{n}厘米，预计还会增加。",
    },
    "patchy_down": {
        "en": "Around {place} and the wider region, patchy snow averages {n} cm and should thin.",
        "nb": "I regionen rundt {place} er snøen flekkvis, omtrent {n} cm, og den blir tynnere.",
        "sv": "I regionen runt {place} är snön fläckvis, ungefär {n} cm, och den tunnar ut.",
        "de": "In der weiteren Region um {place} liegt fleckenweise etwa {n} cm Schnee, und er wird dünner.",
        "fr": "Dans la région autour de {place}, neige éparse d'environ {n} cm, et elle devrait s'amincir.",
        "es": "En la región alrededor de {place} hay nieve irregular, unos {n} cm, y debería adelgazar.",
        "it": "Nella regione intorno a {place} la neve a chiazze è di circa {n} cm e dovrebbe assottigliarsi.",
        "pt-BR": "Na região ao redor de {place} a neve irregular média é de {n} cm e deve afinar.",
        "ja": "{place}周辺の広い地域では、まだらの雪は平均約{n}cmで、薄くなる見込みです。",
        "zh-Hans": "在{place}周边的更大范围内，不均匀积雪平均约{n}厘米，预计会变薄。",
    },
    "solid": {
        "en": "Around {place} and the wider region, snow cover is solid, about {n} cm.",
        "nb": "I regionen rundt {place} er snødekket solid, omtrent {n} cm i snitt.",
        "sv": "I regionen runt {place} är snötäcket rejält, ungefär {n} cm i snitt.",
        "de": "In der weiteren Region um {place} liegt eine geschlossene Decke von etwa {n} cm.",
        "fr": "Dans la région autour de {place}, le manteau neigeux est continu, environ {n} cm.",
        "es": "En la región alrededor de {place} la capa de nieve es continua, unos {n} cm.",
        "it": "Nella regione intorno a {place} il manto nevoso è continuo, circa {n} cm.",
        "pt-BR": "Na região ao redor de {place} a cobertura de neve é contínua, cerca de {n} cm.",
        "ja": "{place}周辺の広い地域では、積雪は厚く、平均約{n}cmです。",
        "zh-Hans": "在{place}周边的更大范围内，积雪较厚，平均约{n}厘米。",
    },
    "solid_up": {
        "en": "Around {place} and the wider region, solid snow averages {n} cm, with more expected.",
        "nb": "I regionen rundt {place} er snødekket solid, omtrent {n} cm, og mer ventes.",
        "sv": "I regionen runt {place} är snötäcket rejält, ungefär {n} cm, och mer väntas.",
        "de": "In der weiteren Region um {place} liegt etwa {n} cm Schnee, und es wird mehr.",
        "fr": "Dans la région autour de {place}, manteau continu d'environ {n} cm, et davantage est attendu.",
        "es": "En la región alrededor de {place} la nieve continua promedia {n} cm, y se espera más.",
        "it": "Nella regione intorno a {place} il manto continuo è di circa {n} cm, e se ne attende altra.",
        "pt-BR": "Na região ao redor de {place} a neve contínua média é de {n} cm, com mais a caminho.",
        "ja": "{place}周辺の広い地域では、厚い積雪は平均約{n}cmで、さらに増える見込みです。",
        "zh-Hans": "在{place}周边的更大范围内，较厚积雪平均约{n}厘米，预计还会增加。",
    },
    "solid_down": {
        "en": "Around {place} and the wider region, solid snow averages {n} cm but should thin.",
        "nb": "I regionen rundt {place} er snødekket solid, omtrent {n} cm, men det blir tynnere.",
        "sv": "I regionen runt {place} är snötäcket rejält, ungefär {n} cm, men det tunnar ut.",
        "de": "In der weiteren Region um {place} liegt etwa {n} cm Schnee, aber er wird dünner.",
        "fr": "Dans la région autour de {place}, manteau continu d'environ {n} cm, mais il devrait s'amincir.",
        "es": "En la región alrededor de {place} la nieve continua promedia {n} cm, pero debería adelgazar.",
        "it": "Nella regione intorno a {place} il manto continuo è di circa {n} cm, ma dovrebbe assottigliarsi.",
        "pt-BR": "Na região ao redor de {place} a neve contínua média é de {n} cm, mas deve afinar.",
        "ja": "{place}周辺の広い地域では、厚い積雪は平均約{n}cmですが、薄くなる見込みです。",
        "zh-Hans": "在{place}周边的更大范围内，较厚积雪平均约{n}厘米，但预计会变薄。",
    },
}


def _fill_rule(kind: str, lang: str, number: int) -> str | None:
    template = _RULES[kind][lang].replace("{n}", str(number))
    english = None if lang == "en" else _RULES[kind]["en"].replace("{n}", str(number))
    return accept_sentence(template, lang, english)


def rule_text(cell: CellStats, lang: str) -> str:
    kind = kind_of(cell)
    if kind == "incoming":
        number = shown_cm(cell.later_mean_cm)
    else:
        number = max(shown_cm(cell.mean_cm), 1 if "{n}" in _RULES[kind][lang] else 0)
    cleaned = _fill_rule(kind, lang, number)
    if cleaned is None:
        # The template itself is wrong. Tests fail on this; do not publish a
        # different depth or the English no-snow line.
        raise RuntimeError(f"rule text failed for {kind}/{lang}")
    return cleaned


def _rule_entry(cell: CellStats) -> dict:
    return {
        "text": {lang: rule_text(cell, lang) for lang in LANGUAGES},
        "source": {lang: "rule" for lang in LANGUAGES},
    }


def system_prompt() -> str:
    langs = ", ".join(LANGUAGES)
    return (
        "You write short public snow summaries for map cells. "
        "Reply with one JSON object only, no markdown and no analysis. "
        'Shape: {"cells":[{"id":"i,j","sentences":{'
        + ", ".join(f'"{lang}":"..."' for lang in LANGUAGES)
        + "}}]}. "
        f"Use exactly these language keys: {langs}. "
        "One factual sentence per language about snow on the ground in the wider region, "
        "not at a single point. A cell is hundreds of kilometres across. "
        f"At most {MAX_WORDS} words in languages that use spaces, "
        f"and at most {MAX_CJK_CHARS} characters in ja and zh-Hans. "
        "No emoji, URLs, domains, phone numbers, HTML, markdown, city names, or people. "
        "Do not invent temperature, rain, wind, or history. "
        f"Include the token {PLACEHOLDER} exactly once, inside wording such as "
        "'Around {place} and the wider region' or the equivalent in that language. "
        "Depths are centimetres, averaged over the whole cell. "
        "snow_cover_pct is the share of the cell deeper than 1 cm. "
        "in_7_days_mean_cm is the average at the end of the forecast week."
    )


def describe_cell(cell: CellStats) -> str:
    cover = int(math.floor(cell.fraction * 100 + 0.5))
    return (
        f"id={cell.id} snow_cover_pct={cover} "
        f"mean_cm={shown_cm(cell.mean_cm)} max_cm={shown_cm(cell.max_cm)} "
        f"in_7_days_mean_cm={shown_cm(cell.later_mean_cm)}"
    )


def messages_for(batch: list[CellStats]) -> list[dict[str, str]]:
    lines = "\n".join(describe_cell(cell) for cell in batch)
    return [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": "Cells:\n" + lines},
    ]


def _valid_model(model: str) -> bool:
    return bool(_MODEL_RE.fullmatch(model))


def _local_http_allowed() -> bool:
    """Test-only. Production stays https so the key is not sent in clear."""
    return os.environ.get("SNOWMAP_AI_ALLOW_HTTP_LOCALHOST", "").strip() == "1"


def _valid_base(url: str) -> bool:
    if not url or len(url) > 200:
        return False
    parts = urllib.parse.urlsplit(url)
    if parts.username or parts.password or parts.query or parts.fragment or not parts.hostname:
        return False
    if parts.scheme == "https":
        return True
    return (
        _local_http_allowed()
        and parts.scheme == "http"
        and parts.hostname in {"127.0.0.1", "localhost"}
    )


def chat_url(base: str) -> str:
    trimmed = base.rstrip("/")
    if trimmed.endswith("/chat/completions"):
        return trimmed
    return trimmed + "/chat/completions"


def clamp_live_ai_daily_cap(value: object) -> int:
    """Value written to ``app_config.live_ai_daily_cap``. Always in 0..20.

    Below 0, or anything that is not an integer, becomes the default (5).
    Above 20 becomes 20.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return LIVE_AI_DAILY_CAP
    if value > LIVE_AI_DAILY_CAP_MAX:
        return LIVE_AI_DAILY_CAP_MAX
    return value


def live_ai_daily_cap(env: Mapping[str, str] | None = None) -> int:
    """Cap the app should apply to explicit live AI calls on one device per day.

    A blank, negative, or non-integer value publishes the default (5).
    A value above 20 publishes 20.
    """
    source = os.environ if env is None else env
    raw = source.get("SNOWMAP_LIVE_AI_DAILY_CAP", "").strip()
    if not raw:
        return LIVE_AI_DAILY_CAP
    try:
        value = int(raw)
    except ValueError:
        print("  sentences: SNOWMAP_LIVE_AI_DAILY_CAP ignored; using the default")
        return LIVE_AI_DAILY_CAP
    if value < 0:
        print("  sentences: SNOWMAP_LIVE_AI_DAILY_CAP ignored; using the default")
        return LIVE_AI_DAILY_CAP
    if value > LIVE_AI_DAILY_CAP_MAX:
        print(f"  sentences: SNOWMAP_LIVE_AI_DAILY_CAP above {LIVE_AI_DAILY_CAP_MAX}; publishing {LIVE_AI_DAILY_CAP_MAX}")
        return LIVE_AI_DAILY_CAP_MAX
    return value


def load_config(env: Mapping[str, str] | None = None) -> AIConfig:
    """AI is on only when the key, base URL, and model are all set.

    There is no default provider. A key on its own does not call anywhere.
    """
    source = os.environ if env is None else env
    key = source.get("SNOWMAP_AI_API_KEY", "").strip()
    base = source.get("SNOWMAP_AI_BASE_URL", "").strip()
    model = source.get("SNOWMAP_AI_MODEL", "").strip()
    raw_calls = source.get("SNOWMAP_AI_MAX_CALLS", "").strip()
    max_calls = HARD_MAX_CALLS
    if raw_calls:
        try:
            max_calls = int(raw_calls)
        except ValueError:
            return AIConfig(key, base, model, 0, False, "SNOWMAP_AI_MAX_CALLS is not an integer")
    max_calls = max(0, min(HARD_MAX_CALLS, max_calls))
    if not key:
        return AIConfig("", base, model, max_calls, False, "no API key")
    if not base or not model:
        return AIConfig(key, base, model, max_calls, False, "base URL or model is unset")
    if not _valid_base(base):
        return AIConfig(key, "", model, max_calls, False, "base URL was rejected")
    if not _valid_model(model):
        return AIConfig(key, base, "", max_calls, False, "model name was rejected")
    if max_calls == 0:
        return AIConfig(key, base, model, 0, False, "call cap is 0")
    return AIConfig(key, base, model, max_calls, True, "ok")


def _close_socket(sock) -> None:
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def _abort_connection(conn: http.client.HTTPConnection, response=None, sockets=()) -> None:
    """Close every socket the call is using so a slow drip cannot outlive the deadline.

    ``getresponse`` may hand the socket to the response object. Closing only
    ``conn.sock`` then does nothing, and the read keeps going.
    """
    for sock in sockets:
        _close_socket(sock)
    _close_socket(getattr(conn, "sock", None))
    if response is not None:
        try:
            response.close()
        except Exception:
            pass
        _close_socket(getattr(getattr(response, "fp", None), "raw", None))
    try:
        conn.close()
    except OSError:
        pass


def post_chat(url: str, key: str, model: str, messages: list[dict[str, str]], timeout: float) -> dict:
    """POST one chat completion. ``timeout`` is a wall-clock limit for the whole call.

    Redirects are not followed. The key is sent only in the Authorization header.
    """
    if timeout <= 0:
        raise TimeoutError("wall-clock budget exhausted")
    parts = urllib.parse.urlsplit(url)
    if parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError("URL must not carry a query, fragment, or userinfo")
    path = parts.path or "/"
    if parts.scheme == "https":
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            parts.hostname, parts.port or 443, timeout=timeout)
    elif (
        _local_http_allowed()
        and parts.scheme == "http"
        and parts.hostname in {"127.0.0.1", "localhost"}
    ):
        conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)
    else:
        raise ValueError("only https URLs are allowed")
    body = json.dumps({
        "model": model,
        "temperature": 0.2,
        "max_tokens": MAX_TOKENS,
        "messages": messages,
    }).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {key}",
        "User-Agent": USER_AGENT,
        "Content-Length": str(len(body)),
    }
    fired = False
    sockets: list = []
    response_box: dict = {}

    def _on_deadline() -> None:
        nonlocal fired
        fired = True
        _abort_connection(conn, response_box.get("response"), sockets)

    timer = threading.Timer(timeout, _on_deadline)
    timer.daemon = True
    timer.start()
    try:
        conn.connect()
        if conn.sock is not None:
            sockets.append(conn.sock)
        if fired:
            raise TimeoutError("wall-clock timeout")
        conn.request("POST", path, body=body, headers=headers)
        response = conn.getresponse()
        response_box["response"] = response
        if conn.sock is not None and conn.sock not in sockets:
            sockets.append(conn.sock)
        status = response.status
        if 300 <= status < 400:
            raise urllib.error.HTTPError(url, status, "redirect blocked", response.msg, None)
        if status != 200:
            raise urllib.error.HTTPError(url, status, "unexpected status", response.msg, None)
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    except Exception as error:
        if fired:
            raise TimeoutError("wall-clock timeout") from error
        raise
    finally:
        timer.cancel()
        conn.close()
    if fired:
        raise TimeoutError("wall-clock timeout")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("response too large")
    return json.loads(raw.decode("utf-8"))


def _message_text(payload: dict) -> tuple[str | None, str | None]:
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None, None
    choice = choices[0]
    finish = choice.get("finish_reason")
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    content = message.get("content")
    if not isinstance(content, str):
        return None, finish if isinstance(finish, str) else None
    return content, finish if isinstance(finish, str) else None


def parse_sentences(content: str) -> dict[str, dict]:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    rows = data.get("cells") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return {}
    found: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        cell_id = row.get("id")
        sentences = row.get("sentences")
        if isinstance(cell_id, str) and isinstance(sentences, dict) and cell_id not in found:
            found[cell_id] = sentences
    return found


def apply_ai(
    chosen: list[CellStats],
    config: AIConfig,
    transport: Callable[..., dict],
    deadline: float,
) -> tuple[dict[tuple[int, int], dict[str, str]], int]:
    """Return accepted sentences and the number of calls started."""
    accepted: dict[tuple[int, int], dict[str, str]] = {}
    if not config.enabled:
        return accepted, 0
    calls = 0
    url = chat_url(config.base_url)
    batches = [chosen[i:i + BATCH_SIZE] for i in range(0, len(chosen), BATCH_SIZE)]
    for batch in batches:
        if calls >= config.max_calls:
            break
        remaining = deadline - time.monotonic()
        if remaining < 1:
            print("  sentences: AI budget reached; remaining cells stay on rules")
            break
        calls += 1
        wanted = {cell.id: cell for cell in batch}
        call_timeout = min(float(REQUEST_TIMEOUT_S), remaining)
        try:
            payload = transport(
                url, config.api_key, config.model, messages_for(batch), call_timeout)
            content, finish = _message_text(payload) if isinstance(payload, dict) else (None, None)
            if content is None or (finish or "").lower() == "length":
                print("  sentences: AI reply was empty or cut off; batch stays on rules")
                continue
            parsed = parse_sentences(content)
        except urllib.error.HTTPError as error:
            # Status only. The body and the exception text can echo the request.
            print(f"  sentences: AI call failed (HTTP {error.code})")
            continue
        except Exception as error:  # noqa: BLE001 — one bad call must not drop earlier lines
            print(f"  sentences: AI call failed ({type(error).__name__})")
            continue
        for cell_id in wanted:
            raw = parsed.get(cell_id)
            if not isinstance(raw, dict):
                continue
            cleaned: dict[str, str] = {}
            for lang in LANGUAGES:
                if lang not in raw:
                    continue
                line = validate_sentence(raw.get(lang), lang)
                if line is not None:
                    cleaned[lang] = line
            english = cleaned.get("en")
            kept = {
                lang: line
                for lang, line in cleaned.items()
                if script_ok(line, lang, None if lang == "en" else english)
            }
            if kept:
                accepted[(wanted[cell_id].i, wanted[cell_id].j)] = kept
    return accepted, calls


def build_document(
    cells: list[CellStats],
    *,
    generated: str,
    run: str,
    first_date: str,
    last_date: str,
    model: str,
    ai_calls: int,
    ai_text: Mapping[tuple[int, int], Mapping[str, str]] | None = None,
    live_ai_daily_cap: int = LIVE_AI_DAILY_CAP,
) -> dict:
    if len(cells) != N_LAT * N_LON:
        raise ValueError("sentence grid does not cover every cell")
    ai_text = ai_text or {}
    rows = []
    for cell in cells:
        entry = _rule_entry(cell)
        extra = ai_text.get((cell.i, cell.j), {})
        for lang, text in extra.items():
            if lang in entry["text"]:
                entry["text"][lang] = text
                entry["source"][lang] = "ai"
        rows.append(entry)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated": generated,
        "run": run,
        "model": model,
        "languages": list(LANGUAGES),
        "placeholder": PLACEHOLDER,
        "max_age_hours": MAX_AGE_HOURS,
        "app_config": {"live_ai_daily_cap": clamp_live_ai_daily_cap(live_ai_daily_cap)},
        "window": {"first_date": first_date, "last_date": last_date},
        "grid": {
            "resolution_deg": RESOLUTION_DEG,
            "lat_south": LAT_SOUTH,
            "lon_west": LON_WEST,
            "n_lat": N_LAT,
            "n_lon": N_LON,
            "index": "i * n_lon + j",
        },
        "ai_calls": ai_calls,
        "cells": rows,
    }


def write_rule_file(
    path: Path,
    today,
    later,
    *,
    generated: str,
    run: str,
    first_date: str,
    last_date: str,
    env: Mapping[str, str] | None = None,
    inputs_path: Path | None = None,
) -> dict:
    """Write rule sentences only. This never calls a model and never reads the API key."""
    cells = summarize(today, later)
    daily_cap = live_ai_daily_cap(env)
    document = build_document(
        cells,
        generated=generated,
        run=run,
        first_date=first_date,
        last_date=last_date,
        model="rule-based",
        ai_calls=0,
        live_ai_daily_cap=daily_cap,
    )
    _atomic_write(path, document)
    if inputs_path is not None:
        _atomic_write(inputs_path, {
            "generated": generated,
            "run": run,
            "first_date": first_date,
            "last_date": last_date,
            "live_ai_daily_cap": daily_cap,
            "cells": [asdict(cell) for cell in cells],
        })
    print(f"  sentences: wrote {path.name}, model=rule-based")
    return document


def upgrade_file(
    sentence_path: Path,
    inputs_path: Path,
    *,
    env: Mapping[str, str] | None = None,
    transport: Callable[..., dict] | None = None,
) -> int:
    """Fill AI lines into an existing rule file. Leave that file untouched on failure.

    The wall-clock budget is ``AI_BUDGET_S`` inside this process. The workflow
    also kills the step on its own short timeout, after the rule file exists.
    """
    if not sentence_path.is_file() or not inputs_path.is_file():
        print("  sentences: rule file or inputs missing; nothing to upgrade")
        return 0
    try:
        inputs = json.loads(inputs_path.read_text(encoding="utf-8"))
        cells = [CellStats(**row) for row in inputs["cells"]]
        config = load_config(env)
        if not config.enabled:
            print(f"  sentences: rule-based ({config.reason})")
            return 0
        host = urllib.parse.urlsplit(chat_url(config.base_url)).hostname
        print(f"  sentences: model={config.model} host={host} max_calls={config.max_calls}")
        chosen = select_for_ai(cells, config.max_calls * BATCH_SIZE)
        # apply_ai catches per call, including a nested JSON RecursionError,
        # and returns lines already accepted plus the real call count.
        ai_text, calls = apply_ai(
            chosen, config, transport or post_chat, time.monotonic() + AI_BUDGET_S)
        model = config.model if any(ai_text.values()) else "rule-based"
        document = build_document(
            cells,
            generated=inputs["generated"],
            run=inputs["run"],
            first_date=inputs["first_date"],
            last_date=inputs["last_date"],
            model=model,
            ai_calls=calls,
            ai_text=ai_text,
            live_ai_daily_cap=int(inputs["live_ai_daily_cap"]),
        )
        _atomic_write(sentence_path, document)
        ai_rows = sum(1 for row in document["cells"] if "ai" in row["source"].values())
        print(f"  sentences: upgraded {sentence_path.name}, {ai_rows} cells with an AI line, {calls} calls, model={model}")
    except Exception as error:  # noqa: BLE001 — the rule file from the image step stays
        print(f"  sentences: upgrade failed ({type(error).__name__}); rule file kept")
    return 0


def write_sentences(
    path: Path,
    today,
    later,
    *,
    generated: str,
    run: str,
    first_date: str,
    last_date: str,
    env: Mapping[str, str] | None = None,
    transport: Callable[..., dict] | None = None,
) -> dict:
    """Write rules, then upgrade in-process. Tests use this. The workflow does not."""
    document = write_rule_file(
        path, today, later,
        generated=generated, run=run, first_date=first_date, last_date=last_date, env=env,
    )
    if not load_config(env).enabled:
        return document
    inputs = path.with_name(path.name + ".inputs.json")
    try:
        write_rule_file(
            path, today, later,
            generated=generated, run=run, first_date=first_date, last_date=last_date,
            env=env, inputs_path=inputs,
        )
        upgrade_file(path, inputs, env=env, transport=transport)
        return json.loads(path.read_text(encoding="utf-8"))
    finally:
        inputs.unlink(missing_ok=True)


def _temp_path(path: Path) -> Path:
    """Temp file outside ``site/``, which is what GitHub Pages publishes."""
    if "site" in path.parts:
        directory = Path("build")
        directory.mkdir(parents=True, exist_ok=True)
        return directory / (path.name + ".tmp")
    return path.with_name(path.name + ".tmp")


def _atomic_write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temp_path(path)
    try:
        temporary.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sentences", default="site/snow/ai_sentences.json")
    parser.add_argument("--inputs", default="build/sentence_inputs.json")
    args = parser.parse_args(argv)
    return upgrade_file(Path(args.sentences), Path(args.inputs))


if __name__ == "__main__":
    sys.exit(main())
