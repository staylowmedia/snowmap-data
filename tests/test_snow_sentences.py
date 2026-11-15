"""Rule fallback, validation, and cell mapping for ai_sentences.json."""
from __future__ import annotations

import io
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import snow_sentences as snow  # noqa: E402


def cell(**kwargs) -> snow.CellStats:
    base = dict(i=10, j=10, mean_cm=0.0, max_cm=0.0, fraction=0.0, later_mean_cm=0.0, n=400)
    base.update(kwargs)
    return snow.CellStats(**base)


class CellMappingTest(unittest.TestCase):
    def test_known_places(self):
        self.assertEqual(snow.cell_index(59.91, 10.75), (29, 38))       # Oslo, cell [55, 60) x [10, 15)
        self.assertEqual(snow.cell_index(69.65, 18.96), (31, 39))       # Tromsø
        self.assertEqual(snow.cell_index(-33.87, 151.21), (11, 66))     # Sydney
        self.assertEqual(snow.cell_index(0.0, 0.0), (18, 36))

    def test_poles_and_date_line(self):
        self.assertEqual(snow.cell_index(90.0, 0.0), (35, 36))
        self.assertEqual(snow.cell_index(-90.0, 0.0), (0, 36))
        self.assertEqual(snow.cell_index(0.0, 180.0), (18, 0))
        self.assertEqual(snow.cell_index(0.0, -180.0), (18, 0))
        self.assertEqual(snow.cell_index(0.0, 179.9), (18, 71))
        self.assertEqual(snow.cell_index(0.0, -179.9), (18, 0))
        self.assertEqual(snow.cell_index(0.0, 360.0), (18, 36))
        self.assertEqual(snow.cell_index(0.0, -540.0), (18, 0))

    def test_edges_belong_to_the_higher_cell(self):
        # 60°N is the southern edge of the next cell, not the cell that holds 59.9°N.
        self.assertEqual(snow.cell_index(60.0, 10.0)[0], snow.cell_index(59.9, 10.0)[0] + 1)
        self.assertEqual(snow.cell_index(10.0, 15.0)[1], snow.cell_index(10.0, 14.9)[1] + 1)

    def test_rejects_non_finite(self):
        self.assertRaises(ValueError, snow.cell_index, float("nan"), 0.0)
        self.assertRaises(ValueError, snow.cell_index, 0.0, float("inf"))

    def test_offset_matches_formula(self):
        i, j = snow.cell_index(69.65, 18.96)
        self.assertEqual(snow.cell_offset(i, j), i * snow.N_LON + j)

    def test_gfs_windows_cover_the_globe_once(self):
        covered_rows = []
        for i in range(snow.N_LAT):
            r0, r1 = snow.row_span(i)
            self.assertLess(r0, r1)
            covered_rows.append((r0, r1))
        self.assertEqual(covered_rows[-1][0], 0)          # north pole cell is last
        self.assertEqual(covered_rows[-1][1], 21)
        flat = []
        for r0, r1 in covered_rows:
            flat.extend(range(r0, r1))
        self.assertEqual(sorted(flat), list(range(snow.GFS_ROWS)))

        covered_cols = []
        for j in range(snow.N_LON):
            c0, c1 = snow.col_span(j)
            self.assertEqual(c1 - c0, 20)
            covered_cols.append((c0, c1))
        flat_cols = []
        for c0, c1 in covered_cols:
            flat_cols.extend(range(c0, c1))
        self.assertEqual(sorted(flat_cols), list(range(snow.GFS_COLS)))
        # The date line is column 720, which is the first column of cell j=0, not j=71.
        self.assertEqual(snow.col_span(0), (720, 740))
        self.assertEqual(snow.col_span(71), (700, 720))


