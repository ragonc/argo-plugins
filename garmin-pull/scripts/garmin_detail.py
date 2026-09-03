#!/usr/bin/env python3
"""garmin_detail.py -- everything Garmin stored for one day, not just the summary.

The `detail` category (also what --full-day turns on) fetches the intraday
series the watch recorded: heart rate every couple of minutes, stress and body
battery every three, steps per 15 minutes, breathing rate, SpO2, HRV readings
through the night, sleep stages and movement, plus that day's snapshots such
as training readiness, hydration, training status, endurance score, fitness
age and race predictions.

Two things are kept:
  raw/detail/<day>/<name>.json   the untouched Garmin response, one per endpoint
  timeline rows                  every timestamped value, flattened to one long
                                 table: date, series, time_gmt, value, value2
  snapshot rows                  one row per (date, metric, value) for the
                                 non-timeline endpoints

Every endpoint below was checked against a live account on 2026-09-03; the
array shapes the flatteners expect are the ones seen that day. A response that
changes shape flattens to nothing rather than crashing, and the raw JSON is
still on disk. About 14 requests per day: a full-day pull is roughly four
times the cost of the summary categories.
"""
from __future__ import annotations

import json
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from garmin_client import GarminAPIError, GarminConnectError, GarminSession

# name -> (path builder(day, username), params builder(day) | None)
ENDPOINTS: dict[str, tuple[Callable[[str, str], str], Callable[[str], dict] | None]] = {
    "heart_rate": (lambda d, u: "/wellness-service/wellness/dailyHeartRate", lambda d: {"date": d}),
    "stress": (lambda d, u: f"/wellness-service/wellness/dailyStress/{d}", None),
    "body_battery": (lambda d, u: "/wellness-service/wellness/bodyBattery/reports/daily",
                     lambda d: {"startDate": d, "endDate": d}),
    "steps": (lambda d, u: "/wellness-service/wellness/dailySummaryChart", lambda d: {"date": d}),
    "respiration": (lambda d, u: f"/wellness-service/wellness/daily/respiration/{d}", None),
    "spo2": (lambda d, u: f"/wellness-service/wellness/daily/spo2/{d}", None),
    "intensity_minutes": (lambda d, u: f"/wellness-service/wellness/daily/im/{d}", None),
    "hrv": (lambda d, u: f"/hrv-service/hrv/{d}", None),
    "sleep": (lambda d, u: f"/wellness-service/wellness/dailySleepData/{urllib.parse.quote(u, safe='')}",
              lambda d: {"date": d, "nonSleepBufferMinutes": "60"}),
    "training_readiness": (lambda d, u: f"/metrics-service/metrics/trainingreadiness/{d}", None),
    "training_status": (lambda d, u: f"/metrics-service/metrics/trainingstatus/aggregated/{d}", None),
    "hydration": (lambda d, u: f"/usersummary-service/usersummary/hydration/daily/{d}", None),
    "endurance_score": (lambda d, u: "/metrics-service/metrics/endurancescore", lambda d: {"calendarDate": d}),
    "fitness_age": (lambda d, u: f"/fitnessage-service/fitnessage/{d}", None),
}
REQUESTS_PER_DAY = len(ENDPOINTS)


def fetch_detail(session: GarminSession, day: str, out_dir: Path, pause: float = 0.3) -> dict[str, str]:
    """Fetch every endpoint for `day` into out_dir/<name>.json. Returns
    {name: "ok" | "empty" | "error: ..."}; raises only on rate limiting so the
    caller can stop the whole run."""
    out_dir.mkdir(parents=True, exist_ok=True)
    username = session._garmin_username()
    status: dict[str, str] = {}
    for i, (name, (path_fn, params_fn)) in enumerate(ENDPOINTS.items()):
        target = out_dir / f"{name}.json"
        if target.exists():
            status[name] = "ok"
            continue
        if i and pause:
            time.sleep(pause)
        try:
            raw = session.connectapi(path_fn(day, username), params=params_fn(day) if params_fn else None)
        except GarminConnectError as exc:
            if "429" in str(exc):
                raise
            status[name] = f"error: {str(exc)[:120]}"
            continue
        target.write_text(json.dumps(raw, indent=1) + "\n")
        status[name] = "ok" if raw else "empty"
    (out_dir / "_index.json").write_text(json.dumps(status, indent=1) + "\n")
    return status


