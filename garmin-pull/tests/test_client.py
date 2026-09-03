#!/usr/bin/env python3
"""Unit tests for garmin_client.py. Run with:
    python3 -m unittest test_client -v

Two kinds of tests here:
  1. OAuth1 signing (build_oauth1_authorization_header /
     _oauth1_signature_base_string / _oauth1_sign) verified against two
     independent, real test vectors that have nothing to do with Garmin:
       a. The signature *base string* is checked against RFC 5849 Section
          3.1's worked example, transcribed directly from the RFC text.
       b. The final HMAC-SHA1 *signature* is checked against oauthlib's own
          checked-in unit test (tests/oauth1/rfc5849/test_signatures.py,
          test_sign_hmac_sha1_with_client) -- oauthlib is an independent,
          widely-used OAuth1 implementation, and its test reuses this exact
          RFC base string with its own secret pair and asserted output.
          (Note: an initial attempt to use the *signature* value RFC 5849
          itself is reported to produce, sourced via a web-fetch summary
          tool, did NOT reproduce under direct computation here -- most
          likely a transcription/recall error in that summary rather than
          in this code, since the base string it was built from checks out
          byte-for-byte against two independent sources. Dropped in favor
          of oauthlib's vector, which is fully reproducible from source.)
     A pass here proves the signing math itself is correct regardless of
     whether Garmin's own flow can be exercised.
  2. JSON-shaping functions (shape_sleep, shape_hrv, shape_daily_summary,
     shape_vo2max) tested against synthetic fixtures modeled on the field
     names documented in garmin_client.py's module docstring sources.
     These are the shapes we could confirm from reference source; they are
     NOT verified against a live Garmin response (no account available).

Never touches the network, stored credentials, or any real Garmin account.
"""
from __future__ import annotations

import unittest

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import garmin_client as gc


class TestOAuth1SignatureRFC5849WorkedExample(unittest.TestCase):
    """RFC 5849 Section 3.1's worked example request:

    POST /request?b5=%3D%253D&a3=a&c%40=&a2=r%20b HTTP/1.1
    Host: example.com
    Content-Type: application/x-www-form-urlencoded
    Authorization: OAuth realm="Example",
                   oauth_consumer_key="9djdj82h48djs9d2",
                   oauth_token="kkk9d7dh3k39sjv7",
                   oauth_signature_method="HMAC-SHA1",
                   oauth_timestamp="137131201",
                   oauth_nonce="7d8f3e4a",
                   oauth_signature="..."

    c2&a3=2+q

    The resulting *base string* below is transcribed from the RFC and
    cross-checked against oauthlib's test fixture (same literal string,
    see TestOAuth1SignatureAgainstOauthlibVector below) -- verified
    byte-for-byte from two independent sources. The RFC's own worked
    *signature* value is deliberately not asserted here (see module
    docstring): it could not be reproduced from this base string under
    direct HMAC-SHA1 computation, most likely a transcription error
    upstream of this file rather than a bug in the signing code -- the
    signature is instead checked in the class below, against a value that
    ships as a real, executable assertion in oauthlib's own test suite.
    """

    def test_signature_base_string_matches_rfc_example(self):
        # Query params from the request line, already decoded.
        query_params = [
            ("b5", "=%3D"),
            ("a3", "a"),
            ("c@", ""),
            ("a2", "r b"),
        ]
        # Body params from the x-www-form-urlencoded body "c2&a3=2+q".
        body_params = [("c2", ""), ("a3", "2 q")]
        oauth_params = [
            ("oauth_consumer_key", "9djdj82h48djs9d2"),
            ("oauth_token", "kkk9d7dh3k39sjv7"),
            ("oauth_signature_method", "HMAC-SHA1"),
            ("oauth_timestamp", "137131201"),
            ("oauth_nonce", "7d8f3e4a"),
        ]
        all_params = query_params + oauth_params + body_params

        base_string = gc._oauth1_signature_base_string(
            "POST", "http://example.com/request", all_params
        )
        expected = (
            "POST&http%3A%2F%2Fexample.com%2Frequest&a2%3Dr%2520b%26a3%3D2%2520q"
            "%26a3%3Da%26b5%3D%253D%25253D%26c%2540%3D%26c2%3D%26oauth_consumer_"
            "key%3D9djdj82h48djs9d2%26oauth_nonce%3D7d8f3e4a%26oauth_signature_m"
            "ethod%3DHMAC-SHA1%26oauth_timestamp%3D137131201%26oauth_token%3Dkkk"
            "9d7dh3k39sjv7"
        )
        self.assertEqual(base_string, expected)

