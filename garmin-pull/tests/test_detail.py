"""Flattening tests for garmin_detail.py, using the response shapes observed on a
live account on 2026-09-03. No network."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import garmin_detail as gd  # noqa: E402

DAY = "2026-09-02"
RAW = {
    "heart_rate": {"minHeartRate": 44, "maxHeartRate": 144,
                   "heartRateValues": [[1788300000000, 57], [1788300120000, None], [1788300240000, 58]]},
    "stress": {"stressValuesArray": [[1788300000000, 21], [1788300180000, -1]],
               "bodyBatteryValuesArray": [[1788300000000, "MEASURED", 12, 3.0]]},
    "body_battery": [{"charged": 89, "drained": 68,
                      "bodyBatteryActivityEvent": [{"eventType": "SLEEP", "eventStartTimeGmt": "2026-09-01T21:15:50.0",
                                                    "bodyBatteryImpact": 82}]}],
    "steps": [{"startGMT": "2026-09-01T22:00:00.0", "endGMT": "2026-09-01T22:15:00.0", "steps": 0,
               "primaryActivityLevel": "sedentary"}],
    "respiration": {"respirationValuesArray": [[1788300120000, 17.0]],
                    "respirationAveragesValuesArray": [[1788303600000, 16.78, 19.0, 15.0]]},
    "spo2": {"spO2HourlyAverages": [[1788300000000, 92]]},
    "intensity_minutes": {"imValuesArray": [[1788367499999, 2]]},
    "hrv": {"hrvReadings": [{"hrvValue": 28, "readingTimeGMT": "2026-09-01T21:19:55.0"}]},
    "sleep": {"sleepLevels": [{"startGMT": "2026-09-01T21:15:50.0", "endGMT": "2026-09-01T21:17:50.0", "activityLevel": 1.0}],
              "sleepMovement": [{"startGMT": "2026-09-01T20:15:00.0", "endGMT": "2026-09-01T20:16:00.0", "activityLevel": 5.5}],
              "sleepHeartRate": [{"value": 69, "startGMT": 1788297360000}],
              "sleepStress": [{"value": 31, "startGMT": 1788297300000}],
              "sleepBodyBattery": [{"value": 9, "startGMT": 1788297300000}],
              "hrvData": [{"value": 28.0, "startGMT": 1788297595000}],
              "sleepRestlessMoments": [{"value": 1, "startGMT": 1788298430000}],
              "wellnessEpochRespirationDataDTOList": [{"startTimeGMT": 1788297350000, "respirationValue": 17.0}],
              "wellnessEpochSPO2DataDTOList": [{"epochTimestamp": "2026-09-01T21:16:00.0", "spo2Reading": 95}]},
    "training_readiness": [{"score": 52, "level": "MODERATE", "sleepScore": 94, "recoveryTime": 1000}],
    "training_status": {"mostRecentVO2Max": {"generic": {"vo2MaxPreciseValue": 51.9}},
                        "mostRecentTrainingStatus": {"latestTrainingStatusData": {
                            "3489799164": {"trainingStatusFeedbackPhrase": "MAINTAINING_2", "weeklyTrainingLoad": 473}}}},
    "hydration": {"valueInML": 0.0, "goalInML": 3373.0, "sweatLossInML": 573.0},
    "endurance_score": {"overallScore": 6072, "classification": 3},
    "fitness_age": {"fitnessAge": 28.4, "chronologicalAge": 31},
}


class TestTimeline(unittest.TestCase):
    def setUp(self):
        self.rows = gd.flatten_timeline(DAY, RAW)
        self.by_series = {}
        for row in self.rows:
            self.by_series.setdefault(row["series"], []).append(row)

    def test_every_series_present(self):
        expected = {"heart_rate_bpm", "stress_level", "body_battery_level", "body_battery_event", "steps_15min",
                    "respiration_brpm", "respiration_hourly_avg", "spo2_hourly_avg_pct", "intensity_minutes",
                    "hrv_reading_ms", "sleep_stage_level", "sleep_movement", "sleep_heart_rate_bpm",
                    "sleep_stress_level", "sleep_body_battery_level", "sleep_hrv_ms", "sleep_restless",
                    "sleep_respiration_brpm", "sleep_spo2_pct"}
        self.assertEqual(set(self.by_series), expected)

    def test_null_values_are_dropped_and_timestamps_are_iso(self):
        hr = self.by_series["heart_rate_bpm"]
        self.assertEqual([r["value"] for r in hr], [57, 58])
        self.assertEqual(hr[0]["time_gmt"], "2026-09-01T22:00:00Z")

    def test_body_battery_keeps_status_in_value2(self):
        bb = self.by_series["body_battery_level"][0]
        self.assertEqual((bb["value"], bb["value2"]), (12, "MEASURED"))

    def test_text_timestamps(self):
        self.assertEqual(self.by_series["hrv_reading_ms"][0]["time_gmt"], "2026-09-01T21:19:55Z")
        self.assertEqual(self.by_series["steps_15min"][0]["time_gmt"], "2026-09-01T22:00:00Z")

    def test_every_row_has_the_same_columns(self):
        for row in self.rows:
            self.assertEqual(set(row), {"date", "series", "time_gmt", "value", "value2"})

    def test_empty_or_odd_input_flattens_to_nothing(self):
        self.assertEqual(gd.flatten_timeline(DAY, {}), [])
        self.assertEqual(gd.flatten_timeline(DAY, {"heart_rate": {"heartRateValues": "nope"}, "steps": [1, 2]}), [])


class TestSnapshots(unittest.TestCase):
    def test_metrics(self):
        snap = {r["metric"]: r["value"] for r in gd.flatten_snapshots(DAY, RAW)}
        self.assertEqual(snap["training_readiness_score"], 52)
        self.assertEqual(snap["training_status"], "MAINTAINING_2")
        self.assertEqual(snap["training_load_7d"], 473)
        self.assertEqual(snap["vo2max_generic"], 51.9)
        self.assertEqual(snap["hydration_goal_ml"], 3373.0)
        self.assertEqual(snap["endurance_score"], 6072)
        self.assertEqual(snap["fitness_age"], 28.4)
        self.assertEqual(snap["body_battery_charged"], 89)
        self.assertEqual(snap["heart_rate_max"], 144)

    def test_missing_is_missing_not_none(self):
        rows = gd.flatten_snapshots(DAY, {"hydration": {"valueInML": 250}})
        self.assertEqual(rows, [{"date": DAY, "metric": "hydration_ml", "value": 250}])


class TestDetailComplete(unittest.TestCase):
    def test_index_semantics(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self.assertFalse(gd.detail_complete(d))
            (d / "_index.json").write_text(json.dumps({name: "ok" for name in gd.ENDPOINTS}))
            self.assertTrue(gd.detail_complete(d))
            (d / "_index.json").write_text(json.dumps({**{name: "ok" for name in gd.ENDPOINTS}, "spo2": "error: 500"}))
            self.assertFalse(gd.detail_complete(d))


if __name__ == "__main__":
    unittest.main()
