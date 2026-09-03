"""Flattening tests for garmin_detail.py, using the response shapes observed on a
live account on 2026-09-03, plus the ways those shapes could change. No network."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import garmin_detail as gd  # noqa: E402

DAY = "2026-09-02"
RAW = {
    "heart_rate": {"minHeartRate": 44, "maxHeartRate": 144, "userProfilePK": 1,
                   "heartRateValueDescriptors": [{"index": 0, "key": "timestamp"}, {"index": 1, "key": "heartrate"}],
                   "heartRateValues": [[1788300000000, 57], [1788300120000, None], [1788300240000, 58]]},
    "stress": {"maxStressLevel": 97, "avgStressLevel": 26,
               "stressValueDescriptorsDTOList": [{"index": 0, "key": "timestamp"}, {"index": 1, "key": "stressLevel"}],
               "stressValuesArray": [[1788300000000, 21], [1788300180000, -1]],
               "bodyBatteryValueDescriptorsDTOList": [
                   {"bodyBatteryValueDescriptorIndex": 0, "bodyBatteryValueDescriptorKey": "timestamp"},
                   {"bodyBatteryValueDescriptorIndex": 1, "bodyBatteryValueDescriptorKey": "bodyBatteryStatus"},
                   {"bodyBatteryValueDescriptorIndex": 2, "bodyBatteryValueDescriptorKey": "bodyBatteryLevel"},
                   {"bodyBatteryValueDescriptorIndex": 3, "bodyBatteryValueDescriptorKey": "bodyBatteryVersion"}],
               "bodyBatteryValuesArray": [[1788300000000, "MEASURED", 12, 3.0]]},
    "body_battery": [{"date": DAY, "charged": 89, "drained": 68,
                      "bodyBatteryValueDescriptorDTOList": [
                          {"bodyBatteryValueDescriptorIndex": 0, "bodyBatteryValueDescriptorKey": "timestamp"},
                          {"bodyBatteryValueDescriptorIndex": 1, "bodyBatteryValueDescriptorKey": "bodyBatteryLevel"}],
                      "bodyBatteryValuesArray": [[1788300000000, 12]],
                      "bodyBatteryActivityEvent": [{"eventType": "SLEEP", "eventStartTimeGmt": "2026-09-01T21:15:50.0",
                                                    "bodyBatteryImpact": 82, "durationInMilliseconds": 29940000}]}],
    "steps": [{"startGMT": "2026-09-01T22:00:00.0", "endGMT": "2026-09-01T22:15:00.0", "steps": 0,
               "pushes": 0, "primaryActivityLevel": "sedentary", "activityLevelConstant": True}],
    "respiration": {"respirationValueDescriptorsDTOList": [{"index": 0, "key": "timestamp"}, {"index": 1, "key": "respiration"}],
                    "respirationValuesArray": [[1788300120000, 17.0]],
                    "respirationAveragesValueDescriptorDTOList": [
                        {"respirationAveragesValueDescriptorIndex": 0, "respirationAveragesValueDescriptionKey": "timestamp"},
                        {"respirationAveragesValueDescriptorIndex": 1, "respirationAveragesValueDescriptionKey": "respirationAverageValue"},
                        {"respirationAveragesValueDescriptorIndex": 2, "respirationAveragesValueDescriptionKey": "respirationHighValue"},
                        {"respirationAveragesValueDescriptorIndex": 3, "respirationAveragesValueDescriptionKey": "respirationLowValue"}],
                    "respirationAveragesValuesArray": [[1788303600000, 16.78, 19.0, 15.0]]},
    "spo2": {"averageSpO2": 93, "spO2HourlyAverages": [[1788300000000, 92]]},
    "intensity_minutes": {"weeklyTotal": 40, "imValuesArray": [[1788367499999, 2]]},
    "hrv": {"hrvSummary": {"weeklyAvg": 52, "lastNightAvg": 53, "status": "BALANCED", "baseline": {"balancedLow": 48}},
            "hrvReadings": [{"hrvValue": 28, "readingTimeGMT": "2026-09-01T21:19:55.0", "readingTimeLocal": "2026-09-01T23:19:55.0"}]},
    "sleep": {"dailySleepDTO": {"deepSleepSeconds": 5280, "sleepScores": {"overall": {"value": 94}}},
              "sleepLevels": [{"startGMT": "2026-09-01T21:15:50.0", "endGMT": "2026-09-01T21:17:50.0", "activityLevel": 1.0}],
              "sleepMovement": [{"startGMT": "2026-09-01T20:15:00.0", "endGMT": "2026-09-01T20:16:00.0", "activityLevel": 5.5}],
              "sleepHeartRate": [{"value": 69, "startGMT": 1788297360000}],
              "sleepStress": [{"value": 31, "startGMT": 1788297300000}],
              "sleepBodyBattery": [{"value": 9, "startGMT": 1788297300000}],
              "hrvData": [{"value": 28.0, "startGMT": 1788297595000}],
              "sleepRestlessMoments": [{"value": 1, "startGMT": 1788298430000}],
              "wellnessEpochRespirationDataDTOList": [{"startTimeGMT": 1788297350000, "respirationValue": 17.0}],
              "wellnessEpochSPO2DataDTOList": [{"userProfilePK": 1, "epochTimestamp": "2026-09-01T21:16:00.0", "deviceId": 3,
                                                "calendarDate": "2026-09-01T00:00:00.0", "epochDuration": 60, "spo2Reading": 95}]},
    "training_readiness": [{"userProfilePK": 1, "calendarDate": DAY, "timestamp": "2026-09-02T17:47:37.0", "deviceId": 3,
                            "level": "MODERATE", "feedbackLong": "x", "feedbackShort": "y", "score": 52, "sleepScore": 94,
                            "recoveryTime": 1000, "acuteLoad": 473},
                           {"userProfilePK": 1, "calendarDate": DAY, "timestamp": "2026-09-02T07:00:00.0", "deviceId": 3,
                            "level": "LOW", "feedbackLong": "x", "feedbackShort": "y", "score": 30, "sleepScore": 94,
                            "recoveryTime": 1400, "acuteLoad": 470}],
    "training_status": {"userId": 1, "mostRecentVO2Max": {"generic": {"vo2MaxPreciseValue": 51.9, "vo2MaxValue": 52.0}},
                        "mostRecentTrainingStatus": {"latestTrainingStatusData": {
                            "3489799164": {"trainingStatusFeedbackPhrase": "MAINTAINING_2", "weeklyTrainingLoad": 473}}}},
    "hydration": {"userId": 1, "calendarDate": DAY, "valueInML": 0.0, "goalInML": 3373.0, "sweatLossInML": 573.0},
    "endurance_score": {"overallScore": 6072, "classification": 3, "primaryTrainingDevice": True},
    "fitness_age": {"fitnessAge": 28.4, "chronologicalAge": 31, "components": {"vo2Max": {"value": 51.9}}},
}


class TestTimeline(unittest.TestCase):
    def setUp(self):
        self.rows = gd.flatten_timeline(DAY, RAW)
        self.by_series: dict[str, list[dict]] = {}
        for row in self.rows:
            self.by_series.setdefault(row["series"], []).append(row)

    def test_known_series_get_friendly_names(self):
        expected = {"heart_rate_bpm", "stress_level", "body_battery_level", "body_battery_report_level",
                    "body_battery_event", "steps_15min", "respiration_brpm", "respiration_hourly_avg",
                    "spo2_hourly_avg_pct", "intensity_minutes", "hrv_reading_ms", "sleep_stage_level",
                    "sleep_movement", "sleep_heart_rate_bpm", "sleep_stress_level", "sleep_body_battery_level",
                    "sleep_hrv_ms", "sleep_restless", "sleep_respiration_brpm", "sleep_spo2_pct"}
        self.assertEqual(set(self.by_series), expected)

    def test_null_values_are_dropped_and_timestamps_are_iso(self):
        hr = self.by_series["heart_rate_bpm"]
        self.assertEqual([r["value"] for r in hr], [57, 58])
        self.assertEqual(hr[0]["time_gmt"], "2026-09-01T22:00:00Z")

    def test_descriptors_pick_level_and_status_columns(self):
        bb = self.by_series["body_battery_level"][0]
        self.assertEqual((bb["value"], bb["value2"]), (12, "MEASURED"))
        avg = self.by_series["respiration_hourly_avg"][0]
        self.assertEqual(avg["value"], 16.78)

    def test_dict_lists(self):
        self.assertEqual(self.by_series["hrv_reading_ms"][0]["time_gmt"], "2026-09-01T21:19:55Z")
        steps = self.by_series["steps_15min"][0]
        self.assertEqual((steps["value"], steps["value2"]), (0, "sedentary"))
        event = self.by_series["body_battery_event"][0]
        self.assertEqual((event["value"], event["value2"]), (82, "SLEEP"))
        stage = self.by_series["sleep_stage_level"][0]
        self.assertEqual(stage["value2"], "2026-09-01T21:17:50Z")  # end of the stage, no text field to take

    def test_every_row_has_the_same_columns(self):
        for row in self.rows:
            self.assertEqual(set(row), {"date", "series", "time_gmt", "value", "value2"})


class TestAdaptsToChangedShapes(unittest.TestCase):
    def test_reordered_columns_follow_the_descriptors(self):
        hr = {"heartRateValueDescriptors": [{"index": 0, "key": "heartrate"}, {"index": 1, "key": "timestamp"}],
              "heartRateValues": [[57, 1788300000000], [58, 1788300120000]]}
        rows = gd.flatten_endpoint(DAY, "heart_rate", hr)[0]
        self.assertEqual([(r["time_gmt"], r["value"]) for r in rows],
                         [("2026-09-01T22:00:00Z", 57), ("2026-09-01T22:02:00Z", 58)])

    def test_extra_column_is_ignored_missing_descriptors_assume_ts_value(self):
        rows = gd.flatten_endpoint(DAY, "heart_rate", {"heartRateValues": [[1788300000000, 60, "extra"]]})[0]
        self.assertEqual(rows[0]["value"], 60)

    def test_unknown_array_gets_a_generated_name(self):
        raw = {"newThingValues": [[1788300000000, 5]], "newThingValueDescriptors": [{"index": 0, "key": "timestamp"},
                                                                                     {"index": 1, "key": "newThingLevel"}]}
        rows = gd.flatten_endpoint(DAY, "stress", raw)[0]
        self.assertEqual(rows[0]["series"], "stress_new_thing_values")
        self.assertEqual(rows[0]["value"], 5)

    def test_new_scalar_shows_up_as_a_snapshot(self):
        snap = {r["metric"]: r["value"] for r in gd.flatten_endpoint(DAY, "hydration", {"valueInML": 1, "newMetric": 7})[1]}
        self.assertEqual(snap["hydration_ml"], 1)
        self.assertEqual(snap["hydration_new_metric"], 7)

    def test_garbage_flattens_to_nothing_not_a_crash(self):
        for bad in ({}, {"heartRateValues": "nope"}, [1, 2, 3], "text", None, {"x": [[None, None]]}):
            self.assertEqual(gd.flatten_endpoint(DAY, "heart_rate", bad)[0], [])


class TestNeverGuessesTheValueColumn(unittest.TestCase):
    def test_two_numeric_columns_without_a_value_name_are_left_out_and_reported(self):
        raw = {"thingValues": [[1788300000000, 5, 9]],
               "thingValueDescriptors": [{"index": 0, "key": "timestamp"}, {"index": 1, "key": "alpha"},
                                         {"index": 2, "key": "beta"}]}
        notes = []
        rows = gd.flatten_endpoint(DAY, "stress", raw, notes)[0]
        self.assertEqual(rows, [])
        self.assertEqual(len(notes), 1)
        self.assertIn("stress.thingValues", notes[0])
        self.assertIn("alpha, beta", notes[0])

    def test_one_numeric_column_is_still_taken_without_a_value_name(self):
        raw = {"thingValues": [[1788300000000, "MEASURED", 7]],
               "thingValueDescriptors": [{"index": 0, "key": "timestamp"}, {"index": 1, "key": "kind"},
                                         {"index": 2, "key": "zeta"}]}
        rows = gd.flatten_endpoint(DAY, "stress", raw)[0]
        self.assertEqual((rows[0]["value"], rows[0]["value2"]), (7, "MEASURED"))

    def test_no_descriptors_and_two_numbers_is_ambiguous(self):
        notes = []
        rows = gd.flatten_endpoint(DAY, "heart_rate", {"heartRateValues": [[1788300000000, 60, 70]]}, notes)[0]
        self.assertEqual(rows, [])
        self.assertTrue(notes and "no descriptors" in notes[0])

    def test_dict_list_with_two_unknown_numbers_is_left_out(self):
        items = [{"startGMT": "2026-09-01T21:15:50.0", "foo": 1, "bar": 2}]
        notes = []
        self.assertEqual(gd.flatten_endpoint(DAY, "sleep", {"newList": items}, notes)[0], [])
        self.assertIn("sleep.newList", notes[0])

    def test_dict_list_with_one_unknown_number_is_taken(self):
        items = [{"startGMT": "2026-09-01T21:15:50.0", "foo": 1, "label": "x"}]
        rows = gd.flatten_endpoint(DAY, "sleep", {"newList": items})[0]
        self.assertEqual((rows[0]["value"], rows[0]["value2"]), (1, "x"))

    def test_flatten_day_collects_notes_once_per_endpoint(self):
        raw = {"heart_rate": {"heartRateValues": [[1788300000000, 60, 70]]}, "hydration": {"valueInML": 3}}
        timeline, snapshots, notes = gd.flatten_day(DAY, raw)
        self.assertEqual(timeline, [])
        self.assertEqual(len(notes), 1)
        self.assertEqual(snapshots[0]["metric"], "hydration_ml")


class TestPerDeviceSnapshots(unittest.TestCase):
    TWO = {"mostRecentTrainingStatus": {"latestTrainingStatusData": {
        "111": {"trainingStatusFeedbackPhrase": "MAINTAINING_2", "weeklyTrainingLoad": 473, "primaryTrainingDevice": False},
        "222": {"trainingStatusFeedbackPhrase": "PRODUCTIVE_1", "weeklyTrainingLoad": 512, "primaryTrainingDevice": True}}}}

    def test_primary_device_keeps_the_plain_name_and_the_other_gets_its_id(self):
        snap = {r["metric"]: r["value"] for r in gd.flatten_endpoint(DAY, "training_status", self.TWO)[1]}
        self.assertEqual(snap["training_status"], "PRODUCTIVE_1")
        self.assertEqual(snap["training_load_7d"], 512)
        self.assertEqual(snap["training_status_device_111_weekly_training_load"], 473)
        self.assertEqual(snap["training_status_device_111_training_status_feedback_phrase"], "MAINTAINING_2")

    def test_first_device_is_primary_when_none_is_flagged(self):
        raw = {"latestTrainingStatusData": {"5": {"weeklyTrainingLoad": 1}, "6": {"weeklyTrainingLoad": 2}}}
        snap = {r["metric"]: r["value"] for r in gd.flatten_endpoint(DAY, "training_status", raw)[1]}
        self.assertEqual(snap["training_status_latest_training_status_data_by_device_weekly_training_load"], 1)
        self.assertEqual(snap["training_status_latest_training_status_data_device_6_weekly_training_load"], 2)

    def test_single_device_is_unchanged(self):
        snap = {r["metric"]: r["value"] for r in gd.flatten_endpoint(DAY, "training_status", RAW["training_status"])[1]}
        self.assertEqual(snap["training_load_7d"], 473)


class TestAliasTables(unittest.TestCase):
    def test_friendly_names_are_unique(self):
        series = list(gd.SERIES_ALIASES.values())
        self.assertEqual(len(series), len(set(series)))
        metrics = list(gd.METRIC_ALIASES.values())
        self.assertEqual(len(metrics), len(set(metrics)))
        # a friendly metric name must not collide with a prefix-shortened generated name
        shortened = {short for _, short in gd.PREFIX_ALIASES}
        self.assertFalse(set(metrics) & shortened)


class _StubSession:
    """Stands in for GarminSession in fetch_detail: answers per endpoint name."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def _garmin_username(self):
        return "u"

    def connectapi(self, path, *, params=None):
        self.calls.append(path)
        for name, (path_fn, _) in gd.ENDPOINTS.items():
            if path_fn(DAY, "u") == path:
                answer = self.answers.get(name, {"ok": 1})
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError(path)


