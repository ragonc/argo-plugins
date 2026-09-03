#!/usr/bin/env python3
"""garmin_detail.py -- everything Garmin stored for one day, not just the summary.

The `detail` category (what --full-day turns on) fetches the intraday series the
watch recorded -- heart rate, stress, body battery, steps per 15 minutes,
breathing, SpO2, HRV readings, sleep stages and movement -- plus that day's
snapshots: training readiness, training status, hydration, endurance score,
fitness age.

Two things are kept:
  raw/detail/<day>/<name>.json   the untouched Garmin response, one per endpoint
  timeline rows                  every timestamped value, one long table:
                                 date, series, time_gmt, value, value2
  snapshot rows                  one row per (date, metric, value)

How the flattening adapts to Garmin's shapes instead of hardcoding them:
  * Garmin ships a descriptor list next to every array-of-arrays
    (`heartRateValueDescriptors` for `heartRateValues`, and so on) saying which
    column is the timestamp and what the others mean. The flattener reads those
    descriptors, so a reordered or extended array still lands in the right
    columns. Without descriptors it assumes [timestamp, value].
  * Any list of dicts with a recognisable time field becomes a series when the
    value column is unambiguous: a known value field, or exactly one numeric
    field. When two or more numeric fields could be the value, nothing is
    guessed: the series is left out and the report names it, so you can look at
    the raw JSON instead of trusting a plausible-looking wrong number.
  * Every scalar field of a response becomes a snapshot metric, named from the
    endpoint and the field. New fields Garmin adds show up on their own. When
    Garmin reports per device (training status does), the primary device keeps
    the plain name and any other device gets its id in the metric name.
  * The names you would expect (heart_rate_bpm, stress_level, ...) come from a
    small alias table; anything unknown gets a generated name rather than being
    dropped.
  * `schema_baseline.json` records the field names seen on 2026-09-03. Each pull
    compares what it saw and reports added or missing fields per endpoint, so
    you learn about a change from the report, not from an empty column.

About 14 requests per day: a full-day pull is roughly four times the cost of the
summary categories.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from garmin_client import GarminConnectError, GarminSession

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
BASELINE_PATH = Path(__file__).resolve().parent / "schema_baseline.json"

# (endpoint, array or list field) -> friendly series name. Anything not listed
# is named "<endpoint>_<field>" automatically.
SERIES_ALIASES: dict[tuple[str, str], str] = {
    ("heart_rate", "heartRateValues"): "heart_rate_bpm",
    ("stress", "stressValuesArray"): "stress_level",
    ("stress", "bodyBatteryValuesArray"): "body_battery_level",
    ("steps", ""): "steps_15min",
    ("respiration", "respirationValuesArray"): "respiration_brpm",
    ("respiration", "respirationAveragesValuesArray"): "respiration_hourly_avg",
    ("spo2", "spO2HourlyAverages"): "spo2_hourly_avg_pct",
    ("spo2", "spO2SingleValues"): "spo2_single_pct",
    ("intensity_minutes", "imValuesArray"): "intensity_minutes",
    ("hrv", "hrvReadings"): "hrv_reading_ms",
    ("sleep", "sleepLevels"): "sleep_stage_level",
    ("sleep", "sleepMovement"): "sleep_movement",
    ("sleep", "sleepHeartRate"): "sleep_heart_rate_bpm",
    ("sleep", "sleepStress"): "sleep_stress_level",
    ("sleep", "sleepBodyBattery"): "sleep_body_battery_level",
    ("sleep", "hrvData"): "sleep_hrv_ms",
    ("sleep", "sleepRestlessMoments"): "sleep_restless",
    ("sleep", "wellnessEpochRespirationDataDTOList"): "sleep_respiration_brpm",
    ("sleep", "wellnessEpochSPO2DataDTOList"): "sleep_spo2_pct",
    ("sleep", "breathingDisruptionData"): "sleep_breathing_disruption",
    ("body_battery", "bodyBatteryValuesArray"): "body_battery_report_level",
    ("body_battery", "bodyBatteryActivityEvent"): "body_battery_event",
}
# (endpoint, generated metric) -> friendly snapshot name
METRIC_ALIASES: dict[str, str] = {
    "hydration_value_in_ml": "hydration_ml",
    "hydration_goal_in_ml": "hydration_goal_ml",
    "hydration_sweat_loss_in_ml": "sweat_loss_ml",
    "endurance_score_overall_score": "endurance_score",
    "endurance_score_classification": "endurance_classification",
    "fitness_age_fitness_age": "fitness_age",
    "fitness_age_chronological_age": "chronological_age",
    "body_battery_charged": "body_battery_charged",
    "body_battery_drained": "body_battery_drained",
    "heart_rate_min_heart_rate": "heart_rate_min",
    "heart_rate_max_heart_rate": "heart_rate_max",
    "training_status_most_recent_vo2_max_generic_vo2_max_precise_value": "vo2max_generic",
    "training_status_most_recent_training_status_latest_training_status_data_by_device_training_status_feedback_phrase": "training_status",
    "training_status_most_recent_training_status_latest_training_status_data_by_device_weekly_training_load": "training_load_7d",
}

TIME_KEYS = ("startGMT", "startTimeGMT", "timestamp", "readingTimeGMT", "epochTimestamp", "eventStartTimeGmt",
             "startTimestampGMT", "timestampGMT", "epochEndTimestampGmt")
VALUE_PREFERENCE = ("value", "hrvValue", "steps", "activityLevel", "respirationValue", "spo2Reading",
                    "bodyBatteryImpact", "respirationAverageValue", "score")
NOISE_KEYS = re.compile(r"(userProfilePK|userProfilePk|userId|deviceId|Descriptor|version|Version|calendarDate|"
                        r"startTimestamp|endTimestamp|sleepStartTimestamp|sleepEndTimestamp|timestamp|Timestamp|"
                        r"Offset|Origin|primaryTrainingDevice)")


# --- fetching --------------------------------------------------------------------

def fetch_detail(session: GarminSession, day: str, out_dir: Path, pause: float = 0.3) -> dict[str, str]:
    """Fetch every endpoint for `day` into out_dir/<name>.json. Returns
    {name: "ok" | "empty" | "none: HTTP 4xx" | "error: ..."}. "none" means
    Garmin refused that endpoint for that day (404 and friends): final, not
    retried on later runs. "error" (5xx, network) is retried next run.
    Raises only on rate limiting so the caller can stop the whole run."""
    out_dir.mkdir(parents=True, exist_ok=True)
    username = session._garmin_username()
    status = _read_index(out_dir)
    requested = 0
    for name, (path_fn, params_fn) in ENDPOINTS.items():
        target = out_dir / f"{name}.json"
        if target.exists():
            status[name] = "ok" if target.stat().st_size > 5 else "empty"
            continue
        if str(status.get(name, "")).startswith("none"):
            continue
        if requested and pause:
            time.sleep(pause)
        requested += 1
        try:
            raw = session.connectapi(path_fn(day, username), params=params_fn(day) if params_fn else None)
        except GarminConnectError as exc:
            if exc.rate_limited:
                _write_index(out_dir, status)
                raise
            status[name] = f"{'none' if exc.permanent else 'error'}: {str(exc)[:120]}"
            continue
        target.write_text(json.dumps(raw, indent=1) + "\n")
        status[name] = "ok" if raw else "empty"
    _write_index(out_dir, status)
    return status


def _read_index(out_dir: Path) -> dict[str, str]:
    try:
        data = json.loads((out_dir / "_index.json").read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_index(out_dir: Path, status: dict[str, str]) -> None:
    (out_dir / "_index.json").write_text(json.dumps(status, indent=1) + "\n")


def detail_complete(out_dir: Path) -> bool:
    """True when every endpoint has a final answer for the day: data, an
    empty response, or a definitive refusal. Only "error" entries (and
    endpoints never asked) keep a day incomplete."""
    status = _read_index(out_dir)
    return bool(status) and set(status) >= set(ENDPOINTS) and all(
        not str(v).startswith("error") for v in status.values())


# --- small helpers ---------------------------------------------------------------

def snake(name: str) -> str:
    name = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name)
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _iso(value) -> str | None:
    """Epoch milliseconds or Garmin's '2026-09-01T21:19:55.0' -> ISO Z; None if neither."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(value, str) and len(value) >= 19 and value[4] == "-" and value[10] == "T":
        return value[:19] + "Z"
    return None


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _descriptor_map(response: dict, field: str) -> dict[int, str] | None:
    """Find the descriptor list that belongs to `field` and turn it into
    {column index: key}. Garmin names them <prefix>ValueDescriptors...,
    where <prefix> is the array name minus its 'Values...' suffix."""
    stem = re.sub(r"(Values?Array|Values|HourlyAverages|SingleValues)$", "", field).lower()
    for key, value in response.items():
        if "descriptor" not in key.lower() or not isinstance(value, list) or not value:
            continue
        if not key.lower().startswith(stem[:4]) and stem[:4] not in key.lower():
            continue
        mapping: dict[int, str] = {}
        for entry in value:
            if not isinstance(entry, dict):
                continue
            index = next((v for k, v in entry.items() if "index" in k.lower() and _is_number(v)), None)
            name = next((v for k, v in entry.items() if "key" in k.lower() and isinstance(v, str)), None)
            if index is not None and name:
                mapping[int(index)] = name
        if mapping:
            return mapping
    return None