class RuleAndValidationTest(unittest.TestCase):
    def test_every_rule_template_publishes(self):
        samples = [
            cell(),
            cell(later_mean_cm=12),
            cell(mean_cm=0.4, max_cm=2, fraction=0.2, later_mean_cm=0.4),
            cell(mean_cm=6, max_cm=20, fraction=0.3, later_mean_cm=6),
            cell(mean_cm=6, max_cm=20, fraction=0.3, later_mean_cm=20),
            cell(mean_cm=6, max_cm=20, fraction=0.3, later_mean_cm=0),
            cell(mean_cm=40, max_cm=80, fraction=0.9, later_mean_cm=40),
            cell(mean_cm=40, max_cm=80, fraction=0.9, later_mean_cm=60),
            cell(mean_cm=40, max_cm=80, fraction=0.9, later_mean_cm=10),
        ]
        kinds = {snow.kind_of(item) for item in samples}
        self.assertEqual(kinds, set(snow._RULES))
        for item in samples:
            for lang in snow.LANGUAGES:
                text = snow.rule_text(item, lang)
                self.assertEqual(snow.validate_sentence(text, lang), text)
                english = snow.rule_text(item, "en")
                self.assertEqual(snow.accept_sentence(text, lang, None if lang == "en" else english), text)
                self.assertIn("{place}", text)
                self.assertNotIn("http", text.lower())
                self.assertNotIn("<", text)
                self.assertNotIn("{n}", text)
                if lang != "en":
                    self.assertNotEqual(text, english)

    def test_kind_thresholds(self):
        self.assertEqual(snow.kind_of(cell()), "none")
        self.assertEqual(snow.kind_of(cell(later_mean_cm=5)), "incoming")
        self.assertEqual(snow.kind_of(cell(mean_cm=0.2, max_cm=3, fraction=0.01)), "dusting")
        self.assertEqual(snow.kind_of(cell(mean_cm=8, max_cm=30, fraction=0.4, later_mean_cm=8)), "patchy")
        self.assertEqual(snow.kind_of(cell(mean_cm=8, max_cm=30, fraction=0.4, later_mean_cm=14)), "patchy_up")
        self.assertEqual(snow.kind_of(cell(mean_cm=8, max_cm=30, fraction=0.4, later_mean_cm=2)), "patchy_down")
        self.assertEqual(snow.kind_of(cell(mean_cm=25, max_cm=40, fraction=0.8, later_mean_cm=25)), "solid")
        self.assertEqual(snow.kind_of(cell(mean_cm=25, max_cm=40, fraction=0.8, later_mean_cm=40)), "solid_up")
        self.assertEqual(snow.kind_of(cell(mean_cm=25, max_cm=40, fraction=0.8, later_mean_cm=8)), "solid_down")

    def test_rejects_unsafe_text(self):
        good = "Around {place} and the wider region, snow is patchy, about 4 cm."
        self.assertEqual(snow.validate_sentence(good, "en"), good)
        # A bare domain has no "http" substring; the check is not a prefix search.
        self.assertNotIn("http", "Around {place} and the wider region, see evil-example.com today.")
        self.assertIsNone(snow.validate_sentence(
            "Around {place} and the wider region, see evil-example.com today.", "en"))
        self.assertIsNone(snow.validate_sentence(
            "Around {place} and the wider region, see https://evil.example/snow today.", "en"))
        self.assertIsNone(snow.validate_sentence(
            "Around {place} and the wider region, see www.evil.example today.", "en"))
        for number in (
            "555 0100", "555-0100", "555.0100", "55 50 10", "123 456 789",
            "123-456-789", "1 800 555 010", "55501",
        ):
            self.assertIsNone(
                snow.validate_sentence(
                    f"Around {{place}} and the wider region, call {number} now.", "en"),
                number,
            )
        self.assertIsNone(snow.validate_sentence(
            "Around {place} and the wider region, call +47 900 12 345 now.", "en"))
        self.assertIsNone(snow.validate_sentence(
            "Around {place} and the wider region, snow is _solid_ today.", "en"))
        self.assertIsNone(snow.validate_sentence(
            "Around {place} and the wider region, snow is __solid__ today.", "en"))
        self.assertIsNone(snow.validate_sentence(
            "Around {place} and the wider region, see javascript:alert(1) now.", "en"))
        self.assertIsNone(snow.validate_sentence("Snow <b>cover</b> is solid around {place}.", "en"))
        self.assertIsNone(snow.validate_sentence("Around {place} and the wider region, snow is solid. More is due.", "en"))
        self.assertIsNone(snow.validate_sentence("Snow around {place} and {place} in the wider region.", "en"))
        self.assertIsNone(snow.accept_sentence(
            "Around {place} and the wider region, snow is solid today.", "ja", None))
        self.assertIsNone(snow.accept_sentence(good, "nb", good))
        self.assertIsNone(snow.accept_sentence(
            "{place}周辺だけの短い文です。", "en", None))
        self.assertIsNone(snow.validate_sentence(
            "Around {place} and the wider region, see example。com today.", "en"))
        for spoken in (
            "example dot com", "example (dot) com", "example punkt com",
            "example punktum com", "example point com", "example d0t com",
            "exampledotcom", "example. com",
        ):
            self.assertIsNone(
                snow.validate_sentence(
                    f"Around {{place}} and the wider region, see {spoken} today.", "en"),
                spoken,
            )
        self.assertIsNotNone(snow.validate_sentence(
            "Around {place} and the wider region, the snow point is solid.", "en"))
        for line, lang in (
            ("{place}请访问snowdeals.com查看积雪。", "zh-Hans"),
            ("{place}周辺の広い地域ではsnowdeals.comを見てください。", "ja"),
            ("{place}请访问evil.ru查看积雪。", "zh-Hans"),
            ("{place}请看snowdeals。com了解积雪。", "zh-Hans"),
        ):
            self.assertIsNone(snow.validate_sentence(line, lang), line)
        self.assertIsNone(snow.validate_sentence(
            "Around {place} and the wider region, call tel:123456 now.", "en"))
        self.assertIsNone(snow.validate_sentence(
            "Around {place} and the wider regiоn, snow is solid today.", "en"))  # Cyrillic о
        self.assertIsNone(snow.validate_sentence(
            "Around {place} and the wider region, Ａ snow is solid today.", "en"))
        self.assertEqual(snow.validate_sentence(
            "Around {place} and the wider region, about 12.5 cm of snow.", "en"),
            "Around {place} and the wider region, about 12.5 cm of snow.")
        self.assertIsNotNone(snow.validate_sentence(
            "Around {place} and the wider region, ca. 12 cm of snow.", "en"))
        self.assertIsNotNone(snow.validate_sentence(
            "In der weiteren Region um {place} liegt z.B. etwas Schnee.", "de"))
        self.assertIsNotNone(snow.validate_sentence(
            "I regionen runt {place} är snön t.ex. fläckvis idag.", "sv"))
        self.assertIsNotNone(snow.validate_sentence(
            "Around {place} and the wider region, on 15.11 snow is patchy.", "en"))
        self.assertIsNotNone(snow.validate_sentence(
            "Around {place} and the wider region, about 2–4 cm of snow.", "en"))
        self.assertIsNotNone(snow.validate_sentence(
            "Around {place} and the wider region, about 1 234 567 cm of snow.", "en"))
        almost = "{place}雪です。"
        self.assertEqual(snow._cjk_count(almost), 3)
        self.assertIsNone(snow.accept_sentence(almost, "ja", None))
        filler = "雪" * (snow.MAX_CJK_CHARS - len("{place}"))
        enough = "{place}" + filler
        self.assertEqual(len(enough), snow.MAX_CJK_CHARS)
        self.assertEqual(snow.validate_sentence(enough, "ja"), enough)
        self.assertIsNone(snow.validate_sentence(enough + "雪", "ja"))

    def test_every_rule_passes_for_every_depth(self):
        english_none = snow.SAFE_SENTENCE
        for kind in snow._RULES:
            for number in (1, 5, 10, 12, 40, 100, 999):
                for lang in snow.LANGUAGES:
                    text = snow._RULES[kind][lang].replace("{n}", str(number))
                    english = None if lang == "en" else snow._RULES[kind]["en"].replace("{n}", str(number))
                    self.assertEqual(snow.accept_sentence(text, lang, english), text, f"{kind} {lang} {number}")
                    if lang != "en":
                        self.assertNotEqual(text, english_none)

    def test_invalid_rule_template_fails_loudly(self):
        item = cell(mean_cm=8, max_cm=30, fraction=0.4, later_mean_cm=20)
        self.assertEqual(snow.kind_of(item), "patchy_up")
        with mock.patch("snow_sentences.accept_sentence", return_value=None):
            with self.assertRaises(RuntimeError):
                snow.rule_text(item, "it")

    def test_allows_one_place_token(self):
        text = "Snow cover around {place} is solid, about 12 cm."
        self.assertEqual(snow.validate_sentence(text, "en"), text)

    def test_interest_prefers_mixed_cover(self):
        bare = cell()
        deep = cell(i=1, mean_cm=80, max_cm=120, fraction=0.95, later_mean_cm=82)
        mixed = cell(i=2, j=3, mean_cm=8, max_cm=40, fraction=0.4, later_mean_cm=20)
        self.assertEqual(snow.interest(bare), 0.0)
        self.assertEqual(snow.interest(deep), 0.0)
        self.assertGreater(snow.interest(mixed), 0.0)
        chosen = snow.select_for_ai([deep, bare, mixed], 1)
        self.assertEqual(chosen, [mixed])
        again = snow.select_for_ai([mixed, deep, bare], 1)
        self.assertEqual(again, [mixed])


