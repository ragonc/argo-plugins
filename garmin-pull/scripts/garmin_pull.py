#!/usr/bin/env python3
"""garmin_pull.py -- pull your Garmin wellness and activity data into files you
can actually use: one JSON per day, one CSV per category, one SQLite database.

    python3 garmin_pull.py                          # last 30 days, everything
    python3 garmin_pull.py --days 90 --only sleep,hrv
    python3 garmin_pull.py --from 2026-01-01 --to 2026-03-31 --only activities --tcx
    python3 garmin_pull.py --out ~/garmin-data --format csv

Categories (--only, comma-separated; default all):
    sleep       stages, score, efficiency, SpO2 and respiration during sleep, sleep need
    hrv         last-night HRV, weekly average, personal baseline, status
    summary     resting HR, stress, body battery, steps, SpO2, intensity minutes, calories
    vo2max      VO2max estimate for the day, when Garmin published one
    activities  workouts with distance, duration, HR, power, training effect (+ --tcx files)

Output folder layout (default ./garmin-data):
    raw/YYYY-MM-DD.json     shaped data for that day (the resume checkpoint)
    raw/activities.json     shaped activity list for the range
    tcx/<activity_id>.tcx   only with --tcx
    sleep.csv hrv.csv summary.csv vo2max.csv activities.csv
    garmin.db               SQLite, one table per category, primary key on date / activity_id

Resumable: a day whose raw JSON already exists is skipped (use --refresh to re-pull).
Rate limits: Garmin answers HTTP 429 when asked too fast or logged into too often.
This script pauses between days, stops cleanly at the first 429, and tells you to
rerun later -- the rerun picks up where it stopped. Nothing is ever invented: a
metric Garmin did not send is an empty cell.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from garmin_client import (  # noqa: E402
    GarminConnectError,
    GarminSession,
    open_session,
    save_session,
    shape_activity,
    shape_daily_summary,
    shape_hrv,
    shape_sleep,
    shape_vo2max,
)

CATEGORIES = ("sleep", "hrv", "summary", "vo2max", "activities")
DAILY = ("sleep", "hrv", "summary", "vo2max")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --- shaping additions (defensive, like the client's own shape_* functions) ---

def shape_summary_extra(raw: dict | None) -> dict:
    """Fields of the daily summary beyond the client's verified core set.
    Names follow the Garmin Connect daily summary payload; anything missing
    is None. Marked 'extra' so a rename on Garmin's side is easy to spot."""
    if not raw:
        return {}
    return {
        "body_battery_high": raw.get("bodyBatteryHighestValue"),
        "body_battery_low": raw.get("bodyBatteryLowestValue"),
        "body_battery_latest": raw.get("bodyBatteryMostRecentValue"),
        "moderate_intensity_minutes": raw.get("moderateIntensityMinutes"),
        "vigorous_intensity_minutes": raw.get("vigorousIntensityMinutes"),
        "floors_ascended": raw.get("floorsAscended"),
        "active_kcal": raw.get("activeKilocalories"),
        "total_kcal": raw.get("totalKilocalories"),
        "min_hr": raw.get("minHeartRate"),
        "max_hr": raw.get("maxHeartRate"),
        "avg_hr_7d": raw.get("lastSevenDaysAvgRestingHeartRate"),
    }


def fetch_day(session: GarminSession, day: str, only: set[str]) -> dict:
    out: dict = {"date": day}
    if "sleep" in only:
        out["sleep"] = shape_sleep(session.fetch_sleep(day))
    if "hrv" in only:
        out["hrv"] = shape_hrv(session.fetch_hrv(day))
    if "summary" in only:
        raw = session.fetch_daily_summary(day)
        out["summary"] = {**shape_daily_summary(raw), **shape_summary_extra(raw)}
    if "vo2max" in only:
        out["vo2max"] = shape_vo2max(session.fetch_vo2max(day))
    return out


# --- range helpers -------------------------------------------------------------

def day_range(start: date, end: date) -> list[str]:
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