def _series_name(endpoint: str, field: str) -> str:
    return SERIES_ALIASES.get((endpoint, field)) or (f"{endpoint}_{snake(field)}" if field else endpoint)


class Unmapped(Exception):
    """Raised by the row builders when a list has a time column but no
    unambiguous value column. Callers turn it into a report line."""


def _array_rows(date: str, series: str, array: list, descriptors: dict[int, str] | None) -> list[dict]:
    """Rows from an array of arrays, columns located via descriptors. With
    descriptors, the value column is the one whose name says it is a value
    (level, value, average, score, steps, minutes ...), else the only numeric
    column; without descriptors, column 1 when it is the only numeric one.
    Several numeric columns and no name to pick by raises Unmapped: a wrong
    column would produce plausible numbers, which is worse than none."""
    sample = next((x for x in array if isinstance(x, (list, tuple)) and x), None)
    if sample is None:
        return []
    ts_index = 0
    value_index: int | None = None
    value2_index: int | None = None
    if descriptors:
        for index, key in descriptors.items():
            if "timestamp" in key.lower() or key.lower().endswith("time"):
                ts_index = index
                break
        candidates = [i for i in sorted(descriptors) if i != ts_index and i < len(sample)]
        numeric = [i for i in candidates if _is_number(sample[i])]
        preferred = [i for i in numeric if re.search(r"level|value|average|avg|score|steps|minutes", descriptors[i], re.I)]
        if preferred:
            value_index = preferred[0]
        elif len(numeric) == 1:
            value_index = numeric[0]
        elif numeric:
            raise Unmapped(f"{len(numeric)} numeric columns ({', '.join(descriptors[i] for i in numeric)}) "
                           f"and none named like a value")
        text = [i for i in candidates if isinstance(sample[i], str)]
        value2_index = text[0] if text else None
    else:
        numeric = [i for i, v in enumerate(sample) if i != ts_index and _is_number(v)]
        if len(numeric) == 1:
            value_index = numeric[0]
        elif numeric:
            raise Unmapped(f"{len(numeric)} numeric columns and no descriptors to name them")
    if value_index is None:
        return []
    rows = []
    for item in array:
        if not isinstance(item, (list, tuple)) or len(item) <= max(ts_index, value_index):
            continue
        ts = _iso(item[ts_index])
        value = item[value_index]
        if ts is None or value is None:
            continue
        value2 = item[value2_index] if value2_index is not None and len(item) > value2_index else None
        rows.append({"date": date, "series": series, "time_gmt": ts, "value": value, "value2": value2})
    return rows