class TestOAuth1SignatureAgainstOauthlibVector(unittest.TestCase):
    """oauthlib's own checked-in unit test
    (tests/oauth1/rfc5849/test_signatures.py::test_sign_hmac_sha1_with_client,
    fetched from oauthlib's GitHub source on 2026-08-24) reuses the exact
    same RFC 5849 Section 3.1 base string as above, with its own secret
    pair, and asserts a specific HMAC-SHA1 output. Reproducing that output
    here, from our own signing code, is an independent, executable check
    that _oauth1_sign implements RFC 5849 3.4.2 correctly -- not merely
    that it agrees with itself."""

    BASE_STRING = (
        "POST&http%3A%2F%2Fexample.com%2Frequest&a2%3Dr%2520b%26a3%3D2%2520q"
        "%26a3%3Da%26b5%3D%253D%25253D%26c%2540%3D%26c2%3D%26oauth_consumer_"
        "key%3D9djdj82h48djs9d2%26oauth_nonce%3D7d8f3e4a%26oauth_signature_m"
        "ethod%3DHMAC-SHA1%26oauth_timestamp%3D137131201%26oauth_token%3Dkkk"
        "9d7dh3k39sjv7"
    )

    def test_signature_matches_oauthlib_fixture(self):
        signature = gc._oauth1_sign(
            self.BASE_STRING,
            "ECrDNoq1VYzzzzzzzzzyAK7TwZNtPnkqatqZZZZ",
            "just-a-string    asdasd",
        )
        self.assertEqual(signature, "wsdNmjGB7lvis0UJuPAmjvX/PXw=")


class TestBuildOAuth1AuthorizationHeader(unittest.TestCase):
    """Sanity checks on the end-to-end header builder used for the two
    real Garmin requests that need OAuth1 signing."""

    def test_header_contains_all_required_oauth_fields(self):
        header = gc.build_oauth1_authorization_header(
            "GET",
            "https://connectapi.garmin.com/oauth-service/oauth/preauthorized?ticket=abc123",
            "consumer-key",
            "consumer-secret",
        )
        self.assertTrue(header.startswith("OAuth "))
        for field in (
            "oauth_consumer_key",
            "oauth_nonce",
            "oauth_signature_method",
            "oauth_timestamp",
            "oauth_version",
            "oauth_signature",
        ):
            self.assertIn(field, header)
        # No token was supplied (2-legged case) -- oauth_token must be absent.
        self.assertNotIn("oauth_token=", header)

    def test_header_includes_token_when_supplied(self):
        header = gc.build_oauth1_authorization_header(
            "POST",
            "https://connectapi.garmin.com/oauth-service/oauth/exchange/user/2.0",
            "consumer-key",
            "consumer-secret",
            token="tok",
            token_secret="tok-secret",
            body_params={"audience": "GARMIN_CONNECT_MOBILE_ANDROID_DI"},
        )
        self.assertIn("oauth_token=", header)

    def test_two_calls_produce_different_nonces_and_signatures(self):
        # Every request must get a fresh nonce -- replay protection.
        header1 = gc.build_oauth1_authorization_header(
            "GET", "https://connectapi.garmin.com/x", "k", "s"
        )
        header2 = gc.build_oauth1_authorization_header(
            "GET", "https://connectapi.garmin.com/x", "k", "s"
        )
        self.assertNotEqual(header1, header2)


