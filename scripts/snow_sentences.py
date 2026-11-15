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

import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
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
LIVE_AI_DAILY_CAP = 5
PLACEHOLDER = "{place}"

BATCH_SIZE = 8
HARD_MAX_CALLS = 16
REQUEST_TIMEOUT_S = 12
AI_BUDGET_S = 60
MAX_TOKENS = 2400
MAX_RESPONSE_BYTES = 512_000

MAX_CHARS = 180
MAX_WORDS = 18
MIN_WORDS = 3
MAX_CJK_CHARS = 90
MIN_CHARS = 8

USER_AGENT = "snowmap-data (github.com/staylowmedia/snowmap-data)"
SAFE_SENTENCE = "Little or no snow on the ground in this area."

_MODEL_RE = re.compile(r"[A-Za-z0-9._:/-]{1,80}")
# Letters (any language), digits, spaces, and a small punctuation set.
# {place} is checked separately and removed before this test.
_ALLOWED_RE = re.compile(r"[\w\s.,;:'’\"()%，、。！？\-]+$", re.UNICODE)
_TERMINATORS = set(".!?。！？")


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
    return sum(text.count(ch) for ch in _TERMINATORS)


def validate_sentence(text: str, lang: str) -> str | None:
    """Return a cleaned sentence, or None when it must not be published."""
    if not isinstance(text, str):
        return None
    cleaned = " ".join(text.split())
    if not cleaned or len(cleaned) < MIN_CHARS or len(cleaned) > MAX_CHARS:
        return None
    if cleaned.count(PLACEHOLDER) > 1:
        return None
    body = cleaned.replace(PLACEHOLDER, " ")
    if "{" in body or "}" in body:
        return None
    lowered = body.lower()
    if "http://" in lowered or "https://" in lowered or "www." in lowered or "://" in lowered:
        return None
    if not _ALLOWED_RE.fullmatch(body.strip()):
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