class TestFetchDetailStatuses(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name) / DAY

    def tearDown(self):
        self._tmp.cleanup()

    def test_4xx_is_final_and_not_asked_again(self):
        from garmin_client import GarminAPIError
        session = _StubSession({"fitness_age": GarminAPIError("HTTP 404", status=404),
                                "spo2": GarminAPIError("HTTP 500", status=500)})
        status = gd.fetch_detail(session, DAY, self.out, pause=0)
        self.assertTrue(status["fitness_age"].startswith("none"))
        self.assertTrue(status["spo2"].startswith("error"))
        self.assertFalse(gd.detail_complete(self.out))
        # next run: only the 5xx endpoint is retried, the 404 is remembered
        session.answers["spo2"] = {"averageSpO2": 93}
        session.calls.clear()
        status = gd.fetch_detail(session, DAY, self.out, pause=0)
        self.assertEqual(session.calls, [gd.ENDPOINTS["spo2"][0](DAY, "u")])
        self.assertEqual(status["spo2"], "ok")
        self.assertTrue(status["fitness_age"].startswith("none"))
        self.assertTrue(gd.detail_complete(self.out))

    def test_rate_limit_saves_progress_and_raises(self):
        from garmin_client import GarminAPIError
        session = _StubSession({"steps": GarminAPIError("HTTP 429", status=429, retry_after=30)})
        with self.assertRaises(GarminAPIError):
            gd.fetch_detail(session, DAY, self.out, pause=0)
        index = json.loads((self.out / "_index.json").read_text())
        self.assertEqual(index["heart_rate"], "ok")
        self.assertNotIn("steps", index)