class TestShapeSleep(unittest.TestCase):
    def test_typical_response(self):
        raw = {
            "dailySleepDTO": {
                "calendarDate": "2026-08-23",
                "deepSleepSeconds": 3600,
                "lightSleepSeconds": 8100,
                "remSleepSeconds": 2700,
                "awakeSleepSeconds": 600,
                "awakeCount": 1,
                "sleepScores": {"overall": {"value": 76}},
                "averageSpO2Value": 92.2,
                "lowestSpO2Value": 85,
                "averageRespirationValue": 15.2,
                "avgSleepStress": 23.6,
            }
        }
        shaped = gc.shape_sleep(raw)
        self.assertEqual(shaped["total_sleep_seconds"], 3600 + 8100 + 2700)
        self.assertEqual(shaped["awakenings_count"], 1)
        self.assertEqual(shaped["overall_sleep_score"], 76)
        # efficiency = sleep / (sleep + awake) = 14400 / 15000
        self.assertAlmostEqual(shaped["efficiency_pct"], 96.0, places=1)

    def test_missing_response_returns_empty_dict(self):
        # None and {} are both "nothing to shape" -- an empty dict is falsy
        # in Python, so shape_sleep's `if not raw` guard catches both.
        self.assertEqual(gc.shape_sleep(None), {})
        self.assertEqual(gc.shape_sleep({}), {})

    def test_never_raises_on_missing_fields(self):
        # Partial/unexpected shape should degrade to None fields, not crash.
        shaped = gc.shape_sleep({"dailySleepDTO": {"calendarDate": "2026-08-23"}})
        self.assertEqual(shaped["calendar_date"], "2026-08-23")
        self.assertIsNone(shaped["total_sleep_seconds"])
        self.assertIsNone(shaped["efficiency_pct"])
        self.assertIsNone(shaped["sleep_need_baseline_minutes"])
        self.assertIsNone(shaped["sleep_need_actual_minutes"])
        self.assertIsNone(shaped["sleep_need_feedback"])

    def test_sleep_need_extracted_from_real_shape(self):
        # Real field name/shape confirmed via a live dailySleepData call on
        # 2026-08-26 -- dailySleepDTO.sleepNeed.{baseline,actual,feedback},
        # values in minutes. This fixture is that real response's sleepNeed
        # block verbatim.
        raw = {
            "dailySleepDTO": {
                "calendarDate": "2026-08-26",
                "sleepNeed": {
                    "baseline": 420,
                    "actual": 390,
                    "feedback": "DECREASED",
                    "trainingFeedback": "NO_CHANGE",
                },
            }
        }
        shaped = gc.shape_sleep(raw)
        self.assertEqual(shaped["sleep_need_baseline_minutes"], 420)
        self.assertEqual(shaped["sleep_need_actual_minutes"], 390)
        self.assertEqual(shaped["sleep_need_feedback"], "DECREASED")


class TestShapeHRV(unittest.TestCase):
    def test_typical_response(self):
        raw = {
            "hrvSummary": {
                "calendarDate": "2026-08-23",
                "status": "LOW",
                "lastNightAvg": 36,
                "weeklyAvg": 45,
                "baseline": {"balancedLow": 49, "balancedUpper": 62},
            }
        }
        shaped = gc.shape_hrv(raw)
        self.assertEqual(shaped["last_night_avg_ms"], 36)
        self.assertEqual(shaped["weekly_avg_ms"], 45)
        self.assertEqual(shaped["baseline_low_ms"], 49)
        self.assertEqual(shaped["baseline_high_ms"], 62)
        self.assertEqual(shaped["status"], "LOW")

    def test_missing_response(self):
        self.assertEqual(gc.shape_hrv(None), {})
        self.assertEqual(gc.shape_hrv({}), {})