# {n} is the relevant depth in whole centimetres. incoming uses the later day.
_RULES: dict[str, dict[str, str]] = {
    "none": {
        "en": "Little or no snow on the ground in this area.",
        "nb": "Lite eller ingen snø på bakken i dette området.",
        "sv": "Lite eller ingen snö på marken i det här området.",
        "de": "Wenig oder kein Schnee auf dem Boden in diesem Gebiet.",
        "fr": "Peu ou pas de neige au sol dans cette zone.",
        "es": "Poca o ninguna nieve en el suelo en esta zona.",
        "it": "Poca o nessuna neve al suolo in questa zona.",
        "pt-BR": "Pouca ou nenhuma neve no chão nesta área.",
        "ja": "この地域の地面に雪はほとんどありません。",
        "zh-Hans": "这一带地面几乎没有积雪。",
    },
    "incoming": {
        "en": "Little snow here now, with about {n} cm expected within a week.",
        "nb": "Lite snø her nå, men omtrent {n} cm er ventet i løpet av uken.",
        "sv": "Lite snö här nu, men ungefär {n} cm väntas inom en vecka.",
        "de": "Hier liegt jetzt kaum Schnee, etwa {n} cm binnen einer Woche.",
        "fr": "Peu de neige ici maintenant, environ {n} cm d'ici une semaine.",
        "es": "Hay poca nieve ahora, y se esperan unos {n} cm en una semana.",
        "it": "Poca neve ora, ma circa {n} cm entro una settimana.",
        "pt-BR": "Pouca neve agora, com cerca de {n} cm em uma semana.",
        "ja": "今はほとんど雪がなく、1週間以内に約{n}cmの見込みです。",
        "zh-Hans": "目前几乎没有积雪，一周内预计约{n}厘米。",
    },
    "dusting": {
        "en": "Only a thin dusting of snow in this area.",
        "nb": "Bare et tynt dryss av snø i dette området.",
        "sv": "Bara ett tunt lager snö i det här området.",
        "de": "Nur eine dünne Schneeschicht in diesem Gebiet.",
        "fr": "Seulement une fine couche de neige dans cette zone.",
        "es": "Solo una capa muy fina de nieve en esta zona.",
        "it": "Solo un velo sottile di neve in questa zona.",
        "pt-BR": "Apenas uma camada fina de neve nesta área.",
        "ja": "この地域の雪はごく薄い程度です。",
        "zh-Hans": "这一带只有很薄的一层雪。",
    },
    "patchy": {
        "en": "Patchy snow in this area, about {n} cm on average.",
        "nb": "Flekkvis snø i dette området, omtrent {n} cm i snitt.",
        "sv": "Fläckvis snö i det här området, ungefär {n} cm i snitt.",
        "de": "Fleckenweise Schnee in diesem Gebiet, etwa {n} cm im Mittel.",
        "fr": "Neige éparse dans cette zone, environ {n} cm en moyenne.",
        "es": "Nieve irregular en esta zona, unos {n} cm de media.",
        "it": "Neve a chiazze in questa zona, circa {n} cm in media.",
        "pt-BR": "Neve irregular nesta área, cerca de {n} cm em média.",
        "ja": "この地域の雪はまだらで、平均約{n}cmです。",
        "zh-Hans": "这一带积雪不均匀，平均约{n}厘米。",
    },
    "patchy_up": {
        "en": "Patchy snow in this area, about {n} cm, with more expected.",
        "nb": "Flekkvis snø i dette området, omtrent {n} cm, og mer er ventet.",
        "sv": "Fläckvis snö i det här området, ungefär {n} cm, och mer väntas.",
        "de": "Fleckenweise Schnee hier, etwa {n} cm, und es wird mehr.",
        "fr": "Neige éparse dans cette zone, environ {n} cm, et davantage vient.",
        "es": "Nieve irregular en esta zona, unos {n} cm, y se espera más.",
        "it": "Neve a chiazze qui, circa {n} cm, e se ne attende altra.",
        "pt-BR": "Neve irregular nesta área, cerca de {n} cm, com mais a caminho.",
        "ja": "雪はまだらで平均約{n}cm、さらに増える見込みです。",
        "zh-Hans": "这一带积雪不均匀，平均约{n}厘米，预计还会增加。",
    },
    "patchy_down": {
        "en": "Patchy snow in this area, about {n} cm, and it should thin.",
        "nb": "Flekkvis snø i dette området, omtrent {n} cm, og det blir tynnere.",
        "sv": "Fläckvis snö i det här området, ungefär {n} cm, och det tunnar ut.",
        "de": "Fleckenweise Schnee hier, etwa {n} cm, und er wird dünner.",
        "fr": "Neige éparse dans cette zone, environ {n} cm, et elle s'amincit.",
        "es": "Nieve irregular en esta zona, unos {n} cm, y debería adelgazar.",
        "it": "Neve a chiazze qui, circa {n} cm, e dovrebbe assottigliarsi.",
        "pt-BR": "Neve irregular nesta área, cerca de {n} cm, e deve afinar.",
        "ja": "雪はまだらで平均約{n}cm、薄くなる見込みです。",
        "zh-Hans": "这一带积雪不均匀，平均约{n}厘米，预计会变薄。",
    },
    "solid": {
        "en": "Solid snow cover in this area, about {n} cm on average.",
        "nb": "Solid snødekke i dette området, omtrent {n} cm i snitt.",
        "sv": "Rejält snötäcke i det här området, ungefär {n} cm i snitt.",
        "de": "Geschlossene Schneedecke in diesem Gebiet, etwa {n} cm im Mittel.",
        "fr": "Manteau neigeux continu dans cette zone, environ {n} cm en moyenne.",
        "es": "Capa de nieve continua en esta zona, unos {n} cm de media.",
        "it": "Manto nevoso continuo in questa zona, circa {n} cm in media.",
        "pt-BR": "Cobertura de neve contínua nesta área, cerca de {n} cm em média.",
        "ja": "この地域は積雪が厚く、平均約{n}cmです。",
        "zh-Hans": "这一带积雪较厚，平均约{n}厘米。",
    },
    "solid_up": {
        "en": "Solid snow cover in this area, about {n} cm, with more expected.",
        "nb": "Solid snødekke i dette området, omtrent {n} cm, og mer er ventet.",
        "sv": "Rejält snötäcke i det här området, ungefär {n} cm, och mer väntas.",
        "de": "Geschlossene Schneedecke hier, etwa {n} cm, und es wird mehr.",
        "fr": "Manteau neigeux continu ici, environ {n} cm, et davantage vient.",
        "es": "Capa de nieve continua aquí, unos {n} cm, y se espera más.",
        "it": "Manto nevoso continuo qui, circa {n} cm, e se ne attende altra.",
        "pt-BR": "Cobertura contínua nesta área, cerca de {n} cm, com mais a caminho.",
        "ja": "積雪は厚く平均約{n}cmで、さらに増える見込みです。",
        "zh-Hans": "这一带平均积雪约{n}厘米，预计还会增加。",
    },
    "solid_down": {
        "en": "Solid snow cover in this area, about {n} cm, but it should thin.",
        "nb": "Solid snødekke i dette området, omtrent {n} cm, men det blir tynnere.",
        "sv": "Rejält snötäcke i det här området, ungefär {n} cm, men det tunnar ut.",
        "de": "Geschlossene Schneedecke hier, etwa {n} cm, aber sie wird dünner.",
        "fr": "Manteau neigeux continu ici, environ {n} cm, mais il s'amincit.",
        "es": "Capa de nieve continua aquí, unos {n} cm, pero debería adelgazar.",
        "it": "Manto nevoso continuo qui, circa {n} cm, ma si assottiglia.",
        "pt-BR": "Cobertura contínua nesta área, cerca de {n} cm, mas deve afinar.",
        "ja": "積雪は厚く平均約{n}cmですが、薄くなる見込みです。",
        "zh-Hans": "这一带平均积雪约{n}厘米，但预计会变薄。",
    },
}