class AppConfigTest(unittest.TestCase):
    def test_default_and_override(self):
        self.assertEqual(snow.live_ai_daily_cap({}), 5)
        self.assertEqual(snow.live_ai_daily_cap({"SNOWMAP_LIVE_AI_DAILY_CAP": "12"}), 12)
        self.assertEqual(snow.live_ai_daily_cap({"SNOWMAP_LIVE_AI_DAILY_CAP": "0"}), 0)
        self.assertEqual(snow.live_ai_daily_cap({"SNOWMAP_LIVE_AI_DAILY_CAP": "20"}), 20)
        buffer = io.StringIO()
        with mock.patch("sys.stdout", buffer):
            self.assertEqual(snow.live_ai_daily_cap({"SNOWMAP_LIVE_AI_DAILY_CAP": "nope"}), 5)
            self.assertEqual(snow.live_ai_daily_cap({"SNOWMAP_LIVE_AI_DAILY_CAP": "-3"}), 5)
            self.assertEqual(snow.live_ai_daily_cap({"SNOWMAP_LIVE_AI_DAILY_CAP": "21"}), 20)
            self.assertEqual(snow.live_ai_daily_cap({"SNOWMAP_LIVE_AI_DAILY_CAP": "100"}), 20)
        logged = buffer.getvalue()
        self.assertNotIn("nope", logged)
        self.assertNotIn("100", logged)
        self.assertIn("20", logged)

    def test_published_cap_is_clamped_to_0_through_20(self):
        cells = [cell(i=i, j=j) for i in range(snow.N_LAT) for j in range(snow.N_LON)]
        document = snow.build_document(
            cells, generated="2026-11-15T06:25:00Z", run="2026111500",
            first_date="2026-11-15", last_date="2026-11-22", model="rule-based", ai_calls=0,
            live_ai_daily_cap=99,
        )
        self.assertEqual(document["app_config"]["live_ai_daily_cap"], 20)
        low = snow.build_document(
            cells, generated="2026-11-15T06:25:00Z", run="2026111500",
            first_date="2026-11-15", last_date="2026-11-22", model="rule-based", ai_calls=0,
            live_ai_daily_cap=-4,
        )
        self.assertEqual(low["app_config"]["live_ai_daily_cap"], 5)
        self.assertEqual(snow.clamp_live_ai_daily_cap(True), 5)
        self.assertEqual(snow.clamp_live_ai_daily_cap(False), 5)

    def test_published_file_uses_the_env_value(self):
        cells = [cell(i=i, j=j) for i in range(snow.N_LAT) for j in range(snow.N_LON)]
        target = Path(os.environ.get("TMPDIR", "/tmp")) / "ai_sentences_cap.json"
        try:
            with mock.patch("snow_sentences.summarize", return_value=cells):
                document = snow.write_sentences(
                    target, None, None,
                    generated="2026-11-15T06:25:00Z",
                    run="2026111500",
                    first_date="2026-11-15",
                    last_date="2026-11-22",
                    env={"SNOWMAP_LIVE_AI_DAILY_CAP": "9"},
                )
            self.assertEqual(document["app_config"], {"live_ai_daily_cap": 9})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["app_config"]["live_ai_daily_cap"], 9)
        finally:
            target.unlink(missing_ok=True)