class TestShapeDailySummary(unittest.TestCase):
    def test_typical_response(self):
        raw = {
            "calendarDate": "2026-08-22",
            "restingHeartRate": 52,
            "lastSevenDaysAvgRestingHeartRate": 53,
            "averageStressLevel": 32.8,
            "maxStressLevel": 71,
            "totalSteps": 17241,
            "averageSpO2": 92.5,
            "lowestSpO2": 84,
        }
        shaped = gc.shape_daily_summary(raw)
        self.assertEqual(shaped["resting_hr"], 52)
        self.assertEqual(shaped["steps"], 17241)
        self.assertEqual(shaped["avg_stress"], 32.8)
        self.assertEqual(shaped["avg_spo2"], 92.5)

    def test_missing_response(self):
        self.assertEqual(gc.shape_daily_summary(None), {})


class TestShapeVo2max(unittest.TestCase):
    def test_list_response_with_generic_block(self):
        raw = [
            {"generic": {"calendarDate": "2026-08-22", "vo2MaxValue": 53, "vo2MaxPreciseValue": 53.4}}
        ]
        shaped = gc.shape_vo2max(raw)
        self.assertEqual(shaped["vo2max"], 53.4)
        self.assertEqual(shaped["calendar_date"], "2026-08-22")

    def test_no_update_returns_empty_dict(self):
        # "no update" days: generic block present but no vo2max fields, or
        # an empty list -- either way, nothing to report, not a crash.
        self.assertEqual(gc.shape_vo2max([]), {})
        self.assertEqual(gc.shape_vo2max([{"generic": {}}]), {})
        self.assertEqual(gc.shape_vo2max(None), {})

    def test_dict_response_also_handled(self):
        raw = {"generic": {"calendarDate": "2026-08-22", "vo2MaxValue": 53}}
        shaped = gc.shape_vo2max(raw)
        self.assertEqual(shaped["vo2max"], 53)


class TestShapeActivity(unittest.TestCase):
    def test_typical_response(self):
        raw = {
            "activityId": 12345678901,
            "activityName": "Zurich Tempo Run",
            "activityType": {"typeId": 1, "typeKey": "running"},
            "startTimeLocal": "2026-08-30 07:15:00",
            "startTimeGMT": "2026-08-30 05:15:00",
            "distance": 10234.5,
            "duration": 2705.0,
            "elapsedDuration": 2710.0,
            "movingDuration": 2700.0,
            "elevationGain": 88.0,
            "elevationLoss": 90.0,
            "averageSpeed": 3.78,
            "maxSpeed": 5.1,
            "calories": 712.0,
            "averageHR": 158,
            "maxHR": 179,
            "steps": 9120,
        }
        shaped = gc.shape_activity(raw)
        self.assertEqual(shaped["activity_id"], 12345678901)
        self.assertEqual(shaped["activity_name"], "Zurich Tempo Run")
        self.assertEqual(shaped["activity_type"], "running")
        self.assertEqual(shaped["start_time_local"], "2026-08-30 07:15:00")
        self.assertEqual(shaped["distance_m"], 10234.5)
        self.assertEqual(shaped["average_hr"], 158)
        self.assertEqual(shaped["max_hr"], 179)

    def test_missing_response_returns_empty_dict(self):
        self.assertEqual(gc.shape_activity(None), {})
        self.assertEqual(gc.shape_activity({}), {})

    def test_never_raises_on_missing_fields(self):
        shaped = gc.shape_activity({"activityId": 1})
        self.assertEqual(shaped["activity_id"], 1)
        self.assertIsNone(shaped["activity_type"])
        self.assertIsNone(shaped["distance_m"])