def rule_text(cell: CellStats, lang: str) -> str:
    kind = kind_of(cell)
    if kind == "incoming":
        number = shown_cm(cell.later_mean_cm)
    else:
        number = max(shown_cm(cell.mean_cm), 1 if "{n}" in _RULES[kind][lang] else 0)
    template = _RULES[kind][lang]
    text = template.replace("{n}", str(number))
    cleaned = validate_sentence(text, lang)
    if cleaned is None:
        fallback = validate_sentence(SAFE_SENTENCE, "en")
        if fallback is None:
            raise RuntimeError("safe sentence failed validation")
        return fallback
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
        "One factual sentence per language about snow on the ground. "
        f"At most {MAX_WORDS} words in languages that use spaces, "
        f"and at most 40 characters in ja and zh-Hans. "
        "No emoji, URLs, HTML, markdown, city names, country names, or people. "
        "Do not invent temperature, rain, wind, or history. "
        f"You may include the token {PLACEHOLDER} at most once. "
        "The sentence must stay factual if that token is deleted. "
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


def _valid_base(url: str) -> bool:
    if not url or len(url) > 200 or not url.startswith("https://"):
        return False
    parts = urllib.parse.urlsplit(url)
    if parts.username or parts.password or parts.query or parts.fragment:
        return False
    if parts.scheme != "https" or not parts.hostname:
        return False
    return True


def chat_url(base: str) -> str:
    trimmed = base.rstrip("/")
    if trimmed.endswith("/chat/completions"):
        return trimmed
    return trimmed + "/chat/completions"