def detail_complete(out_dir: Path) -> bool:
    index = out_dir / "_index.json"
    if not index.exists():
        return False
    try:
        status = json.loads(index.read_text())
    except json.JSONDecodeError:
        return False
    return all(not str(v).startswith("error") for v in status.values()) and set(status) >= set(ENDPOINTS)


# --- flattening ----------------------------------------------------------------

def _iso(ms) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OSError):
        return None


def _iso_text(text) -> str | None:
    """Garmin's '2026-09-01T21:19:55.0' -> '2026-09-01T21:19:55Z'."""
    if not isinstance(text, str) or len(text) < 19:
        return None
    return text[:19] + "Z"


def _rows(date: str, series: str, points, value2=None) -> list[dict]:
    rows = []
    for ts, value in points:
        if ts is None or value is None:
            continue
        rows.append({"date": date, "series": series, "time_gmt": ts, "value": value, "value2": value2})
    return rows


def _pairs(array, ts_index=0, value_index=1):
    for item in array or []:
        if isinstance(item, (list, tuple)) and len(item) > max(ts_index, value_index):
            yield _iso(item[ts_index]), item[value_index]


def flatten_timeline(date: str, raw: dict[str, object]) -> list[dict]:
    rows: list[dict] = []
    hr = raw.get("heart_rate") or {}
    rows += _rows(date, "heart_rate_bpm", _pairs(hr.get("heartRateValues")))

    stress = raw.get("stress") or {}
    rows += _rows(date, "stress_level", _pairs(stress.get("stressValuesArray")))
    for item in stress.get("bodyBatteryValuesArray") or []:
        if isinstance(item, list) and len(item) >= 3:
            rows.append({"date": date, "series": "body_battery_level", "time_gmt": _iso(item[0]),
                         "value": item[2], "value2": item[1]})

    for bucket in raw.get("steps") or []:
        if isinstance(bucket, dict):
            rows.append({"date": date, "series": "steps_15min", "time_gmt": _iso_text(bucket.get("startGMT")),
                         "value": bucket.get("steps"), "value2": bucket.get("primaryActivityLevel")})

    resp = raw.get("respiration") or {}
    rows += _rows(date, "respiration_brpm", _pairs(resp.get("respirationValuesArray")))
    rows += _rows(date, "respiration_hourly_avg", _pairs(resp.get("respirationAveragesValuesArray")))

    spo2 = raw.get("spo2") or {}
    rows += _rows(date, "spo2_hourly_avg_pct", _pairs(spo2.get("spO2HourlyAverages")))

    im = raw.get("intensity_minutes") or {}
    rows += _rows(date, "intensity_minutes", _pairs(im.get("imValuesArray")))

    hrv = raw.get("hrv") or {}
    rows += _rows(date, "hrv_reading_ms",
                  ((_iso_text(r.get("readingTimeGMT")), r.get("hrvValue")) for r in hrv.get("hrvReadings") or []
                   if isinstance(r, dict)))

    sleep = raw.get("sleep") or {}
    for key, series in (("sleepHeartRate", "sleep_heart_rate_bpm"), ("sleepStress", "sleep_stress_level"),
                        ("sleepBodyBattery", "sleep_body_battery_level"), ("hrvData", "sleep_hrv_ms"),
                        ("sleepRestlessMoments", "sleep_restless")):
        rows += _rows(date, series, ((_iso(p.get("startGMT")), p.get("value")) for p in sleep.get(key) or []
                                     if isinstance(p, dict)))
    for key, series in (("sleepLevels", "sleep_stage_level"), ("sleepMovement", "sleep_movement")):
        for p in sleep.get(key) or []:
            if isinstance(p, dict):
                rows.append({"date": date, "series": series, "time_gmt": _iso_text(p.get("startGMT")),
                             "value": p.get("activityLevel"), "value2": _iso_text(p.get("endGMT"))})
    rows += _rows(date, "sleep_respiration_brpm",
                  ((_iso(p.get("startTimeGMT")), p.get("respirationValue"))
                   for p in sleep.get("wellnessEpochRespirationDataDTOList") or [] if isinstance(p, dict)))
    rows += _rows(date, "sleep_spo2_pct",
                  ((_iso_text(p.get("epochTimestamp")), p.get("spo2Reading"))
                   for p in sleep.get("wellnessEpochSPO2DataDTOList") or [] if isinstance(p, dict)))
    for bb in raw.get("body_battery") or []:
        if isinstance(bb, dict):
            for ev in bb.get("bodyBatteryActivityEvent") or []:
                if isinstance(ev, dict):
                    rows.append({"date": date, "series": "body_battery_event", "time_gmt": _iso_text(ev.get("eventStartTimeGmt")),
                                 "value": ev.get("bodyBatteryImpact"), "value2": ev.get("eventType")})
    return [r for r in rows if r.get("time_gmt")]