def pull_days(session: GarminSession, days: list[str], only: set[str], raw_dir: Path,
              pause: float, refresh: bool) -> tuple[int, int, bool]:
    """Returns (pulled, skipped, rate_limited)."""
    pulled = skipped = 0
    for i, day in enumerate(days):
        target = raw_dir / f"{day}.json"
        if target.exists() and not refresh:
            skipped += 1
            continue
        if pulled and pause:
            time.sleep(pause)
        try:
            result = fetch_day(session, day, only)
        except GarminConnectError as exc:
            if "429" in str(exc):
                log(f"garmin: HTTP 429 (rate limited) at {day} after {pulled} day(s). "
                    f"Wait an hour or so and rerun -- it resumes from here.")
                return pulled, skipped, True
            log(f"garmin: {day} failed ({exc}) -- recorded, continuing")
            result = {"date": day, "error": str(exc)[:300]}
        target.write_text(json.dumps(result, indent=2) + "\n")
        pulled += 1
        log(f"garmin: {day} ok ({i + 1}/{len(days)})")
    return pulled, skipped, False


def pull_activities(session: GarminSession, since: str, until: str, raw_dir: Path,
                    tcx_dir: Path | None) -> list[dict]:
    raw = session.fetch_activities_since(since, max_pages=100)
    shaped = []
    for entry in raw:
        item = shape_activity(entry)
        start = (item.get("start_time_local") or "")[:10]
        if start and start > until:
            continue
        item["tcx_path"] = None
        if tcx_dir is not None and item.get("activity_id") is not None:
            path = tcx_dir / f"{item['activity_id']}.tcx"
            if not path.exists():
                try:
                    path.write_bytes(session.fetch_activity_tcx(item["activity_id"]))
                    time.sleep(0.5)
                except GarminConnectError as exc:
                    log(f"garmin: TCX for {item['activity_id']} failed ({exc})")
            if path.exists():
                item["tcx_path"] = str(path)
        shaped.append(item)
    (raw_dir / "activities.json").write_text(json.dumps(shaped, indent=2) + "\n")
    log(f"garmin: {len(shaped)} activities between {since} and {until}")
    return shaped


# --- outputs -------------------------------------------------------------------

def load_all_days(raw_dir: Path) -> list[dict]:
    days = []
    for path in sorted(raw_dir.glob("????-??-??.json")):
        try:
            days.append(json.loads(path.read_text()))
        except json.JSONDecodeError:
            continue
    return days


def rows_for(category: str, days: list[dict]) -> list[dict]:
    rows = []
    for day in days:
        block = day.get(category)
        if not isinstance(block, dict):
            continue
        row = {"date": day["date"]}
        row.update({k: v for k, v in block.items() if k != "calendar_date"})
        rows.append(row)
    return rows


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def write_sqlite(db_path: Path, tables: dict[str, tuple[str, list[dict]]]) -> None:
    """tables = {name: (primary_key_column, rows)}. Columns are created from
    the rows; every value is stored as it comes (numbers stay numbers)."""
    db = sqlite3.connect(db_path)
    for name, (pk, rows) in tables.items():
        if not rows:
            continue
        columns: list[str] = []
        for row in rows:
            for key in row:
                if key not in columns:
                    columns.append(key)
        defs = ", ".join(f'"{c}"' + (" PRIMARY KEY" if c == pk else "") for c in columns)
        db.execute(f'CREATE TABLE IF NOT EXISTS "{name}" ({defs})')
        existing = {r[1] for r in db.execute(f'PRAGMA table_info("{name}")')}
        for c in columns:
            if c not in existing:
                db.execute(f'ALTER TABLE "{name}" ADD COLUMN "{c}"')
        placeholders = ", ".join("?" for _ in columns)
        db.executemany(
            f'INSERT OR REPLACE INTO "{name}" ({", ".join(chr(34) + c + chr(34) for c in columns)}) '
            f"VALUES ({placeholders})",
            [tuple(row.get(c) for c in columns) for row in rows],
        )
    db.commit()
    db.close()


