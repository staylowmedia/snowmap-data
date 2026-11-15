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
                self.assertNotIn("http", text.lower())
                self.assertNotIn("<", text)
                self.assertNotIn("{n}", text)

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
        good = "Patchy snow in this area, about 4 cm on average."
        self.assertEqual(snow.validate_sentence(good, "en"), good)
        self.assertIsNone(snow.validate_sentence("See https://evil.example/snow", "en"))
        self.assertIsNone(snow.validate_sentence("See www.evil.example for snow", "en"))
        self.assertIsNone(snow.validate_sentence("Snow <b>cover</b> is solid here.", "en"))
        self.assertIsNone(snow.validate_sentence("Snow cover is solid. More is due.", "en"))
        self.assertIsNone(snow.validate_sentence("Snow around {place} and {place}.", "en"))
        self.assertIsNone(snow.validate_sentence("Snow {place} {town} today here.", "en"))
        self.assertIsNone(snow.validate_sentence("Snow 😀 is solid in this area today.", "en"))
        self.assertIsNone(snow.validate_sentence("x" * 181, "en"))
        self.assertIsNone(snow.validate_sentence("Too short.", "en"))
        self.assertIsNone(snow.validate_sentence("one two", "en"))
        long = " ".join(["snow"] * 19)
        self.assertIsNone(snow.validate_sentence(long, "en"))
        self.assertIsNone(snow.validate_sentence("この地域の雪" * 20, "ja"))

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
        buffer = io.StringIO()
        with mock.patch("sys.stdout", buffer):
            self.assertEqual(snow.live_ai_daily_cap({"SNOWMAP_LIVE_AI_DAILY_CAP": "nope"}), 5)
            self.assertEqual(snow.live_ai_daily_cap({"SNOWMAP_LIVE_AI_DAILY_CAP": "-3"}), 5)
        self.assertNotIn("nope", buffer.getvalue())

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
            sentences = {
                lang: "Solid snow cover in this area, about 12 cm."
                for lang in snow.LANGUAGES
            }
            # One language is unsafe and must fall back on its own.
            sentences["nb"] = "Se https://example.com nå med en gang."
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
        captured = {}

        class Response:
            status = 200

            def read(self, _n):
                return b'{"choices":[]}'

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class Opener:
            def open(self, request, timeout):
                captured["url"] = request.full_url
                captured["timeout"] = timeout
                captured["auth"] = request.get_header("Authorization")
                captured["body"] = request.data
                return Response()

        with mock.patch("snow_sentences.urllib.request.build_opener", return_value=Opener()):
            snow.post_chat(
                "https://api.example.test/v1/chat/completions",
                "unit-test-key",
                "demo",
                [{"role": "user", "content": "hi"}],
                5,
            )
        self.assertNotIn("unit-test-key", captured["url"])
        self.assertEqual(captured["auth"], "Bearer unit-test-key")
        self.assertEqual(captured["timeout"], 5)
        body = json.loads(captured["body"])
        self.assertEqual(body["model"], "demo")
        self.assertEqual(body["max_tokens"], snow.MAX_TOKENS)
        self.assertNotIn("unit-test-key", captured["body"].decode())

    def test_redirect_is_blocked(self):
        handler = snow._NoRedirect()
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(mock.Mock(full_url="https://api.example.test/v1"), None, 302, "found", None, "https://elsewhere.test")

    def test_call_cap_cannot_exceed_the_hard_limit(self):
        config = snow.load_config({
            "SNOWMAP_AI_API_KEY": "unit-test-key",
            "SNOWMAP_AI_BASE_URL": "https://api.example.test/v1",
            "SNOWMAP_AI_MODEL": "demo",
            "SNOWMAP_AI_MAX_CALLS": "999",
        })
        self.assertEqual(config.max_calls, snow.HARD_MAX_CALLS)


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


if __name__ == "__main__":
    unittest.main()