class DocumentTest(unittest.TestCase):
    def test_rule_document_shape_and_mapping(self):
        cells = [cell(i=i, j=j) for i in range(snow.N_LAT) for j in range(snow.N_LON)]
        # One interesting cell so the rule text is not uniform.
        cells[snow.cell_offset(29, 38)] = cell(i=29, j=38, mean_cm=18, max_cm=40, fraction=0.7)
        document = snow.build_document(
            cells, generated="2026-11-15T06:25:00Z", run="2026111500",
            first_date="2026-11-15", last_date="2026-11-22", model="rule-based", ai_calls=0,
        )
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["generated"], "2026-11-15T06:25:00Z")
        self.assertEqual(document["run"], "2026111500")
        self.assertEqual(document["model"], "rule-based")
        self.assertEqual(document["languages"], list(snow.LANGUAGES))
        self.assertEqual(document["placeholder"], "{place}")
        self.assertEqual(document["max_age_hours"], 30)
        self.assertEqual(document["app_config"], {"live_ai_daily_cap": 5})
        self.assertEqual(document["window"], {"first_date": "2026-11-15", "last_date": "2026-11-22"})
        self.assertEqual(document["grid"]["resolution_deg"], 5.0)
        self.assertEqual(len(document["cells"]), snow.N_LAT * snow.N_LON)
        oslo = document["cells"][snow.cell_offset(29, 38)]
        self.assertEqual(oslo["source"]["en"], "rule")
        self.assertIn("18 cm", oslo["text"]["en"])
        bare = document["cells"][snow.cell_offset(0, 0)]
        self.assertNotIn("18 cm", bare["text"]["en"])
        encoded = json.dumps(document)
        self.assertNotIn("http://", encoded)
        self.assertNotIn("https://", encoded)

    def test_ai_text_keeps_rules_when_one_language_fails(self):
        item = cell(i=4, j=5, mean_cm=12, max_cm=20, fraction=0.6)
        cells = [cell(i=i, j=j) for i in range(snow.N_LAT) for j in range(snow.N_LON)]
        cells[snow.cell_offset(4, 5)] = item
        accepted = {
            (4, 5): {
                "en": "Patchy snow around {place}, about 12 cm on average.",
                "nb": "Se https://example.com for mer snø i området.",
            }
        }
        # The Norwegian line is unsafe and must not be passed in by apply_ai.
        # build_document trusts its caller, so this checks the merge the writer uses
        # after validation: only cleaned lines are inserted.
        cleaned = {}
        for lang, text in accepted[(4, 5)].items():
            ok = snow.validate_sentence(text, lang)
            if ok:
                cleaned[lang] = ok
        document = snow.build_document(
            cells, generated="2026-11-15T06:25:00Z", run="2026111500",
            first_date="2026-11-15", last_date="2026-11-22", model="demo-model", ai_calls=1,
            ai_text={(4, 5): cleaned},
        )
        row = document["cells"][snow.cell_offset(4, 5)]
        self.assertEqual(row["source"]["en"], "ai")
        self.assertIn("{place}", row["text"]["en"])
        self.assertEqual(row["source"]["nb"], "rule")
        self.assertNotIn("http", row["text"]["nb"])
        self.assertEqual(document["model"], "demo-model")

    def test_parse_fenced_json_and_ignore_unknown_ids(self):
        payload = "```json\n" + json.dumps({
            "cells": [
                {"id": "1,2", "sentences": {"en": "Solid snow cover in this area, about 20 cm."}},
                {"id": "9,9", "sentences": {"en": "Patchy snow in this area, about 3 cm."}},
                {"id": "1,2", "sentences": {"en": "Second copy must not win over the first."}},
            ]
        }) + "\n```"
        parsed = snow.parse_sentences(payload)
        self.assertEqual(set(parsed), {"1,2", "9,9"})
        self.assertIn("20 cm", parsed["1,2"]["en"])
        self.assertEqual(snow.parse_sentences("not json"), {})
        self.assertEqual(snow.parse_sentences('{"cells": []}'), {})