class TestSnapshots(unittest.TestCase):
    def test_metrics_and_aliases(self):
        snap = {r["metric"]: r["value"] for r in gd.flatten_snapshots(DAY, RAW)}
        self.assertEqual(snap["training_readiness_score"], 52)  # newest entry wins
        self.assertEqual(snap["training_readiness_level"], "MODERATE")
        self.assertEqual(snap["training_status"], "MAINTAINING_2")
        self.assertEqual(snap["training_load_7d"], 473)
        self.assertEqual(snap["vo2max_generic"], 51.9)
        self.assertEqual(snap["hydration_goal_ml"], 3373.0)
        self.assertEqual(snap["endurance_score"], 6072)
        self.assertEqual(snap["fitness_age"], 28.4)
        self.assertEqual(snap["body_battery_charged"], 89)
        self.assertEqual(snap["heart_rate_max"], 144)
        self.assertEqual(snap["hrv_last_night_avg"], 53)
        self.assertEqual(snap["hrv_baseline_balanced_low"], 48)
        self.assertEqual(snap["sleep_deep_sleep_seconds"], 5280)
        self.assertEqual(snap["sleep_sleep_scores_overall_value"], 94)

    def test_ids_and_timestamps_are_not_metrics(self):
        metrics = {r["metric"] for r in gd.flatten_snapshots(DAY, RAW)}
        self.assertFalse(any("user_profile" in m or "device_id" in m or "calendar_date" in m for m in metrics), metrics)