def write_outputs(out: Path, only: set[str], formats: set[str], activities: list[dict] | None) -> None:
    days = load_all_days(out / "raw")
    tables: dict[str, tuple[str, list[dict]]] = {}
    for category in DAILY:
        if category in only:
            tables[category] = ("date", rows_for(category, days))
    if activities is not None:
        tables["activities"] = ("activity_id", activities)
    elif "activities" in only and (out / "raw" / "activities.json").exists():
        tables["activities"] = ("activity_id", json.loads((out / "raw" / "activities.json").read_text()))
    if "csv" in formats:
        for name, (_, rows) in tables.items():
            write_csv(out / f"{name}.csv", rows)
    if "sqlite" in formats:
        write_sqlite(out / "garmin.db", tables)
    counts = ", ".join(f"{name} {len(rows)}" for name, (_, rows) in tables.items())
    log(f"garmin: wrote {', '.join(sorted(formats))} to {out} ({counts})")


# --- CLI -----------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=30, help="how many days back from today (default 30)")
    parser.add_argument("--from", dest="start", help="YYYY-MM-DD (overrides --days)")
    parser.add_argument("--to", dest="end", help="YYYY-MM-DD (default today)")
    parser.add_argument("--only", default=",".join(CATEGORIES),
                        help="comma-separated subset of: " + ", ".join(CATEGORIES))
    parser.add_argument("--out", type=Path, default=Path("garmin-data"), help="output folder")
    parser.add_argument("--format", default="json,csv,sqlite", help="any of json,csv,sqlite (json is always kept)")
    parser.add_argument("--tcx", action="store_true", help="also download each activity's TCX file")
    parser.add_argument("--pause", type=float, default=1.0, help="seconds between days (be kind to Garmin)")
    parser.add_argument("--refresh", action="store_true", help="re-pull days that already exist")
    parser.add_argument("--mfa-code", help="two-factor code if you already have it")
    parser.add_argument("--no-cache", action="store_true", help="full login, ignore the cached session")
    args = parser.parse_args()

    only = {c.strip() for c in args.only.split(",") if c.strip()}
    unknown = only - set(CATEGORIES)
    if unknown:
        log(f"garmin: unknown category {', '.join(sorted(unknown))}; choose from {', '.join(CATEGORIES)}")
        return 2
    formats = {f.strip() for f in args.format.split(",") if f.strip()} | {"json"}
    end = date.fromisoformat(args.end) if args.end else datetime.now(timezone.utc).date()
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=args.days - 1)
    if start > end:
        log("garmin: --from is after --to")
        return 2

    raw_dir = args.out / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    tcx_dir = None
    if args.tcx:
        tcx_dir = args.out / "tcx"
        tcx_dir.mkdir(parents=True, exist_ok=True)

    try:
        session = open_session(mfa_code=args.mfa_code, use_cache=not args.no_cache, log=log)
    except GarminConnectError as exc:
        log(f"garmin: {exc}")
        return 1

    rate_limited = False
    daily = only & set(DAILY)
    if daily:
        days = day_range(start, end)
        log(f"garmin: {len(days)} day(s) {start} .. {end}, categories: {', '.join(sorted(daily))}")
        pulled, skipped, rate_limited = pull_days(session, days, daily, raw_dir, args.pause, args.refresh)
        log(f"garmin: {pulled} pulled, {skipped} already present")
    activities = None
    if "activities" in only and not rate_limited:
        try:
            activities = pull_activities(session, start.isoformat(), end.isoformat(), raw_dir, tcx_dir)
        except GarminConnectError as exc:
            log(f"garmin: activities failed ({exc})")
            rate_limited = "429" in str(exc)

    if not args.no_cache:
        try:
            save_session(session)
        except OSError:
            pass
    write_outputs(args.out, only, formats, activities)
    if rate_limited:
        log("garmin: stopped early because of rate limiting -- rerun in an hour to continue")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