def live_ai_daily_cap(env: Mapping[str, str] | None = None) -> int:
    """Cap the app should apply to explicit live AI calls on one device per day.

    A blank value keeps the default. A value that is not an integer ≥ 0 also
    keeps the default, so a typo still publishes a file the app can trust.
    """
    source = os.environ if env is None else env
    raw = source.get("SNOWMAP_LIVE_AI_DAILY_CAP", "").strip()
    if not raw:
        return LIVE_AI_DAILY_CAP
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value < 0:
        print("  sentences: SNOWMAP_LIVE_AI_DAILY_CAP ignored; using the default")
        return LIVE_AI_DAILY_CAP
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect blocked", headers, fp)


def post_chat(url: str, key: str, model: str, messages: list[dict[str, str]], timeout: float) -> dict:
    payload = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": MAX_TOKENS,
        "messages": messages,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {key}",
            "User-Agent": USER_AGENT,
        },
    )
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(request, timeout=timeout) as response:
        if getattr(response, "status", 200) != 200:
            raise urllib.error.HTTPError(
                url, response.status, "unexpected status", response.headers, response)
        raw = response.read(MAX_RESPONSE_BYTES + 1)
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
        if calls >= config.max_calls or time.monotonic() >= deadline:
            break
        calls += 1
        wanted = {cell.id: cell for cell in batch}
        try:
            payload = transport(
                url, config.api_key, config.model, messages_for(batch), REQUEST_TIMEOUT_S)
        except urllib.error.HTTPError as error:
            print(f"  sentences: AI call failed (HTTP {error.code})")
            continue
        except Exception as error:  # noqa: BLE001 — one bad call must not stop the build
            print(f"  sentences: AI call failed ({type(error).__name__})")
            continue
        content, finish = _message_text(payload) if isinstance(payload, dict) else (None, None)
        if content is None or (finish or "").lower() == "length":
            print("  sentences: AI reply was empty or cut off; batch stays on rules")
            continue
        parsed = parse_sentences(content)
        for cell_id, cell in wanted.items():
            raw = parsed.get(cell_id)
            if not isinstance(raw, dict):
                continue
            kept: dict[str, str] = {}
            for lang in LANGUAGES:
                cleaned = validate_sentence(raw.get(lang), lang) if lang in raw else None
                if cleaned is not None:
                    kept[lang] = cleaned
            if kept:
                accepted[(cell.i, cell.j)] = kept
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
        "app_config": {"live_ai_daily_cap": live_ai_daily_cap},
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
    """Write ``ai_sentences.json``. Always returns a full rule document on AI failure."""
    cells = summarize(today, later)
    config = load_config(env)
    daily_cap = live_ai_daily_cap(env)
    ai_text: dict[tuple[int, int], dict[str, str]] = {}
    calls = 0
    model = "rule-based"
    if not config.enabled:
        print(f"  sentences: rule-based ({config.reason})")
    else:
        host = urllib.parse.urlsplit(chat_url(config.base_url)).hostname
        print(f"  sentences: model={config.model} host={host} max_calls={config.max_calls}")
        chosen = select_for_ai(cells, config.max_calls * BATCH_SIZE)
        caller = transport or post_chat
        try:
            ai_text, calls = apply_ai(chosen, config, caller, time.monotonic() + AI_BUDGET_S)
        except Exception as error:  # noqa: BLE001
            print(f"  sentences: AI step failed ({type(error).__name__}); rules only")
            ai_text, calls = {}, 0
        if any(ai_text.values()):
            model = config.model
    document = build_document(
        cells,
        generated=generated,
        run=run,
        first_date=first_date,
        last_date=last_date,
        model=model,
        ai_calls=calls,
        ai_text=ai_text,
        live_ai_daily_cap=daily_cap,
    )
    _atomic_write(path, document)
    ai_rows = sum(1 for row in document["cells"] if "ai" in row["source"].values())
    print(f"  sentences: wrote {path.name}, {ai_rows} cells with an AI line, {calls} calls, model={model}")
    return document


def _atomic_write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