class _StubActivitiesSession(gc.GarminSession):
    """A GarminSession whose fetch_activities() is stubbed with canned
    pages instead of a real connectapi() call, so
    fetch_activities_since()'s paging/stop logic can be tested without any
    network access."""

    def __init__(self, pages: list[list[dict]]):
        super().__init__()
        self._pages = pages
        self.calls: list[tuple[int, int]] = []

    def fetch_activities(self, *, start: int = 0, limit: int = 20) -> list:
        self.calls.append((start, limit))
        page_index = start // limit
        if page_index >= len(self._pages):
            return []
        return self._pages[page_index]


def _activity(activity_id: int, day: str) -> dict:
    return {"activityId": activity_id, "startTimeLocal": f"{day} 07:00:00"}


class TestFetchActivitiesSince(unittest.TestCase):
    def test_stops_at_cutoff_date(self):
        pages = [[
            _activity(3, "2026-08-30"),
            _activity(2, "2026-08-29"),
            _activity(1, "2026-08-27"),  # older than cutoff -- excluded
        ]]
        session = _StubActivitiesSession(pages)
        result = session.fetch_activities_since("2026-08-29", page_size=20, pause=0)
        self.assertEqual([a["activityId"] for a in result], [3, 2])
        self.assertEqual(session.calls, [(0, 20)])  # one page, no need to page further

    def test_pages_when_first_page_full_and_still_in_range(self):
        page0 = [_activity(i, "2026-08-30") for i in range(3, 1, -1)]  # 2 entries, size < page_size
        pages = [page0]
        session = _StubActivitiesSession(pages)
        result = session.fetch_activities_since("2026-08-01", page_size=2, pause=0)
        # page0 has exactly page_size entries and none is older than cutoff,
        # so a second page is requested; the stub has none -> empty -> stop.
        self.assertEqual(len(session.calls), 2)
        self.assertEqual([a["activityId"] for a in result], [3, 2])

    def test_empty_result_when_no_activities(self):
        session = _StubActivitiesSession([[]])
        result = session.fetch_activities_since("2026-08-01", pause=0)
        self.assertEqual(result, [])
        self.assertFalse(session.activity_paging_capped)

    def test_max_pages_is_a_hard_stop(self):
        # Every page is exactly page_size long and always in range, so
        # without max_pages this would page forever.
        pages = [[_activity(i, "2026-08-30")] for i in range(50)]
        session = _StubActivitiesSession(pages)
        result = session.fetch_activities_since("2026-08-01", page_size=1, max_pages=5, pause=0)
        self.assertEqual(len(session.calls), 5)
        self.assertEqual(len(result), 5)


class TestErrorStatus(unittest.TestCase):
    def test_http_error_carries_status_and_retry_after(self):
        exc = gc._http_error(gc.GarminAPIError, "connectapi /x failed", 429, {"Retry-After": "120"}, b"slow down")
        self.assertIsInstance(exc, gc.GarminAPIError)
        self.assertEqual((exc.status, exc.retry_after), (429, 120))
        self.assertTrue(exc.rate_limited)
        self.assertFalse(exc.permanent)
        self.assertIn("HTTP 429", str(exc))

    def test_permanent_means_4xx_but_not_429(self):
        self.assertTrue(gc._http_error(gc.GarminAPIError, "c", 404, {}, b"").permanent)
        self.assertTrue(gc._http_error(gc.GarminAPIError, "c", 403, {}, b"").permanent)
        self.assertFalse(gc._http_error(gc.GarminAPIError, "c", 500, {}, b"").permanent)
        self.assertFalse(gc._http_error(gc.GarminAPIError, "c", 429, {}, b"").permanent)
        self.assertFalse(gc.GarminConnectError("could not reach host").permanent)
        self.assertIsNone(gc.GarminConnectError("could not reach host").status)

    def test_retry_after_header_is_case_insensitive_and_tolerant(self):
        self.assertEqual(gc._retry_after({"retry-after": "5"}), 5)
        self.assertEqual(gc._retry_after({"Retry-After": "2.9"}), 2)
        self.assertIsNone(gc._retry_after({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}))
        self.assertIsNone(gc._retry_after({}))

    def test_connectapi_raises_with_status(self):
        session = gc.GarminSession()
        session.oauth2_token = gc.OAuth2Token("t", "Bearer", "r", int(gc.time.time()) + 3600, 0)
        session._request = lambda *a, **k: (404, {}, b"nope")
        with self.assertRaises(gc.GarminAPIError) as ctx:
            session.connectapi("/x")
        self.assertEqual(ctx.exception.status, 404)