def flatten_snapshots(date: str, raw: dict[str, object]) -> list[dict]:
    out: list[tuple[str, object]] = []

    def add(metric, value):
        if value is not None:
            out.append((metric, value))

    readiness = raw.get("training_readiness") or []
    latest = readiness[0] if isinstance(readiness, list) and readiness and isinstance(readiness[0], dict) else {}
    for key in ("score", "level", "sleepScore", "recoveryTime", "acuteLoad", "hrvWeeklyAverage", "stressHistoryFactorPercent"):
        add(f"training_readiness_{key}", latest.get(key))
    ts = raw.get("training_status") or {}
    vo2 = (ts.get("mostRecentVO2Max") or {}).get("generic") or {}
    add("vo2max_generic", vo2.get("vo2MaxPreciseValue", vo2.get("vo2MaxValue")))
    status = (ts.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData") or {}
    for dev in status.values() if isinstance(status, dict) else []:
        if isinstance(dev, dict):
            add("training_status", dev.get("trainingStatusFeedbackPhrase"))
            add("training_load_7d", dev.get("weeklyTrainingLoad"))
            break
    hyd = raw.get("hydration") or {}
    add("hydration_ml", hyd.get("valueInML"))
    add("hydration_goal_ml", hyd.get("goalInML"))
    add("sweat_loss_ml", hyd.get("sweatLossInML"))
    es = raw.get("endurance_score") or {}
    add("endurance_score", es.get("overallScore"))
    add("endurance_classification", es.get("classification"))
    fa = raw.get("fitness_age") or {}
    add("fitness_age", fa.get("fitnessAge"))
    add("chronological_age", fa.get("chronologicalAge"))
    bb = raw.get("body_battery") or []
    if isinstance(bb, list) and bb and isinstance(bb[0], dict):
        add("body_battery_charged", bb[0].get("charged"))
        add("body_battery_drained", bb[0].get("drained"))
    hr = raw.get("heart_rate") or {}
    add("heart_rate_min", hr.get("minHeartRate"))
    add("heart_rate_max", hr.get("maxHeartRate"))
    return [{"date": date, "metric": m, "value": v} for m, v in out]


def load_detail_day(day_dir: Path) -> dict[str, object]:
    raw: dict[str, object] = {}
    for name in ENDPOINTS:
        path = day_dir / f"{name}.json"
        if path.exists():
            try:
                raw[name] = json.loads(path.read_text())
            except json.JSONDecodeError:
                continue
    return raw


def detail_tables(detail_root: Path) -> tuple[list[dict], list[dict]]:
    timeline: list[dict] = []
    snapshots: list[dict] = []
    if not detail_root.exists():
        return timeline, snapshots
    for day_dir in sorted(p for p in detail_root.iterdir() if p.is_dir()):
        raw = load_detail_day(day_dir)
        if raw:
            timeline += flatten_timeline(day_dir.name, raw)
            snapshots += flatten_snapshots(day_dir.name, raw)
    return timeline, snapshots