def _dict_rows(date: str, series: str, items: list) -> list[dict]:
    """Rows from a list of dicts that carry a time field and a numeric value."""
    sample = next((x for x in items if isinstance(x, dict)), None)
    if sample is None:
        return []
    time_key = next((k for k in TIME_KEYS if k in sample and _iso(sample[k])), None)
    if time_key is None:
        time_key = next((k for k, v in sample.items() if "gmt" in k.lower() and _iso(v)), None)
    if time_key is None:
        return []
    value_key = next((k for k in VALUE_PREFERENCE if k in sample and _is_number(sample[k])), None)
    if value_key is None:
        numeric = [k for k, v in sample.items() if _is_number(v) and k != time_key and not NOISE_KEYS.search(k)]
        if len(numeric) == 1:
            value_key = numeric[0]
        elif numeric:
            raise Unmapped(f"{len(numeric)} numeric fields ({', '.join(numeric[:6])}) and none is a known value field")
    if value_key is None:
        return []
    text_key = next((k for k, v in sample.items() if isinstance(v, str) and k != time_key and _iso(v) is None
                     and not NOISE_KEYS.search(k)), None)
    end_key = next((k for k in ("endGMT", "endTimeGMT") if k in sample), None)
    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ts = _iso(item.get(time_key))
        value = item.get(value_key)
        if ts is None or value is None:
            continue
        value2 = item.get(text_key) if text_key else (_iso(item.get(end_key)) if end_key else None)
        rows.append({"date": date, "series": series, "time_gmt": ts, "value": value, "value2": value2})
    return rows