class AICallTest(unittest.TestCase):
    def setUp(self):
        self._http = mock.patch.dict(os.environ, {"SNOWMAP_AI_ALLOW_HTTP_LOCALHOST": "1"})
        self._http.start()

    def tearDown(self):
        self._http.stop()
    def test_no_key_makes_no_call_and_hides_nothing_secret(self):
        calls = []

        def transport(*args):
            calls.append(args)
            raise AssertionError("transport should not run")

        cells = [cell(i=i, j=j, mean_cm=12, max_cm=20, fraction=0.6) for i in range(2) for j in range(2)]
        # Pad to a full grid inside write via summarize; here call apply only through load_config.
        config = snow.load_config({})
        self.assertFalse(config.enabled)
        self.assertIn("no API key", config.reason)
        self.assertNotIn("key", repr(config).split("key_set=")[0])
        buffer = io.StringIO()
        with mock.patch("sys.stdout", buffer):
            accepted, n = snow.apply_ai(cells, config, transport, deadline=snow.time.monotonic() + 5)
        self.assertEqual(calls, [])
        self.assertEqual(accepted, {})
        self.assertEqual(n, 0)

    def test_key_without_endpoint_does_not_call(self):
        secret = "unit-test-key-not-a-real-secret"
        config = snow.load_config({"SNOWMAP_AI_API_KEY": secret})
        self.assertFalse(config.enabled)
        self.assertNotIn(secret, repr(config))
        config = snow.load_config({
            "SNOWMAP_AI_API_KEY": secret,
            "SNOWMAP_AI_BASE_URL": "http://api.example.test/v1",
            "SNOWMAP_AI_MODEL": "demo",
        })
        self.assertFalse(config.enabled)
        config = snow.load_config({
            "SNOWMAP_AI_API_KEY": secret,
            "SNOWMAP_AI_BASE_URL": "https://user:secret@api.example.test/v1",
            "SNOWMAP_AI_MODEL": "demo",
        })
        self.assertFalse(config.enabled)
        config = snow.load_config({
            "SNOWMAP_AI_API_KEY": secret,
            "SNOWMAP_AI_BASE_URL": "https://api.example.test/v1?token=secret",
            "SNOWMAP_AI_MODEL": "demo",
        })
        self.assertFalse(config.enabled)

    def test_batch_validates_per_language_and_stops_at_the_cap(self):
        secret = "unit-test-key-not-a-real-secret"
        config = snow.load_config({
            "SNOWMAP_AI_API_KEY": secret,
            "SNOWMAP_AI_BASE_URL": "https://api.example.test/v1/",
            "SNOWMAP_AI_MODEL": "demo-model",
            "SNOWMAP_AI_MAX_CALLS": "1",
        })
        self.assertTrue(config.enabled)
        self.assertEqual(snow.chat_url(config.base_url), "https://api.example.test/v1/chat/completions")
        seen = []

        def transport(url, key, model, messages, timeout):
            seen.append((url, key, model, messages, timeout))
            batch_ids = []
            for line in messages[1]["content"].splitlines():
                if line.startswith("id="):
                    batch_ids.append(line.split()[0].split("=", 1)[1])
            sample = cell(mean_cm=20, max_cm=40, fraction=0.8, later_mean_cm=20)
            sentences = {lang: snow.rule_text(sample, lang) for lang in snow.LANGUAGES}
            # One language is unsafe and must fall back on its own.
            sentences["nb"] = "I regionen rundt {place} se evil-example.com i dag."
            return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
                "cells": [{"id": cell_id, "sentences": sentences} for cell_id in batch_ids]
            })}}]}

        chosen = [
            cell(i=i, j=j, mean_cm=12, max_cm=30, fraction=0.4, later_mean_cm=20)
            for i in range(3) for j in range(4)
        ]  # 12 cells -> two batches, but the cap is one call
        accepted, calls = snow.apply_ai(chosen, config, transport, deadline=snow.time.monotonic() + 30)
        self.assertEqual(calls, 1)
        self.assertEqual(len(seen), 1)
        url, key, model, messages, timeout = seen[0]
        self.assertEqual(url, "https://api.example.test/v1/chat/completions")
        self.assertEqual(key, secret)
        self.assertNotIn(secret, url)
        self.assertNotIn(secret, json.dumps(messages))
        self.assertNotIn("latitude", json.dumps(messages).lower())
        self.assertNotIn("longitude", json.dumps(messages).lower())
        self.assertEqual(model, "demo-model")
        self.assertEqual(timeout, snow.REQUEST_TIMEOUT_S)
        self.assertEqual(len(accepted), snow.BATCH_SIZE)
        sample = next(iter(accepted.values()))
        self.assertEqual(set(sample), set(snow.LANGUAGES) - {"nb"})
        self.assertNotIn("http", " ".join(sample.values()))

    def test_cut_off_reply_is_discarded(self):
        config = snow.load_config({
            "SNOWMAP_AI_API_KEY": "unit-test-key",
            "SNOWMAP_AI_BASE_URL": "https://api.example.test/v1",
            "SNOWMAP_AI_MODEL": "demo",
        })

        def transport(*args):
            return {"choices": [{"finish_reason": "length", "message": {"content": "{\"cells\":[]}"}}]}

        accepted, calls = snow.apply_ai([cell(mean_cm=12, max_cm=20, fraction=0.5)], config, transport, snow.time.monotonic() + 5)
        self.assertEqual(calls, 1)
        self.assertEqual(accepted, {})

    def test_http_error_does_not_log_the_body(self):
        config = snow.load_config({
            "SNOWMAP_AI_API_KEY": "unit-test-key",
            "SNOWMAP_AI_BASE_URL": "https://api.example.test/v1",
            "SNOWMAP_AI_MODEL": "demo",
        })

        def transport(*args):
            raise urllib.error.HTTPError(
                "https://api.example.test/v1/chat/completions",
                401,
                "nope",
                hdrs=None,
                fp=io.BytesIO(b"secret-body-should-not-be-logged"),
            )

        buffer = io.StringIO()
        with mock.patch("sys.stdout", buffer):
            accepted, calls = snow.apply_ai(
                [cell(mean_cm=12, max_cm=20, fraction=0.5)], config, transport, snow.time.monotonic() + 5)
        logged = buffer.getvalue()
        self.assertIn("HTTP 401", logged)
        self.assertNotIn("secret-body", logged)
        self.assertNotIn("unit-test-key", logged)
        self.assertEqual(accepted, {})
        self.assertEqual(calls, 1)

    def test_post_chat_puts_the_key_in_the_header_only(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        captured = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                captured["body"] = self.rfile.read(length)
                captured["auth"] = self.headers.get("Authorization")
                captured["path"] = self.path
                payload = b'{"choices":[]}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            snow.post_chat(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                "unit-test-key",
                "demo",
                [{"role": "user", "content": "hi"}],
                5,
            )
        finally:
            server.shutdown()
        self.assertEqual(captured["path"], "/v1/chat/completions")
        self.assertNotIn("unit-test-key", captured["path"])
        self.assertEqual(captured["auth"], "Bearer unit-test-key")
        body = json.loads(captured["body"])
        self.assertEqual(body["model"], "demo")
        self.assertEqual(body["max_tokens"], snow.MAX_TOKENS)
        self.assertNotIn("unit-test-key", captured["body"].decode())

    def test_redirect_is_not_followed(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        landed = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path.startswith("/landed"):
                    landed.append(self.path)
                    payload = b'{"choices":[]}'
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self.send_response(302)
                port = self.server.server_address[1]
                self.send_header("Location", f"http://127.0.0.1:{port}/landed")
                self.end_headers()

            def log_message(self, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            with self.assertRaises(urllib.error.HTTPError) as caught:
                snow.post_chat(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    "unit-test-key",
                    "demo",
                    [{"role": "user", "content": "hi"}],
                    5,
                )
        finally:
            server.shutdown()
        self.assertEqual(caught.exception.code, 302)
        self.assertEqual(landed, [])

    def test_drip_cannot_outlive_the_wall_clock(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        import time

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "8000")
                self.end_headers()
                try:
                    for _ in range(40):
                        time.sleep(0.25)
                        self.wfile.write(b"x")
                        self.wfile.flush()
                except Exception:
                    return

            def log_message(self, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started = time.monotonic()
        try:
            port = server.server_address[1]
            with self.assertRaises(TimeoutError):
                snow.post_chat(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    "unit-test-key",
                    "demo",
                    [{"role": "user", "content": "hi"}],
                    0.4,
                )
        finally:
            server.shutdown()
        self.assertLess(time.monotonic() - started, 2.0)

    def test_call_cap_cannot_exceed_the_hard_limit(self):
        config = snow.load_config({
            "SNOWMAP_AI_API_KEY": "unit-test-key",
            "SNOWMAP_AI_BASE_URL": "https://api.example.test/v1",
            "SNOWMAP_AI_MODEL": "demo",
            "SNOWMAP_AI_MAX_CALLS": "999",
        })
        self.assertEqual(config.max_calls, snow.HARD_MAX_CALLS)
        seventeen = snow.load_config({
            "SNOWMAP_AI_API_KEY": "unit-test-key",
            "SNOWMAP_AI_BASE_URL": "https://api.example.test/v1",
            "SNOWMAP_AI_MODEL": "demo",
            "SNOWMAP_AI_MAX_CALLS": "17",
        })
        self.assertEqual(seventeen.max_calls, 16)
        self.assertNotEqual(seventeen.max_calls, 17)

    def test_seventeenth_call_is_not_started(self):
        config = snow.load_config({
            "SNOWMAP_AI_API_KEY": "unit-test-key",
            "SNOWMAP_AI_BASE_URL": "https://api.example.test/v1",
            "SNOWMAP_AI_MODEL": "demo",
            "SNOWMAP_AI_MAX_CALLS": "17",
        })
        started = []

        def transport(*_args):
            started.append(1)
            return {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}

        cells = [
            cell(i=n // snow.N_LON, j=n % snow.N_LON, mean_cm=12, max_cm=20, fraction=0.4, later_mean_cm=20)
            for n in range(17 * snow.BATCH_SIZE)
        ]
        _accepted, calls = snow.apply_ai(cells, config, transport, snow.time.monotonic() + 30)
        self.assertEqual(calls, 16)
        self.assertEqual(len(started), 16)

    def test_generic_exception_does_not_log_the_key(self):
        secret = "unit-test-key-not-a-real-secret"
        config = snow.load_config({
            "SNOWMAP_AI_API_KEY": secret,
            "SNOWMAP_AI_BASE_URL": "https://api.example.test/v1",
            "SNOWMAP_AI_MODEL": "demo",
        })

        def transport(*_args):
            raise RuntimeError(f"provider failed with key {secret}")

        buffer = io.StringIO()
        with mock.patch("sys.stdout", buffer):
            accepted, calls = snow.apply_ai(
                [cell(mean_cm=12, max_cm=20, fraction=0.5, later_mean_cm=12)],
                config, transport, snow.time.monotonic() + 5)
        self.assertNotIn(secret, buffer.getvalue())
        self.assertIn("RuntimeError", buffer.getvalue())
        self.assertEqual(accepted, {})
        self.assertEqual(calls, 1)

    def test_nested_json_keeps_earlier_lines_and_the_call_count(self):
        config = snow.load_config({
            "SNOWMAP_AI_API_KEY": "unit-test-key",
            "SNOWMAP_AI_BASE_URL": "https://api.example.test/v1",
            "SNOWMAP_AI_MODEL": "demo",
        })
        sample = cell(mean_cm=20, max_cm=40, fraction=0.8, later_mean_cm=20)
        good = {lang: snow.rule_text(sample, lang) for lang in snow.LANGUAGES}
        nested = '{"a":' * 10000 + "1" + "}" * 10000
        started = []

        def transport(_url, _key, _model, messages, _timeout):
            started.append(1)
            if len(started) > 1:
                return {"choices": [{"finish_reason": "stop", "message": {"content": nested}}]}
            ids = []
            for line in messages[1]["content"].splitlines():
                if line.startswith("id="):
                    ids.append(line.split()[0].split("=", 1)[1])
            return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
                "cells": [{"id": cell_id, "sentences": good} for cell_id in ids]
            })}}]}

        cells = [
            cell(i=n // snow.N_LON, j=n % snow.N_LON, mean_cm=12, max_cm=30, fraction=0.4, later_mean_cm=20)
            for n in range(snow.BATCH_SIZE * 2)
        ]
        buffer = io.StringIO()
        with mock.patch("sys.stdout", buffer):
            accepted, calls = snow.apply_ai(cells, config, transport, snow.time.monotonic() + 30)
        self.assertEqual(calls, 2)
        self.assertEqual(len(accepted), snow.BATCH_SIZE)
        self.assertIn("RecursionError", buffer.getvalue())
        self.assertNotIn("unit-test-key", buffer.getvalue())


def _numpy_installed() -> bool:
    try:
        import numpy  # noqa: F401
    except ImportError:
        return False
    return True


@unittest.skipUnless(_numpy_installed(), "numpy is not installed")
class GridSummaryTest(unittest.TestCase):
    def test_oslo_point_lands_in_the_oslo_cell(self):
        import numpy as np
        today = np.zeros((snow.GFS_ROWS, snow.GFS_COLS), dtype=np.float32)
        later = np.zeros_like(today)
        # 59.75°N, 10.75°E is inside the Oslo cell [55, 60) x [10, 15).
        row = int(round((90.0 - 59.75) / 0.25))
        col = int(round(10.75 / 0.25))
        today[row, col] = 0.20  # 20 cm
        cells = snow.summarize(today, later)
        oslo = cells[snow.cell_offset(*snow.cell_index(59.75, 10.75))]
        self.assertEqual((oslo.i, oslo.j), (29, 38))
        self.assertGreater(oslo.max_cm, 19)
        neighbour = cells[snow.cell_offset(30, 38)]
        self.assertEqual(neighbour.max_cm, 0.0)
        self.assertEqual(len(cells), snow.N_LAT * snow.N_LON)

    def test_date_line_point_lands_in_column_zero(self):
        import numpy as np
        today = np.zeros((snow.GFS_ROWS, snow.GFS_COLS), dtype=np.float32)
        later = today
        row = int(round((90.0 - 0.0) / 0.25))
        today[row, 720] = 1.0
        cells = snow.summarize(today, later)
        on_line = cells[snow.cell_offset(*snow.cell_index(0.0, 180.0))]
        self.assertEqual((on_line.i, on_line.j), (18, 0))
        self.assertGreater(on_line.max_cm, 90)
        other = cells[snow.cell_offset(18, 71)]
        self.assertEqual(other.max_cm, 0.0)

    def test_rule_file_is_complete_without_a_key(self):
        import numpy as np
        today = np.zeros((snow.GFS_ROWS, snow.GFS_COLS), dtype=np.float32)
        later = np.zeros_like(today)
        # A solid block inside one cell, so the file is not a single repeated sentence.
        r0, r1 = snow.row_span(29)
        c0, c1 = snow.col_span(38)
        today[r0:r1, c0:c1] = 0.30
        secret = "unit-test-key-not-a-real-secret"
        target = Path(os.environ.get("TMPDIR", "/tmp")) / "ai_sentences_test.json"
        try:
            buffer = io.StringIO()
            with mock.patch("sys.stdout", buffer):
                document = snow.write_sentences(
                    target, today, later,
                    generated="2026-11-15T06:25:00Z",
                    run="2026111500",
                    first_date="2026-11-15",
                    last_date="2026-11-22",
                    env={"SNOWMAP_AI_API_KEY": secret},  # no URL and no model: must not call
                )
            logged = buffer.getvalue()
            self.assertNotIn(secret, logged)
            raw = target.read_text(encoding="utf-8")
            self.assertNotIn(secret, raw)
            self.assertLess(len(raw.encode("utf-8")), 4_500_000)
            self.assertEqual(document["model"], "rule-based")
            self.assertEqual(document["ai_calls"], 0)
            oslo = document["cells"][snow.cell_offset(29, 38)]
            self.assertEqual(oslo["source"]["nb"], "rule")
            self.assertIn("cm", oslo["text"]["en"])
            self.assertTrue(all(set(row["source"].values()) == {"rule"} for row in document["cells"]))
        finally:
            target.unlink(missing_ok=True)


class PromptBudgetTest(unittest.TestCase):
    def test_prompt_has_no_coordinates_or_place_names(self):
        batch = [cell(i=i, j=i, mean_cm=12, max_cm=40, fraction=0.42, later_mean_cm=18) for i in range(snow.BATCH_SIZE)]
        messages = snow.messages_for(batch)
        blob = json.dumps(messages)
        self.assertNotIn("Troms", blob)
        self.assertNotIn("Oslo", blob)
        self.assertNotIn("latitude", blob.lower())
        self.assertNotIn("longitude", blob.lower())
        self.assertLess(len(messages[0]["content"]), 2500)
        self.assertIn(f"at most {snow.MAX_CJK_CHARS} characters", messages[0]["content"])
        self.assertNotIn("40 characters", messages[0]["content"])
        self.assertIn("wider region", messages[0]["content"])


class BudgetTest(unittest.TestCase):
    def test_phase_budget_and_remaining_timeout(self):
        self.assertEqual(snow.AI_BUDGET_S, 60)
        self.assertEqual(snow.REQUEST_TIMEOUT_S, 12)
        self.assertEqual(snow.MAX_CJK_CHARS, 90)
        self.assertEqual(snow.MAX_TOKENS, 6144)
        temporary = snow._temp_path(Path("site/snow/ai_sentences.json"))
        self.assertNotIn("site", temporary.parts)
        self.assertTrue(snow._SCHEME_RE.search("tel:12"))
        self.assertEqual(snow._normalize_dots("snowdeals。com"), "snowdeals.com")
        self.assertFalse(snow._valid_base("http://127.0.0.1:9/v1"))
        with self.assertRaises(ValueError):
            snow.post_chat("http://127.0.0.1:9/v1/chat/completions", "k", "m", [], 1)
        config = snow.load_config({
            "SNOWMAP_AI_API_KEY": "unit-test-key",
            "SNOWMAP_AI_BASE_URL": "https://api.example.test/v1",
            "SNOWMAP_AI_MODEL": "demo",
        })
        seen = []

        def transport(*args):
            seen.append(args[-1])
            return {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}

        item = cell(mean_cm=12, max_cm=30, fraction=0.4, later_mean_cm=20)
        snow.apply_ai([item], config, transport, snow.time.monotonic() + 100)
        self.assertEqual(seen, [12])
        seen.clear()
        snow.apply_ai([item], config, transport, snow.time.monotonic() + 4)
        self.assertEqual(len(seen), 1)
        self.assertLess(seen[0], 12)
        self.assertGreater(seen[0], 3)
        seen.clear()
        accepted, calls = snow.apply_ai([item], config, transport, snow.time.monotonic() + 0.2)
        self.assertEqual(calls, 0)
        self.assertEqual(seen, [])
        self.assertEqual(accepted, {})


class ScriptMainTest(unittest.TestCase):
    def test_script_writes_ai_lines(self):
        import subprocess
        import tempfile
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        sample = cell(mean_cm=20, max_cm=40, fraction=0.8, later_mean_cm=20)
        good = {lang: snow.rule_text(sample, lang) for lang in snow.LANGUAGES}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                body = json.loads(raw.decode("utf-8"))
                user = body["messages"][1]["content"]
                ids = [line.split()[0].split("=", 1)[1] for line in user.splitlines() if line.startswith("id=")]
                payload = json.dumps({"cells": [{"id": cell_id, "sentences": good} for cell_id in ids]}).encode()
                outer = json.dumps({
                    "choices": [{"finish_reason": "stop", "message": {"content": payload.decode()}}],
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(outer)))
                self.end_headers()
                self.wfile.write(outer)

            def log_message(self, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        root = Path(tempfile.mkdtemp())
        try:
            snow_dir = root / "site" / "snow"
            snow_dir.mkdir(parents=True)
            sentences = snow_dir / "ai_sentences.json"
            sentences.write_text('{"schema_version":1}\n', encoding="utf-8")
            rows = []
            for i in range(snow.N_LAT):
                for j in range(snow.N_LON):
                    rows.append({"i": i, "j": j, "mean_cm": 0.0, "max_cm": 0.0, "fraction": 0.0, "later_mean_cm": 0.0, "n": 400})
            rows[10 * snow.N_LON + 10] = {
                "i": 10, "j": 10, "mean_cm": 8.0, "max_cm": 40.0, "fraction": 0.4, "later_mean_cm": 20.0, "n": 400,
            }
            inputs = root / "build" / "sentence_inputs.json"
            inputs.parent.mkdir()
            inputs.write_text(json.dumps({
                "generated": "2026-11-15T06:25:00Z",
                "run": "2026111500",
                "first_date": "2026-11-15",
                "last_date": "2026-11-22",
                "live_ai_daily_cap": 5,
                "cells": rows,
            }), encoding="utf-8")
            script = Path(__file__).resolve().parents[1] / "scripts" / "snow_sentences.py"
            env = {
                "PATH": os.environ.get("PATH", ""),
                "SNOWMAP_AI_API_KEY": "unit-test-key-not-real",
                "SNOWMAP_AI_BASE_URL": f"http://127.0.0.1:{port}/v1",
                "SNOWMAP_AI_MODEL": "demo",
                "SNOWMAP_AI_MAX_CALLS": "1",
                "SNOWMAP_AI_ALLOW_HTTP_LOCALHOST": "1",
            }
            completed = subprocess.run(
                [sys.executable, str(script), "--sentences", str(sentences), "--inputs", str(inputs)],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertNotIn("NameError", completed.stdout + completed.stderr)
            self.assertNotIn("unit-test-key-not-real", completed.stdout + completed.stderr)
            document = json.loads(sentences.read_text(encoding="utf-8"))
            row = document["cells"][10 * snow.N_LON + 10]
            self.assertEqual(row["source"]["en"], "ai")
            self.assertIn("{place}", row["text"]["en"])
            self.assertGreater(document["ai_calls"], 0)
            self.assertEqual(list(root.joinpath("site").rglob("*.tmp")), [])
        finally:
            server.shutdown()
            import shutil
            shutil.rmtree(root, ignore_errors=True)


class PublishGuardTest(unittest.TestCase):
    def test_sentence_failure_does_not_escape(self):
        import importlib
        import sys
        import types
        for name in ("eccodes", "numpy", "PIL", "requests"):
            if name in sys.modules:
                continue
            try:
                importlib.import_module(name)
            except ModuleNotFoundError:
                module = types.ModuleType(name)
                if name == "requests":
                    class _Session:
                        def __init__(self):
                            self.headers = {}
                    module.Session = _Session
                if name == "PIL":
                    module.Image = object
                sys.modules[name] = module
        import build_snow
        with mock.patch("snow_sentences.write_rule_file", side_effect=RuntimeError("disk")):
            build_snow.publish_sentences(
                Path("/tmp"), None, None,
                generated="2026-11-15T00:00:00Z",
                run="2026111500",
                first_date="2026-11-15",
                last_date="2026-11-22",
                inputs_path=Path("/tmp/snow-sentence-inputs.json"),
            )

    def test_failed_upgrade_keeps_the_rule_file(self):
        target = Path(os.environ.get("TMPDIR", "/tmp")) / "ai_sentences_keep.json"
        inputs = Path(os.environ.get("TMPDIR", "/tmp")) / "sentence_inputs_bad.json"
        try:
            target.write_text('{"schema_version":1,"kept":true}\n', encoding="utf-8")
            inputs.write_text("not json", encoding="utf-8")
            code = snow.upgrade_file(target, inputs, env={})
            self.assertEqual(code, 0)
            self.assertEqual(target.read_text(encoding="utf-8"), '{"schema_version":1,"kept":true}\n')
        finally:
            target.unlink(missing_ok=True)
            inputs.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
