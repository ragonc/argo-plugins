#!/usr/bin/env python3
"""garmin_pull.py -- pull your Garmin wellness and activity data into files you
can actually use: one JSON per day, one CSV per category, one SQLite database.

    python3 garmin_pull.py                              # last 30 days, everything
    python3 garmin_pull.py --last 90d --what sleep,hrv
    python3 garmin_pull.py --since 2026-01-01 --until 2026-03-31 --what activities --workout-files
    python3 garmin_pull.py --everything                 # full drop: all data since your first activity
    python3 garmin_pull.py --out ~/garmin-data --format csv

What (--what, comma-separated; default all):
    sleep       stages, score, efficiency, SpO2 and respiration during sleep, sleep need
    hrv         last-night HRV, weekly average, personal baseline, status
    summary     resting HR, stress, body battery, steps, SpO2, intensity minutes, calories
    vo2max      VO2max estimate for the day, when Garmin published one
    activities  workouts with distance, duration, HR, power, training effect

When: --last 30d | 12w | 6m | 2y   (default 30d)
      --since YYYY-MM-DD [--until YYYY-MM-DD]
      --everything   all categories + workout files, from your first Garmin activity to today

Output folder layout (default ./garmin-data):
    raw/YYYY-MM-DD.json     shaped data for that day (the resume checkpoint)
    raw/activities.json     shaped activity list for the range
    tcx/<activity_id>.tcx   only with --workout-files
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

def parse_last(value: str) -> int:
    """'30d' -> 30, '12w' -> 84, '6m' -> 183, '2y' -> 730; a bare number means days."""
    value = value.strip().lower()
    unit = "d"
    if value and value[-1] in "dwmy":
        unit, value = value[-1], value[:-1]
    try:
        n = int(value)
    except ValueError as exc:
        raise ValueError(f"--last wants something like 30d, 12w, 6m or 2y, not {value + unit!r}") from exc
    return int(n * {"d": 1, "w": 7, "m": 30.5, "y": 365}[unit])


def find_first_activity_date(session: GarminSession, page_size: int = 100, max_pages: int = 200) -> date | None:
    """Oldest activity on the account, by paging the activity list to its end.
    Costs one request per `page_size` activities. None if there are no activities."""
    oldest: str | None = None
    start = 0
    for _ in range(max_pages):
        page = session.fetch_activities(start=start, limit=page_size)
        if not page:
            break
        for entry in page:
            day = (entry.get("startTimeLocal") or "")[:10]
            if day and (oldest is None or day < oldest):
                oldest = day
        if len(page) < page_size:
            break
        start += page_size
        time.sleep(0.3)
    return date.fromisoformat(oldest) if oldest else None


def estimate_minutes(n_days: int, n_categories: int, pause: float) -> float:
    return n_days * (n_categories * 0.4 + pause) / 60


def day_range(start: date, end: date) -> list[str]:
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


def pull_days(session: GarminSession, days: list[str], only: set[str], raw_dir: Path,
              pause: float, refresh: bool) -> tuple[int, int, bool]:
    """Returns (pulled, skipped, rate_limited)."""
    pulled = skipped = 0
    for i, day in enumerate(days):
        target = raw_dir / f"{day}.json"
        existing = _read_day(target) if target.exists() and not refresh else None
        missing = set(only) if existing is None or "error" in existing else {c for c in only if c not in existing}
        if not missing:
            skipped += 1
            continue
        if pulled and pause:
            time.sleep(pause)
        try:
            result = {**(existing or {}), **fetch_day(session, day, missing)}
            result.pop("error", None)
        except GarminConnectError as exc:
            if "429" in str(exc):
                log(f"garmin: HTTP 429 (rate limited) at {day} after {pulled} day(s). "
                    f"Wait an hour or so and rerun -- it resumes from here.")
                return pulled, skipped, True
            log(f"garmin: {day} failed ({exc}) -- recorded, continuing")
            result = {"date": day, "error": str(exc)[:300]}
        target.write_text(json.dumps(result, indent=2) + "\n")
        pulled += 1
        log(f"garmin: {day} ok ({i + 1}/{len(days)})" + (f", added {', '.join(sorted(missing))}" if existing else ""))
    return pulled, skipped, False


def _read_day(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


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
    what = parser.add_argument_group("what to pull")
    what.add_argument("--what", default="all", metavar="LIST",
                      help="'all' or a comma list of: " + ", ".join(CATEGORIES) + " (default all)")
    what.add_argument("--workout-files", action="store_true",
                      help="also save each workout's TCX file (per-second heart rate, pace, GPS)")
    what.add_argument("--everything", action="store_true",
                      help="full drop: all categories, workout files, from your first activity to today")
    when = parser.add_argument_group("when")
    when.add_argument("--last", default=None, metavar="PERIOD", help="30d, 12w, 6m, 2y ... (default 30d)")
    when.add_argument("--since", metavar="DATE", help="YYYY-MM-DD, first day to pull")
    when.add_argument("--until", metavar="DATE", help="YYYY-MM-DD, last day to pull (default today)")
    where = parser.add_argument_group("where")
    where.add_argument("--out", type=Path, default=Path("garmin-data"), metavar="FOLDER",
                       help="output folder (default ./garmin-data)")
    where.add_argument("--format", default="csv,sqlite", metavar="LIST",
                       help="csv, sqlite, or both (default both; raw JSON is always written)")
    adv = parser.add_argument_group("rarely needed")
    adv.add_argument("--refresh", action="store_true", help="re-pull days that are already on disk")
    adv.add_argument("--pause", type=float, default=1.0, metavar="SECONDS", help="wait between days (default 1)")
    adv.add_argument("--mfa-code", metavar="CODE", help="two-factor code if you already have it")
    adv.add_argument("--no-cache", action="store_true", help="ignore the cached session, log in fresh")
    adv.add_argument("--yes", action="store_true", help="skip the confirmation for long pulls")
    # older spellings, still accepted
    parser.add_argument("--only", dest="what_alias", help=argparse.SUPPRESS)
    parser.add_argument("--days", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--from", dest="since_alias", help=argparse.SUPPRESS)
    parser.add_argument("--to", dest="until_alias", help=argparse.SUPPRESS)
    parser.add_argument("--tcx", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    what_arg = args.what_alias or args.what
    only = set(CATEGORIES) if args.everything or what_arg.strip().lower() == "all" else {
        c.strip().lower() for c in what_arg.split(",") if c.strip()
    }
    unknown = only - set(CATEGORIES)
    if unknown:
        log(f"garmin: unknown category {', '.join(sorted(unknown))}; choose from {', '.join(CATEGORIES)} or all")
        return 2
    formats = {f.strip() for f in args.format.split(",") if f.strip()} | {"json"}
    want_tcx = args.workout_files or args.tcx or args.everything

    try:
        session = open_session(mfa_code=args.mfa_code, use_cache=not args.no_cache, log=log)
    except GarminConnectError as exc:
        log(f"garmin: {exc}")
        return 1

    today = datetime.now(timezone.utc).date()
    until_arg = args.until or args.until_alias
    end = date.fromisoformat(until_arg) if until_arg else today
    since_arg = args.since or args.since_alias
    if args.everything and not since_arg:
        log("garmin: finding your first activity to know how far back to go ...")
        first = find_first_activity_date(session)
        if first is None:
            log("garmin: no activities on this account; taking the last 3 years of daily data")
            first = end - timedelta(days=3 * 365)
        start = first
        log(f"garmin: first activity on {first}")
    elif since_arg:
        start = date.fromisoformat(since_arg)
    else:
        try:
            n_days = args.days if args.days else parse_last(args.last or "30d")
        except ValueError as exc:
            log(f"garmin: {exc}")
            return 2
        start = end - timedelta(days=n_days - 1)
    if start > end:
        log("garmin: --since is after --until")
        return 2

    daily = only & set(DAILY)
    days = day_range(start, end) if daily else []
    raw_dir = args.out / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    def _needs(d: str) -> bool:
        if args.refresh:
            return True
        existing = _read_day(raw_dir / f"{d}.json") if (raw_dir / f"{d}.json").exists() else None
        return existing is None or "error" in existing or any(c not in existing for c in daily)
    todo = [d for d in days if _needs(d)]
    minutes = estimate_minutes(len(todo), len(daily), args.pause)
    log(f"garmin: {start} .. {end}, {', '.join(sorted(only))}"
        + (", with workout files" if want_tcx else "")
        + f" -> {args.out}")
    if todo:
        log(f"garmin: {len(todo)} day(s) to fetch, about {max(1, round(minutes))} min"
            + (f" ({len(days) - len(todo)} already on disk)" if len(days) != len(todo) else ""))
    if minutes > 15 and not args.yes:
        if not sys.stdin.isatty():
            log("garmin: long pull; rerun with --yes to confirm")
            return 2
        answer = input("garmin: that is a long pull. Start? You can stop any time and rerun to continue [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            return 0

    tcx_dir = None
    if want_tcx:
        tcx_dir = args.out / "tcx"
        tcx_dir.mkdir(parents=True, exist_ok=True)

    rate_limited = False
    if daily:
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
        log("garmin: stopped early because Garmin rate-limited us. Rerun the same command in about an hour; it continues from here.")
        return 3
    log("garmin: done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