def _device_names(node: dict) -> dict[str, str]:
    """Garmin keys some blocks by device id. One device keeps the plain
    'by_device' name (so the friendly aliases apply); with several, the one
    flagged primaryTrainingDevice (else the first) is 'by_device' and the
    others are 'device_<id>', so two watches never collapse into one metric."""
    ids = [k for k in node if k.isdigit()]
    if len(ids) <= 1:
        return {k: "by_device" for k in ids}
    primary = next((k for k in ids if isinstance(node[k], dict) and node[k].get("primaryTrainingDevice")), ids[0])
    return {k: ("by_device" if k == primary else f"device_{k}") for k in ids}


def _scalars(prefix: str, node, out: list[tuple[str, object]], depth: int = 0) -> None:
    """Collect every scalar under `node` as (metric, value), recursing into
    dicts. Numeric dict keys (device ids) are named by _device_names()."""
    if depth > 4:
        return
    if isinstance(node, dict):
        devices = _device_names(node)
        for key, value in node.items():
            name = devices.get(key) or snake(key)
            if isinstance(value, dict):
                _scalars(f"{prefix}_{name}", value, out, depth + 1)
            elif value is None or isinstance(value, list) or NOISE_KEYS.search(key):
                continue
            elif isinstance(value, (int, float, str, bool)):
                out.append((f"{prefix}_{name}", value))


# --- flattening ------------------------------------------------------------------