class TestSchemaDrift(unittest.TestCase):
    def test_no_drift_against_own_schema(self):
        baseline = {ep: gd.schema_of(resp) for ep, resp in RAW.items()}
        self.assertEqual(gd.schema_drift(RAW, baseline), {})

    def test_added_and_missing_fields_are_reported(self):
        baseline = {ep: gd.schema_of(resp) for ep, resp in RAW.items()}
        changed = json.loads(json.dumps(RAW))
        changed["heart_rate"]["brandNew"] = 1
        del changed["hydration"]["goalInML"]
        drift = gd.schema_drift(changed, baseline)
        self.assertEqual(drift["heart_rate"], {"added": ["brandNew"], "missing": []})
        self.assertEqual(drift["hydration"], {"added": [], "missing": ["goalInML"]})

    def test_descriptor_keys_are_part_of_the_schema(self):
        self.assertIn("heartRateValueDescriptors:heartrate", gd.schema_of(RAW["heart_rate"]))

    def test_shipped_baseline_covers_every_endpoint(self):
        baseline = gd.load_baseline()
        self.assertEqual(set(baseline), set(gd.ENDPOINTS))


class TestDetailComplete(unittest.TestCase):
    def test_index_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self.assertFalse(gd.detail_complete(d))
            (d / "_index.json").write_text(json.dumps({name: "ok" for name in gd.ENDPOINTS}))
            self.assertTrue(gd.detail_complete(d))
            (d / "_index.json").write_text(json.dumps({**{name: "ok" for name in gd.ENDPOINTS}, "spo2": "error: 500"}))
            self.assertFalse(gd.detail_complete(d))


class TestSnake(unittest.TestCase):
    def test_cases(self):
        self.assertEqual(gd.snake("mostRecentVO2Max"), "most_recent_vo2_max")
        self.assertEqual(gd.snake("valueInML"), "value_in_ml")
        self.assertEqual(gd.snake("wellnessEpochSPO2DataDTOList"), "wellness_epoch_spo2_data_dto_list")


if __name__ == "__main__":
    unittest.main()
