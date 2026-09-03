#!/usr/bin/env python3
"""garmin_doctor.py -- is this machine ready to pull? One command, plain answers.

    python3 garmin_doctor.py            # checks, then one cheap request to Garmin
    python3 garmin_doctor.py --offline  # checks only, no network

Checks, in order: Python version, cached session and its age, credentials file or
environment (a warning, not a failure, while a session is cached), output folder
from a previous pull (and whether that pull was cut short), and finally a single
small request to Garmin to prove the session works.
Every line is OK / WARN / FAIL with what to do about it. Exit code 0 when
everything needed for a pull is in place, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from garmin_client import (  # noqa: E402
    CREDENTIALS_PATH,
    SESSION_CACHE_PATH,
    GarminConnectError,
    load_credentials,
    load_session,
    open_session,
    save_session,
)

OK, WARN, FAIL = "OK  ", "WARN", "FAIL"


def line(status: str, text: str) -> None:
    print(f"[{status}] {text}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="skip the request to Garmin")
    parser.add_argument("--out", type=Path, default=Path("garmin-data"), help="output folder to inspect")
    args = parser.parse_args()
    failed = False

    # 1. python
    version = sys.version_info
    if version >= (3, 11):
        line(OK, f"Python {platform.python_version()} on {platform.system()}")
    else:
        line(FAIL, f"Python {platform.python_version()} is too old: 3.11 or newer is needed (tomllib)")
        failed = True

    # 2. session cache (checked first: with a usable session, credentials are not needed today)
    session = None
    if SESSION_CACHE_PATH.exists():
        age_h = (time.time() - SESSION_CACHE_PATH.stat().st_mtime) / 3600
        session = load_session(SESSION_CACHE_PATH)
        if session is None:
            line(WARN, f"cached session at {SESSION_CACHE_PATH} is unreadable (last saved {age_h:.0f}h ago); "
                       f"the next pull logs in again and may ask for a two-factor code")
        else:
            line(OK, f"cached session found, last saved {age_h:.0f}h ago"
                     + (" (over 11 months: the year-long token may expire soon)" if age_h > 11 * 30 * 24 else ""))
        if os.name != "nt" and SESSION_CACHE_PATH.stat().st_mode & 0o077:
            line(WARN, f"{SESSION_CACHE_PATH} is readable by other users; run: chmod 600 {SESSION_CACHE_PATH}")
    else:
        line(WARN, "no cached session yet: the first pull logs in and may ask for a two-factor code")

    # 3. credentials
    setup = f"python3 {Path(__file__).with_name('garmin_setup.py')}"
    try:
        load_credentials()
        source = "environment" if os.environ.get("GARMIN_USERNAME") else str(CREDENTIALS_PATH)
        line(OK, f"credentials found ({source})")
        if CREDENTIALS_PATH.exists() and os.name != "nt":
            mode = CREDENTIALS_PATH.stat().st_mode & 0o777
            if mode & 0o077:
                line(WARN, f"{CREDENTIALS_PATH} is readable by other users; run: chmod 600 {CREDENTIALS_PATH}")
    except GarminConnectError:
        if session is not None:
            line(OK, f"no stored password (the default); when the session expires, run  {setup}")
        else:
            line(FAIL, f"not logged in yet: run  {setup}  in your terminal")
            failed = True

    # 4. previous output
    report = args.out / "report.txt"
    raw = args.out / "raw"
    if raw.exists():
        days = sorted(p.stem for p in raw.glob("????-??-??.json"))
        detail_days = sorted(p.name for p in (raw / "detail").iterdir()) if (raw / "detail").exists() else []
        line(OK, f"{args.out}: {len(days)} day(s) of summaries"
                 + (f" ({days[0]} .. {days[-1]})" if days else "")
                 + (f", {len(detail_days)} day(s) of detail" if detail_days else ""))
        if report.exists() and "STOPPED EARLY" in report.read_text():
            line(WARN, "the last pull was cut short by rate limiting; rerun the same command to continue")
    else:
        line(OK, f"no previous pull in {args.out} (that is fine for a first run)")

    # 5. one request
    if args.offline:
        line(OK, "offline: skipped the request to Garmin")
        return 1 if failed else 0
    if failed:
        line(FAIL, "not trying Garmin until the items above are fixed")
        return 1
    try:
        started = time.time()
        if session is None:
            session = open_session(interactive=sys.stdin.isatty(), log=lambda _m: None)
        profile = session.connectapi("/userprofile-service/socialProfile")
        name = (profile or {}).get("displayName") or (profile or {}).get("userName") or "?"
        save_session(session)
        line(OK, f"Garmin answered in {time.time() - started:.1f}s, logged in as {name}")
    except GarminConnectError as exc:
        text = str(exc)
        if exc.rate_limited:
            wait = f"about {exc.retry_after // 60 + 1} minutes" if exc.retry_after else "an hour"
            line(FAIL, f"Garmin is rate-limiting this connection right now; wait {wait} and try again")
        elif "two-factor" in text or "MFA" in text:
            line(FAIL, "a two-factor code is needed: run garmin_setup.py in your terminal")
        else:
            line(FAIL, f"Garmin request failed: {text[:200]}")
        return 1
    print("ready: python3 " + str(Path(__file__).with_name("garmin_pull.py")) + " --last 30d")
    return 0


if __name__ == "__main__":
    sys.exit(main())
