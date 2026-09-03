#!/usr/bin/env python3
"""garmin_pull.py -- pull your Garmin wellness and activity data into files you
can actually use: one JSON per day, one CSV per category, one SQLite database.

    python3 garmin_pull.py                              # last 30 days, daily summaries of everything
    python3 garmin_pull.py --last 90d --what sleep,hrv
    python3 garmin_pull.py --since 2026-01-01 --until 2026-03-31 --what activities --workout-files
    python3 garmin_pull.py --full-day --last 7d         # every data point the watch stored, per day
    python3 garmin_pull.py --all-history                # from your first Garmin activity to today
    python3 garmin_pull.py --out ~/garmin-data --format csv

What (--what, comma-separated; default all = the five daily summaries):
    sleep       stages, score, efficiency, SpO2 and respiration during sleep, sleep need
    hrv         last-night HRV, weekly average, personal baseline, status
    summary     resting HR, stress, body battery, steps, SpO2, intensity minutes, calories
    vo2max      VO2max estimate for the day, when Garmin published one
    activities  workouts with distance, duration, HR, power, training effect
    detail      every intraday data point: heart rate, stress, body battery, steps per 15 min,
                breathing, SpO2, HRV readings, sleep stages and movement, plus that day's
                training readiness, hydration, training status, endurance score, fitness age
    --full-day  shorthand for all five + detail + workout files

When: --last 30d | 12w | 6m | 2y   (default 30d)
      --since YYYY-MM-DD [--until YYYY-MM-DD]
      --all-history   from your first Garmin activity to today

Output folder layout (default ./garmin-data):
    raw/YYYY-MM-DD.json           shaped daily data (the resume checkpoint)
    raw/activities.json           shaped activity list for the range
    raw/detail/YYYY-MM-DD/*.json  untouched Garmin responses, one per endpoint (detail)
    tcx/<activity_id>.tcx         only with --workout-files
    sleep.csv hrv.csv summary.csv vo2max.csv activities.csv
    timeline.csv snapshots.csv    detail, flattened: every timestamped value / every daily metric
    garmin.db                     SQLite, one table per file above

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
import garmin_detail  # noqa: E402
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

CATEGORIES = ("sleep", "hrv", "summary", "vo2max", "activities", "detail")
SUMMARIES = ("sleep", "hrv", "summary", "vo2max", "activities")
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
    daily = only & set(DAILY)
    want_detail = "detail" in only
    for i, day in enumerate(days):
        target = raw_dir / f"{day}.json"
        existing = _read_day(target) if target.exists() and not refresh else None
        missing = set(daily) if existing is None or "error" in existing else {c for c in daily if c not in existing}
        detail_dir = raw_dir / "detail" / day
        need_detail = want_detail and (refresh or not garmin_detail.detail_complete(detail_dir))
        if not missing and not need_detail:
            skipped += 1
            continue
        if pulled and pause:
            time.sleep(pause)
        try:
            if missing:
                result = {**(existing or {}), **fetch_day(session, day, missing)}
                result.pop("error", None)
                target.write_text(json.dumps(result, indent=2) + "\n")
            if need_detail:
                status = garmin_detail.fetch_detail(session, day, detail_dir)
                failed = [k for k, v in status.items() if str(v).startswith("error")]
                if failed:
                    log(f"garmin: {day} detail: {len(failed)} endpoint(s) failed ({', '.join(failed)})")
        except GarminConnectError as exc:
            if "429" in str(exc):
                log(f"garmin: HTTP 429 (rate limited) at {day} after {pulled} day(s). "
                    f"Wait an hour or so and rerun -- it resumes from here.")
                return pulled, skipped, True
            log(f"garmin: {day} failed ({exc}) -- recorded, continuing")
            target.write_text(json.dumps({"date": day, "error": str(exc)[:300]}, indent=2) + "\n")
        pulled += 1
        added = sorted(missing) + (["detail"] if need_detail else [])
        log(f"garmin: {day} ok ({i + 1}/{len(days)})" + (f", added {', '.join(added)}" if existing else ""))
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


def write_sqlite(db_path: Path, tables: dict[str, tuple[str | tuple[str, ...], list[dict]]]) -> None:
    """tables = {name: (primary_key, rows)}; the key is a column name or a tuple
    of them. Columns are created from the rows; values are stored as they come."""
    db = sqlite3.connect(db_path)
    for name, (pk, rows) in tables.items():
        if not rows:
            continue
        pk_cols = (pk,) if isinstance(pk, str) else tuple(pk)
        columns: list[str] = []
        for row in rows:
            for key in row:
                if key not in columns:
                    columns.append(key)
        defs = ", ".join(f'"{c}"' for c in columns) + ", PRIMARY KEY (" + ", ".join(f'"{c}"' for c in pk_cols) + ")"
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


def write_outputs(out: Path, only: set[str], formats: set[str], activities: list[dict] | None) -> tuple[dict, dict]:
    days = load_all_days(out / "raw")
    tables: dict[str, tuple[str, list[dict]]] = {}
    for category in DAILY:
        if category in only:
            tables[category] = ("date", rows_for(category, days))
    if activities is not None:
        tables["activities"] = ("activity_id", activities)
    elif "activities" in only and (out / "raw" / "activities.json").exists():
        tables["activities"] = ("activity_id", json.loads((out / "raw" / "activities.json").read_text()))
    drift: dict = {}
    if "detail" in only:
        timeline, snapshots, drift = garmin_detail.detail_tables(out / "raw" / "detail")
        tables["timeline"] = (("date", "series", "time_gmt"), timeline)
        tables["snapshots"] = (("date", "metric"), snapshots)
    if "csv" in formats:
        for name, (_, rows) in tables.items():
            write_csv(out / f"{name}.csv", rows)
    if "sqlite" in formats:
        write_sqlite(out / "garmin.db", tables)
    counts = ", ".join(f"{name} {len(rows)}" for name, (_, rows) in tables.items())
    log(f"garmin: wrote {', '.join(sorted(formats))} to {out} ({counts})")
    return tables, drift


def report(out: Path, only: set[str], days: list[str], tables: dict, drift: dict, rate_limited: bool) -> str:
    """The plain-words summary printed after every pull: what came in, what is
    missing, and anything Garmin changed. Also saved as report.txt."""
    raw_dir = out / "raw"
    lines = ["", "=== Garmin pull report ==="]
    daily = only & set(DAILY)
    if days:
        on_disk = [d for d in days if (raw_dir / f"{d}.json").exists()]
        errors = [d for d in on_disk if "error" in (_read_day(raw_dir / f"{d}.json") or {})]
        gaps = [d for d in days if d not in on_disk]
        lines.append(f"days: {days[0]} .. {days[-1]}: {len(on_disk) - len(errors)} complete, "
                     f"{len(errors)} with errors, {len(gaps)} not fetched")
        if errors:
            lines.append("  errors on: " + ", ".join(errors[:10]) + (" ..." if len(errors) > 10 else ""))
        if daily:
            empty = {c: 0 for c in sorted(daily)}
            for d in on_disk:
                data = _read_day(raw_dir / f"{d}.json") or {}
                for c in daily:
                    if c in data and not data[c]:
                        empty[c] += 1
            blank = [f"{c} ({n} day{'s' if n != 1 else ''})" for c, n in empty.items() if n]
            if blank:
                lines.append("  no data from Garmin for: " + ", ".join(blank))
        if "detail" in only:
            incomplete = [d for d in days if not garmin_detail.detail_complete(raw_dir / "detail" / d)]
            if incomplete:
                lines.append(f"  detail incomplete on {len(incomplete)} day(s): " + ", ".join(incomplete[:8]))
    for name, (_, rows) in tables.items():
        if name == "timeline":
            series: dict[str, int] = {}
            for row in rows:
                series[row["series"]] = series.get(row["series"], 0) + 1
            lines.append(f"timeline: {len(rows)} measurements in {len(series)} series")
            for s_name, n in sorted(series.items()):
                lines.append(f"  {s_name}: {n}")
        else:
            lines.append(f"{name}: {len(rows)} rows")
    if drift:
        lines.append("Garmin changed something (raw JSON still has everything, generic flattening applied):")
        for endpoint, change in drift.items():
            if change.get("added"):
                lines.append(f"  {endpoint}: new fields " + ", ".join(change["added"][:8]))
            if change.get("missing"):
                lines.append(f"  {endpoint}: fields gone " + ", ".join(change["missing"][:8]))
    files = sorted(p.name for p in out.iterdir() if p.is_file())
    lines.append("files: " + ", ".join(files))
    if rate_limited:
        lines.append("STOPPED EARLY: Garmin rate-limited the connection. Rerun the same command in about an hour.")
    text = "\n".join(lines)
    (out / "report.txt").write_text(text.strip() + "\n")
    return text


# --- CLI -----------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    what = parser.add_argument_group("what to pull")
    what.add_argument("--what", default="all", metavar="LIST",
                      help="'all' (the five daily summaries) or a comma list of: " + ", ".join(CATEGORIES))
    what.add_argument("--full-day", action="store_true",
                      help="every data point the watch stored each day: all summaries + detail + workout files")
    what.add_argument("--workout-files", action="store_true",
                      help="also save each workout's TCX file (per-second heart rate, pace, GPS)")
    when = parser.add_argument_group("when")
    when.add_argument("--last", default=None, metavar="PERIOD", help="30d, 12w, 6m, 2y ... (default 30d)")
    when.add_argument("--since", metavar="DATE", help="YYYY-MM-DD, first day to pull")
    when.add_argument("--until", metavar="DATE", help="YYYY-MM-DD, last day to pull (default today)")
    when.add_argument("--all-history", action="store_true",
                      help="from your first Garmin activity to today (long; stops and resumes on rate limits)")
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

    what_arg = (args.what_alias or args.what).strip().lower()
    only = set(SUMMARIES) if what_arg == "all" else {c.strip() for c in what_arg.split(",") if c.strip()}
    if args.full_day:
        only = set(CATEGORIES)
    unknown = only - set(CATEGORIES)
    if unknown:
        log(f"garmin: unknown category {', '.join(sorted(unknown))}; choose from {', '.join(CATEGORIES)} or all")
        return 2
    formats = {f.strip() for f in args.format.split(",") if f.strip()} | {"json"}
    want_tcx = args.workout_files or args.tcx or args.full_day

    try:
        session = open_session(mfa_code=args.mfa_code, use_cache=not args.no_cache, log=log)
    except GarminConnectError as exc:
        log(f"garmin: {exc}")
        return 1

    today = datetime.now(timezone.utc).date()
    until_arg = args.until or args.until_alias
    end = date.fromisoformat(until_arg) if until_arg else today
    since_arg = args.since or args.since_alias
    if args.all_history and not since_arg:
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
    per_day = only & (set(DAILY) | {"detail"})
    days = day_range(start, end) if per_day else []
    raw_dir = args.out / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    def _needs(d: str) -> bool:
        if args.refresh:
            return True
        existing = _read_day(raw_dir / f"{d}.json") if (raw_dir / f"{d}.json").exists() else None
        summaries_missing = existing is None or "error" in existing or any(c not in existing for c in daily)
        detail_missing = "detail" in only and not garmin_detail.detail_complete(raw_dir / "detail" / d)
        return summaries_missing or detail_missing
    todo = [d for d in days if _needs(d)]
    weight = len(daily) + (garmin_detail.REQUESTS_PER_DAY if "detail" in only else 0)
    minutes = estimate_minutes(len(todo), weight, args.pause)
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
    if per_day:
        pulled, skipped, rate_limited = pull_days(session, days, per_day, raw_dir, args.pause, args.refresh)
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
    tables, drift = write_outputs(args.out, only, formats, activities)
    log(report(args.out, only, days, tables, drift, rate_limited))
    if rate_limited:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