def flatten_endpoint(date: str, endpoint: str, response,
                     unmapped: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    """(timeline rows, snapshot rows) for one raw response of any shape. A
    list whose value column cannot be picked safely is left out of the
    timeline and described in `unmapped` (when given) as
    "<endpoint>.<field>: <why>"."""
    timeline: list[dict] = []
    scalars: list[tuple[str, object]] = []
    notes = unmapped if unmapped is not None else []
    if isinstance(response, list):
        if response and all(isinstance(x, dict) for x in response):
            sample = response[0]
            has_time = any(k in sample and _iso(sample[k]) for k in TIME_KEYS) or any(
                "gmt" in k.lower() and _iso(v) for k, v in sample.items())
            nested = any(isinstance(v, list) for v in sample.values())
            if has_time and not nested and len(sample) <= 8:
                timeline += _list_rows(date, endpoint, "", response, {}, notes)
            else:
                # records: newest entry gives the snapshots, nested arrays flatten as series
                newest = max(response, key=lambda x: str(x.get("timestamp") or x.get("calendarDate") or ""))
                _scalars(endpoint, newest, scalars)
                for key, value in newest.items():
                    if isinstance(value, list) and value:
                        timeline += _list_rows(date, endpoint, key, value, newest, notes)
        return timeline, _snapshot_rows(date, scalars)
    if not isinstance(response, dict):
        return timeline, []
    _scalars(endpoint, response, scalars)
    for key, value in response.items():
        if isinstance(value, list) and value and "descriptor" not in key.lower():
            timeline += _list_rows(date, endpoint, key, value, response, notes)
    return timeline, _snapshot_rows(date, scalars)


def _list_rows(date: str, endpoint: str, field: str, value: list, parent: dict, notes: list[str]) -> list[dict]:
    series = _series_name(endpoint, field)
    try:
        if isinstance(value[0], (list, tuple)):
            return _array_rows(date, series, value, _descriptor_map(parent, field))
        if isinstance(value[0], dict):
            return _dict_rows(date, series, value)
    except Unmapped as why:
        notes.append(f"{endpoint}.{field or '(list)'}: {why}; not flattened, see the raw JSON")
    return []


# Generated names repeat the endpoint inside Garmin's own wrapper names
# ("sleep_daily_sleep_dto_deep_sleep_seconds"); these prefixes are shortened.
PREFIX_ALIASES: tuple[tuple[str, str], ...] = (
    ("sleep_daily_sleep_dto_", "sleep_"),
    ("hrv_hrv_summary_", "hrv_"),
    ("body_battery_body_battery_", "body_battery_"),
    ("body_battery_end_of_day_body_battery_", "body_battery_end_of_day_"),
    ("training_status_most_recent_training_status_latest_training_status_data_by_device_", "training_status_"),
    ("training_status_most_recent_training_load_balance_metrics_map_by_device_", "training_load_balance_"),
    # secondary devices: same wrappers, "device_<id>" instead of "by_device"
    ("training_status_most_recent_training_status_latest_training_status_data_", "training_status_"),
    ("training_status_most_recent_training_load_balance_metrics_map_", "training_load_balance_"),
    ("training_status_most_recent_", "training_status_"),
)


def _snapshot_rows(date: str, scalars: list[tuple[str, object]]) -> list[dict]:
    rows, seen = [], set()
    for metric, value in scalars:
        metric = METRIC_ALIASES.get(metric, metric)
        for prefix, short in PREFIX_ALIASES:
            if metric.startswith(prefix):
                metric = short + metric[len(prefix):]
                break
        metric = metric.replace("_sp_o2", "_spo2")
        if metric in seen:
            continue
        seen.add(metric)
        rows.append({"date": date, "metric": metric, "value": value})
    return rows


def flatten_timeline(date: str, raw: dict[str, object], unmapped: list[str] | None = None) -> list[dict]:
    rows: list[dict] = []
    for endpoint, response in raw.items():
        rows += flatten_endpoint(date, endpoint, response, unmapped)[0]
    return rows


def flatten_snapshots(date: str, raw: dict[str, object]) -> list[dict]:
    rows: list[dict] = []
    for endpoint, response in raw.items():
        rows += flatten_endpoint(date, endpoint, response)[1]
    return rows


def flatten_day(date: str, raw: dict[str, object]) -> tuple[list[dict], list[dict], list[str]]:
    """(timeline, snapshots, unmapped notes) for one day's raw responses."""
    timeline: list[dict] = []
    snapshots: list[dict] = []
    notes: list[str] = []
    for endpoint, response in raw.items():
        t, s = flatten_endpoint(date, endpoint, response, notes)
        timeline += t
        snapshots += s
    return timeline, snapshots, notes


# --- schema drift ------------------------------------------------------------------

def schema_of(response) -> list[str]:
    """Field names that matter for flattening: top-level keys, plus the
    descriptor keys of every array, plus the keys of list-of-dict entries."""
    names: set[str] = set()

    def walk(node, prefix=""):
        if isinstance(node, dict):
            for key, value in node.items():
                name = f"{prefix}{'by_device' if key.isdigit() else key}"
                names.add(name)
                if isinstance(value, dict):
                    walk(value, name + ".")
                elif isinstance(value, list) and value:
                    if "descriptor" in key.lower():
                        for entry in value:
                            if isinstance(entry, dict):
                                k = next((v for kk, v in entry.items() if "key" in kk.lower()), None)
                                if k:
                                    names.add(f"{name}:{k}")
                    elif isinstance(value[0], dict):
                        walk(value[0], name + "[].")
        elif isinstance(node, list) and node and isinstance(node[0], dict):
            walk(node[0], prefix + "[].")

    walk(response)
    return sorted(names)


def load_baseline() -> dict[str, list[str]]:
    try:
        return json.loads(BASELINE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def schema_drift(raw: dict[str, object], baseline: dict[str, list[str]] | None = None) -> dict[str, dict[str, list[str]]]:
    """{endpoint: {"added": [...], "missing": [...]}} for endpoints that differ
    from the baseline. Empty responses are not reported as missing everything."""
    baseline = load_baseline() if baseline is None else baseline
    drift: dict[str, dict[str, list[str]]] = {}
    for endpoint, response in raw.items():
        if endpoint not in baseline or not response:
            continue
        seen, known = set(schema_of(response)), set(baseline[endpoint])
        added = sorted(seen - known)
        # A descriptor key only counts as missing when that descriptor list is
        # actually populated: an empty list (a day with no intensity minutes,
        # say) is absence of data, not a change of shape.
        populated = {k for k, v in response.items() if isinstance(v, list) and v} if isinstance(response, dict) else set()
        missing = sorted(n for n in known - seen if ":" not in n or n.split(":", 1)[0] in populated)
        if added or missing:
            drift[endpoint] = {"added": added, "missing": missing}
    return drift


# --- loading what is on disk ---------------------------------------------------------

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


class DriftLog:
    """Schema drift and unmapped lists merged across days: a field counts as
    added/missing if any day shows it; an unmapped list is reported once."""

    def __init__(self, baseline: dict[str, list[str]] | None = None):
        self.baseline = load_baseline() if baseline is None else baseline
        self._drift: dict[str, dict[str, set[str]]] = {}
        self._unmapped: dict[str, str] = {}  # note -> first day seen

    def add(self, day: str, raw: dict[str, object], notes: list[str]) -> None:
        for endpoint, change in schema_drift(raw, self.baseline).items():
            slot = self._drift.setdefault(endpoint, {"added": set(), "missing": set()})
            slot["added"] |= set(change["added"])
            slot["missing"] |= set(change["missing"])
        for note in notes:
            self._unmapped.setdefault(note, day)

    def result(self) -> dict:
        """{endpoint: {"added": [...], "missing": [...]}, plus "unmapped":
        ["<note> (first seen <day>)", ...] when any list was left out}."""
        out: dict = {e: {k: sorted(v) for k, v in c.items()} for e, c in self._drift.items()}
        if self._unmapped:
            out["unmapped"] = [f"{note} (first seen {day})" for note, day in sorted(self._unmapped.items())]
        return out


def iter_detail_days(detail_root: Path):
    """Yield (day, timeline rows, snapshot rows, unmapped notes, raw) one day
    at a time, so a multi-year pull never holds more than a day in memory."""
    if not detail_root.exists():
        return
    for day_dir in sorted(p for p in detail_root.iterdir() if p.is_dir()):
        raw = load_detail_day(day_dir)
        if not raw:
            continue
        timeline, snapshots, notes = flatten_day(day_dir.name, raw)
        yield day_dir.name, timeline, snapshots, notes, raw


def detail_tables(detail_root: Path) -> tuple[list[dict], list[dict], dict]:
    """(timeline, snapshots, drift) for every day under detail_root, all in
    memory. Fine for a few weeks; garmin_pull streams per day instead."""
    timeline: list[dict] = []
    snapshots: list[dict] = []
    log = DriftLog()
    for day, t, s, notes, raw in iter_detail_days(detail_root):
        timeline += t
        snapshots += s
        log.add(day, raw, notes)
    return timeline, snapshots, log.result()