class TestLoadCredentials(unittest.TestCase):
    def test_env_vars_win_over_missing_config(self):
        import os
        from pathlib import Path

        os.environ["GARMIN_USERNAME"] = "test@example.com"
        os.environ["GARMIN_PASSWORD"] = "hunter2"
        try:
            username, password = gc.load_credentials(path=Path("/nonexistent/credentials.toml"))
            self.assertEqual(username, "test@example.com")
            self.assertEqual(password, "hunter2")
        finally:
            del os.environ["GARMIN_USERNAME"]
            del os.environ["GARMIN_PASSWORD"]

    def test_missing_config_and_missing_env_raises(self):
        import os
        from pathlib import Path

        os.environ.pop("GARMIN_USERNAME", None)
        os.environ.pop("GARMIN_PASSWORD", None)
        with self.assertRaises(gc.GarminConfigError):
            gc.load_credentials(path=Path("/nonexistent/credentials.toml"))

    def test_config_missing_garmin_section_raises(self):
        import os
        import tempfile
        from pathlib import Path

        os.environ.pop("GARMIN_USERNAME", None)
        os.environ.pop("GARMIN_PASSWORD", None)
        with tempfile.NamedTemporaryFile(suffix=".toml", delete=False, mode="w") as f:
            f.write('[other_section]\nkey = "value"\n')
            path = Path(f.name)
        try:
            with self.assertRaises(gc.GarminConfigError):
                gc.load_credentials(path=path)
        finally:
            path.unlink()


