#!/usr/bin/env python3
"""garmin_pull.py -- pull your Garmin wellness and activity data into files you
can actually use: one JSON per day, one CSV per category, one SQLite database.

    python3 garmin_pull.py                              # last 30 days, daily summaries of everything
    python3 garmin_pull.py --last 90d --what sleep,hrv
    python3 garmin_pull.py --since 2026-01-01 --until 2026-03-31 --what activities --workout-files
    python3 garmin_pull.py --full-day --last 7d         # every data point the watch stored, per day
    python3 garmin_pull.py --all-history                # from your first Garmin activity to today
    python3 garmin_pull.py --out ~/garmin-data --format csv

What (--what, comma-separated; default summaries = the five daily ones):
    sleep       stages, score, efficiency, SpO2 and respiration during sleep, sleep need
    hrv         last-night HRV, weekly average, personal baseline, status
    summary     resting HR, stress, body battery, steps, SpO2, intensity minutes, calories
    vo2max      VO2max estimate for the day, when Garmin published one
    activities  workouts with distance, duration, HR, power, training effect
    detail      every intraday data point: heart rate, stress, body battery, steps per 15 min,
                breathing, SpO2, HRV readings, sleep stages and movement, plus that day's
                training readiness, hydration, training status, endurance score, fitness age
    --full-day  shorthand for summaries + detail + workout files; with --what it
                adds detail (and workout files) to what you chose

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
This script pauses between days and, at a 429, waits the time Garmin asks for
when that is short (Retry-After up to two minutes) and tries once more; otherwise
it stops cleanly and tells you to rerun later -- the rerun picks up where it
stopped. Nothing is ever invented: a metric Garmin did not send is an empty cell.
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


RETRY_AFTER_MAX_WAIT = 120  # seconds: wait-and-retry once when Garmin asks for at most this


def missing_summaries(existing: dict | None, daily: set[str]) -> set[str]:
    """Which of the wanted summary categories a day file still lacks. A
    recorded error never throws away categories already on disk."""
    if existing is None:
        return set(daily)
    return {c for c in daily if c not in existing}


def pull_days(session: GarminSession, days: list[str], only: set[str], raw_dir: Path,
              pause: float, refresh: bool, sleep=time.sleep) -> tuple[int, int, bool, int | None]:
    """Returns (pulled, skipped, rate_limited, retry_after_seconds)."""
    pulled = skipped = 0
    daily = only & set(DAILY)
    want_detail = "detail" in only
    retried = False
    i = 0
    while i < len(days):
        day = days[i]
        target = raw_dir / f"{day}.json"
        existing = _read_day(target) if target.exists() and not refresh else None
        missing = missing_summaries(existing, daily)
        detail_dir = raw_dir / "detail" / day
        need_detail = want_detail and (refresh or not garmin_detail.detail_complete(detail_dir))
        if not missing and not need_detail:
            skipped += 1
            i += 1
            continue
        if pulled and pause:
            sleep(pause)
        try:
            if missing:
                result = {**(existing or {}), **fetch_day(session, day, missing)}
                result.pop("error", None)
                target.write_text(json.dumps(result, indent=2) + "\n")
            if need_detail:
                status = garmin_detail.fetch_detail(session, day, detail_dir)
                failed = [k for k, v in status.items() if str(v).startswith("error")]
                refused = [k for k, v in status.items() if str(v).startswith("none")]
                if failed:
                    log(f"garmin: {day} detail: {len(failed)} endpoint(s) failed, retried next run ({', '.join(failed)})")
                if refused:
                    log(f"garmin: {day} detail: Garmin has no {', '.join(refused)} for this day")
        except GarminConnectError as exc:
            if exc.rate_limited:
                wait = exc.retry_after
                if wait is not None and wait <= RETRY_AFTER_MAX_WAIT and not retried:
                    log(f"garmin: HTTP 429 at {day}; Garmin asks for {wait}s, waiting once then retrying")
                    sleep(wait + 1)
                    retried = True
                    continue
                asks = f" Garmin asks for {wait // 60 + 1} min." if wait else ""
                log(f"garmin: HTTP 429 (rate limited) at {day} after {pulled} day(s).{asks} "
                    f"Wait and rerun -- it resumes from here.")
                return pulled, skipped, True, wait
            log(f"garmin: {day} failed ({exc}) -- recorded, continuing")
            kept = {k: v for k, v in (existing or {}).items() if k != "error"}
            target.write_text(json.dumps({**kept, "date": day, "error": str(exc)[:300]}, indent=2) + "\n")
        pulled += 1
        i += 1
        added = sorted(missing) + (["detail"] if need_detail else [])
        log(f"garmin: {day} ok ({i}/{len(days)})" + (f", added {', '.join(added)}" if existing else ""))
    return pulled, skipped, False, None


def _read_day(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def pull_activities(session: GarminSession, since: str, until: str, raw_dir: Path,
                    tcx_dir: Path | None) -> list[dict]:
    raw = session.fetch_activities_since(since)
    if getattr(session, "activity_paging_capped", False):
        log(f"garmin: stopped paging the activity list after {len(raw)} activities (the safety cap); "
            f"older ones are not included -- rerun with an explicit --since to get them")
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


def upsert_rows(db: sqlite3.Connection, name: str, pk: str | tuple[str, ...], rows: list[dict]) -> None:
    """INSERT OR REPLACE `rows` into table `name`, creating the table and any
    new columns from the rows themselves. Values are stored as they come."""
    if not rows:
        return
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


def write_sqlite(db_path: Path, tables: dict[str, tuple[str | tuple[str, ...], list[dict]]]) -> None:
    """tables = {name: (primary_key, rows)}; the key is a column name or a tuple
    of them."""
    db = sqlite3.connect(db_path)
    for name, (pk, rows) in tables.items():
        upsert_rows(db, name, pk, rows)
    db.commit()
    db.close()


TIMELINE_COLUMNS = ("date", "series", "time_gmt", "value", "value2")
SNAPSHOT_COLUMNS = ("date", "metric", "value")


def write_detail(out: Path, formats: set[str]) -> tuple[dict[str, int], dict[str, int], dict]:
    """Flatten raw/detail one day at a time straight into garmin.db and the
    two CSVs, so a multi-year --full-day pull never holds more than a day of
    rows in memory. Returns ({table: rows}, {series: rows}, drift)."""
    counts = {"timeline": 0, "snapshots": 0}
    series: dict[str, int] = {}
    drift_log = garmin_detail.DriftLog()
    db = sqlite3.connect(out / "garmin.db") if "sqlite" in formats else None
    csv_files = {}
    writers = {}
    if "csv" in formats:
        for name, columns in (("timeline", TIMELINE_COLUMNS), ("snapshots", SNAPSHOT_COLUMNS)):
            handle = (out / f"{name}.csv").open("w", newline="")
            csv_files[name] = handle
            writers[name] = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writers[name].writeheader()
    try:
        for day, timeline, snapshots, notes, raw in garmin_detail.iter_detail_days(out / "raw" / "detail"):
            drift_log.add(day, raw, notes)
            counts["timeline"] += len(timeline)
            counts["snapshots"] += len(snapshots)
            for row in timeline:
                series[row["series"]] = series.get(row["series"], 0) + 1
            if db is not None:
                upsert_rows(db, "timeline", ("date", "series", "time_gmt"), timeline)
                upsert_rows(db, "snapshots", ("date", "metric"), snapshots)
                db.commit()
            if writers:
                writers["timeline"].writerows(timeline)
                writers["snapshots"].writerows(snapshots)
    finally:
        if db is not None:
            db.close()
        for handle in csv_files.values():
            handle.close()
    for name in ("timeline", "snapshots"):
        if not counts[name] and (out / f"{name}.csv").exists():
            (out / f"{name}.csv").unlink()
    return counts, series, drift_log.result()


def write_outputs(out: Path, only: set[str], formats: set[str],
                  activities: list[dict] | None) -> tuple[dict[str, int], dict[str, int], dict]:
    """Returns ({table: row count}, {timeline series: row count}, drift)."""
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
    counts = {name: len(rows) for name, (_, rows) in tables.items()}
    series: dict[str, int] = {}
    drift: dict = {}
    if "detail" in only:
        detail_counts, series, drift = write_detail(out, formats)
        counts.update(detail_counts)
    log(f"garmin: wrote {', '.join(sorted(formats))} to {out} ("
        + ", ".join(f"{name} {n}" for name, n in counts.items()) + ")")
    return counts, series, drift


def day_state(raw_dir: Path, day: str, daily: set[str], want_detail: bool) -> str:
    """'complete' | 'error' | 'missing' for one day, judged only on what was
    asked for: a detail-only pull is complete when the detail is, a summary
    pull when every wanted category is in the day file."""
    data = _read_day(raw_dir / f"{day}.json") if (raw_dir / f"{day}.json").exists() else None
    have_summaries = not daily or (data is not None and all(c in data for c in daily))
    have_detail = not want_detail or garmin_detail.detail_complete(raw_dir / "detail" / day)
    if have_summaries and have_detail:
        return "complete"
    if data is not None and "error" in data:
        return "error"
    if want_detail and (raw_dir / "detail" / day / "_index.json").exists():
        return "error"
    return "missing"


def report(out: Path, only: set[str], days: list[str], counts: dict[str, int], series: dict[str, int],
           drift: dict, rate_limited: bool, retry_after: int | None = None) -> str:
    """The plain-words summary printed after every pull: what came in, what is
    missing, and anything Garmin changed. Also saved as report.txt."""
    raw_dir = out / "raw"
    lines = ["", "=== Garmin pull report ==="]
    daily = only & set(DAILY)
    want_detail = "detail" in only
    if days:
        state = {d: day_state(raw_dir, d, daily, want_detail) for d in days}
        errors = [d for d, s in state.items() if s == "error"]
        gaps = [d for d, s in state.items() if s == "missing"]
        lines.append(f"days: {days[0]} .. {days[-1]}: {len(days) - len(errors) - len(gaps)} complete, "
                     f"{len(errors)} with errors, {len(gaps)} not fetched")
        if errors:
            lines.append("  errors on: " + ", ".join(errors[:10]) + (" ..." if len(errors) > 10 else ""))
        if daily:
            empty = {c: 0 for c in sorted(daily)}
            for d in days:
                data = _read_day(raw_dir / f"{d}.json") if (raw_dir / f"{d}.json").exists() else None
                for c in daily:
                    if data and c in data and not data[c]:
                        empty[c] += 1
            blank = [f"{c} ({n} day{'s' if n != 1 else ''})" for c, n in empty.items() if n]
            if blank:
                lines.append("  no data from Garmin for: " + ", ".join(blank))
        if want_detail:
            incomplete = [d for d in days if not garmin_detail.detail_complete(raw_dir / "detail" / d)]
            if incomplete:
                lines.append(f"  detail incomplete on {len(incomplete)} day(s): " + ", ".join(incomplete[:8]))
    for name, n in counts.items():
        if name == "timeline":
            lines.append(f"timeline: {n} measurements in {len(series)} series")
            for s_name, s_n in sorted(series.items()):
                lines.append(f"  {s_name}: {s_n}")
        else:
            lines.append(f"{name}: {n} rows")
    unmapped = drift.get("unmapped") if drift else None
    changes = {k: v for k, v in drift.items() if k != "unmapped"} if drift else {}
    if changes:
        lines.append("Garmin changed something (raw JSON still has everything, generic flattening applied):")
        for endpoint, change in changes.items():
            if change.get("added"):
                lines.append(f"  {endpoint}: new fields " + ", ".join(change["added"][:8]))
            if change.get("missing"):
                lines.append(f"  {endpoint}: fields gone " + ", ".join(change["missing"][:8]))
    if unmapped:
        lines.append("Not flattened (no safe value column; the raw JSON has it):")
        for note in unmapped[:8]:
            lines.append(f"  {note}")
    files = sorted(p.name for p in out.iterdir() if p.is_file())
    lines.append("files: " + ", ".join(files))
    if rate_limited:
        when = f"in about {retry_after // 60 + 1} minutes" if retry_after else "in about an hour"
        lines.append(f"STOPPED EARLY: Garmin rate-limited the connection. Rerun the same command {when}.")
    text = "\n".join(lines)
    (out / "report.txt").write_text(text.strip() + "\n")
    return text


# --- CLI -----------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    what = parser.add_argument_group("what to pull")
    what.add_argument("--what", default=None, metavar="LIST",
                      help="'summaries' (default: the five daily summaries) or a comma list of: "
                           + ", ".join(CATEGORIES))
    what.add_argument("--full-day", action="store_true",
                      help="every data point the watch stored each day: summaries + detail + workout files "
                           "(with --what: adds detail and workout files to your list)")
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

    what_arg = (args.what_alias or args.what or "summaries").strip().lower()
    only = set(SUMMARIES) if what_arg in ("summaries", "all") else {c.strip() for c in what_arg.split(",") if c.strip()}
    if args.full_day:
        only = set(CATEGORIES) if what_arg in ("summaries", "all") else only | {"detail"}
    unknown = only - set(CATEGORIES)
    if unknown:
        log(f"garmin: unknown category {', '.join(sorted(unknown))}; choose from {', '.join(CATEGORIES)} or summaries")
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

    todo = [d for d in days if args.refresh or day_state(raw_dir, d, daily, "detail" in only) != "complete"]
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
    retry_after = None
    if per_day:
        pulled, skipped, rate_limited, retry_after = pull_days(session, days, per_day, raw_dir, args.pause, args.refresh)
        log(f"garmin: {pulled} pulled, {skipped} already present")
    activities = None
    if "activities" in only and not rate_limited:
        try:
            activities = pull_activities(session, start.isoformat(), end.isoformat(), raw_dir, tcx_dir)
        except GarminConnectError as exc:
            log(f"garmin: activities failed ({exc})")
            rate_limited = exc.rate_limited
            retry_after = exc.retry_after

    if not args.no_cache:
        try:
            save_session(session)
        except OSError:
            pass
    counts, series, drift = write_outputs(args.out, only, formats, activities)
    log(report(args.out, only, days, counts, series, drift, rate_limited, retry_after))
    if rate_limited:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