class TestSessionCaching(unittest.TestCase):
    """save_session()/load_session() round-tripped against synthetic token
    data only -- no network, no real Garmin account, no credentials file.
    This is the whole point: caching logic is plain file I/O + a couple of
    time comparisons, fully testable without ever touching Garmin's
    servers (which is explicitly NOT done in this task -- see the build
    report)."""

    def setUp(self):
        import tempfile

        self._tmpdir = tempfile.TemporaryDirectory()
        self.cache_path = gc.Path(self._tmpdir.name) / "session-cache-test.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _fake_session(self, *, access_expires_in=3600, refresh_expires_in=7776000):
        """A GarminSession with synthetic-but-well-formed tokens, as if a
        real login had just completed."""
        session = gc.GarminSession()
        session.oauth1_token = gc.OAuth1Token(
            oauth_token="fake-oauth1-token", oauth_token_secret="fake-oauth1-secret"
        )
        now = int(gc.time.time())
        session.oauth2_token = gc.OAuth2Token(
            access_token="fake-access-token",
            token_type="Bearer",
            refresh_token="fake-refresh-token",
            expires_at=now + access_expires_in,
            refresh_token_expires_at=now + refresh_expires_in,
            scope="",
        )
        session._username = "fake_garmin_username"
        return session

    def test_round_trip_preserves_tokens(self):
        session = self._fake_session()
        gc.save_session(session, self.cache_path)
        restored = gc.load_session(self.cache_path)

        self.assertIsNotNone(restored)
        self.assertEqual(restored.oauth1_token.oauth_token, "fake-oauth1-token")
        self.assertEqual(restored.oauth1_token.oauth_token_secret, "fake-oauth1-secret")
        self.assertEqual(restored.oauth2_token.access_token, "fake-access-token")
        self.assertEqual(restored.oauth2_token.refresh_token, "fake-refresh-token")
        self.assertEqual(restored._username, "fake_garmin_username")

    def test_saved_file_is_chmod_600(self):
        import stat

        session = self._fake_session()
        gc.save_session(session, self.cache_path)
        mode = stat.S_IMODE(self.cache_path.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_load_session_missing_file_returns_none(self):
        missing = self.cache_path.parent / "does-not-exist.json"
        self.assertIsNone(gc.load_session(missing))

    def test_load_session_corrupt_json_returns_none(self):
        self.cache_path.write_text("{not valid json", encoding="utf-8")
        self.assertIsNone(gc.load_session(self.cache_path))

    def test_load_session_missing_fields_returns_none(self):
        self.cache_path.write_text(
            gc.json.dumps({"cache_version": gc.SESSION_CACHE_VERSION, "oauth1": {}}),
            encoding="utf-8",
        )
        self.assertIsNone(gc.load_session(self.cache_path))

    def test_load_session_wrong_cache_version_returns_none(self):
        session = self._fake_session()
        gc.save_session(session, self.cache_path)
        data = gc.json.loads(self.cache_path.read_text(encoding="utf-8"))
        data["cache_version"] = 999
        self.cache_path.write_text(gc.json.dumps(data), encoding="utf-8")
        self.assertIsNone(gc.load_session(self.cache_path))

    def test_load_session_with_expired_refresh_token_is_still_usable(self):
        # The OAuth2 refresh token is never used: refresh_oauth2() re-mints
        # the access token from the OAuth1 token (about a year). A cache
        # whose refresh_token_expires_at is in the past must therefore NOT
        # be refused -- refusing it was what forced a monthly re-login.
        session = self._fake_session(access_expires_in=-100, refresh_expires_in=-10)
        gc.save_session(session, self.cache_path)
        restored = gc.load_session(self.cache_path)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.oauth1_token.oauth_token, "fake-oauth1-token")

    def test_saved_file_is_written_atomically(self):
        # No temp file left behind, and the target is the complete document.
        session = self._fake_session()
        gc.save_session(session, self.cache_path)
        self.assertEqual([p.name for p in self.cache_path.parent.iterdir()], [self.cache_path.name])
        self.assertIn("oauth1", gc.json.loads(self.cache_path.read_text()))

    def test_credentials_file_is_0600_and_atomic(self):
        import stat
        path = self.cache_path.parent / "credentials.toml"
        gc.save_credentials("me@example.com", 'p"w', path)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["credentials.toml"])
        self.assertEqual(gc.load_credentials(path), ("me@example.com", 'p"w'))

    def test_load_session_with_expired_access_but_live_refresh_is_usable(self):
        # This is the case caching exists for: access token expired, but
        # the refresh token (and therefore the OAuth1 token pair used to
        # re-mint it) is still good. load_session() must NOT refuse this --
        # GarminSession.connectapi() is what lazily refreshes it later.
        session = self._fake_session(access_expires_in=-100, refresh_expires_in=7776000)
        gc.save_session(session, self.cache_path)
        restored = gc.load_session(self.cache_path)
        self.assertIsNotNone(restored)
        self.assertTrue(restored.oauth2_token.expired)
        self.assertFalse(restored.oauth2_token.refresh_expired)

    def test_cache_file_never_deleted_on_refusal(self):
        # This project never deletes without being asked -- confirm a
        # refused cache (wrong version) is left on disk untouched, not
        # removed as a side effect of load_session() refusing it.
        self.cache_path.write_text(gc.json.dumps({"cache_version": 999}), encoding="utf-8")
        self.assertIsNone(gc.load_session(self.cache_path))
        self.assertTrue(self.cache_path.is_file())

    def test_save_session_without_login_raises(self):
        session = gc.GarminSession()  # never logged in -- no tokens
        with self.assertRaises(gc.GarminAuthError):
            gc.save_session(session, self.cache_path)

    def test_save_session_overwrites_previous_cache(self):
        session1 = self._fake_session()
        gc.save_session(session1, self.cache_path)

        session2 = self._fake_session()
        session2.oauth2_token.access_token = "second-fake-access-token"
        gc.save_session(session2, self.cache_path)

        restored = gc.load_session(self.cache_path)
        self.assertEqual(restored.oauth2_token.access_token, "second-fake-access-token")

    def test_open_session_refreshes_an_expired_access_token_up_front(self):
        session = self._fake_session(access_expires_in=-100, refresh_expires_in=-10)
        gc.save_session(session, self.cache_path)
        calls = []

        def fake_refresh(self_):
            calls.append("refresh")
            self_.oauth2_token.expires_at = int(gc.time.time()) + 3600

        original = gc.GarminSession.refresh_oauth2
        gc.GarminSession.refresh_oauth2 = fake_refresh
        try:
            restored = gc.open_session(cache_path=self.cache_path, interactive=False, log=lambda _m: None)
        finally:
            gc.GarminSession.refresh_oauth2 = original
        self.assertEqual(calls, ["refresh"])
        self.assertFalse(restored.oauth2_token.expired)
        # and the refreshed token was written back
        self.assertFalse(gc.load_session(self.cache_path).oauth2_token.expired)

    def test_open_session_falls_back_to_login_when_refresh_is_refused(self):
        session = self._fake_session(access_expires_in=-100)
        gc.save_session(session, self.cache_path)
        events = []

        def fake_refresh(self_):
            raise gc.GarminAuthError("OAuth1->OAuth2 token exchange failed: HTTP 401", status=401)

        def fake_login(self_, username, password, *, mfa_code=None):
            events.append(("login", username))
            self_.oauth1_token = gc.OAuth1Token("new-o1", "new-s")
            self_.oauth2_token = gc.OAuth2Token("new-access", "Bearer", "r", int(gc.time.time()) + 3600,
                                                int(gc.time.time()) + 3600)

        originals = gc.GarminSession.refresh_oauth2, gc.GarminSession.login
        gc.GarminSession.refresh_oauth2, gc.GarminSession.login = fake_refresh, fake_login
        try:
            restored = gc.open_session(cache_path=self.cache_path, interactive=False,
                                       credentials=("me@example.com", "pw"), log=lambda _m: None)
        finally:
            gc.GarminSession.refresh_oauth2, gc.GarminSession.login = originals
        self.assertEqual(events, [("login", "me@example.com")])
        self.assertEqual(restored.oauth2_token.access_token, "new-access")
        self.assertEqual(gc.load_session(self.cache_path).oauth1_token.oauth_token, "new-o1")

    def test_open_session_does_not_log_in_when_refresh_is_rate_limited(self):
        session = self._fake_session(access_expires_in=-100)
        gc.save_session(session, self.cache_path)

        def fake_refresh(self_):
            raise gc.GarminAuthError("HTTP 429", status=429, retry_after=90)

        original = gc.GarminSession.refresh_oauth2
        gc.GarminSession.refresh_oauth2 = fake_refresh
        try:
            with self.assertRaises(gc.GarminAuthError) as ctx:
                gc.open_session(cache_path=self.cache_path, interactive=False,
                                credentials=("me@example.com", "pw"), log=lambda _m: None)
        finally:
            gc.GarminSession.refresh_oauth2 = original
        self.assertTrue(ctx.exception.rate_limited)
        self.assertEqual(ctx.exception.retry_after, 90)

    def test_mfa_token_is_not_persisted(self):
        # oauth1.mfa_token is a one-shot login value, deliberately excluded
        # from the cache (see save_session()'s docstring / inline comment).
        session = self._fake_session()
        session.oauth1_token.mfa_token = "should-not-survive-a-cache-round-trip"
        gc.save_session(session, self.cache_path)
        raw = gc.json.loads(self.cache_path.read_text(encoding="utf-8"))
        self.assertNotIn("mfa_token", raw["oauth1"])
        restored = gc.load_session(self.cache_path)
        self.assertIsNone(restored.oauth1_token.mfa_token)


if __name__ == "__main__":
    unittest.main()
